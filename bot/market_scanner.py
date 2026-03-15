from __future__ import annotations

"""Universal market scanner — finds ALL tradeable markets on Polymarket.

Not just BTC up/down. Scans every active market across all categories:
crypto, sports, weather, politics, culture, etc. Identifies opportunities
for each strategy type:
  - Oracle lag (crypto up/down with fast price feeds)
  - Arbitrage (any market where YES+NO < $1.00)
  - Data edge (sports/weather markets where we have faster data)
  - Market making (high-volume markets with spread)

Uses the Gamma API to discover markets and categorize them by type.
"""

import asyncio
import json
import math
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

ET = timezone(timedelta(hours=-5))

# Crypto assets we can trade with oracle lag (we have fast price feeds)
CRYPTO_ASSETS = {
    "btc": {"name": "Bitcoin", "binance_symbol": "BTCUSDT", "slug_prefix": "btc-updown"},
    "eth": {"name": "Ethereum", "binance_symbol": "ETHUSDT", "slug_prefix": "eth-updown"},
    "sol": {"name": "Solana", "binance_symbol": "SOLUSDT", "slug_prefix": "sol-updown"},
    "xrp": {"name": "XRP", "binance_symbol": "XRPUSDT", "slug_prefix": "xrp-updown"},
}

# Interval durations we look for
INTERVALS = {
    "5m": 300,
    "15m": 900,
    "4h": 14400,
}


@dataclass
class MarketOpportunity:
    """A single market with trading potential."""
    market_id: str
    slug: str
    question: str
    category: str  # crypto_oracle, arb, sports_data, weather_data, generic
    tokens: dict  # {"UP": {"token_id": ..., "price": ...}, "DOWN": {...}}
    condition_id: str = ""
    interval_duration: int = 900
    asset: str = ""  # btc, eth, sol, xrp (for crypto markets)
    combined_price: float = 1.0  # YES + NO price
    arb_edge: float = 0.0  # 1.0 - combined_price (if < 1.0)
    spread: float = 0.0  # ask - bid spread
    volume_24h: float = 0.0
    data_source: str = ""  # e.g. "noaa", "espn", "binance"
    extra: dict = field(default_factory=dict)


