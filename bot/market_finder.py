from __future__ import annotations

"""Finds active BTC Up/Down markets on Polymarket via the Gamma API.

These markets use epoch-based slugs that encode the interval start time:
  15-min: btc-updown-15m-{epoch}    (PREFERRED — cross-trader net +$305K)
  5-min:  btc-updown-5m-{epoch}
  4-hour: btc-updown-4h-{epoch}
  Hourly: bitcoin-up-or-down-{month}-{day}-{hour}{ampm}-et
  Daily:  bitcoin-up-or-down-on-{month}-{day}

Priority: 15-min > hourly > daily (per cross-trader performance data).
"""

import json
import time
import logging
from datetime import datetime, timezone, timedelta
import httpx

logger = logging.getLogger(__name__)

# Eastern Time offset (UTC-5, or UTC-4 during DST)
ET = timezone(timedelta(hours=-5))


def _current_15m_epoch() -> int:
    """Get the Unix epoch for the start of the current 15-min window."""
    now = int(time.time())
    return now - (now % 900)


def _current_5m_epoch() -> int:
    """Get the Unix epoch for the start of the current 5-min window."""
    now = int(time.time())
    return now - (now % 300)


def _build_15m_slug(epoch: int) -> str:
    """Build slug for a 15-min market. Example: 'btc-updown-15m-1771126200'"""
    return f"btc-updown-15m-{epoch}"


def _build_5m_slug(epoch: int) -> str:
    """Build slug for a 5-min market. Example: 'btc-updown-5m-1771126200'"""
    return f"btc-updown-5m-{epoch}"


def _build_hourly_slug(dt_et: datetime) -> str:
    """Build slug for the hourly market.
    Example: 'bitcoin-up-or-down-february-14-3am-et'
    """
    month = dt_et.strftime("%B").lower()
    day = dt_et.day
    h = dt_et.hour
    ampm = "am" if h < 12 else "pm"
    h12 = h % 12 if h % 12 != 0 else 12
    return f"bitcoin-up-or-down-{month}-{day}-{h12}{ampm}-et"


def _build_daily_slug(dt_et: datetime) -> str:
    """Build slug for the daily market.
    Example: 'bitcoin-up-or-down-on-february-14'
    """
    month = dt_et.strftime("%B").lower()
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
        self._cache_ttl: float = 10  # seconds

    def _fetch_market_by_slug(self, slug: str) -> dict | None:
        """Fetch a market by slug. Tries /events first, then /markets."""
        try:
            resp = self.client.get(
                f"{self.gamma_host}/events",
                params={"slug": slug},
            )
            resp.raise_for_status()
            events = resp.json()
            if events and events[0].get("markets"):
                return events[0]["markets"][0]
        except Exception:
            pass

        # Fallback: try /markets endpoint directly
        try:
            resp = self.client.get(
                f"{self.gamma_host}/markets",
                params={"slug": slug},
            )
            resp.raise_for_status()
            markets = resp.json()
            if markets:
                return markets[0]
        except Exception:
            pass

        return None

    def get_best_market(self) -> dict | None:
        """Get the best active BTC Up/Down market for right now.

        Priority: 15-min (preferred by strategy) > hourly > daily.
        """
        now_utc = datetime.now(timezone.utc)
        now_et = now_utc.astimezone(ET)

        # Build current 15-min slug
        epoch_15m = _current_15m_epoch()
        slug_15m = _build_15m_slug(epoch_15m)

        # Check cache
        if (self._cache
                and self._cache_slug == slug_15m
                and (time.time() - self._cache_ts) < self._cache_ttl):
            return self._cache

        # ---- TRY 15-MIN MARKET (preferred) ----
        if self.prefer_15min:
            market = self._fetch_market_by_slug(slug_15m)
            if market and market.get("active") and not market.get("closed"):
                market["_interval_duration"] = 900
                self._cache = market
                self._cache_slug = slug_15m
                self._cache_ts = time.time()
                logger.info("Found 15-min market: %s", market.get("question", ""))
                return market

        # ---- TRY HOURLY MARKET ----
        hourly_slug = _build_hourly_slug(now_et)
        market = self._fetch_market_by_slug(hourly_slug)
        if market and market.get("active") and not market.get("closed"):
            market["_interval_duration"] = 3600
            self._cache = market
            self._cache_slug = hourly_slug
            self._cache_ts = time.time()
            logger.info("Found hourly market: %s", market.get("question", ""))
            return market

        # ---- TRY DAILY MARKET ----
        daily_slug = _build_daily_slug(now_et)
        market = self._fetch_market_by_slug(daily_slug)
        if market and market.get("active") and not market.get("closed"):
            market["_interval_duration"] = 86400
            self._cache = market
            self._cache_slug = daily_slug
            self._cache_ts = time.time()
            logger.info("Found daily market: %s", market.get("question", ""))
            return market

        # ---- TRY TOMORROW DAILY ----
        tomorrow_et = now_et + timedelta(days=1)
        tomorrow_slug = _build_daily_slug(tomorrow_et)
        market = self._fetch_market_by_slug(tomorrow_slug)
        if market and market.get("active") and not market.get("closed"):
            market["_interval_duration"] = 86400
            self._cache = market
            self._cache_slug = tomorrow_slug
            self._cache_ts = time.time()
            logger.info("Found tomorrow daily market: %s", market.get("question", ""))
            return market

        logger.warning("No active BTC Up/Down market found for %s",
                        now_et.strftime("%b %d %I:%M%p ET"))
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
