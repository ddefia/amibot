"""Data edge strategy — trade with information faster than the market.

Uses FREE public APIs to get real-world data before Polymarket prices update:

1. WEATHER: NOAA API (free, no key) for temperature/weather prediction markets
2. SPORTS: ESPN/TheSportsDB (free) for live scores
3. NEWS: RSS feeds + free news APIs for breaking news sentiment
4. CRYPTO: Already handled by multi_feed.py (Binance/Kraken/Coinbase)

The core principle from the research PDF (Tab 8):
"The edge is not prediction. It's timing."
We get data from the source BEFORE Polymarket's oracle updates.
"""

import asyncio
import time
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class DataSignal:
    """A signal from an external data source."""
    source: str  # "noaa", "espn", "newsapi", etc.
    market_keyword: str  # Keyword to match against Polymarket market questions
    direction: str  # "yes", "no", or "unknown"
    confidence: float  # 0-1
    data_value: str  # The actual data (e.g., "72°F", "Lakers 105")
    timestamp: float
    details: str = ""


class WeatherEdge:
    """Get weather data from NOAA before Polymarket weather markets update.

    NOAA API: https://api.weather.gov — completely free, no API key needed.
    Updates every ~6 minutes. Polymarket weather oracles often lag by 15-30 min.

    Targets: "Will it rain in NYC?", temperature markets, etc.
    """

    BASE_URL = "https://api.weather.gov"

    # Major city weather stations for Polymarket weather markets
    STATIONS = {
        "nyc": {"lat": 40.7128, "lon": -74.0060, "name": "New York"},
        "la": {"lat": 34.0522, "lon": -118.2437, "name": "Los Angeles"},
        "chicago": {"lat": 41.8781, "lon": -87.6298, "name": "Chicago"},
        "miami": {"lat": 25.7617, "lon": -80.1918, "name": "Miami"},
        "dc": {"lat": 38.9072, "lon": -77.0369, "name": "Washington DC"},
    }

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=10,
            headers={"User-Agent": "(polymarket-bot, contact@example.com)"},
        )
        self._cache: dict[str, dict] = {}
        self._cache_ts: dict[str, float] = {}

    async def get_current_conditions(self, city: str = "nyc") -> DataSignal | None:
        """Get current weather conditions for a city."""
        station = self.STATIONS.get(city)
        if not station:
            return None

        # Cache for 5 minutes (NOAA updates every ~6 min)
        cache_key = f"conditions_{city}"
        if cache_key in self._cache and (time.time() - self._cache_ts.get(cache_key, 0)) < 300:
            return self._cache[cache_key]

        try:
            # Step 1: Get the observation station for these coordinates
            resp = await self.client.get(
                f"{self.BASE_URL}/points/{station['lat']},{station['lon']}"
            )
            resp.raise_for_status()
            point_data = resp.json()

            # Step 2: Get latest observation
            obs_url = point_data["properties"].get("observationStations")
            if not obs_url:
                return None

            resp = await self.client.get(obs_url)
            resp.raise_for_status()
            stations = resp.json()

            if not stations.get("features"):
                return None

            station_id = stations["features"][0]["properties"]["stationIdentifier"]

            resp = await self.client.get(
                f"{self.BASE_URL}/stations/{station_id}/observations/latest"
            )
            resp.raise_for_status()
            obs = resp.json()

            props = obs.get("properties", {})
            temp_c = props.get("temperature", {}).get("value")
            temp_f = (temp_c * 9 / 5 + 32) if temp_c is not None else None
            description = props.get("textDescription", "")
            wind_speed = props.get("windSpeed", {}).get("value")  # km/h

            signal = DataSignal(
                source="noaa",
                market_keyword=station["name"].lower(),
                direction="unknown",
                confidence=0.9,  # NOAA is authoritative
                data_value=f"{temp_f:.0f}°F" if temp_f else description,
                timestamp=time.time(),
                details=f"temp={temp_f:.0f}°F desc='{description}' wind={wind_speed}km/h",
            )

            self._cache[cache_key] = signal
            self._cache_ts[cache_key] = time.time()
            return signal

        except Exception as e:
            logger.debug("NOAA weather fetch failed for %s: %s", city, e)
            return None

    async def get_forecast(self, city: str = "nyc") -> DataSignal | None:
        """Get short-term forecast (for prediction markets about future weather)."""
        station = self.STATIONS.get(city)
        if not station:
            return None

        try:
            resp = await self.client.get(
                f"{self.BASE_URL}/points/{station['lat']},{station['lon']}"
            )
            resp.raise_for_status()
            point_data = resp.json()

            forecast_url = point_data["properties"].get("forecastHourly")
            if not forecast_url:
                return None

            resp = await self.client.get(forecast_url)
            resp.raise_for_status()
            forecast = resp.json()

            periods = forecast.get("properties", {}).get("periods", [])
            if not periods:
                return None

            # Next hour forecast
            next_period = periods[0]
            temp = next_period.get("temperature")
            rain_chance = next_period.get("probabilityOfPrecipitation", {}).get("value", 0)

            return DataSignal(
                source="noaa_forecast",
                market_keyword=station["name"].lower(),
                direction="yes" if rain_chance and rain_chance > 50 else "no",
                confidence=min(0.85, (rain_chance or 0) / 100) if rain_chance else 0.5,
                data_value=f"{temp}°F, {rain_chance}% rain",
                timestamp=time.time(),
                details=f"forecast: {next_period.get('shortForecast', '')}",
            )
        except Exception as e:
            logger.debug("NOAA forecast failed for %s: %s", city, e)
            return None

    async def close(self):
        await self.client.aclose()


