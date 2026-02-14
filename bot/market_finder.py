"""Finds active BTC Up/Down markets on Polymarket via the Gamma API.

Supports both 5-min and 15-min interval markets. Prefers 15-min because
cross-trader data shows 15-min = +$305K net vs 5-min = -$8.4K net.
"""

import re
import time
import logging
import httpx

logger = logging.getLogger(__name__)

# Patterns that appear in BTC Up/Down market questions/slugs
BTC_PATTERNS = [
    "bitcoin-up-or-down",
    "btc-updown",
    "btc-up-down",
    "bitcoin up or down",
]


def _parse_duration_from_question(question: str) -> int | None:
    """Parse interval duration from a market question string.

    Examples:
        "Bitcoin Up or Down - February 13, 6:15PM-6:30PM ET" → 900 (15-min)
        "Bitcoin Up or Down - February 13, 5PM ET" → 3600 (hourly)
        "Bitcoin Up or Down - February 13, 5:00PM-5:05PM ET" → 300 (5-min)
    """
    # Try time range: "6:15PM-6:30PM" or "6:15 PM - 6:30 PM"
    m = re.search(
        r'(\d{1,2}):(\d{2})\s*(AM|PM)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(AM|PM)',
        question, re.IGNORECASE,
    )
    if m:
        h1, m1, ap1 = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        h2, m2, ap2 = int(m.group(4)), int(m.group(5)), m.group(6).upper()
        t1 = ((h1 % 12) + (12 if ap1 == "PM" else 0)) * 60 + m1
        t2 = ((h2 % 12) + (12 if ap2 == "PM" else 0)) * 60 + m2
        if t2 > t1:
            return (t2 - t1) * 60

    # Try single time: "5PM ET" or "5 PM ET" (hourly market)
    if re.search(r'\d{1,2}\s*(AM|PM)\s+ET', question, re.IGNORECASE):
        if not re.search(r'\d{1,2}:\d{2}', question):
            return 3600

    return None


class MarketFinder:
    """Discovers and tracks active BTC Up/Down markets."""

    def __init__(self, gamma_host: str, prefer_15min: bool = True):
        self.gamma_host = gamma_host
        self.prefer_15min = prefer_15min
        self.client = httpx.Client(timeout=15)
        self._cache: list[dict] = []
        self._cache_ts: float = 0
        self._cache_ttl: float = 10  # seconds

    def get_active_markets(self) -> list[dict]:
        """Fetch all currently active BTC Up/Down markets.

        Caches results for 10 seconds to avoid spamming the API.
        """
        now = time.time()
        if self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        try:
            resp = self.client.get(
                f"{self.gamma_host}/markets",
                params={"active": "true", "closed": "false", "limit": 200},
            )
            resp.raise_for_status()
            all_markets = resp.json()
        except Exception:
            logger.exception("Failed to fetch markets from Gamma API")
            return self._cache  # Return stale cache on error

        # Filter to BTC Up/Down markets
        btc_markets = []
        for m in all_markets:
            slug = m.get("slug", "").lower()
            question = m.get("question", "").lower()
            combined = slug + " " + question

            if any(p in combined for p in BTC_PATTERNS):
                # Parse interval duration from the question
                q = m.get("question", "")
                duration = _parse_duration_from_question(q)
                m["_interval_duration"] = duration or 900  # default 15-min
                btc_markets.append(m)

        self._cache = btc_markets
        self._cache_ts = now
        logger.info("Found %d active BTC Up/Down markets", len(btc_markets))
        return btc_markets

    def get_best_market(self) -> dict | None:
        """Get the best active market to trade right now.

        Prefers 15-min intervals (cross-trader data: +$305K net).
        Falls back to 5-min only if no 15-min available.
        Returns the market with the most time remaining for entry.
        """
        markets = self.get_active_markets()
        if not markets:
            return None

        now = int(time.time())

        # Separate by interval duration
        markets_15m = [m for m in markets if m.get("_interval_duration", 900) == 900]
        markets_5m = [m for m in markets if m.get("_interval_duration", 900) == 300]
        markets_other = [m for m in markets
                         if m.get("_interval_duration", 900) not in (300, 900)]

        # Prefer 15-min, then other, then 5-min
        if self.prefer_15min:
            candidates = markets_15m or markets_other or markets_5m
        else:
            candidates = markets_5m or markets_15m or markets_other

        if not candidates:
            return None

        # If multiple candidates, return the first active one
        # (Gamma API returns them in chronological order)
        return candidates[0]

    def get_current_market(self) -> dict | None:
        """Alias for get_best_market() — backward compatible."""
        return self.get_best_market()

    def parse_market(self, market: dict) -> dict:
        """Extract trading-relevant fields from a market response.

        Handles both "Up"/"Down" and "Yes"/"No" outcome labels.
        """
        tokens = market.get("clobTokenIds", [])
        prices = market.get("outcomePrices", [])
        outcomes = market.get("outcomes", [])

        parsed = {
            "condition_id": market.get("conditionId"),
            "slug": market.get("slug", ""),
            "question": market.get("question", ""),
            "interval_duration": market.get("_interval_duration", 900),
            "tokens": {},
        }

        for i, outcome in enumerate(outcomes):
            label = str(outcome).upper().strip() if isinstance(outcome, str) else f"OUTCOME_{i}"
            # Normalize labels: "Yes" → "UP", "No" → "DOWN"
            if label in ("YES", "YES 🟢"):
                label = "UP"
            elif label in ("NO", "NO 🔴"):
                label = "DOWN"

            token_id = tokens[i] if i < len(tokens) else None
            try:
                price = float(prices[i]) if i < len(prices) else None
            except (ValueError, TypeError):
                price = None

            parsed["tokens"][label] = {
                "token_id": token_id,
                "price": price,
            }

        return parsed

    def close(self):
        self.client.close()
