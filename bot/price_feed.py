"""Real-time BTC price feeds from Binance and Polymarket RTDS (Chainlink).

The latency gap between these two feeds is the core edge for the latency
arb strategy. Binance updates in ~100ms, Chainlink in ~3-15 seconds.
"""

import asyncio
import json
import time
import logging
from collections import deque
from dataclasses import dataclass

import websockets

logger = logging.getLogger(__name__)

# Max age before a price is considered stale
STALE_THRESHOLD_MS = 30_000  # 30 seconds

# Rolling window for volume aggregation
VOLUME_WINDOW_SECONDS = 300  # 5-minute rolling volume window


@dataclass
class PriceTick:
    source: str  # "binance" or "chainlink"
    symbol: str
    price: float
    timestamp_ms: int


@dataclass
class VolumeTick:
    """A single Binance trade with volume data."""
    price: float
    quantity_btc: float   # BTC quantity
    quantity_usd: float   # USD equivalent (price * qty)
    timestamp_ms: int
    is_buyer_maker: bool  # True = sell aggressor, False = buy aggressor


class PriceFeed:
    """Manages WebSocket connections to Binance and Polymarket RTDS for
    real-time BTC price data. The latency gap between these two feeds
    is the core edge for the latency arbitrage strategy.

    Also tracks rolling BTC trade volume from Binance for signal confirmation.
    High-volume moves are more likely to sustain direction (higher confidence).
    Low-volume moves may be fakeouts (lower confidence).
    """

    BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    RTDS_WS = "wss://ws-live-data.polymarket.com"

    def __init__(self):
        self.latest_binance: PriceTick | None = None
        self.latest_chainlink: PriceTick | None = None
        self._callbacks: list = []
        self._running = False
        self._binance_last_update: float = 0
        self._chainlink_last_update: float = 0

        # Rolling volume tracking (5-min window)
        self._volume_window: deque[VolumeTick] = deque()
        self._volume_window_seconds = VOLUME_WINDOW_SECONDS
        # Baseline: calibrated from BTC/USDT typical 5-min volume
        # ~$50M per 5-min is normal BTC volume, update dynamically
        self._volume_ema: float = 0.0  # Exponential moving average of 5-min volume
        self._volume_ema_alpha: float = 0.1  # EMA smoothing factor

    def on_tick(self, callback):
        """Register a callback for price updates: callback(tick: PriceTick)."""
        self._callbacks.append(callback)

    async def _notify(self, tick: PriceTick):
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(tick)
                else:
                    cb(tick)
            except Exception:
                logger.exception("Error in price tick callback")

    async def _binance_stream(self):
        """Connect to Binance trade stream for lowest-latency BTC/USDT prices."""
        while self._running:
            try:
                async with websockets.connect(self.BINANCE_WS) as ws:
                    logger.info("Connected to Binance BTC/USDT trade stream")
                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            price = float(data["p"])
                            ts_ms = int(data["T"])

                            tick = PriceTick(
                                source="binance",
                                symbol="BTC/USDT",
                                price=price,
                                timestamp_ms=ts_ms,
                            )
                            self.latest_binance = tick
                            self._binance_last_update = time.time()

                            # Track volume from every Binance trade
                            qty_btc = float(data.get("q", 0))
                            if qty_btc > 0:
                                vol_tick = VolumeTick(
                                    price=price,
                                    quantity_btc=qty_btc,
                                    quantity_usd=price * qty_btc,
                                    timestamp_ms=ts_ms,
                                    is_buyer_maker=bool(data.get("m", False)),
                                )
                                self._volume_window.append(vol_tick)
                                self._prune_volume_window(ts_ms)

                            await self._notify(tick)
                        except (KeyError, ValueError) as e:
                            logger.warning("Bad Binance message: %s", e)
            except Exception:
                logger.exception("Binance stream error, reconnecting in 2s")
                await asyncio.sleep(2)

    async def _chainlink_stream(self):
        """Connect to Polymarket RTDS for Chainlink BTC/USD prices.
        This is the resolution source — the price that actually determines
        whether 'Up' or 'Down' wins."""
        sub_msg = json.dumps(
            {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": '{"symbol":"btc/usd"}',
                    }
                ],
            }
        )

        while self._running:
            try:
                async with websockets.connect(self.RTDS_WS) as ws:
                    await ws.send(sub_msg)
                    logger.info("Connected to Polymarket RTDS Chainlink stream")
                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            if "value" not in data:
                                continue

                            # Validate timestamp — reject if missing (can't measure lag)
                            raw_ts = data.get("timestamp")
                            if raw_ts:
                                ts_ms = int(raw_ts)
                            else:
                                ts_ms = int(time.time() * 1000)
                                logger.debug("Chainlink tick missing timestamp, using now")

                            tick = PriceTick(
                                source="chainlink",
                                symbol="BTC/USD",
                                price=float(data["value"]),
                                timestamp_ms=ts_ms,
                            )
                            self.latest_chainlink = tick
                            self._chainlink_last_update = time.time()
                            await self._notify(tick)
                        except (KeyError, ValueError) as e:
                            logger.warning("Bad Chainlink message: %s", e)
            except Exception:
                logger.exception("Chainlink RTDS stream error, reconnecting in 2s")
                await asyncio.sleep(2)

    async def _health_monitor(self):
        """Periodically log feed health and warn on stale data."""
        while self._running:
            await asyncio.sleep(30)
            now = time.time()

            binance_age = now - self._binance_last_update if self._binance_last_update else float('inf')
            chainlink_age = now - self._chainlink_last_update if self._chainlink_last_update else float('inf')

            if binance_age > 10:
                logger.warning("Binance feed stale (%.1fs since last update)", binance_age)
            if chainlink_age > 30:
                logger.warning("Chainlink feed stale (%.1fs since last update)", chainlink_age)

            if self.latest_binance and self.latest_chainlink:
                gap = self.latest_binance.price - self.latest_chainlink.price
                gap_pct = gap / self.latest_binance.price * 100
                logger.info(
                    "Feed health: Binance=$%.0f (%.1fs ago) Chainlink=$%.0f (%.1fs ago) gap=$%.0f (%.3f%%)",
                    self.latest_binance.price, binance_age,
                    self.latest_chainlink.price, chainlink_age,
                    gap, gap_pct,
                )

    async def start(self):
        """Start both price feed streams and health monitor concurrently."""
        self._running = True
        await asyncio.gather(
            self._binance_stream(),
            self._chainlink_stream(),
            self._health_monitor(),
        )

    def stop(self):
        self._running = False

    # ---- Volume analysis ----

    def _prune_volume_window(self, now_ms: int | None = None):
        """Remove ticks older than the volume window."""
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        cutoff = now_ms - (self._volume_window_seconds * 1000)
        while self._volume_window and self._volume_window[0].timestamp_ms < cutoff:
            self._volume_window.popleft()

    def get_volume_stats(self, window_seconds: int | None = None) -> dict:
        """Get rolling volume statistics over the window.

        Returns:
            total_usd: Total USD volume in window
            buy_usd: Buy-side (aggressor) volume
            sell_usd: Sell-side (aggressor) volume
            buy_ratio: Buy volume / total volume (>0.5 = buy pressure)
            trade_count: Number of trades in window
            avg_trade_usd: Average trade size in USD
            volume_ratio: Current volume / EMA volume (>1 = above average)
        """
        if window_seconds is None:
            window_seconds = self._volume_window_seconds

        now_ms = int(time.time() * 1000)
        cutoff = now_ms - (window_seconds * 1000)

        total_usd = 0.0
        buy_usd = 0.0
        sell_usd = 0.0
        count = 0

        for vt in self._volume_window:
            if vt.timestamp_ms >= cutoff:
                total_usd += vt.quantity_usd
                count += 1
                if vt.is_buyer_maker:
                    sell_usd += vt.quantity_usd  # Seller is aggressor
                else:
                    buy_usd += vt.quantity_usd  # Buyer is aggressor

        # Update EMA of volume (adaptive baseline)
        if total_usd > 0 and self._volume_ema > 0:
            self._volume_ema = (
                self._volume_ema_alpha * total_usd +
                (1 - self._volume_ema_alpha) * self._volume_ema
            )
        elif total_usd > 0:
            self._volume_ema = total_usd

        volume_ratio = total_usd / self._volume_ema if self._volume_ema > 0 else 1.0

        return {
            "total_usd": total_usd,
            "buy_usd": buy_usd,
            "sell_usd": sell_usd,
            "buy_ratio": buy_usd / total_usd if total_usd > 0 else 0.5,
            "trade_count": count,
            "avg_trade_usd": total_usd / count if count > 0 else 0,
            "volume_ratio": volume_ratio,
            "volume_ema": self._volume_ema,
        }

    def is_binance_fresh(self, max_age_s: float = 10) -> bool:
        """Check if Binance data is fresh enough to trade on."""
        if not self._binance_last_update:
            return False
        return (time.time() - self._binance_last_update) < max_age_s

    def is_chainlink_fresh(self, max_age_s: float = 30) -> bool:
        """Check if Chainlink data is fresh enough to trade on."""
        if not self._chainlink_last_update:
            return False
        return (time.time() - self._chainlink_last_update) < max_age_s

    def get_price_gap(self) -> float | None:
        """Return Binance - Chainlink price difference.
        Positive = Binance higher (BTC trending up, Chainlink lagging).
        Negative = Binance lower (BTC trending down, Chainlink lagging)."""
        if self.latest_binance and self.latest_chainlink:
            return self.latest_binance.price - self.latest_chainlink.price
        return None