class SportsEdge:
    """Get live sports scores before Polymarket sports markets update.

    Uses TheSportsDB (free tier, no key needed) and ESPN public API.
    Targets: live match results, game winners, score totals.
    """

    THESPORTSDB_URL = "https://www.thesportsdb.com/api/v1/json/3"
    ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports"

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10)

    async def get_live_scores(self, sport: str = "soccer") -> list[DataSignal]:
        """Get live scores from ESPN."""
        signals = []

        sport_map = {
            "soccer": "soccer/eng.1",  # Premier League
            "nba": "basketball/nba",
            "nfl": "football/nfl",
            "mlb": "baseball/mlb",
            "nhl": "hockey/nhl",
        }

        sport_path = sport_map.get(sport, sport)

        try:
            resp = await self.client.get(
                f"{self.ESPN_URL}/{sport_path}/scoreboard"
            )
            resp.raise_for_status()
            data = resp.json()

            for event in data.get("events", []):
                name = event.get("name", "")
                status = event.get("status", {}).get("type", {})
                state = status.get("state", "")  # pre, in, post

                if state != "in" and state != "post":
                    continue

                competitions = event.get("competitions", [{}])
                if not competitions:
                    continue

                comp = competitions[0]
                competitors = comp.get("competitors", [])
                if len(competitors) < 2:
                    continue

                home = competitors[0]
                away = competitors[1]
                home_score = int(home.get("score", 0))
                away_score = int(away.get("score", 0))
                home_name = home.get("team", {}).get("displayName", "")
                away_name = away.get("team", {}).get("displayName", "")

                # Determine likely winner
                if home_score > away_score:
                    leader = home_name
                    direction = "yes"  # Home team winning
                elif away_score > home_score:
                    leader = away_name
                    direction = "no"  # Away team winning (if market is "Will home win?")
                else:
                    leader = "tied"
                    direction = "unknown"

                signal = DataSignal(
                    source="espn",
                    market_keyword=f"{home_name} {away_name}".lower(),
                    direction=direction,
                    confidence=0.7 if state == "in" else 0.95,
                    data_value=f"{home_name} {home_score} - {away_score} {away_name}",
                    timestamp=time.time(),
                    details=f"state={state} period={status.get('description', '')}",
                )
                signals.append(signal)

        except Exception as e:
            logger.debug("ESPN %s scores failed: %s", sport, e)

        return signals

    async def get_all_live_scores(self) -> list[DataSignal]:
        """Get live scores across all major sports."""
        results = await asyncio.gather(
            self.get_live_scores("nba"),
            self.get_live_scores("nfl"),
            self.get_live_scores("nhl"),
            self.get_live_scores("soccer"),
            return_exceptions=True,
        )
        signals = []
        for result in results:
            if isinstance(result, list):
                signals.extend(result)
        return signals

    async def close(self):
        await self.client.aclose()


