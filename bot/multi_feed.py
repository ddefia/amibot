"""Multi-asset price feeds — adaptive WebSocket + REST for any environment.

Automatically detects the fastest available transport:
1. Tries Binance global WebSocket (sub-10ms on VPS, blocked on some servers)
2. Falls back to parallel REST polling at 500ms (Kraken + Binance US)

On a London VPS: Binance WS delivers ~240ms price data, Polymarket CLOB at ~1-5ms.
On a blocked server: parallel REST at 500ms intervals.

The feed auto-detects within 5 seconds — no manual configuration needed.
"""

import asyncio
import json
import time
import logging
from dataclasses import dataclass

import httpx
import websockets

logger = logging.getLogger(__name__)

# Binance global WS URLs (work from non-US IPs)
BINANCE_WS = {
    "btc": "wss://stream.binance.com:9443/ws/btcusdt@trade",
    "eth": "wss://stream.binance.com:9443/ws/ethusdt@trade",
    "sol": "wss://stream.binance.com:9443/ws/solusdt@trade",
    "xrp": "wss://stream.binance.com:9443/ws/xrpusdt@trade",
}

ASSETS = {
    "btc": {
        "kraken_pair": "XBTUSD",
        "binance_us_symbol": "BTCUSDT",
        "coinbase_pair": "BTC-USD",
        "coingecko_id": "bitcoin",
    },
    "eth": {
        "kraken_pair": "ETHUSD",
        "binance_us_symbol": "ETHUSDT",
        "coinbase_pair": "ETH-USD",
        "coingecko_id": "ethereum",
    },
    "sol": {
        "kraken_pair": "SOLUSD",
        "binance_us_symbol": "SOLUSDT",
        "coinbase_pair": "SOL-USD",
        "coingecko_id": "solana",
    },
    "xrp": {
        "kraken_pair": "XRPUSD",
        "binance_us_symbol": "XRPUSDT",
        "coinbase_pair": "XRP-USD",
        "coingecko_id": "ripple",
    },
}

FAST_REST = [
    ("Kraken", "https://api.kraken.com/0/public/Ticker?pair={kraken_pair}"),
    ("BinanceUS", "https://api.binance.us/api/v3/ticker/price?symbol={binance_us_symbol}"),
]

SLOW_REST = [
    ("Coinbase", "https://api.coinbase.com/v2/prices/{coinbase_pair}/spot"),
    ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd"),
]


@dataclass
class AssetPrice:
    """Current price state for a single asset."""
    asset: str
    price: float
    timestamp_ms: int
    source: str
    interval_start_price: float | None = None


