"""Real-time BTC price feeds from Binance and Polymarket RTDS (Chainlink)."""

import asyncio
import json
import time
import logging
from dataclasses import dataclass

import websockets

logger = logging.getLogger(__name__)


@dataclass
class PriceTick:
    source: str  # "binance" or "chainlink"
    symbol: str
    price: float
    timestamp_ms: int


class PriceFeed:
    """Manages WebSocket connections to Binance and Polymarket RTDS for
    real-time BTC price data. The latency gap between these two feeds
    is the core edge for the latency arbitrage strategy."""

    BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
    RTDS_WS = "wss://ws-live-data.polymarket.com"

    def __init__(self):
        self.latest_binance: PriceTick | None = None
        self.latest_chainlink: PriceTick | None = None
        self._callbacks: list = []
        self._running = False

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
                        data = json.loads(msg)
                        tick = PriceTick(
                            source="binance",
                            symbol="BTC/USDT",
                            price=float(data["p"]),
                            timestamp_ms=int(data["T"]),
                        )
                        self.latest_binance = tick
                        await self._notify(tick)
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
                        data = json.loads(msg)
                        if "value" not in data:
                            continue
                        tick = PriceTick(
                            source="chainlink",
                            symbol="BTC/USD",
                            price=float(data["value"]),
                            timestamp_ms=int(data.get("timestamp", time.time() * 1000)),
                        )
                        self.latest_chainlink = tick
                        await self._notify(tick)
            except Exception:
                logger.exception("Chainlink RTDS stream error, reconnecting in 2s")
                await asyncio.sleep(2)

    async def start(self):
        """Start both price feed streams concurrently."""
        self._running = True
        await asyncio.gather(
            self._binance_stream(),
            self._chainlink_stream(),
        )

    def stop(self):
        self._running = False

    def get_price_gap(self) -> float | None:
        """Return the difference between Binance and Chainlink prices.
        Positive = Binance is higher (BTC trending up, Chainlink lagging).
        Negative = Binance is lower (BTC trending down, Chainlink lagging)."""
        if self.latest_binance and self.latest_chainlink:
            return self.latest_binance.price - self.latest_chainlink.price
        return None
