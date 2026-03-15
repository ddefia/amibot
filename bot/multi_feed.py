"""Multi-asset price feeds for crypto oracle lag strategy.

Extends the single-asset BTC feed to track ETH, SOL, XRP simultaneously.
Each asset gets its own Binance stream for sub-second price updates.
This multiplies our opportunity set by 4x — same strategy, more markets.

Guy 3 ($355K PnL) trades BTC/ETH/SOL/XRP simultaneously.
"""

import asyncio
import json
import time
import logging
from dataclasses import dataclass

import httpx
import websockets

logger = logging.getLogger(__name__)

ASSETS = {
    "btc": {
        "binance_ws": "wss://stream.binance.com:9443/ws/btcusdt@trade",
        "binance_rest": "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
        "kraken_pair": "XBTUSD",
        "coinbase_pair": "BTC-USD",
        "coingecko_id": "bitcoin",
    },
    "eth": {
        "binance_ws": "wss://stream.binance.com:9443/ws/ethusdt@trade",
        "binance_rest": "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT",
        "kraken_pair": "ETHUSD",
        "coinbase_pair": "ETH-USD",
        "coingecko_id": "ethereum",
    },
    "sol": {
        "binance_ws": "wss://stream.binance.com:9443/ws/solusdt@trade",
        "binance_rest": "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT",
        "kraken_pair": "SOLUSD",
        "coinbase_pair": "SOL-USD",
        "coingecko_id": "solana",
    },
    "xrp": {
        "binance_ws": "wss://stream.binance.com:9443/ws/xrpusdt@trade",
        "binance_rest": "https://api.binance.com/api/v3/ticker/price?symbol=XRPUSDT",
        "kraken_pair": "XRPUSD",
        "coinbase_pair": "XRP-USD",
        "coingecko_id": "ripple",
    },
}

# REST fallback endpoints (geo-resilient)
REST_ENDPOINTS = [
    ("Kraken", "https://api.kraken.com/0/public/Ticker?pair={kraken_pair}"),
    ("Coinbase", "https://api.coinbase.com/v2/prices/{coinbase_pair}/spot"),
    ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids={coingecko_id}&vs_currencies=usd"),
]


@dataclass
class AssetPrice:
    """Current price state for a single asset."""
    asset: str  # btc, eth, sol, xrp
    price: float
    timestamp_ms: int
    source: str  # "binance_ws", "kraken_rest", etc.
    interval_start_price: float | None = None  # Captured at interval boundary