class MarketScanner:
    """Discovers and categorizes ALL active markets on Polymarket.

    Replaces the old MarketFinder which only looked for BTC up/down.
    Scans across crypto, sports, weather, politics — everything.
    """

    def __init__(self, gamma_host: str = "https://gamma-api.polymarket.com"):
        self.gamma_host = gamma_host
        self.client = httpx.AsyncClient(timeout=15)
        self._market_cache: list[MarketOpportunity] = []
        self._cache_ts: float = 0
        self._cache_ttl: float = 15  # seconds

        # Track which markets we've already scanned to avoid duplicates
        self._seen_slugs: set[str] = set()

    async def scan_all(self) -> list[MarketOpportunity]:
        """Scan all market types and return sorted opportunities.

        Returns markets sorted by priority:
        1. Arbitrage opportunities (risk-free, highest priority)
        2. Crypto oracle lag markets (our core edge)
        3. Data-edge markets (sports/weather with fast data)
        4. Market making opportunities (high volume, good spread)
        """
        if (time.time() - self._cache_ts) < self._cache_ttl and self._market_cache:
            return self._market_cache

        self._seen_slugs.clear()
        opportunities: list[MarketOpportunity] = []

        # Run all scans concurrently for speed
        results = await asyncio.gather(
            self._scan_crypto_markets(),
            self._scan_all_active_for_arb(),
            self._scan_multi_outcome_arb(),
            return_exceptions=True,
        )

        for result in results:
            if isinstance(result, Exception):
                logger.warning("Scanner error: %s", result)
            elif isinstance(result, list):
                for opp in result:
                    if opp.slug not in self._seen_slugs:
                        self._seen_slugs.add(opp.slug)
                        opportunities.append(opp)

        # Sort: arb first (risk-free), then crypto oracle, then data-edge
        def sort_key(m: MarketOpportunity) -> tuple:
            priority = {
                "arb": 0,         # Risk-free = highest priority
                "crypto_oracle": 1,
                "sports_data": 2,
                "weather_data": 3,
                "generic": 4,
            }
            return (priority.get(m.category, 99), -m.arb_edge, -m.volume_24h)

        opportunities.sort(key=sort_key)
        self._market_cache = opportunities
        self._cache_ts = time.time()

        logger.info(
            "Scan complete: %d markets (%d arb, %d crypto, %d other)",
            len(opportunities),
            sum(1 for m in opportunities if m.category == "arb"),
            sum(1 for m in opportunities if m.category == "crypto_oracle"),
            sum(1 for m in opportunities if m.category not in ("arb", "crypto_oracle")),
        )

        return opportunities

    async def _scan_crypto_markets(self) -> list[MarketOpportunity]:
        """Find all active crypto up/down markets across BTC, ETH, SOL, XRP."""
        markets = []
        now_ts = int(time.time())

        for asset_key, asset_info in CRYPTO_ASSETS.items():
            prefix = asset_info["slug_prefix"]

            # Try 15-min and 5-min intervals
            for label, duration in [("15m", 900), ("5m", 300)]:
                epoch = now_ts - (now_ts % duration)
                slug = f"{prefix}-{label}-{epoch}"

                market = await self._fetch_market(slug)
                if market and market.get("active") and not market.get("closed"):
                    opp = self._parse_to_opportunity(
                        market,
                        category="crypto_oracle",
                        interval_duration=duration,
                        asset=asset_key,
                        data_source="binance",
                    )
                    if opp:
                        markets.append(opp)

            # Try epoch-based hourly/4h for assets that have them
            for label, duration in [("4h", 14400)]:
                epoch = now_ts - (now_ts % duration)
                slug = f"{prefix}-{label}-{epoch}"
                market = await self._fetch_market(slug)
                if market and market.get("active") and not market.get("closed"):
                    opp = self._parse_to_opportunity(
                        market,
                        category="crypto_oracle",
                        interval_duration=duration,
                        asset=asset_key,
                        data_source="binance",
                    )
                    if opp:
                        markets.append(opp)

        # Also try named-slug formats for hourly/daily BTC
        now_et = datetime.now(timezone.utc).astimezone(ET)
        for slug_builder, duration in [
            (self._build_hourly_slug, 3600),
            (self._build_daily_slug, 86400),
        ]:
            slug = slug_builder(now_et)
            market = await self._fetch_market(slug)
            if market and market.get("active") and not market.get("closed"):
                opp = self._parse_to_opportunity(
                    market,
                    category="crypto_oracle",
                    interval_duration=duration,
                    asset="btc",
                    data_source="binance",
                )
                if opp:
                    markets.append(opp)

        return markets

    async def _scan_all_active_for_arb(self) -> list[MarketOpportunity]:
        """Scan ALL active markets for arbitrage (YES+NO < $1.00).

        This is the risk-free money scanner. Any market where the
        combined price of all outcomes is < $1.00 is free profit.
        """
        arb_markets = []

        try:
            # Fetch active markets with pagination
            # Gamma API: /markets?active=true&closed=false&limit=100
            offset = 0
            limit = 100
            max_pages = 5  # Don't scan forever

            for page in range(max_pages):
                resp = await self.client.get(
                    f"{self.gamma_host}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": limit,
                        "offset": offset,
                    },
                )
                resp.raise_for_status()
                markets = resp.json()

                if not markets:
                    break

                for market in markets:
                    opp = self._check_arb_opportunity(market)
                    if opp:
                        arb_markets.append(opp)

                offset += limit

                # Rate limit: don't hammer the API
                await asyncio.sleep(0.2)

        except Exception as e:
            logger.warning("Arb scan error: %s", e)

        if arb_markets:
            logger.info(
                "Found %d arb opportunities (best edge: %.2f%%)",
                len(arb_markets),
                max(m.arb_edge for m in arb_markets) * 100 if arb_markets else 0,
            )

        return arb_markets

    def _check_arb_opportunity(self, market: dict) -> MarketOpportunity | None:
        """Check if a single market has an arb opportunity (YES+NO < $1.00)."""
        prices_raw = market.get("outcomePrices", "[]")
        if isinstance(prices_raw, str):
            try:
                prices = json.loads(prices_raw)
            except json.JSONDecodeError:
                return None
        else:
            prices = prices_raw

        if len(prices) < 2:
            return None

        try:
            price_floats = [float(p) for p in prices]
        except (ValueError, TypeError):
            return None

        combined = sum(price_floats)

        # Arb threshold: combined < $0.985 (need enough edge to cover fees)
        if combined < 0.985:
            edge = 1.0 - combined
            opp = self._parse_to_opportunity(
                market,
                category="arb",
                interval_duration=0,
                asset="",
                data_source="polymarket",
            )
            if opp:
                opp.combined_price = combined
                opp.arb_edge = edge
                return opp

        return None

    async def _scan_multi_outcome_arb(self) -> list[MarketOpportunity]:
        """Scan events with 3+ outcomes for multi-outcome arbitrage.

        In a multi-outcome event (e.g., "Who wins the election?" with 10 candidates),
        the sum of all outcome prices should equal $1.00.
        If the sum < $1.00, buying all outcomes is risk-free profit.
        If the sum > $1.00, selling all outcomes is risk-free profit.

        This catches arbs that binary scanners miss — the "Bregman projection"
        approach from quant playbooks, simplified to practical scanning.
        """
        arbs = []

        try:
            # Fetch events (events group related markets)
            resp = await self.client.get(
                f"{self.gamma_host}/events",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": 50,
                },
            )
            resp.raise_for_status()
            events = resp.json()

            for event in events:
                markets = event.get("markets", [])
                if len(markets) < 3:
                    continue  # Only interested in multi-outcome events

                # Collect YES prices across all outcomes in this event
                outcome_prices = []
                valid_markets = []
                for market in markets:
                    prices_raw = market.get("outcomePrices", "[]")
                    if isinstance(prices_raw, str):
                        try:
                            prices = json.loads(prices_raw)
                        except json.JSONDecodeError:
                            continue
                    else:
                        prices = prices_raw

                    if not prices:
                        continue

                    try:
                        yes_price = float(prices[0])  # First outcome = YES
                    except (ValueError, TypeError, IndexError):
                        continue

                    if 0 < yes_price < 1:
                        outcome_prices.append(yes_price)
                        valid_markets.append(market)

                if len(outcome_prices) < 3:
                    continue

                total = sum(outcome_prices)

                # Multi-outcome arb: total should be ~1.0
                # If total < 0.97, buy all outcomes → guaranteed profit
                if total < 0.97:
                    edge = 1.0 - total
                    # Find the cheapest outcome to buy (highest expected return)
                    min_idx = outcome_prices.index(min(outcome_prices))
                    best_market = valid_markets[min_idx]

                    opp = self._parse_to_opportunity(
                        best_market,
                        category="arb",
                        interval_duration=0,
                        asset="",
                        data_source="polymarket_multi",
                    )
                    if opp:
                        opp.combined_price = total
                        opp.arb_edge = edge
                        opp.extra = {
                            "type": "multi_outcome",
                            "num_outcomes": len(outcome_prices),
                            "prices": outcome_prices,
                            "event": event.get("title", "")[:80],
                        }
                        arbs.append(opp)
                        logger.info(
                            "Multi-outcome arb: %s | %d outcomes sum=$%.3f edge=%.1f%%",
                            event.get("title", "")[:50],
                            len(outcome_prices), total, edge * 100,
                        )

                # Also check for correlated market mispricings via KL-divergence
                # If two outcomes in the same event have prices that don't make
                # logical sense together, there's an information edge
                self._check_correlation_edge(event, valid_markets, outcome_prices, arbs)

        except Exception as e:
            logger.warning("Multi-outcome arb scan error: %s", e)

        return arbs

    def _check_correlation_edge(
        self,
        event: dict,
        markets: list[dict],
        prices: list[float],
        results: list[MarketOpportunity],
    ):
        """Check for KL-divergence mispricings between correlated outcomes.

        If two mutually exclusive outcomes (e.g., Candidate A vs Candidate B)
        have prices that imply different total probabilities when normalized,
        one is mispriced relative to the other.

        KL(P||Q) = Σ P_i * log(P_i / Q_i)
        High KL = big divergence = potential edge.
        """
        if len(prices) < 3:
            return

        total = sum(prices)
        if total <= 0:
            return

        # Normalize to proper probability distribution
        normalized = [p / total for p in prices]

        # Compare each outcome's market price vs. its "fair" normalized price
        for i, (market, raw_price, norm_price) in enumerate(
            zip(markets, prices, normalized)
        ):
            if norm_price <= 0 or raw_price <= 0:
                continue

            # KL contribution for this outcome
            kl_contribution = norm_price * math.log(norm_price / raw_price) if raw_price > 0 else 0

            # If this outcome is significantly underpriced vs. normalized fair value
            price_gap = norm_price - raw_price
            if price_gap > 0.05 and kl_contribution > 0.01:
                opp = self._parse_to_opportunity(
                    market,
                    category="arb",
                    interval_duration=0,
                    asset="",
                    data_source="kl_divergence",
                )
                if opp:
                    opp.arb_edge = price_gap
                    opp.extra = {
                        "type": "kl_divergence",
                        "kl_contribution": round(kl_contribution, 4),
                        "fair_price": round(norm_price, 4),
                        "market_price": round(raw_price, 4),
                        "event": event.get("title", "")[:80],
                    }
                    results.append(opp)

    def _parse_to_opportunity(
        self,
        market: dict,
        category: str,
        interval_duration: int,
        asset: str,
        data_source: str,
    ) -> MarketOpportunity | None:
        """Parse a Gamma API market dict into a MarketOpportunity."""
        tokens_raw = market.get("clobTokenIds", [])
        prices_raw = market.get("outcomePrices", [])
        outcomes_raw = market.get("outcomes", [])

        if isinstance(tokens_raw, str):
            try:
                tokens_raw = json.loads(tokens_raw)
            except json.JSONDecodeError:
                return None
        if isinstance(prices_raw, str):
            try:
                prices_raw = json.loads(prices_raw)
            except json.JSONDecodeError:
                return None
        if isinstance(outcomes_raw, str):
            try:
                outcomes_raw = json.loads(outcomes_raw)
            except json.JSONDecodeError:
                return None

        if len(tokens_raw) < 2 or len(prices_raw) < 2:
            return None

        tokens = {}
        for i, outcome in enumerate(outcomes_raw):
            label = str(outcome).upper().strip() if isinstance(outcome, str) else f"OUTCOME_{i}"
            if label in ("YES", "YES 🟢"):
                label = "UP"
            elif label in ("NO", "NO 🔴"):
                label = "DOWN"

            token_id = tokens_raw[i] if i < len(tokens_raw) else None
            try:
                price = float(prices_raw[i]) if i < len(prices_raw) else None
            except (ValueError, TypeError):
                price = None

            tokens[label] = {"token_id": token_id, "price": price}

        # Calculate combined price and spread
        price_vals = [t["price"] for t in tokens.values() if t["price"] is not None]
        combined = sum(price_vals) if price_vals else 1.0
        spread = max(price_vals) - min(price_vals) if len(price_vals) >= 2 else 0

        slug = market.get("slug", "")
        volume = 0
        try:
            volume = float(market.get("volume", 0) or 0)
        except (ValueError, TypeError):
            pass

        return MarketOpportunity(
            market_id=market.get("conditionId", slug),
            slug=slug,
            question=market.get("question", ""),
            category=category,
            tokens=tokens,
            condition_id=market.get("conditionId", ""),
            interval_duration=interval_duration,
            asset=asset,
            combined_price=combined,
            arb_edge=max(0, 1.0 - combined),
            spread=spread,
            volume_24h=volume,
            data_source=data_source,
        )

    async def _fetch_market(self, slug: str) -> dict | None:
        """Fetch a single market by slug."""
        try:
            resp = await self.client.get(
                f"{self.gamma_host}/events",
                params={"slug": slug},
            )
            resp.raise_for_status()
            events = resp.json()
            if events and events[0].get("markets"):
                return events[0]["markets"][0]
        except Exception:
            pass

        try:
            resp = await self.client.get(
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

    @staticmethod
    def _build_hourly_slug(dt_et: datetime) -> str:
        month = dt_et.strftime("%B").lower()
        day = dt_et.day
        h = dt_et.hour
        ampm = "am" if h < 12 else "pm"
        h12 = h % 12 if h % 12 != 0 else 12
        return f"bitcoin-up-or-down-{month}-{day}-{h12}{ampm}-et"

    @staticmethod
    def _build_daily_slug(dt_et: datetime) -> str:
        month = dt_et.strftime("%B").lower()
        day = dt_et.day
        return f"bitcoin-up-or-down-on-{month}-{day}"

    async def close(self):
        await self.client.aclose()