class MultiFeed:
    """Adaptive multi-asset price feeds.

    Auto-detects environment:
    - VPS with WebSocket access → Binance global WS (sub-10ms updates)
    - Blocked environment → parallel REST polling (500ms updates)

    Falls back gracefully within 5 seconds, no manual config needed.
    """

    REST_POLL_INTERVAL = 0.5  # 500ms for REST mode
    WS_CONNECT_TIMEOUT = 5.0  # How long to try WS before giving up
    WS_STALE_TIMEOUT = 10.0   # No data in 10s → restart WS

    def __init__(self, assets: list[str] | None = None):
        self.tracked_assets = assets or list(ASSETS.keys())
        self.prices: dict[str, AssetPrice] = {}
        self._running = False
        self._http_client: httpx.AsyncClient | None = None
        self._callbacks: list = []

        # Transport state
        self._ws_available: dict[str, bool] = {}  # asset → WS works?
        self._ws_tested: dict[str, bool] = {}      # asset → tested yet?
        self._rest_endpoint_healthy: dict[str, bool] = {
            name: True for name, _ in FAST_REST
        }
        self._poll_count = 0
        self._mode = "detecting"  # "detecting", "websocket", "rest", "mixed"

    def on_price(self, callback):
        self._callbacks.append(callback)

    async def _notify(self, ap: AssetPrice):
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(ap)
                else:
                    cb(ap)
            except Exception:
                logger.exception("Price callback error for %s", ap.asset)

    def get_price(self, asset: str) -> float | None:
        ap = self.prices.get(asset)
        return ap.price if ap else None

    def get_asset_price(self, asset: str) -> AssetPrice | None:
        return self.prices.get(asset)

    def capture_interval_start(self, asset: str):
        ap = self.prices.get(asset)
        if ap:
            ap.interval_start_price = ap.price
            logger.debug("Interval start %s: $%.2f", asset, ap.price)

    def capture_all_interval_starts(self):
        for asset in self.tracked_assets:
            self.capture_interval_start(asset)

    def is_fresh(self, asset: str, max_age_s: float = 10) -> bool:
        ap = self.prices.get(asset)
        if not ap:
            return False
        return (time.time() - ap.timestamp_ms / 1000) < max_age_s

    async def start(self):
        """Start feeds — auto-detect WebSocket availability, then run."""
        self._running = True
        self._http_client = httpx.AsyncClient(
            timeout=3.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

        logger.info(
            "MultiFeed starting for %s — detecting transport...",
            ", ".join(a.upper() for a in self.tracked_assets),
        )

        # Test WebSocket on first asset to detect environment
        test_asset = self.tracked_assets[0]
        ws_works = await self._test_websocket(test_asset)

        if ws_works:
            self._mode = "websocket"
            logger.info(
                "WebSocket available — using Binance WS (sub-10ms updates)"
            )
            # Start WS feeds for all assets + REST as backup
            tasks = []
            for asset in self.tracked_assets:
                tasks.append(self._ws_feed(asset))
            tasks.append(self._rest_backup_loop())
            tasks.append(self._health_monitor())
            await asyncio.gather(*tasks)
        else:
            self._mode = "rest"
            logger.info(
                "WebSocket blocked — using parallel REST (%dms updates)",
                int(self.REST_POLL_INTERVAL * 1000),
            )
            tasks = [
                self._parallel_rest_loop(),
                self._health_monitor(),
            ]
            await asyncio.gather(*tasks)

    def stop(self):
        self._running = False

    # ---- WebSocket Transport ----

    async def _test_websocket(self, asset: str) -> bool:
        """Quick test: can we connect to Binance WS and get data?"""
        ws_url = BINANCE_WS.get(asset)
        if not ws_url:
            return False

        try:
            async with websockets.connect(
                ws_url, close_timeout=2, open_timeout=self.WS_CONNECT_TIMEOUT,
            ) as ws:
                msg = await asyncio.wait_for(ws.recv(), timeout=self.WS_CONNECT_TIMEOUT)
                data = json.loads(msg)
                if "p" in data:
                    price = float(data["p"])
                    logger.info(
                        "WS test OK: %s = $%.2f from Binance global",
                        asset.upper(), price,
                    )
                    return True
                return False
        except Exception as e:
            logger.info("WS test failed for %s: %s — will use REST", asset.upper(), e)
            return False

    async def _ws_feed(self, asset: str):
        """Run WebSocket feed for a single asset with auto-reconnect."""
        ws_url = BINANCE_WS.get(asset)
        if not ws_url:
            return

        consecutive_fails = 0
        max_fails = 5

        while self._running and consecutive_fails < max_fails:
            try:
                async with websockets.connect(
                    ws_url, close_timeout=2, open_timeout=10,
                    ping_interval=20, ping_timeout=10,
                ) as ws:
                    logger.info("Binance WS connected: %s", asset.upper())
                    consecutive_fails = 0
                    last_msg_time = time.time()

                    async for msg in ws:
                        if not self._running:
                            break

                        try:
                            data = json.loads(msg)
                            price = float(data["p"])
                            ts_ms = int(data["T"])

                            existing = self.prices.get(asset)
                            ap = AssetPrice(
                                asset=asset,
                                price=price,
                                timestamp_ms=ts_ms,
                                source="binance_ws",
                                interval_start_price=(
                                    existing.interval_start_price if existing else None
                                ),
                            )
                            self.prices[asset] = ap
                            await self._notify(ap)
                            last_msg_time = time.time()
                        except (KeyError, ValueError):
                            pass

                        # Stale data detection
                        if (time.time() - last_msg_time) > self.WS_STALE_TIMEOUT:
                            logger.warning(
                                "%s WS stale (no data in %.0fs) — reconnecting",
                                asset.upper(), self.WS_STALE_TIMEOUT,
                            )
                            break

            except Exception as e:
                consecutive_fails += 1
                logger.warning(
                    "%s WS error (fail %d/%d): %s",
                    asset.upper(), consecutive_fails, max_fails, e,
                )
                if consecutive_fails < max_fails:
                    await asyncio.sleep(min(2 ** consecutive_fails, 30))

        if consecutive_fails >= max_fails:
            logger.warning(
                "%s WS failed %d times — switching to REST-only for this asset",
                asset.upper(), max_fails,
            )
            self._ws_available[asset] = False

    async def _rest_backup_loop(self):
        """In WS mode, poll REST every 5s as backup for stale WS data."""
        while self._running:
            await asyncio.sleep(5)
            for asset in self.tracked_assets:
                if not self.is_fresh(asset, max_age_s=10):
                    # WS stale for this asset — get REST price
                    await self._rest_fetch_one(asset)

    # ---- REST Transport ----

    async def _parallel_rest_loop(self):
        """Poll ALL assets via REST in parallel every 500ms."""
        while self._running:
            start = time.time()
            self._poll_count += 1

            tasks = []
            task_meta = []

            for asset in self.tracked_assets:
                config = ASSETS[asset]
                for name, url_template in FAST_REST:
                    if not self._rest_endpoint_healthy.get(name, True):
                        if self._poll_count % 20 != 0:
                            continue
                    try:
                        url = url_template.format(**config)
                        tasks.append(self._fetch_rest_price(asset, name, url, config))
                        task_meta.append((asset, name))
                    except KeyError:
                        continue

                # Periodic slow endpoint check
                if self._poll_count % 10 == 0:
                    for name, url_template in SLOW_REST[:1]:
                        try:
                            url = url_template.format(**config)
                            tasks.append(self._fetch_rest_price(asset, name, url, config))
                            task_meta.append((asset, name))
                        except KeyError:
                            continue

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for (asset, name), result in zip(task_meta, results):
                    if isinstance(result, Exception):
                        self._rest_endpoint_healthy[name] = False
                    elif result is not None:
                        self._rest_endpoint_healthy[name] = True

            elapsed = time.time() - start
            sleep_time = max(0, self.REST_POLL_INTERVAL - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    async def _rest_fetch_one(self, asset: str):
        """Fetch price for a single asset from the fastest available REST endpoint."""
        config = ASSETS[asset]
        for name, url_template in FAST_REST:
            try:
                url = url_template.format(**config)
                result = await self._fetch_rest_price(asset, name, url, config)
                if result is not None:
                    return
            except Exception:
                continue

    async def _fetch_rest_price(
        self, asset: str, name: str, url: str, config: dict,
    ) -> float | None:
        """Fetch a single price from a REST endpoint."""
        try:
            resp = await self._http_client.get(url)
            resp.raise_for_status()
            data = resp.json()

            if "Kraken" in name:
                pair_data = next(iter(data.get("result", {}).values()))
                price = float(pair_data["c"][0])
            elif "BinanceUS" in name:
                price = float(data["price"])
            elif "Coinbase" in name:
                price = float(data["data"]["amount"])
            elif "CoinGecko" in name:
                cg_id = config["coingecko_id"]
                price = float(data[cg_id]["usd"])
            else:
                return None

            ts_ms = int(time.time() * 1000)
            existing = self.prices.get(asset)
            if existing and existing.timestamp_ms >= ts_ms:
                return price

            ap = AssetPrice(
                asset=asset,
                price=price,
                timestamp_ms=ts_ms,
                source=f"{name.lower()}_rest",
                interval_start_price=(
                    existing.interval_start_price if existing else None
                ),
            )
            self.prices[asset] = ap
            await self._notify(ap)
            return price

        except Exception:
            if self._poll_count % 20 == 0:
                logger.debug("%s %s REST failed", asset.upper(), name)
            return None

    # ---- Health Monitor ----

    async def _health_monitor(self):
        """Log feed status every 30s."""
        while self._running:
            await asyncio.sleep(30)
            active = []
            stale = []
            for asset in self.tracked_assets:
                if self.is_fresh(asset, 5):
                    ap = self.prices[asset]
                    active.append(f"{asset.upper()}=${ap.price:.2f}({ap.source})")
                else:
                    stale.append(asset.upper())

            if active:
                logger.info("Feeds [%s]: %s", self._mode, ", ".join(active))
            if stale:
                logger.warning("Feeds stale: %s", ", ".join(stale))
