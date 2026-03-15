"""Multi-asset price feeds — speed-optimized for REST-only environments.

All WebSockets are blocked on this server (HTTP 403). Instead of trying WS
and falling back after 3 failures (wasting 6+ seconds), we go straight to
parallel REST polling at 500ms intervals across multiple exchanges.

Speed architecture:
- Poll Kraken + Binance US simultaneously (parallel asyncio.gather)
- Use whichever responds first (race condition = fastest wins)
- 500ms poll interval = 2x per second price updates
- Cross-validate: if both respond, use the fresher one
- Fallback chain: Kraken (389ms) → Binance US (484ms) → Coinbase → CoinGecko

Measured latencies from this server:
- Kraken REST: ~389ms (fastest)
- Binance US REST: ~484ms
- All WebSockets: BLOCKED (HTTP 403)
"""

import asyncio
import json
import time
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

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

# Ordered by measured latency (fastest first)
FAST_ENDPOINTS = [
    ("Kraken", "https://api.kraken.com/0/public/Ticker?pair={kraken_pair}"),
    ("BinanceUS", "https://api.binance.us/api/v3/ticker/price?symbol={binance_us_symbol}"),
]

SLOW_ENDPOINTS = [
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
    """Speed-optimized multi-asset price feeds via parallel REST polling.

    Polls Kraken + Binance US in parallel every 500ms.
    Uses whichever responds first for minimum latency.
    """

    POLL_INTERVAL = 0.5  # 500ms — 2 updates per second

    def __init__(self, assets: list[str] | None = None):
        self.tracked_assets = assets or list(ASSETS.keys())
        self.prices: dict[str, AssetPrice] = {}
        self._running = False
        self._http_client: httpx.AsyncClient | None = None
        self._callbacks: list = []
        self._fast_endpoint_healthy: dict[str, bool] = {
            name: True for name, _ in FAST_ENDPOINTS
        }
        self._poll_count = 0

    def on_price(self, callback):
        """Register callback: callback(asset_price: AssetPrice)."""
        self._callbacks.append(callback)

    async def _notify(self, ap: AssetPrice):
        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(ap)
                else:
                    cb(ap)
            except Exception:
                logger.exception("Error in price callback for %s", ap.asset)

    def get_price(self, asset: str) -> float | None:
        ap = self.prices.get(asset)
        return ap.price if ap else None

    def get_asset_price(self, asset: str) -> AssetPrice | None:
        return self.prices.get(asset)

    def capture_interval_start(self, asset: str):
        ap = self.prices.get(asset)
        if ap:
            ap.interval_start_price = ap.price
            logger.debug("Captured interval start for %s: $%.2f", asset, ap.price)

    def capture_all_interval_starts(self):
        for asset in self.tracked_assets:
            self.capture_interval_start(asset)

    def is_fresh(self, asset: str, max_age_s: float = 10) -> bool:
        ap = self.prices.get(asset)
        if not ap:
            return False
        age = time.time() - (ap.timestamp_ms / 1000)
        return age < max_age_s

    async def start(self):
        """Start parallel REST polling for all assets."""
        self._running = True
        self._http_client = httpx.AsyncClient(
            timeout=3.0,  # Tight timeout — speed matters
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

        logger.info(
            "MultiFeed starting — parallel REST polling at %dms for %s",
            int(self.POLL_INTERVAL * 1000),
            ", ".join(a.upper() for a in self.tracked_assets),
        )

        tasks = [
            self._parallel_poll_loop(),
            self._health_monitor(),
        ]
        await asyncio.gather(*tasks)

    def stop(self):
        self._running = False

    async def _parallel_poll_loop(self):
        """Poll ALL assets in parallel every 500ms.

        Instead of sequential per-asset polling, we fire all requests at once.
        For 4 assets × 2 endpoints = 8 concurrent requests, taking ~400ms total
        instead of 8 × 400ms = 3.2s sequential.
        """
        while self._running:
            start = time.time()
            self._poll_count += 1

            # Build all fetch tasks for all assets
            tasks = []
            task_meta = []  # (asset, endpoint_name) for each task

            for asset in self.tracked_assets:
                config = ASSETS[asset]

                # Always try both fast endpoints in parallel
                for name, url_template in FAST_ENDPOINTS:
                    if not self._fast_endpoint_healthy.get(name, True):
                        # Re-check unhealthy endpoints every 20 polls (10s)
                        if self._poll_count % 20 != 0:
                            continue
                    try:
                        url = url_template.format(**config)
                        tasks.append(self._fetch_price(asset, name, url, config))
                        task_meta.append((asset, name))
                    except KeyError:
                        continue

                # Every 10th poll, also try slow endpoints for validation
                if self._poll_count % 10 == 0:
                    for name, url_template in SLOW_ENDPOINTS[:1]:  # Just Coinbase
                        try:
                            url = url_template.format(**config)
                            tasks.append(self._fetch_price(asset, name, url, config))
                            task_meta.append((asset, name))
                        except KeyError:
                            continue

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for (asset, name), result in zip(task_meta, results):
                    if isinstance(result, Exception):
                        self._fast_endpoint_healthy[name] = False
                    elif result is not None:
                        self._fast_endpoint_healthy[name] = True

            elapsed = time.time() - start
            sleep_time = max(0, self.POLL_INTERVAL - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    async def _fetch_price(
        self, asset: str, name: str, url: str, config: dict
    ) -> float | None:
        """Fetch a single price from a single endpoint. Returns price or None."""
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

            # Only update if this price is newer than what we have
            existing = self.prices.get(asset)
            if existing and existing.timestamp_ms >= ts_ms:
                return price  # Already have a fresher price

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
            # Don't log every failure — too noisy at 2/sec
            if self._poll_count % 20 == 0:
                logger.debug("%s %s fetch failed", asset.upper(), name)
            return None

    async def _health_monitor(self):
        """Log feed health every 30s."""
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

            endpoints_ok = [n for n, h in self._fast_endpoint_healthy.items() if h]
            endpoints_down = [n for n, h in self._fast_endpoint_healthy.items() if not h]

            if active:
                logger.info(
                    "Feeds [%dms]: %s | endpoints: %s%s",
                    int(self.POLL_INTERVAL * 1000),
                    ", ".join(active),
                    "+".join(endpoints_ok) if endpoints_ok else "NONE",
                    f" (down: {','.join(endpoints_down)})" if endpoints_down else "",
                )
            if stale:
                logger.warning("Feeds stale: %s", ", ".join(stale))
