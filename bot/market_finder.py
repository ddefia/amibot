"""Finds active BTC Up/Down markets on Polymarket via the Gamma API.

These markets don't appear in generic /events or /markets queries.
They must be fetched by constructing the exact slug for the current
date/time. Discovered slug patterns:

  Daily:  bitcoin-up-or-down-on-february-14
  Hourly: bitcoin-up-or-down-february-14-3am-et

Priority order: hourly > daily (more trading opportunities, higher edge).
"""

import json
import time
import logging
from datetime import datetime, timezone, timedelta
import httpx

logger = logging.getLogger(__name__)

# Eastern Time offset (UTC-5, or UTC-4 during DST)
ET = timezone(timedelta(hours=-5))


def _month_name(dt: datetime) -> str:
    """Return lowercase full month name."""
    return dt.strftime("%B").lower()


def _build_hourly_slug(dt_et: datetime) -> str:
    """Build slug for the hourly market at the given ET hour.

    Example: 'bitcoin-up-or-down-february-14-3am-et'
    """
    month = _month_name(dt_et)
    day = dt_et.day
    h = dt_et.hour
    ampm = "am" if h < 12 else "pm"
    h12 = h % 12 if h % 12 != 0 else 12
    return f"bitcoin-up-or-down-{month}-{day}-{h12}{ampm}-et"


def _build_daily_slug(dt_et: datetime) -> str:
    """Build slug for the daily market.

    Example: 'bitcoin-up-or-down-on-february-14'
    """
    month = _month_name(dt_et)
    day = dt_et.day
    return f"bitcoin-up-or-down-on-{month}-{day}"


class MarketFinder:
    """Discovers and tracks active BTC Up/Down markets via slug construction."""

    def __init__(self, gamma_host: str, prefer_15min: bool = True):
        self.gamma_host = gamma_host
        self.prefer_15min = prefer_15min
        self.client = httpx.Client(timeout=15)
        self._cache: dict | None = None
        self._cache_slug: str = ""
        self._cache_ts: float = 0
        self._cache_ttl: float = 30  # seconds

    def _fetch_event_by_slug(self, slug: str) -> dict | None:
        """Fetch a single event by exact slug. Returns the first market or None."""
        try:
            resp = self.client.get(
                f"{self.gamma_host}/events",
                params={"slug": slug},
            )
            resp.raise_for_status()
            events = resp.json()
            if events and events[0].get("markets"):
                return events[0]["markets"][0]
        except Exception as e:
            logger.debug("Slug %s not found: %s", slug, e)
        return None

    def get_best_market(self) -> dict | None:
        """Get the best active BTC Up/Down market for right now.

        Tries hourly first (current hour, then next hour for early entry),
        then falls back to the daily market.
        """
        now_utc = datetime.now(timezone.utc)
        now_et = now_utc.astimezone(ET)

        # Check cache
        hourly_slug = _build_hourly_slug(now_et)
        if (self._cache
                and self._cache_slug == hourly_slug
                and (time.time() - self._cache_ts) < self._cache_ttl):
            return self._cache

        # Try current hourly market
        market = self._fetch_event_by_slug(hourly_slug)
        if market:
            active = market.get("active", False)
            closed = market.get("closed", True)
            if active and not closed:
                market["_interval_duration"] = 3600
                self._cache = market
                self._cache_slug = hourly_slug
                self._cache_ts = time.time()
                logger.info("Found hourly market: %s", market.get("question", ""))
                return market

        # Try daily market
        daily_slug = _build_daily_slug(now_et)
        market = self._fetch_event_by_slug(daily_slug)
        if market:
            active = market.get("active", False)
            closed = market.get("closed", True)
            if active and not closed:
                # Daily markets: noon-to-noon ET = 86400s
                market["_interval_duration"] = 86400
                self._cache = market
                self._cache_slug = daily_slug
                self._cache_ts = time.time()
                logger.info("Found daily market: %s", market.get("question", ""))
                return market

        # Try tomorrow's daily (in case today's has closed)
        tomorrow_et = now_et + timedelta(days=1)
        tomorrow_slug = _build_daily_slug(tomorrow_et)
        market = self._fetch_event_by_slug(tomorrow_slug)
        if market:
            active = market.get("active", False)
            closed = market.get("closed", True)
            if active and not closed:
                market["_interval_duration"] = 86400
                self._cache = market
                self._cache_slug = tomorrow_slug
                self._cache_ts = time.time()
                logger.info("Found tomorrow daily market: %s", market.get("question", ""))
                return market

        logger.warning("No active BTC Up/Down market found for %s", now_et.strftime("%b %d %I%p ET"))
        return None

    def get_current_market(self) -> dict | None:
        """Alias for get_best_market() — backward compatible."""
        return self.get_best_market()

    def get_active_markets(self) -> list[dict]:
        """Backward compat: return best market as a list."""
        m = self.get_best_market()
        return [m] if m else []

    def parse_market(self, market: dict) -> dict:
        """Extract trading-relevant fields from a market response.

        Handles both "Up"/"Down" and "Yes"/"No" outcome labels.
        Note: Gamma API returns these fields as JSON strings, not lists.
        """
        tokens = market.get("clobTokenIds", [])
        prices = market.get("outcomePrices", [])
        outcomes = market.get("outcomes", [])

        # Gamma API returns JSON-encoded strings — parse them
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
        if isinstance(prices, str):
            prices = json.loads(prices)
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)

        parsed = {
            "condition_id": market.get("conditionId"),
            "slug": market.get("slug", ""),
            "question": market.get("question", ""),
            "interval_duration": market.get("_interval_duration", 3600),
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
