"""Main bot engine: full trade lifecycle from signal to resolution.

Ties together market discovery, price feeds, strategies, risk management,
and order execution. Supports dry-run mode for paper trading.

Cross-trader lessons applied:
- Prefer 15-min intervals (net +$305K) over 5-min (net -$8.4K)
- Conviction-scaled position sizing (bigger on high confidence)
- Feed disagreement penalty (25% confidence cut)
- Sell-side exposure calculated correctly
- Risk-adjusted sizing after loss streaks
"""

import asyncio
import json
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
    1. Discovers the best BTC market (prefers 15-min intervals)
    2. Streams real-time prices from Binance + Chainlink
    3. Evaluates SELL signals via latency arb (Guy 1's approach)
    4. Places orders with risk-adjusted sizing
    5. Tracks fills and resolves positions at interval end
    6. Records P&L and adjusts for loss streaks

    Based on Guy 1 (dys123): Sharpe 18.62, $131.6K P&L, 53.7% WR.
    """

    def __init__(self, config: Config):
        self.config = config
        self.market_finder = MarketFinder(
            config.gamma_host,
            prefer_15min=config.prefer_15min,
        )
        self.price_feed = PriceFeed()
        self.executor = Executor(config)
        self.risk = RiskManager(config)

        # Strategies
        self.latency_arb = LatencyArbStrategy(
            min_edge=config.min_edge_threshold,
            confidence_threshold=config.confidence_threshold,
            position_usd=config.position_usd,
        )
        self.mispricing = MispricingStrategy(
            max_position=config.max_position_size,
        )

        # State — current interval
        self.current_market: dict | None = None
        self.current_parsed: dict | None = None
        self.interval_start_price: float | None = None
        self.interval_start_ts: int = 0
        self.interval_duration: int = 900  # Detected from market
        self.positions_this_interval: int = 0
        self.total_active_usd: float = 0

        # State — tracking
        self._running = False
        self._tick_count = 0
        self._last_fill_check: float = 0
        self._last_signal_ts: float = 0  # Rate limit signals

    async def run(self):
        """Main entry point — start the bot."""
        self._running = True
        mode = "DRY RUN" if self.config.dry_run else "LIVE"
        logger.info("Bot starting [%s] — SELL-side latency arb (Guy 1 mode)", mode)

        balance = self.executor.get_balance()
        if balance is not None:
            logger.info("Available balance: $%.2f USDC", balance)
            self.risk.set_balance(balance)

        # Register price tick handler
        self.price_feed.on_tick(self._on_price_tick)

        # Run price feeds, trading loop, and fill checker concurrently
        await asyncio.gather(
            self.price_feed.start(),
            self._trading_loop(),
            self._fill_check_loop(),
        )

    async def _trading_loop(self):
        """Main trading loop — runs every second."""
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("Error in trading loop tick")
            await asyncio.sleep(1)

    async def _fill_check_loop(self):
        """Periodically check for filled orders."""
        while self._running:
            await asyncio.sleep(self.config.fill_check_interval_seconds)
            try:
                newly_filled = self.executor.check_fills()
                for order in newly_filled:
                    self._log_event("fill", {
                        "order_id": order.order_id,
                        "side": order.side,
                        "price": order.price,
                        "shares": order.filled_shares,
                    })
            except Exception:
                logger.exception("Error in fill check")

            # Periodic cleanup of old orders
            self.executor.cleanup_old_orders()

    async def _tick(self):
        """Single iteration of the trading loop."""
        now = int(time.time())
        self._tick_count += 1

        # Detect interval boundaries using actual market data
        # Use the interval duration from the current market, or 900 (15-min) default
        interval_mod = self.interval_duration if self.interval_duration > 0 else 900
        current_interval = now - (now % interval_mod)
        seconds_into_interval = now % interval_mod

        # ---- NEW INTERVAL ----
        if current_interval != self.interval_start_ts:
            # Resolve positions from the PREVIOUS interval
            if self.interval_start_ts > 0:
                self._resolve_previous_interval()

            self.interval_start_ts = current_interval
            self.positions_this_interval = 0
            self.total_active_usd = 0

            # Reset per-interval bankroll tracking
            self.risk.reset_interval_deployed()

            # Cancel stale orders from previous interval
            self.executor.cancel_all()

            # Capture the Chainlink price at interval start
            if self.price_feed.latest_chainlink:
                self.interval_start_price = self.price_feed.latest_chainlink.price
                logger.info("New %d-min interval | start price: $%.2f",
                            interval_mod // 60, self.interval_start_price)
            else:
                self.interval_start_price = None
                logger.warning("No Chainlink price at interval start — waiting")

            # Find the best active market
            self.current_market = self.market_finder.get_best_market()
            if self.current_market:
                self.current_parsed = self.market_finder.parse_market(self.current_market)
                self.interval_duration = self.current_parsed.get("interval_duration", 900)
                interval_min = self.interval_duration // 60
                logger.info("Market: %s (%d-min)", self.current_parsed["slug"], interval_min)

                if interval_min <= 5:
                    logger.warning(
                        "5-min interval — higher thresholds active "
                        "(cross-trader: 5-min=-$8.4K, 15-min=+$305K)"
                    )
            else:
                logger.warning("No active BTC market found — waiting")
                return

        # ---- PERIODIC BALANCE REFRESH ----
        if self.risk.needs_balance_refresh():
            balance = self.executor.get_balance()
            if balance is not None:
                self.risk.set_balance(balance)

        # ---- SKIP CHECKS ----
        if not self.current_market or not self.interval_start_price:
            return
        if not self.price_feed.latest_binance or not self.price_feed.latest_chainlink:
            return
        # Check feed freshness
        if not self.price_feed.is_binance_fresh():
            if self._tick_count % 30 == 0:
                logger.warning("Binance feed stale — skipping")
            return
        if not self.price_feed.is_chainlink_fresh():
            if self._tick_count % 30 == 0:
                logger.warning("Chainlink feed stale — skipping")
            return

        # Max positions per interval
        if self.positions_this_interval >= self.config.max_positions_per_interval:
            return

        # Rate limit: don't evaluate more than once per 5 seconds
        if time.time() - self._last_signal_ts < 5:
            return

        # ---- GET MARKET PRICES ----
        parsed = self.current_parsed
        up_token = parsed["tokens"].get("UP", {})
        down_token = parsed["tokens"].get("DOWN", {})

        if not up_token.get("price") or not down_token.get("price"):
            return

        # Feed live market prices to executor for paper fill simulation
        if self.config.dry_run:
            token_prices = {}
            if up_token.get("token_id") and up_token.get("price"):
                token_prices[up_token["token_id"]] = up_token["price"]
            if down_token.get("token_id") and down_token.get("price"):
                token_prices[down_token["token_id"]] = down_token["price"]
            if token_prices:
                self.executor.update_market_prices(token_prices)

        # ---- EVALUATE STRATEGY (with volume confirmation) ----
        volume_stats = self.price_feed.get_volume_stats()
        signal = self.latency_arb.evaluate(
            interval_start_price=self.interval_start_price,
            current_binance_price=self.price_feed.latest_binance.price,
            current_chainlink_price=self.price_feed.latest_chainlink.price,
            market_up_price=up_token["price"],
            market_down_price=down_token["price"],
            seconds_into_interval=seconds_into_interval,
            interval_duration=self.interval_duration,
            current_exposure_usd=self.total_active_usd,
            volume_stats=volume_stats,
        )

        # Fallback: mispricing (pure arb when Up+Down < $1.00)
        if signal.side == Side.NONE:
            mispricing_signal = self.mispricing.evaluate(
                market_up_price=up_token["price"],
                market_down_price=down_token["price"],
            )
            if mispricing_signal.side != Side.NONE:
                signal = mispricing_signal

        self._last_signal_ts = time.time()

        if signal.side == Side.NONE:
            return

        # ---- RISK CHECK ----
        allowed, reason = self.risk.check_allowed(signal)
        if not allowed:
            logger.debug("Blocked by risk: %s", reason)
            return

        # Apply risk-adjusted sizing (reduces after loss streaks)
        adjusted_size = self.risk.adjust_size(signal)
        if adjusted_size != signal.size:
            logger.info("Size adjusted: $%.0f → $%.0f (loss streak)", signal.size, adjusted_size)
            signal = type(signal)(
                side=signal.side,
                confidence=signal.confidence,
                edge=signal.edge,
                price=signal.price,
                size=adjusted_size,
                reason=signal.reason,
            )

        # ---- SELECT TOKEN ----
        if signal.side in (Side.BUY_UP, Side.SELL_UP):
            token_id = up_token["token_id"]
        else:
            token_id = down_token["token_id"]

        if not token_id:
            return

        is_sell = signal.side in (Side.SELL_UP, Side.SELL_DOWN)

        # ---- EXECUTE ----
        logger.info("SIGNAL: %s", signal.reason)
        result = self.executor.place_order(
            signal=signal,
            token_id=token_id,
            post_only=self.config.maker_only,
            is_sell=is_sell,
        )

        if result.success:
            self.risk.record_trade(signal)
            self.positions_this_interval += 1
            self.total_active_usd += signal.size

            logger.info(
                "Position #%d opened | %s | $%.0f | total active: $%.0f | balance remaining: $%.0f",
                self.positions_this_interval,
                "SELL" if is_sell else "BUY",
                signal.size,
                self.total_active_usd,
                (self.config.max_exposure_usdc - self.total_active_usd),
            )

            self._log_event("trade", {
                "side": signal.side.value,
                "price": signal.price,
                "size_usd": signal.size,
                "confidence": signal.confidence,
                "edge": signal.edge,
                "interval_duration": self.interval_duration,
                "seconds_in": seconds_into_interval,
            })
        else:
            logger.warning("Order failed: %s", result.error)

    def _resolve_previous_interval(self):
        """Resolve positions from the previous interval.

        Checks Binance vs interval start price to determine Up or Down,
        then resolves all trades from that interval.
        """
        if not self.interval_start_price or not self.price_feed.latest_binance:
            return

        close_price = self.price_feed.latest_binance.price
        resolved_up = close_price > self.interval_start_price
        direction = "UP" if resolved_up else "DOWN"
        delta = close_price - self.interval_start_price
        delta_pct = delta / self.interval_start_price * 100

        logger.info(
            "Interval resolved: %s (open=$%.2f close=$%.2f %+.2f%%)",
            direction, self.interval_start_price, close_price, delta_pct,
        )

        # Resolve all trades from the previous interval
        resolved_count = 0
        for i, trade in enumerate(self.risk.trades):
            if trade.resolved:
                continue
            # Only resolve trades that are old enough to be from previous interval
            if trade.timestamp < self.interval_start_ts:
                continue

            # Determine if this trade won
            if trade.side in ("sell_down",):
                won = resolved_up  # Sold Down, BTC went Up → Down=$0 → we win
            elif trade.side in ("sell_up",):
                won = not resolved_up  # Sold Up, BTC went Down → Up=$0 → we win
            elif trade.side in ("buy_up",):
                won = resolved_up
            elif trade.side in ("buy_down",):
                won = not resolved_up
            else:
                continue

            self.risk.record_resolution(i, won)
            resolved_count += 1

            # Settle paper balance for this trade
            if self.config.dry_run:
                # Find the matching paper order by side+price+size
                for oid, order in self.executor.tracked_orders.items():
                    if (order.status == "filled"
                            and order.side == trade.side
                            and abs(order.price - trade.price) < 0.001
                            and abs(order.size_usd - trade.size) < 1):
                        self.executor.paper_settle_resolution(oid, won)
                        break

            self._log_event("resolution", {
                "side": trade.side,
                "won": won,
                "pnl": trade.pnl,
                "price": trade.price,
                "size": trade.size,
            })

        if resolved_count > 0:
            stats = self.risk.get_stats()
            logger.info(
                "Resolved %d position(s) | Win rate: %.1f%% | Daily P&L: $%.2f | "
                "Total P&L: $%.2f | Consecutive losses: %d",
                resolved_count,
                stats["win_rate"] * 100,
                stats["daily_pnl"],
                stats["total_pnl"],
                stats["consecutive_losses"],
            )

    async def _on_price_tick(self, tick: PriceTick):
        """Handle incoming price ticks — monitor for feed gaps."""
        # Log significant price gaps (potential arb opportunities)
        if tick.source == "binance" and self.price_feed.latest_chainlink:
            gap = tick.price - self.price_feed.latest_chainlink.price
            gap_bps = abs(gap) / tick.price * 10000
            if gap_bps > 10:  # Log when gap exceeds 10bps
                logger.debug(
                    "Price gap: %.1f bps (Binance=$%.0f Chainlink=$%.0f)",
                    gap_bps, tick.price, self.price_feed.latest_chainlink.price,
                )

    def stop(self):
        """Stop the bot gracefully."""
        logger.info("Stopping bot...")
        self._running = False
        self.price_feed.stop()
        self.executor.cancel_all()
        self.market_finder.close()

        stats = self.risk.get_stats()
        logger.info("Final stats: %s", json.dumps(stats, indent=2))

    def _log_event(self, event: str, data: dict):
        """Log a structured event to the trade log."""
        entry = {"ts": time.time(), "event": event, **data}
        try:
            with open(self.config.trade_log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