class NewsEdge:
    """Monitor breaking news via free RSS feeds and APIs.

    Uses free, no-key-required sources:
    - Google News RSS (real-time, free)
    - Wikipedia Current Events (structured, free)
    - Reddit JSON API (free, no key)
    """

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=10)
        self._seen_headlines: set[str] = set()

    async def get_breaking_news(self) -> list[DataSignal]:
        """Fetch breaking news from multiple free sources."""
        signals = []

        # Google News RSS (top stories)
        try:
            resp = await self.client.get(
                "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
            )
            if resp.status_code == 200:
                # Simple RSS parsing without external dependency
                text = resp.text
                items = text.split("<item>")[1:]  # Skip header
                for item in items[:10]:  # Top 10 headlines
                    title_start = item.find("<title>") + 7
                    title_end = item.find("</title>")
                    if title_start > 6 and title_end > title_start:
                        title = item[title_start:title_end].strip()
                        # Skip if we've seen this headline
                        if title in self._seen_headlines:
                            continue
                        self._seen_headlines.add(title)

                        signal = DataSignal(
                            source="google_news",
                            market_keyword=title.lower()[:100],
                            direction="unknown",
                            confidence=0.5,
                            data_value=title,
                            timestamp=time.time(),
                        )
                        signals.append(signal)
        except Exception as e:
            logger.debug("Google News RSS failed: %s", e)

        # Reddit — free JSON API, no key needed. Faster than news sites.
        # Monitors prediction-market-relevant subreddits for breaking info.
        for subreddit in ["worldnews", "sports", "cryptocurrency", "politics"]:
            try:
                resp = await self.client.get(
                    f"https://www.reddit.com/r/{subreddit}/hot.json?limit=5",
                    headers={"User-Agent": "polymarket-bot/1.0"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for post in data.get("data", {}).get("children", []):
                        title = post.get("data", {}).get("title", "")
                        score = post.get("data", {}).get("score", 0)

                        if not title or title in self._seen_headlines:
                            continue
                        if score < 100:  # Only high-engagement posts
                            continue

                        self._seen_headlines.add(title)
                        signals.append(DataSignal(
                            source=f"reddit_{subreddit}",
                            market_keyword=title.lower()[:100],
                            direction="unknown",
                            confidence=min(0.6, 0.4 + score / 10000),
                            data_value=title,
                            timestamp=time.time(),
                            details=f"r/{subreddit} score={score}",
                        ))
            except Exception as e:
                logger.debug("Reddit r/%s failed: %s", subreddit, e)

        # Keep seen headlines from growing unbounded
        if len(self._seen_headlines) > 500:
            self._seen_headlines = set(list(self._seen_headlines)[-200:])

        return signals

    async def close(self):
        await self.client.aclose()


class DataEdgeAggregator:
    """Aggregates all external data sources into a unified signal feed.

    Periodically polls all data sources and matches signals against
    active Polymarket markets. When a data signal matches a market,
    it generates a trading signal.
    """

    def __init__(self):
        self.weather = WeatherEdge()
        self.sports = SportsEdge()
        self.news = NewsEdge()
        self._signals: list[DataSignal] = []
        self._running = False

    async def start(self, poll_interval: float = 30):
        """Start polling all data sources."""
        self._running = True
        while self._running:
            signals = []

            results = await asyncio.gather(
                self.weather.get_current_conditions("nyc"),
                self.weather.get_current_conditions("la"),
                self.sports.get_all_live_scores(),
                self.news.get_breaking_news(),
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, DataSignal):
                    signals.append(result)
                elif isinstance(result, list):
                    signals.extend(result)

            self._signals = signals
            if signals:
                logger.info(
                    "Data edge: %d signals (%d weather, %d sports, %d news)",
                    len(signals),
                    sum(1 for s in signals if "noaa" in s.source),
                    sum(1 for s in signals if s.source == "espn"),
                    sum(1 for s in signals if "news" in s.source),
                )

            await asyncio.sleep(poll_interval)

    def get_signals(self) -> list[DataSignal]:
        """Get current data signals."""
        return self._signals

    def match_market(self, market_question: str) -> DataSignal | None:
        """Find a data signal that matches a Polymarket market question.

        Simple keyword matching — looks for team names, city names,
        weather terms in the market question.
        """
        question_lower = market_question.lower()

        for signal in self._signals:
            # Check if any signal keywords appear in the market question
            keywords = signal.market_keyword.split()
            match_count = sum(1 for kw in keywords if len(kw) > 3 and kw in question_lower)

            if match_count >= 2:  # At least 2 keyword matches
                return signal

        return None

    def stop(self):
        self._running = False

    async def close(self):
        await asyncio.gather(
            self.weather.close(),
            self.sports.close(),
            self.news.close(),
        )