class MultiFeed:
    """Manages price feeds for multiple crypto assets simultaneously.

    Each asset has its own WebSocket connection (or REST fallback).
    Provides a unified interface for the engine to query any asset's price.
    """

    WS_FAIL_THRESHOLD = 3
    REST_POLL_INTERVAL = 2.0

    def __init__(self, assets: list[str] | None = None):
        """
        Args:
            assets: List of asset keys to track. Default: all of them.
        """
        self.tracked_assets = assets or list(ASSETS.keys())
        self.prices: dict[str, AssetPrice] = {}
        self._running = False
        self._http_client: httpx.AsyncClient | None = None
        self._ws_fails: dict[str, int] = {a: 0 for a in self.tracked_assets}
        self._callbacks: list = []

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
        """Get the latest price for an asset."""
        ap = self.prices.get(asset)
        return ap.price if ap else None

    def get_asset_price(self, asset: str) -> AssetPrice | None:
        """Get full AssetPrice object."""
        return self.prices.get(asset)

    def capture_interval_start(self, asset: str):
        """Capture the current price as interval start for an asset."""
        ap = self.prices.get(asset)
        if ap:
            ap.interval_start_price = ap.price
            logger.debug("Captured interval start for %s: $%.2f", asset, ap.price)

    def capture_all_interval_starts(self):
        """Capture interval start prices for ALL tracked assets."""
        for asset in self.tracked_assets:
            self.capture_interval_start(asset)

    def is_fresh(self, asset: str, max_age_s: float = 10) -> bool:
        """Check if an asset's price data is fresh."""
        ap = self.prices.get(asset)
        if not ap:
            return False
        age = time.time() - (ap.timestamp_ms / 1000)
        return age < max_age_s

    async def start(self):
        """Start all asset price feeds concurrently."""
        self._running = True
        tasks = []
        for asset in self.tracked_assets:
            tasks.append(self._asset_feed(asset))
        tasks.append(self._health_monitor())
        await asyncio.gather(*tasks)

    def stop(self):
        self._running = False
        if self._http_client:
            asyncio.get_event_loop().create_task(self._http_client.aclose())

    async def _asset_feed(self, asset: str):
        """Run price feed for a single asset. Tries WS first, falls back to REST."""
        config = ASSETS[asset]

        while self._running:
            if self._ws_fails[asset] >= self.WS_FAIL_THRESHOLD:
                await self._rest_poll(asset, config)
                return

            try:
                async with websockets.connect(config["binance_ws"]) as ws:
                    logger.info("Connected to Binance WS for %s", asset.upper())
                    self._ws_fails[asset] = 0
                    async for msg in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(msg)
                            price = float(data["p"])
                            ts_ms = int(data["T"])
                            ap = AssetPrice(
                                asset=asset,
                                price=price,
                                timestamp_ms=ts_ms,
                                source="binance_ws",
                                interval_start_price=(
                                    self.prices[asset].interval_start_price
                                    if asset in self.prices else None
                                ),
                            )
                            self.prices[asset] = ap
                            await self._notify(ap)
                        except (KeyError, ValueError):
                            pass
            except Exception:
                self._ws_fails[asset] += 1
                if self._ws_fails[asset] >= self.WS_FAIL_THRESHOLD:
                    logger.warning(
                        "%s WS failed %d times — switching to REST",
                        asset.upper(), self._ws_fails[asset],
                    )
                else:
                    await asyncio.sleep(2)

    async def _rest_poll(self, asset: str, config: dict):
        """REST fallback for a single asset. Rotates through endpoints."""
        client = self._get_http_client()

        endpoints = []
        for name, url_template in REST_ENDPOINTS:
            try:
                url = url_template.format(**config)
                endpoints.append((name, url))
            except KeyError:
                continue

        active_idx = 0
        consecutive_failures = 0

        logger.info("%s REST polling started (every %.0fs)", asset.upper(), self.REST_POLL_INTERVAL)

        while self._running:
            name, url = endpoints[active_idx]
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()

                if "CoinGecko" in name:
                    cg_id = config["coingecko_id"]
                    price = float(data[cg_id]["usd"])
                elif "Kraken" in name:
                    pair_data = next(iter(data.get("result", {}).values()))
                    price = float(pair_data["c"][0])
                elif "Coinbase" in name:
                    price = float(data["data"]["amount"])
                else:
                    price = float(data["price"])

                ts_ms = int(time.time() * 1000)
                ap = AssetPrice(
                    asset=asset,
                    price=price,
                    timestamp_ms=ts_ms,
                    source=f"{name.lower()}_rest",
                    interval_start_price=(
                        self.prices[asset].interval_start_price
                        if asset in self.prices else None
                    ),
                )
                self.prices[asset] = ap
                consecutive_failures = 0
                await self._notify(ap)

            except Exception as e:
                logger.warning("%s REST (%s) error: %s", asset.upper(), name, e)
                consecutive_failures += 1
                active_idx = (active_idx + 1) % len(endpoints)
                if consecutive_failures >= len(endpoints):
                    logger.error("All %s REST endpoints failed — retrying in 10s", asset.upper())
                    consecutive_failures = 0
                    await asyncio.sleep(10)
                    continue

            await asyncio.sleep(self.REST_POLL_INTERVAL)

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10)
        return self._http_client

    async def _health_monitor(self):
        """Log feed health for all assets every 30s."""
        while self._running:
            await asyncio.sleep(30)
            active = []
            stale = []
            for asset in self.tracked_assets:
                if self.is_fresh(asset, 15):
                    ap = self.prices[asset]
                    active.append(f"{asset.upper()}=${ap.price:.2f}")
                else:
                    stale.append(asset.upper())

            if active:
                logger.info("Feeds OK: %s", ", ".join(active))
            if stale:
                logger.warning("Feeds stale: %s", ", ".join(stale))
