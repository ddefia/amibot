"""Analyze trader data from Polymarket BTC 5-min markets.

Designed to ingest CSV/JSON data from the 5 trader folders (successful and
failing traders) and extract actionable patterns:
- Entry timing within 5-min intervals
- Position sizing patterns
- Win rate by time of day, price conditions, volatility
- Edge estimation and strategy classification
"""

import os
import logging
from pathlib import Path

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class TraderAnalyzer:
    """Load and analyze individual trader data to reverse-engineer strategies."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.traders: dict[str, pd.DataFrame] = {}

    def load_all(self) -> dict[str, pd.DataFrame]:
        """Load all trader data from subdirectories.

        Expected structure:
            data_dir/
                trader_1/
                    trades.csv (or *.json)
                trader_2/
                    ...
        """
        if not self.data_dir.exists():
            logger.warning("Data directory %s does not exist", self.data_dir)
            return {}

        for folder in sorted(self.data_dir.iterdir()):
            if not folder.is_dir():
                continue

            trader_name = folder.name
            df = self._load_trader_folder(folder)
            if df is not None and not df.empty:
                self.traders[trader_name] = df
                logger.info(
                    "Loaded %d trades for trader '%s'", len(df), trader_name
                )

        logger.info("Loaded %d traders total", len(self.traders))
        return self.traders

    def _load_trader_folder(self, folder: Path) -> pd.DataFrame | None:
        """Load all trade files from a single trader folder."""
        frames = []

        for f in folder.iterdir():
            try:
                if f.suffix == ".csv":
                    df = pd.read_csv(f)
                    frames.append(df)
                elif f.suffix == ".json":
                    df = pd.read_json(f)
                    frames.append(df)
            except Exception:
                logger.warning("Failed to load %s", f)

        if not frames:
            return None

        combined = pd.concat(frames, ignore_index=True)
        return self._normalize_columns(combined)

    def _normalize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """Normalize column names to a standard format."""
        col_map = {}
        for col in df.columns:
            lower = col.lower().strip()
            if "time" in lower or "date" in lower or "timestamp" in lower:
                col_map[col] = "timestamp"
            elif "price" in lower and "entry" in lower:
                col_map[col] = "entry_price"
            elif "price" in lower and "exit" in lower:
                col_map[col] = "exit_price"
            elif lower == "price":
                col_map[col] = "entry_price"
            elif "side" in lower or "direction" in lower:
                col_map[col] = "side"
            elif "size" in lower or "amount" in lower or "quantity" in lower:
                col_map[col] = "size"
            elif "pnl" in lower or "profit" in lower or "p&l" in lower:
                col_map[col] = "pnl"
            elif "outcome" in lower or "result" in lower or "won" in lower:
                col_map[col] = "outcome"
            elif "market" in lower or "slug" in lower:
                col_map[col] = "market"
            elif "token" in lower:
                col_map[col] = "token_id"

        df = df.rename(columns=col_map)

        # Parse timestamps
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
            df = df.sort_values("timestamp").reset_index(drop=True)

        return df

    def analyze_trader(self, name: str) -> dict:
        """Generate a full analysis of a single trader's behavior."""
        if name not in self.traders:
            return {"error": f"Trader '{name}' not found"}

        df = self.traders[name]
        analysis = {
            "name": name,
            "total_trades": len(df),
        }

        # Win/loss metrics
        if "outcome" in df.columns:
            wins = df["outcome"].astype(str).str.lower().isin(["win", "won", "true", "1", "yes"])
            analysis["wins"] = int(wins.sum())
            analysis["losses"] = int((~wins).sum())
            analysis["win_rate"] = float(wins.mean())
        elif "pnl" in df.columns:
            analysis["wins"] = int((df["pnl"] > 0).sum())
            analysis["losses"] = int((df["pnl"] <= 0).sum())
            analysis["win_rate"] = float((df["pnl"] > 0).mean())

        # P&L analysis
        if "pnl" in df.columns:
            pnl = df["pnl"].astype(float)
            analysis["total_pnl"] = float(pnl.sum())
            analysis["avg_win"] = float(pnl[pnl > 0].mean()) if (pnl > 0).any() else 0
            analysis["avg_loss"] = float(pnl[pnl <= 0].mean()) if (pnl <= 0).any() else 0
            analysis["max_win"] = float(pnl.max())
            analysis["max_loss"] = float(pnl.min())
            analysis["profit_factor"] = (
                float(pnl[pnl > 0].sum() / abs(pnl[pnl < 0].sum()))
                if (pnl < 0).any() and pnl[pnl < 0].sum() != 0
                else float("inf")
            )
            analysis["sharpe"] = float(pnl.mean() / pnl.std()) if pnl.std() > 0 else 0

        # Position sizing patterns
        if "size" in df.columns:
            sizes = df["size"].astype(float)
            analysis["avg_size"] = float(sizes.mean())
            analysis["median_size"] = float(sizes.median())
            analysis["max_size"] = float(sizes.max())
            analysis["size_stddev"] = float(sizes.std())

        # Entry price patterns
        if "entry_price" in df.columns:
            prices = df["entry_price"].astype(float)
            analysis["avg_entry_price"] = float(prices.mean())
            analysis["median_entry_price"] = float(prices.median())
            # Do they prefer cheap options (< $0.35)?
            analysis["pct_entries_below_35c"] = float((prices < 0.35).mean())
            # Do they buy near 50/50 odds?
            analysis["pct_entries_near_50c"] = float(
                ((prices > 0.40) & (prices < 0.60)).mean()
            )

        # Timing analysis
        if "timestamp" in df.columns and df["timestamp"].notna().any():
            ts = df["timestamp"]
            analysis["trading_hours"] = self._analyze_timing(ts)

            # Interval timing: when during the 5-min window do they trade?
            seconds_in_interval = ts.dt.second + (ts.dt.minute % 5) * 60
            analysis["avg_seconds_into_interval"] = float(seconds_in_interval.mean())
            analysis["entry_timing_distribution"] = {
                "first_60s": float((seconds_in_interval < 60).mean()),
                "60_120s": float(
                    ((seconds_in_interval >= 60) & (seconds_in_interval < 120)).mean()
                ),
                "120_180s": float(
                    ((seconds_in_interval >= 120) & (seconds_in_interval < 180)).mean()
                ),
                "180_240s": float(
                    ((seconds_in_interval >= 180) & (seconds_in_interval < 240)).mean()
                ),
                "last_60s": float((seconds_in_interval >= 240).mean()),
            }

        # Side preference
        if "side" in df.columns:
            side_counts = df["side"].astype(str).str.lower().value_counts()
            analysis["side_preference"] = side_counts.to_dict()

        return analysis

    def _analyze_timing(self, timestamps: pd.Series) -> dict:
        """Analyze what hours of the day the trader is most active."""
        hours = timestamps.dt.hour
        hour_counts = hours.value_counts().sort_index()
        peak_hour = int(hour_counts.idxmax()) if not hour_counts.empty else -1

        return {
            "peak_hour_utc": peak_hour,
            "trades_per_hour": hour_counts.to_dict(),
            "active_hours": int(hour_counts[hour_counts > 0].count()),
        }

    def compare_traders(self) -> pd.DataFrame:
        """Compare all loaded traders side by side."""
        rows = []
        for name in self.traders:
            analysis = self.analyze_trader(name)
            rows.append(analysis)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df = df.sort_values("win_rate", ascending=False) if "win_rate" in df.columns else df
        return df

    def classify_strategy(self, name: str) -> str:
        """Attempt to classify what strategy a trader is using based on their
        trade patterns."""
        analysis = self.analyze_trader(name)
        if "error" in analysis:
            return "unknown"

        clues = []

        # High win rate + trades later in interval → latency arb
        win_rate = analysis.get("win_rate", 0)
        avg_entry_time = analysis.get("avg_seconds_into_interval", 150)
        if win_rate > 0.80 and avg_entry_time > 120:
            clues.append("latency_arb")

        # Buys cheap options (< $0.35) → buy-low strategy
        if analysis.get("pct_entries_below_35c", 0) > 0.5:
            clues.append("buy_low")

        # Trades very frequently + near 50c → market making
        total = analysis.get("total_trades", 0)
        near_50 = analysis.get("pct_entries_near_50c", 0)
        if total > 500 and near_50 > 0.5:
            clues.append("market_making")

        # Balanced side preference → mispricing / arb (buys both sides)
        side_pref = analysis.get("side_preference", {})
        if side_pref:
            values = list(side_pref.values())
            if len(values) >= 2:
                ratio = min(values) / max(values) if max(values) > 0 else 0
                if ratio > 0.4:
                    clues.append("mispricing_arb")

        if not clues:
            if win_rate > 0.6:
                return "profitable_unknown"
            return "unprofitable_or_unknown"

        return " + ".join(clues)

    def extract_strategy_params(self, name: str) -> dict:
        """Extract concrete strategy parameters from a successful trader's data
        that can be fed into our bot's strategy config."""
        analysis = self.analyze_trader(name)
        if "error" in analysis:
            return {}

        params = {
            "strategy_type": self.classify_strategy(name),
            "suggested_position_size": analysis.get("median_size", 100),
            "suggested_entry_price_max": analysis.get("median_entry_price", 0.50),
            "win_rate_benchmark": analysis.get("win_rate", 0.5),
            "optimal_entry_window_seconds": (
                int(analysis.get("avg_seconds_into_interval", 150)),
                min(285, int(analysis.get("avg_seconds_into_interval", 150)) + 60),
            ),
        }

        # If they have high profit factor, use their sizing approach
        if analysis.get("profit_factor", 0) > 2.0:
            params["kelly_fraction"] = 0.25  # quarter Kelly
            params["aggressive_sizing"] = True
        else:
            params["kelly_fraction"] = 0.10  # tenth Kelly
            params["aggressive_sizing"] = False

        return params
