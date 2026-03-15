"""Unified multi-strategy engine — runs ALL strategies across ALL market types.

Replaces the single-strategy BotEngine with a system that simultaneously runs:
1. Oracle Lag (crypto up/down) — across BTC, ETH, SOL, XRP
2. Arbitrage (any market where YES+NO < $1.00) — risk-free profit
3. Data Edge (sports/weather) — faster data than Polymarket oracles
4. Market Making (high-volume markets) — spread + maker rebates

Architecture:
- MarketScanner discovers ALL opportunities (replaces MarketFinder)
- MultiFeed provides multi-asset crypto prices (replaces single PriceFeed)
- DataEdgeAggregator provides weather/sports/news signals
- Each strategy type has its own evaluation loop
- All orders go through the same Executor + RiskManager

The core principle: "The edge is not prediction. It's timing."
"""

import asyncio
import json
import time
import logging

from bot.config import Config
from bot.market_scanner import MarketScanner, MarketOpportunity
from bot.multi_feed import MultiFeed
from bot.data_edge import DataEdgeAggregator, DataSignal
from bot.strategies import LatencyArbStrategy, MispricingStrategy, MarketMakingStrategy, Signal, Side
from bot.executor import Executor
from bot.risk import RiskManager

logger = logging.getLogger(__name__)


