"""Finds active BTC 5-minute markets on Polymarket via the Gamma API."""

import time
import logging
import httpx

logger = logging.getLogger(__name__)


class MarketFinder:
    """Discovers and tracks active BTC 5-min Up/Down markets."""

    SLUG_PREFIX = "btc-updown-5m-"

    def __init__(self, gamma_host: str):
        self.gamma_host = gamma_host
        self.client = httpx.Client(timeout=10)

    def get_active_markets(self) -> list[dict]:
        """Fetch all currently active BTC 5-min markets."""
        resp = self.client.get(
            f"{self.gamma_host}/markets",
            params={"active": "true", "closed": "false", "limit": 100},
        )
        resp.raise_for_status()
        markets = resp.json()

        btc_5m = [
            m
            for m in markets
            if m.get("slug", "").startswith(self.SLUG_PREFIX)
        ]
        logger.info("Found %d active BTC 5-min markets", len(btc_5m))
        return btc_5m

    def get_current_market(self) -> dict | None:
        """Get the market for the current 5-minute interval."""
        now = int(time.time())
        # Round down to nearest 5-minute boundary
        interval_start = now - (now % 300)

        markets = self.get_active_markets()
        for m in markets:
            slug = m.get("slug", "")
            try:
                ts = int(slug.replace(self.SLUG_PREFIX, ""))
                if ts == interval_start:
                    return m
            except ValueError:
                continue
        return None

    def get_next_market(self) -> dict | None:
        """Get the market for the next 5-minute interval."""
        now = int(time.time())
        next_interval = now - (now % 300) + 300

        markets = self.get_active_markets()
        for m in markets:
            slug = m.get("slug", "")
            try:
                ts = int(slug.replace(self.SLUG_PREFIX, ""))
                if ts == next_interval:
                    return m
            except ValueError:
                continue
        return None

    def parse_market(self, market: dict) -> dict:
        """Extract trading-relevant fields from a market response."""
        tokens = market.get("clobTokenIds", [])
        prices = market.get("outcomePrices", [])
        outcomes = market.get("outcomes", [])

        parsed = {
            "condition_id": market.get("conditionId"),
            "slug": market.get("slug", ""),
            "question": market.get("question", ""),
            "tokens": {},
        }

        for i, outcome in enumerate(outcomes):
            label = outcome.upper() if isinstance(outcome, str) else f"OUTCOME_{i}"
            parsed["tokens"][label] = {
                "token_id": tokens[i] if i < len(tokens) else None,
                "price": float(prices[i]) if i < len(prices) else None,
            }

        return parsed

    def close(self):
        self.client.close()
