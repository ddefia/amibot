"""Main bot engine: ties together market discovery, price feeds, strategies,
risk management, and order execution into a single async loop.

Updated based on deep trade-by-trade analysis of 5 Polymarket traders.
Now supports SELL-side maker orders (Guy 1's exact strategy) and
15-min interval detection in addition to 5-min.

Cross-trader lessons applied:
- Prefer 15-min intervals (net +$305K) over 5-min (net -$8.4K)
- Conviction-scaled position sizing (bigger on high confidence)
- Stronger feed disagreement penalty
- Sell-side exposure calculated correctly
"""

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
    1. Discovers the current BTC market (5-min or 15-min intervals)
    2. Streams real-time prices from Binance + Chainlink
    3. Evaluates SELL signals via latency arb strategy (Guy 1's approach)
    4. Places SELL maker orders at $0.51 when edge detected
    5. Tracks positions and P&L through resolution

    Based on Guy 1 (dys123): Sharpe 18.62, $131.6k P&L, 53.7% WR.
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
            confidence_threshold=0.55,
            position_usd=1500,
        )
        self.mispricing = MispricingStrategy(
            max_position=config.max_position_size,
            # Pure arb only — no cheap single-side buys (cross-trader lesson)
        )

        # State
        self.current_market: dict | None = None
        self.interval_start_price: float | None = None
        self.interval_start_ts: int = 0
        self.interval_duration: int = 900  # Default 15-min, detect from market
        self.active_positions: list[dict] = []  # Track multiple positions
        self.total_active_usd: float = 0
        self._running = False

    async def run(self):
        """Main entry point — start the bot."""
        self._running = True
        logger.info("Bot starting — SELL-side latency arb strategy (Guy 1 mode)")

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

    def _detect_interval_duration(self) -> int:
        """Detect interval duration from the current market slug/name.
        Guy 1 trades 99.6% on 15-min intervals, Guy 4 uses both 5 and 15."""
        if not self.current_market:
            return 900

        slug = str(self.current_market.get("question", ""))
        # Parse time range like "6:15PM-6:30PM" to determine duration
        import re
        m = re.search(r'(\d+):(\d+)(AM|PM)\s*-\s*(\d+):(\d+)(AM|PM)', slug)
        if m:
            h1, m1, ap1 = int(m.group(1)), int(m.group(2)), m.group(3)
            h2, m2, ap2 = int(m.group(4)), int(m.group(5)), m.group(6)
            # Convert to minutes
            t1 = (h1 % 12 + (12 if ap1 == 'PM' else 0)) * 60 + m1
            t2 = (h2 % 12 + (12 if ap2 == 'PM' else 0)) * 60 + m2
            diff = (t2 - t1) * 60  # seconds
            if diff > 0:
                return diff

        # Check for hour-based patterns like "5PM ET" vs "5:00PM-5:15PM ET"
        if re.search(r'\d+[AP]M\s+ET\s*-\s*(Yes|No|Up|Down)', slug):
            return 3600  # hourly

        return 900  # default 15-min

    async def _tick(self):
        """Single iteration of the trading loop."""
        now = int(time.time())

        # Detect interval boundaries
        # For 15-min: interval = now - (now % 900)
        # For 5-min: interval = now - (now % 300)
        interval_mod = self.interval_duration if self.interval_duration > 0 else 900
        current_interval = now - (now % interval_mod)
        seconds_into_interval = now % interval_mod

        # New interval — find the market and record opening price
        if current_interval != self.interval_start_ts:
            logger.info("New %d-min interval starting at %d",
                        interval_mod // 60, current_interval)
            self.interval_start_ts = current_interval
            self.active_positions = []
            self.total_active_usd = 0

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
                self.interval_duration = self._detect_interval_duration()
                interval_min = self.interval_duration // 60
                logger.info("Active market: %s (%d-min interval)",
                            parsed["slug"], interval_min)
                # Cross-trader lesson: 5-min intervals are net losers
                if interval_min <= 5:
                    logger.warning(
                        "5-min interval detected — higher edge/confidence required "
                        "(cross-trader data: 5-min = -$8.4K net, 15-min = +$305K net)"
                    )
            else:
                logger.warning("No active market found")
                return

        # Skip if we don't have the data we need
        if not self.current_market or not self.interval_start_price:
            return
        if not self.price_feed.latest_binance or not self.price_feed.latest_chainlink:
            return

        # The strategy handles its own timing window internally
        parsed = self.market_finder.parse_market(self.current_market)
        up_token = parsed["tokens"].get("UP", {})
        down_token = parsed["tokens"].get("DOWN", {})

        if not up_token.get("price") or not down_token.get("price"):
            return

        # Evaluate SELL-side latency arb (primary strategy)
        signal = self.latency_arb.evaluate(
            interval_start_price=self.interval_start_price,
            current_binance_price=self.price_feed.latest_binance.price,
            current_chainlink_price=self.price_feed.latest_chainlink.price,
            market_up_price=up_token["price"],
            market_down_price=down_token["price"],
            seconds_into_interval=seconds_into_interval,
            interval_duration=self.interval_duration,
            current_exposure_usd=self.total_active_usd,
        )

        # Fallback: check mispricing (pure arb when Up+Down < $1.00)
        if signal.side == Side.NONE:
            mispricing_signal = self.mispricing.evaluate(
                market_up_price=up_token["price"],
                market_down_price=down_token["price"],
            )
            if mispricing_signal.side != Side.NONE:
                signal = mispricing_signal

        if signal.side == Side.NONE:
            return

        # Risk check
        allowed, reason = self.risk.check_allowed(signal)
        if not allowed:
            logger.debug("Trade blocked by risk manager: %s", reason)
            return

        # Select token based on signal side
        if signal.side in (Side.BUY_UP, Side.SELL_UP):
            token_id = up_token["token_id"]
        else:
            token_id = down_token["token_id"]

        if not token_id:
            return

        # Determine if this is a sell order
        is_sell = signal.side in (Side.SELL_UP, Side.SELL_DOWN)

        # Execute
        logger.info("SIGNAL: %s", signal.reason)
        result = self.executor.place_order(
            signal=signal,
            token_id=token_id,
            post_only=self.config.maker_only,
            is_sell=is_sell,
        )

        if result.success:
            self.risk.record_trade(signal)
            position = {
                "signal": signal,
                "order_id": result.order_id,
                "interval": self.interval_start_ts,
                "usd_amount": signal.size,
            }
            self.active_positions.append(position)
            self.total_active_usd += signal.size
            logger.info("Position opened: %s | Side: %s | $%.0f | Active: $%.0f",
                        result.order_id,
                        "SELL" if is_sell else "BUY",
                        signal.size,
                        self.total_active_usd)
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
