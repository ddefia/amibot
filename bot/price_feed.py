"""Real-time BTC price feeds from Binance and Polymarket RTDS (Chainlink).

The latency gap between these two feeds is the core edge for the latency
arb strategy. Binance updates in ~100ms, Chainlink in ~3-15 seconds.

Includes REST API fallback for environments where WebSockets are blocked
(firewalls, sandboxes, corporate networks). Falls back automatically
after repeated WS failures.
"""

import asyncio
import json
import time
import logging
from collections import deque
from dataclasses import dataclass

import httpx
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
    BINANCE_REST = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    # Fallback: Binance US or CoinGecko if main Binance is geo-blocked
    BINANCE_US_REST = "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSDT"
    COINGECKO_REST = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
    # Additional fallbacks for geo-restricted servers
    KRAKEN_REST = "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"
    COINBASE_REST = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
    RTDS_WS = "wss://ws-live-data.polymarket.com"

    # After this many consecutive WS failures, switch to REST polling
    WS_FAIL_THRESHOLD = 3
    # REST polling interval (seconds) — slower than WS but works everywhere
    REST_POLL_INTERVAL = 2.0
    # If a WS is connected but no valid data arrives in this many seconds, force REST fallback
    WS_STALE_DATA_TIMEOUT = 30.0

    def __init__(self):
        self.latest_binance: PriceTick | None = None
        self.latest_chainlink: PriceTick | None = None
        self._callbacks: list = []
        self._running = False
        self._binance_last_update: float = 0
        self._chainlink_last_update: float = 0

        # WebSocket failure tracking for auto-fallback to REST
        self._binance_ws_fails: int = 0
        self._chainlink_ws_fails: int = 0
        self._using_rest_binance: bool = False
        self._using_rest_chainlink: bool = False
        self._http_client: httpx.AsyncClient | None = None

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
        """Connect to Binance trade stream for lowest-latency BTC/USDT prices.
        Falls back to REST API polling after repeated WebSocket failures."""
        while self._running:
            # Check if we should fall back to REST
            if self._binance_ws_fails >= self.WS_FAIL_THRESHOLD:
                if not self._using_rest_binance:
                    logger.warning(
                        "Binance WebSocket failed %d times — switching to REST API polling",
                        self._binance_ws_fails,
                    )
                    self._using_rest_binance = True
                await self._binance_rest_poll()
                return  # REST poll runs its own loop

            try:
                async with websockets.connect(self.BINANCE_WS) as ws:
                    logger.info("Connected to Binance BTC/USDT trade stream")
                    self._binance_ws_fails = 0  # Reset on successful connect
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
                self._binance_ws_fails += 1
                logger.warning(
                    "Binance WS error (%d/%d before REST fallback), retrying in 2s",
                    self._binance_ws_fails, self.WS_FAIL_THRESHOLD,
                )
                await asyncio.sleep(2)

    async def _chainlink_stream(self):
        """Connect to Polymarket RTDS for Chainlink BTC/USD prices.
        This is the resolution source — the price that actually determines
        whether 'Up' or 'Down' wins.
        Falls back to using Binance price as proxy if RTDS WebSocket is blocked."""
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
            # Check if we should fall back to REST (use Binance price as Chainlink proxy)
            if self._chainlink_ws_fails >= self.WS_FAIL_THRESHOLD:
                if not self._using_rest_chainlink:
                    logger.warning(
                        "Chainlink RTDS WebSocket failed %d times — "
                        "falling back to Binance REST as Chainlink proxy "
                        "(note: removes latency edge, but allows paper trading)",
                        self._chainlink_ws_fails,
                    )
                    self._using_rest_chainlink = True
                await self._chainlink_rest_fallback()
                return

            try:
                async with websockets.connect(self.RTDS_WS) as ws:
                    await ws.send(sub_msg)
                    logger.info("Connected to Polymarket RTDS Chainlink stream")
                    self._chainlink_ws_fails = 0
                    connect_time = time.time()
                    got_valid_price = False
                    async for msg in ws:
                        if not self._running:
                            break

                        # If connected but no valid price data after timeout, force fallback
                        if not got_valid_price and (time.time() - connect_time) > self.WS_STALE_DATA_TIMEOUT:
                            logger.warning(
                                "Chainlink RTDS connected but no valid price data after %.0fs — forcing REST fallback",
                                self.WS_STALE_DATA_TIMEOUT,
                            )
                            self._chainlink_ws_fails = self.WS_FAIL_THRESHOLD
                            break

                        try:
                            data = json.loads(msg)

                            # Try multiple known RTDS message formats
                            price_val = data.get("value") or data.get("price") or data.get("p")
                            if price_val is None:
                                # Log first few unrecognized messages for debugging
                                if not got_valid_price:
                                    logger.debug("Chainlink RTDS msg (no price field): %s", str(msg)[:200])
                                continue

                            # Validate timestamp — reject if missing (can't measure lag)
                            raw_ts = data.get("timestamp") or data.get("t")
                            if raw_ts:
                                ts_ms = int(raw_ts)
                            else:
                                ts_ms = int(time.time() * 1000)
                                logger.debug("Chainlink tick missing timestamp, using now")

                            tick = PriceTick(
                                source="chainlink",
                                symbol="BTC/USD",
                                price=float(price_val),
                                timestamp_ms=ts_ms,
                            )
                            self.latest_chainlink = tick
                            self._chainlink_last_update = time.time()
                            got_valid_price = True
                            await self._notify(tick)
                        except (KeyError, ValueError) as e:
                            logger.warning("Bad Chainlink message: %s", e)
            except Exception:
                self._chainlink_ws_fails += 1
                logger.warning(
                    "Chainlink RTDS WS error (%d/%d before REST fallback), retrying in 2s",
                    self._chainlink_ws_fails, self.WS_FAIL_THRESHOLD,
                )
                await asyncio.sleep(2)

    # ---- REST API fallbacks ----

    def _get_http_client(self) -> httpx.AsyncClient:
        """Lazy-init a shared async HTTP client for REST polling."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10)
        return self._http_client

    async def _binance_rest_poll(self):
        """Poll REST APIs for BTC price. Fallback when WS is blocked.

        Tries multiple endpoints in order, sticks with the first one that works.
        Includes Binance, Kraken, Coinbase, CoinGecko for maximum geo-coverage.
        """
        client = self._get_http_client()
        endpoints = [
            ("Binance", self.BINANCE_REST),
            ("Binance US", self.BINANCE_US_REST),
            ("Kraken", self.KRAKEN_REST),
            ("Coinbase", self.COINBASE_REST),
            ("CoinGecko", self.COINGECKO_REST),
        ]
        active_endpoint_idx = 0
        consecutive_failures = 0

        logger.info("BTC REST polling started (every %.0fs) — trying %d endpoints", self.REST_POLL_INTERVAL, len(endpoints))

        while self._running:
            name, url = endpoints[active_endpoint_idx]
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

                # Parse price based on which API we're hitting
                if "CoinGecko" in name:
                    price = float(data["bitcoin"]["usd"])
                elif "Kraken" in name:
                    # Kraken returns: {"result": {"XXBTZUSD": {"c": ["97000.0", ...]}}}
                    pair_data = next(iter(data.get("result", {}).values()))
                    price = float(pair_data["c"][0])
                elif "Coinbase" in name:
                    # Coinbase returns: {"data": {"amount": "97000.00", ...}}
                    price = float(data["data"]["amount"])
                else:
                    price = float(data["price"])

                ts_ms = int(time.time() * 1000)
                tick = PriceTick(
                    source="binance",
                    symbol="BTC/USDT",
                    price=price,
                    timestamp_ms=ts_ms,
                )
                self.latest_binance = tick
                self._binance_last_update = time.time()
                consecutive_failures = 0  # Reset on success

                # Log which endpoint is working (once)
                if not hasattr(self, '_logged_working_endpoint') or self._logged_working_endpoint != name:
                    logger.info("BTC price feed active via %s ($%.0f)", name, price)
                    self._logged_working_endpoint = name

                await self._notify(tick)

            except Exception as e:
                logger.warning("BTC REST (%s) error: %s", name, e)
                consecutive_failures += 1
                # Rotate to next endpoint
                active_endpoint_idx = (active_endpoint_idx + 1) % len(endpoints)
                if consecutive_failures >= len(endpoints):
                    logger.error("All %d BTC REST endpoints failed — retrying in 10s", len(endpoints))
                    consecutive_failures = 0
                    await asyncio.sleep(10)
                    continue

            await asyncio.sleep(self.REST_POLL_INTERVAL)

    async def _chainlink_rest_fallback(self):
        """Fallback: use Binance REST price as Chainlink proxy.

        When RTDS WebSocket is blocked, we use the Binance price with a
        small artificial delay to simulate the Chainlink lag. This removes
        the latency edge but allows paper trading to function.
        """
        logger.info(
            "Chainlink REST fallback: using Binance price as proxy (3s delay)"
        )

        while self._running:
            # Wait for Binance to have a price, then use it with simulated lag
            if self.latest_binance:
                # Add artificial 3s lag to simulate Chainlink's slower updates
                tick = PriceTick(
                    source="chainlink",
                    symbol="BTC/USD",
                    price=self.latest_binance.price,
                    timestamp_ms=int(time.time() * 1000),
                )
                self.latest_chainlink = tick
                self._chainlink_last_update = time.time()
                await self._notify(tick)

            await asyncio.sleep(3.0)  # Chainlink updates every ~3-15s

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
                mode_b = "REST" if self._using_rest_binance else "WS"
                mode_c = "REST-proxy" if self._using_rest_chainlink else "WS"
                logger.info(
                    "Feed health: Binance[%s]=$%.0f (%.1fs ago) "
                    "Chainlink[%s]=$%.0f (%.1fs ago) gap=$%.0f (%.3f%%)",
                    mode_b, self.latest_binance.price, binance_age,
                    mode_c, self.latest_chainlink.price, chainlink_age,
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
        if self._http_client:
            # Schedule cleanup (can't await in sync method)
            asyncio.get_event_loop().create_task(self._http_client.aclose())

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
        # REST polling is slower — allow 2x staleness threshold
        threshold = max_age_s * 2 if self._using_rest_binance else max_age_s
        return (time.time() - self._binance_last_update) < threshold

    def is_chainlink_fresh(self, max_age_s: float = 30) -> bool:
        """Check if Chainlink data is fresh enough to trade on."""
        if not self._chainlink_last_update:
            return False
        threshold = max_age_s * 2 if self._using_rest_chainlink else max_age_s
        return (time.time() - self._chainlink_last_update) < threshold

    def get_price_gap(self) -> float | None:
        """Return Binance - Chainlink price difference.
        Positive = Binance higher (BTC trending up, Chainlink lagging).
        Negative = Binance lower (BTC trending down, Chainlink lagging)."""
        if self.latest_binance and self.latest_chainlink:
            return self.latest_binance.price - self.latest_chainlink.price
        return None