class UnifiedEngine:
    """Multi-strategy trading engine.

    Runs all 4 strategy types concurrently:
    - Oracle lag on crypto markets (BTC/ETH/SOL/XRP)
    - Arbitrage on any mispriced market
    - Data edge on sports/weather markets
    - Market making on high-volume markets

    Each strategy operates independently but shares:
    - Risk manager (global exposure limits)
    - Executor (order placement)
    - Trade log (unified event stream)
    """

    def __init__(self, config: Config):
        self.config = config

        # Market discovery
        self.scanner = MarketScanner(config.gamma_host)

        # Price feeds (multi-asset crypto)
        self.feeds = MultiFeed(assets=["btc", "eth", "sol", "xrp"])

        # External data sources (weather, sports, news)
        self.data_edge = DataEdgeAggregator()

        # Execution + risk (shared across all strategies)
        self.executor = Executor(config)
        self.risk = RiskManager(config)

        # Strategies
        self.latency_arb = LatencyArbStrategy(
            min_edge=config.min_edge_threshold,
            confidence_threshold=config.confidence_threshold,
            position_usd=config.position_usd,
        )
        self.arb_strategy = MispricingStrategy(
            combined_threshold=0.985,
            max_position=config.position_usd,
        )
        self.mm_strategy = MarketMakingStrategy(
            spread_target=0.04,
            max_position=config.position_usd * 0.5,
        )

        # State
        self._running = False
        self._tick_count = 0
        self._last_scan_ts: float = 0
        self._scan_interval: float = 15  # seconds between market scans
        self._opportunities: list[MarketOpportunity] = []

        # Per-asset interval tracking (for crypto oracle lag)
        self._interval_starts: dict[str, float] = {}  # asset → start_ts
        self._interval_prices: dict[str, float] = {}   # asset → start_price
        self._positions_per_interval: dict[str, int] = {}  # asset → count
        self._last_trade_ts: dict[str, float] = {}     # asset → last trade time

        # Arb tracking
        self._arb_traded: set[str] = set()  # slugs already traded this cycle
        self._last_arb_check: float = 0

        # Market making tracking
        self._mm_active_markets: set[str] = set()  # slugs with active MM orders
        self._mm_inventory: dict[str, dict] = {}  # slug → {"up": shares, "down": shares}
        self._last_mm_check: float = 0

        # Stats
        self._trades_by_strategy: dict[str, int] = {
            "oracle_lag": 0, "arb": 0, "data_edge": 0, "mm": 0,
        }
        self._start_time: float = time.time()
        self._last_stats_report: float = 0
        self._stats_interval: float = 900  # report every 15 minutes
        self._signals_evaluated: int = 0
        self._signals_rejected: int = 0
        self._markets_scanned: int = 0

        # Dashboard history (ring buffers for charts)
        self._pnl_history: list[dict] = []        # [{ts, balance, pnl, exposure}]
        self._signal_log: list[dict] = []          # [{ts, asset, strategy, reason, side, edge, conf}]
        self._last_pnl_snapshot: float = 0
        self._pnl_snapshot_interval: float = 30    # snapshot every 30s

    async def run(self):
        """Main entry point — start all subsystems concurrently."""
        self._running = True
        mode = "PAPER TEST" if self.config.dry_run else "LIVE"
        logger.info("═══ Unified engine starting [%s] — 4 strategies active ═══", mode)
        if self.config.dry_run:
            logger.info("*** TESTING MODE — NO REAL MONEY — tracking all signals, trades, and results ***")

        balance = self.executor.get_balance()
        if balance is not None:
            logger.info("Available balance: $%.2f", balance)
            self.risk.set_balance(balance)

        results = await asyncio.gather(
            self.feeds.start(),
            self._data_edge_loop(),
            self._strategy_loop(),
            self._fill_check_loop(),
            return_exceptions=True,
        )

        task_names = ["feeds", "data_edge", "strategy_loop", "fill_check"]
        for name, result in zip(task_names, results):
            if isinstance(result, Exception):
                logger.error("Task %s crashed: %s", name, result, exc_info=result)

    async def _data_edge_loop(self):
        """Run external data source polling in the background."""
        try:
            await self.data_edge.start(poll_interval=30)
        except Exception:
            logger.exception("Data edge aggregator crashed")

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
            self.executor.cleanup_old_orders()

    async def _strategy_loop(self):
        """Main strategy loop — evaluates all strategies every tick."""
        while self._running:
            try:
                await self._tick()
            except Exception:
                logger.exception("Error in strategy tick")
            await asyncio.sleep(1)

    async def _tick(self):
        """Single tick: scan markets, evaluate all strategies."""
        now = time.time()
        self._tick_count += 1

        # Periodic stats report
        self._log_periodic_stats()

        # P&L snapshot for dashboard chart
        if (now - self._last_pnl_snapshot) >= self._pnl_snapshot_interval:
            self._last_pnl_snapshot = now
            stats = self.risk.get_stats()
            self._pnl_history.append({
                "ts": now,
                "balance": round(stats["current_balance"], 2),
                "pnl": round(stats["total_pnl"], 2),
                "exposure": round(stats["current_exposure"], 2),
            })
            # Keep last 2 hours of data (240 points at 30s intervals)
            if len(self._pnl_history) > 240:
                self._pnl_history = self._pnl_history[-240:]

        # Periodic market scan
        if (now - self._last_scan_ts) > self._scan_interval:
            try:
                self._opportunities = await self.scanner.scan_all()
                self._markets_scanned += len(self._opportunities)
                self._last_scan_ts = now
            except Exception:
                logger.exception("Market scan failed")

        # Periodic balance refresh
        if self.risk.needs_balance_refresh():
            balance = self.executor.get_balance()
            if balance is not None:
                self.risk.set_balance(balance)

        # Evaluate each strategy type
        await self._evaluate_crypto_oracle()
        await self._evaluate_arbitrage()
        await self._evaluate_data_edge()
        await self._evaluate_market_making()

    # ---- STRATEGY 1: Crypto Oracle Lag ----

    async def _evaluate_crypto_oracle(self):
        """Evaluate oracle lag strategy across all crypto assets."""
        crypto_markets = [
            m for m in self._opportunities if m.category == "crypto_oracle"
        ]

        if not crypto_markets:
            return

        now = int(time.time())

        for market in crypto_markets:
            asset = market.asset
            if not asset:
                continue

            # Check if we have fresh price data for this asset
            if not self.feeds.is_fresh(asset, max_age_s=15):
                continue

            ap = self.feeds.get_asset_price(asset)
            if not ap:
                continue

            duration = market.interval_duration or 900
            current_interval = now - (now % duration)
            seconds_into = now % duration

            # Detect new interval → reset state
            if self._interval_starts.get(asset) != current_interval:
                self._interval_starts[asset] = current_interval
                self._positions_per_interval[asset] = 0
                self._last_trade_ts[asset] = 0
                self.feeds.capture_interval_start(asset)
                self._interval_prices[asset] = ap.price
                logger.info(
                    "New %dm interval for %s | start=$%.2f",
                    duration // 60, asset.upper(), ap.price,
                )

            start_price = self._interval_prices.get(asset)
            if not start_price:
                continue

            # Max positions per interval per asset
            if self._positions_per_interval.get(asset, 0) >= self.config.max_positions_per_interval:
                continue

            # Rate limit: min 120s between trades per asset
            last_trade = self._last_trade_ts.get(asset, 0)
            if last_trade and (time.time() - last_trade) < 120:
                continue

            # Rate limit signal evaluation to every 5s
            if self._tick_count % 5 != 0:
                continue

            # Get market prices
            up_token = market.tokens.get("UP", {})
            down_token = market.tokens.get("DOWN", {})
            up_price = up_token.get("price")
            down_price = down_token.get("price")

            if not up_price or not down_price:
                continue

            # Feed paper prices to executor
            if self.config.dry_run:
                token_prices = {}
                if up_token.get("token_id"):
                    token_prices[up_token["token_id"]] = up_price
                if down_token.get("token_id"):
                    token_prices[down_token["token_id"]] = down_price
                if token_prices:
                    self.executor.update_market_prices(token_prices)

            # Evaluate using the latency arb strategy
            # Use MultiFeed price as "binance" and interval start as reference
            signal = self.latency_arb.evaluate(
                interval_start_price=start_price,
                current_binance_price=ap.price,
                current_chainlink_price=ap.price,  # Same source in multi-feed mode
                market_up_price=up_price,
                market_down_price=down_price,
                seconds_into_interval=seconds_into,
                interval_duration=duration,
                current_exposure_usd=self.risk.total_exposure,
            )

            self._signals_evaluated += 1
            self._record_signal(
                asset=asset.upper(), strategy="oracle_lag",
                side=signal.side.value if signal.side != Side.NONE else "none",
                edge=signal.edge, confidence=signal.confidence,
                reason=signal.reason,
                accepted=signal.side != Side.NONE,
            )
            if signal.side == Side.NONE:
                self._signals_rejected += 1
                if self._tick_count % 60 == 0:
                    logger.debug(
                        "%s oracle: %s | Up=$%.2f Down=$%.2f",
                        asset.upper(), signal.reason[:80], up_price, down_price,
                    )
                continue

            # Execute
            await self._execute_signal(
                signal, market, "oracle_lag",
                f"{asset.upper()} oracle lag",
            )

            if signal.side != Side.NONE:
                self._positions_per_interval[asset] = (
                    self._positions_per_interval.get(asset, 0) + 1
                )
                self._last_trade_ts[asset] = time.time()

    # ---- STRATEGY 2: Arbitrage ----

    async def _evaluate_arbitrage(self):
        """Check for risk-free arb opportunities (YES+NO < $1.00)."""
        # Only check every 10 seconds
        if (time.time() - self._last_arb_check) < 10:
            return
        self._last_arb_check = time.time()

        arb_markets = [
            m for m in self._opportunities
            if m.category == "arb" and m.slug not in self._arb_traded
        ]

        for market in arb_markets:
            if market.arb_edge < 0.015:  # Need 1.5% edge minimum
                continue

            up_token = market.tokens.get("UP", {})
            down_token = market.tokens.get("DOWN", {})
            up_price = up_token.get("price", 0.5)
            down_price = down_token.get("price", 0.5)

            signal = self.arb_strategy.evaluate(up_price, down_price)
            self._signals_evaluated += 1

            if signal.side == Side.NONE:
                self._signals_rejected += 1
                continue

            logger.info(
                "ARB: %s | combined=$%.3f edge=%.1f%% | %s",
                market.slug[:40], market.combined_price,
                market.arb_edge * 100, market.question[:60],
            )

            await self._execute_signal(
                signal, market, "arb",
                f"Arb {market.slug[:30]}",
            )

            # Don't trade the same arb twice
            self._arb_traded.add(market.slug)

        # Clean old arb slugs periodically
        if len(self._arb_traded) > 200:
            self._arb_traded.clear()

    # ---- STRATEGY 3: Data Edge ----

    async def _evaluate_data_edge(self):
        """Match data signals (sports/weather/news) to markets."""
        # Only check every 15 seconds
        if self._tick_count % 15 != 0:
            return

        signals = self.data_edge.get_signals()
        if not signals:
            return

        # Try to match each market against data signals
        for market in self._opportunities:
            if market.category in ("crypto_oracle", "arb"):
                continue  # Already handled above

            data_signal = self.data_edge.match_market(market.question)
            if not data_signal:
                continue

            # For news/reddit signals with unknown direction, infer from market price:
            # if YES side is cheap (<0.40), lean YES; if NO side is cheap, lean NO
            if data_signal.direction == "unknown":
                up_p = up_token.get("price", 0.5)
                down_p = down_token.get("price", 0.5)
                if up_p < 0.40:
                    data_signal = DataSignal(
                        source=data_signal.source,
                        market_keyword=data_signal.market_keyword,
                        direction="yes",
                        confidence=min(0.65, data_signal.confidence + 0.1),
                        data_value=data_signal.data_value,
                        timestamp=data_signal.timestamp,
                        details=data_signal.details,
                    )
                elif down_p < 0.40:
                    data_signal = DataSignal(
                        source=data_signal.source,
                        market_keyword=data_signal.market_keyword,
                        direction="no",
                        confidence=min(0.65, data_signal.confidence + 0.1),
                        data_value=data_signal.data_value,
                        timestamp=data_signal.timestamp,
                        details=data_signal.details,
                    )
                else:
                    continue  # Both sides near 50/50, no edge

            if data_signal.confidence < 0.55:
                continue

            # Build a trading signal from the data edge
            up_token = market.tokens.get("UP", {})
            down_token = market.tokens.get("DOWN", {})
            up_price = up_token.get("price", 0.5)
            down_price = down_token.get("price", 0.5)

            if data_signal.direction == "yes":
                side = Side.BUY_UP
                price = up_price
            else:
                side = Side.BUY_DOWN
                price = down_price

            if not price or price <= 0 or price >= 0.95:
                continue

            edge = data_signal.confidence - price
            if edge < 0.05:  # Need 5% edge
                continue

            size = min(
                self.config.position_usd * 0.5,  # Half size for data edge
                self.config.max_exposure_usdc * 0.1,
            )

            signal = Signal(
                side=side,
                confidence=data_signal.confidence,
                edge=edge,
                price=price,
                size=size,
                reason=(
                    f"DATA EDGE [{data_signal.source}]: {data_signal.data_value} | "
                    f"market='{market.question[:50]}' | "
                    f"conf={data_signal.confidence:.0%} edge={edge:.0%}"
                ),
            )

            logger.info(
                "DATA EDGE signal: %s → %s (conf=%.0f%% edge=%.0f%%)",
                data_signal.source, market.question[:40],
                data_signal.confidence * 100, edge * 100,
            )

            await self._execute_signal(
                signal, market, "data_edge",
                f"Data edge ({data_signal.source})",
            )

    # ---- STRATEGY 4: Market Making ----

    async def _evaluate_market_making(self):
        """Post bid+ask on high-volume markets, collect spread + maker rebates.

        Selects markets with good volume and spread, then places both sides.
        Uses inventory tracking to skew quotes and avoid directional exposure.
        """
        # Only check every 30 seconds (MM orders sit in the book)
        if (time.time() - self._last_mm_check) < 30:
            return
        self._last_mm_check = time.time()

        # Don't MM if we're already at high exposure
        if self.risk.total_exposure > self.config.max_exposure_usdc * 0.6:
            return

        # Find suitable markets: high volume, not already traded by other strategies
        crypto_slugs = {m.slug for m in self._opportunities if m.category == "crypto_oracle"}
        arb_slugs = {m.slug for m in self._opportunities if m.category == "arb"}

        mm_candidates = [
            m for m in self._opportunities
            if m.slug not in crypto_slugs
            and m.slug not in arb_slugs
            and m.slug not in self._mm_active_markets
            and m.volume_24h > 1000  # Minimum $1K daily volume
            and m.spread > 0.02      # At least 2 cent spread to capture
            and m.spread < 0.20      # Not too wide (illiquid/risky)
        ]

        # Sort by volume (higher volume = more fills)
        mm_candidates.sort(key=lambda m: -m.volume_24h)

        # Only MM on top 3 markets at a time
        max_mm_markets = 3
        active_count = len(self._mm_active_markets)

        for market in mm_candidates[:max_mm_markets - active_count]:
            up_token = market.tokens.get("UP", {})
            down_token = market.tokens.get("DOWN", {})
            up_price = up_token.get("price")
            down_price = down_token.get("price")

            if not up_price or not down_price:
                continue

            # Calculate midpoint
            midpoint = up_price  # Up price IS the midpoint for a binary market

            # Get current inventory for this market
            inv = self._mm_inventory.get(market.slug, {"up": 0, "down": 0})

            # Generate quotes
            bid_signal, ask_signal = self.mm_strategy.get_quotes(
                midpoint=midpoint,
                current_inventory_up=inv["up"],
                current_inventory_down=inv["down"],
            )

            # Feed paper prices
            if self.config.dry_run:
                token_prices = {}
                if up_token.get("token_id"):
                    token_prices[up_token["token_id"]] = up_price
                if down_token.get("token_id"):
                    token_prices[down_token["token_id"]] = down_price
                if token_prices:
                    self.executor.update_market_prices(token_prices)

            # Place bid (buy UP at lower price)
            if bid_signal.side != Side.NONE and bid_signal.price > 0.01:
                logger.info(
                    "MM BID: %s | %s @ $%.2f | mid=$%.2f",
                    market.slug[:30], bid_signal.side.value,
                    bid_signal.price, midpoint,
                )
                await self._execute_signal(
                    bid_signal, market, "mm",
                    f"MM bid {market.slug[:20]}",
                )

            # Place ask (buy DOWN = sell UP at higher price)
            if ask_signal.side != Side.NONE and ask_signal.price > 0.01:
                logger.info(
                    "MM ASK: %s | %s @ $%.2f | mid=$%.2f",
                    market.slug[:30], ask_signal.side.value,
                    ask_signal.price, midpoint,
                )
                await self._execute_signal(
                    ask_signal, market, "mm",
                    f"MM ask {market.slug[:20]}",
                )

            self._mm_active_markets.add(market.slug)

        # Clean old MM markets every 5 minutes
        if len(self._mm_active_markets) > 10:
            self._mm_active_markets.clear()

    # ---- Shared execution ----

    async def _execute_signal(
        self,
        signal: Signal,
        market: MarketOpportunity,
        strategy: str,
        label: str,
    ):
        """Execute a signal through risk check → executor pipeline."""
        if signal.side == Side.NONE:
            return

        # Risk check
        allowed, reason = self.risk.check_allowed(signal)
        if not allowed:
            logger.info("Blocked by risk (%s): %s", label, reason)
            return

        # Adjust size
        adjusted_size = self.risk.adjust_size(signal)
        if adjusted_size <= 0:
            logger.info("Blocked by sizing (%s): adjusted to $0 (Kelly too small)", label)
            return
        if adjusted_size != signal.size:
            signal = Signal(
                side=signal.side,
                confidence=signal.confidence,
                edge=signal.edge,
                price=signal.price,
                size=adjusted_size,
                reason=signal.reason,
            )

        # Select token — handle both binary (UP/DOWN) and multi-outcome markets
        up_token = market.tokens.get("UP", {})
        down_token = market.tokens.get("DOWN", {})

        if signal.side in (Side.BUY_UP, Side.SELL_UP):
            token_id = up_token.get("token_id")
        else:
            token_id = down_token.get("token_id")

        # Fallback for multi-outcome markets: pick cheapest token
        if not token_id:
            cheapest = None
            for label, tok in market.tokens.items():
                tid = tok.get("token_id")
                if not tid:
                    continue
                if cheapest is None or tok.get("price", 1) < cheapest.get("price", 1):
                    cheapest = tok
            if cheapest:
                token_id = cheapest.get("token_id")

        if not token_id:
            return

        is_sell = signal.side in (Side.SELL_UP, Side.SELL_DOWN)

        logger.info("SIGNAL [%s]: %s", strategy, signal.reason)

        result = self.executor.place_order(
            signal=signal,
            token_id=token_id,
            post_only=self.config.maker_only,
            is_sell=is_sell,
        )

        if result.success:
            self.risk.record_trade(signal)
            self._trades_by_strategy[strategy] = (
                self._trades_by_strategy.get(strategy, 0) + 1
            )

            logger.info(
                "[%s] Position opened | %s | $%.0f | exposure=$%.0f",
                strategy, "SELL" if is_sell else "BUY",
                signal.size, self.risk.total_exposure,
            )

            self._log_event("trade", {
                "strategy": strategy,
                "side": signal.side.value,
                "price": signal.price,
                "size_usd": signal.size,
                "confidence": signal.confidence,
                "edge": signal.edge,
                "market_slug": market.slug,
                "market_question": market.question[:100],
                "asset": market.asset,
                "category": market.category,
            })
        else:
            logger.warning("[%s] Order failed: %s", strategy, result.error)

    def _log_periodic_stats(self):
        """Log a detailed stats report — runs every 15 minutes."""
        now = time.time()
        if (now - self._last_stats_report) < self._stats_interval:
            return
        self._last_stats_report = now

        uptime_s = now - self._start_time
        hours = int(uptime_s // 3600)
        minutes = int((uptime_s % 3600) // 60)

        stats = self.risk.get_stats()
        mode = "PAPER TEST" if self.config.dry_run else "LIVE"

        logger.info(
            "═══ %s MODE — PERIODIC REPORT (uptime %dh %dm) ═══",
            mode, hours, minutes,
        )
        logger.info(
            "Balance: $%.2f (start: $%.2f) | P&L: $%+.2f | Exposure: $%.2f",
            stats["current_balance"], stats["session_start_balance"],
            stats["total_pnl"], stats["current_exposure"],
        )
        logger.info(
            "Trades: %d placed, %d resolved | Wins: %d, Losses: %d | Win rate: %.0f%%",
            stats["total_trades"], stats["resolved"],
            stats["wins"], stats["losses"],
            stats["win_rate"] * 100,
        )
        logger.info(
            "By strategy: oracle_lag=%d, arb=%d, data_edge=%d, mm=%d",
            self._trades_by_strategy.get("oracle_lag", 0),
            self._trades_by_strategy.get("arb", 0),
            self._trades_by_strategy.get("data_edge", 0),
            self._trades_by_strategy.get("mm", 0),
        )
        logger.info(
            "Markets scanned: %d | Signals evaluated: %d | Rejected: %d | Loss streak: %d | Halted: %s",
            self._markets_scanned, self._signals_evaluated,
            self._signals_rejected, stats["consecutive_losses"],
            stats["drawdown_halted"],
        )

        # Also log to trades.jsonl for analysis
        self._log_event("stats_report", {
            "uptime_hours": round(uptime_s / 3600, 2),
            "mode": mode,
            **stats,
            "trades_by_strategy": self._trades_by_strategy,
            "markets_scanned": self._markets_scanned,
            "signals_evaluated": self._signals_evaluated,
            "signals_rejected": self._signals_rejected,
        })

    def stop(self):
        """Stop all subsystems gracefully."""
        mode = "PAPER TEST" if self.config.dry_run else "LIVE"
        logger.info("Stopping unified engine...")
        self._running = False
        self.feeds.stop()
        self.data_edge.stop()
        self.executor.cancel_all()

        uptime_s = time.time() - self._start_time
        hours = int(uptime_s // 3600)
        minutes = int((uptime_s % 3600) // 60)

        stats = self.risk.get_stats()
        stats["trades_by_strategy"] = self._trades_by_strategy

        logger.info("═══ %s MODE — FINAL SESSION REPORT (ran %dh %dm) ═══", mode, hours, minutes)
        logger.info("Final stats: %s", json.dumps(stats, indent=2))

        self._log_event("session_end", {
            "mode": mode,
            "uptime_hours": round(uptime_s / 3600, 2),
            **stats,
        })

    async def close(self):
        """Clean up async resources."""
        await asyncio.gather(
            self.scanner.close(),
            self.data_edge.close(),
            return_exceptions=True,
        )

    def _record_signal(self, asset: str, strategy: str, side: str,
                        edge: float, confidence: float, reason: str,
                        accepted: bool):
        """Record a signal evaluation for the dashboard feed."""
        self._signal_log.append({
            "ts": time.time(),
            "asset": asset,
            "strategy": strategy,
            "side": side,
            "edge": round(edge, 4),
            "confidence": round(confidence, 4),
            "reason": reason[:120],
            "accepted": accepted,
        })
        # Keep last 200 signals
        if len(self._signal_log) > 200:
            self._signal_log = self._signal_log[-200:]

    def _log_event(self, event: str, data: dict):
        """Log a structured event to the trade log."""
        entry = {"ts": time.time(), "event": event, **data}
        try:
            with open(self.config.trade_log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
