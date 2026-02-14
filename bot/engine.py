"""Main bot engine: ties together market discovery, price feeds, strategies,
risk management, and order execution into a single async loop."""

import asyncio
import time
import logging

from bot.config import Config
from bot.market_finder import MarketFinder
from bot.price_feed import PriceFeed, PriceTick
from bot.strategies import LatencyArbStrategy, MispricingStrategy, Side
from bot.executor import Executor
from bot.risk import RiskManager

logger = logging.getLogger(__name__)


class BotEngine:
    """The main trading engine. Runs an async loop that:
    1. Discovers the current 5-min BTC market
    2. Streams real-time prices from Binance + Chainlink
    3. Evaluates trading signals via the selected strategy
    4. Executes orders when edge > threshold, subject to risk limits
    5. Tracks positions and P&L through resolution
    """

    def __init__(self, config: Config):
        self.config = config
        self.market_finder = MarketFinder(config.gamma_host)
        self.price_feed = PriceFeed()
        self.executor = Executor(config)
        self.risk = RiskManager(config)

        # Strategies
        self.latency_arb = LatencyArbStrategy(
            min_edge=config.min_edge_threshold,
            max_position=config.max_position_size,
        )
        self.mispricing = MispricingStrategy(
            max_position=config.max_position_size,
        )

        # State
        self.current_market: dict | None = None
        self.interval_start_price: float | None = None
        self.interval_start_ts: int = 0
        self.active_position: dict | None = None
        self._running = False

    async def run(self):
        """Main entry point — start the bot."""
        self._running = True
        logger.info("Bot starting with strategy: %s", self.config.strategy)

        balance = self.executor.get_balance()
        if balance is not None:
            logger.info("Available balance: $%.2f USDC", balance)

        # Register price tick handler
        self.price_feed.on_tick(self._on_price_tick)

        # Run price feeds and trading loop concurrently
        await asyncio.gather(
            self.price_feed.start(),
            self._trading_loop(),
        )

    async def _trading_loop(self):
        """Main trading loop — runs every second."""
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("Error in trading loop tick")
            await asyncio.sleep(1)

    async def _tick(self):
        """Single iteration of the trading loop."""
        now = int(time.time())
        current_interval = now - (now % 300)
        seconds_into_interval = now % 300

        # New interval — find the market and record opening price
        if current_interval != self.interval_start_ts:
            logger.info("New 5-min interval starting at %d", current_interval)
            self.interval_start_ts = current_interval
            self.active_position = None

            # Cancel any stale orders from previous interval
            self.executor.cancel_all()

            # Capture the Chainlink price at interval start
            if self.price_feed.latest_chainlink:
                self.interval_start_price = self.price_feed.latest_chainlink.price
                logger.info("Interval start price: $%.2f", self.interval_start_price)
            else:
                self.interval_start_price = None
                logger.warning("No Chainlink price at interval start")

            # Find the active market
            self.current_market = self.market_finder.get_current_market()
            if self.current_market:
                parsed = self.market_finder.parse_market(self.current_market)
                logger.info("Active market: %s", parsed["slug"])
            else:
                logger.warning("No active market found for interval %d", current_interval)
                return

        # Skip if we don't have the data we need
        if not self.current_market or not self.interval_start_price:
            return
        if not self.price_feed.latest_binance or not self.price_feed.latest_chainlink:
            return

        # Don't trade in first 10 seconds (let prices settle) or last 15 (too late)
        if seconds_into_interval < 10 or seconds_into_interval > 285:
            return

        # Already have a position this interval? Skip
        if self.active_position:
            return

        parsed = self.market_finder.parse_market(self.current_market)
        up_token = parsed["tokens"].get("UP", {})
        down_token = parsed["tokens"].get("DOWN", {})

        if not up_token.get("price") or not down_token.get("price"):
            return

        # Evaluate strategy
        signal = self.latency_arb.evaluate(
            interval_start_price=self.interval_start_price,
            current_binance_price=self.price_feed.latest_binance.price,
            current_chainlink_price=self.price_feed.latest_chainlink.price,
            market_up_price=up_token["price"],
            market_down_price=down_token["price"],
            seconds_into_interval=seconds_into_interval,
        )

        # Also check mispricing
        mispricing_signal = self.mispricing.evaluate(
            market_up_price=up_token["price"],
            market_down_price=down_token["price"],
        )

        # Use the signal with the higher edge
        best = signal if signal.edge >= mispricing_signal.edge else mispricing_signal

        if best.side == Side.NONE:
            return

        # Risk check
        allowed, reason = self.risk.check_allowed(best)
        if not allowed:
            logger.debug("Trade blocked by risk manager: %s", reason)
            return

        # Adjust size
        best.size = self.risk.adjust_size(best)

        # Select token
        if best.side == Side.BUY_UP:
            token_id = up_token["token_id"]
        else:
            token_id = down_token["token_id"]

        if not token_id:
            return

        # Execute
        logger.info("SIGNAL: %s", best.reason)
        result = self.executor.place_order(
            signal=best,
            token_id=token_id,
            post_only=self.config.maker_only,
        )

        if result.success:
            self.risk.record_trade(best)
            self.active_position = {
                "signal": best,
                "order_id": result.order_id,
                "interval": self.interval_start_ts,
            }
            logger.info("Position opened: %s", result.order_id)
        else:
            logger.warning("Order failed: %s", result.error)

    async def _on_price_tick(self, tick: PriceTick):
        """Handle incoming price ticks — used for logging/monitoring."""
        pass  # Main logic is in _tick(), prices are read from self.price_feed

    def stop(self):
        """Stop the bot gracefully."""
        self._running = False
        self.price_feed.stop()
        self.executor.cancel_all()
        self.market_finder.close()

        stats = self.risk.get_stats()
        logger.info("Bot stopped. Final stats: %s", stats)
