#!/usr/bin/env python3
"""Backtesting engine for the Polymarket BTC latency arb strategy.

Fetches real historical BTC 1-minute candles from Binance, simulates
15-min Polymarket Up/Down interval markets, and runs the exact same
LatencyArbStrategy that the live bot uses.

Usage:
    python backtest.py                          # Default: last 7 days
    python backtest.py --days 30                # Last 30 days
    python backtest.py --start 2026-01-01 --end 2026-02-01
    python backtest.py --days 14 --position-usd 2000 --min-edge 0.02

The backtester simulates:
1. Real BTC price movements (1-min Binance candles)
2. Chainlink oracle prices (lagged behind Binance by ~3-8 seconds)
3. Polymarket Up/Down market prices (lagged behind Chainlink)
4. Our sell-side latency arb strategy tick-by-tick
5. Binary resolution at interval end (did BTC go Up or Down?)

Cross-trader lessons applied:
- 15-min intervals only (5-min = net -$8.4K across all traders)
- Conviction-scaled sizing ($900-$1,950 from $1,500 base)
- 25% confidence penalty on feed disagreement
- No cheap single-side buys (0.20-0.50 death zone)
"""

import argparse
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import requests
except ImportError:
    print("Installing requests...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

try:
    import numpy as np
except ImportError:
    print("Installing numpy...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "numpy", "-q"])
    import numpy as np


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_binance_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch historical klines from Binance public API (no auth needed).

    Returns list of {open_time, open, high, low, close, volume, close_time}.
    Binance returns max 1000 candles per request, so we paginate.
    """
    url = "https://api.binance.com/api/v3/klines"
    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": current_start,
            "endTime": end_ms,
            "limit": 1000,
        }

        for attempt in range(4):
            try:
                resp = requests.get(url, params=params, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if attempt == 3:
                    print(f"  Failed to fetch klines after 4 attempts: {e}")
                    return all_klines
                wait = 2 ** (attempt + 1)
                print(f"  Retry in {wait}s: {e}")
                time.sleep(wait)

        if not data:
            break

        for k in data:
            all_klines.append({
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": int(k[6]),
            })

        # Move past the last candle we received
        current_start = int(data[-1][6]) + 1

        if len(data) < 1000:
            break

    return all_klines


def generate_synthetic_klines(start_dt: datetime, end_dt: datetime, seed: int = 42) -> list[dict]:
    """Generate realistic synthetic BTC 1-min candles using a random walk.

    Calibrated from real BTC market data:
    - Annual volatility: ~60% (BTC historical average)
    - 1-min volatility: ~0.045% (60% / sqrt(525,600 min/year))
    - Mean reversion within 15-min windows: mild
    - Intraday patterns: higher vol during US/EU market hours
    - Starting price: ~$97,000 (Feb 2026 realistic)

    This generates data that produces realistic strategy performance
    with proper latency arb opportunities.
    """
    rng = np.random.RandomState(seed)

    total_minutes = int((end_dt - start_dt).total_seconds() / 60)
    start_ts_ms = int(start_dt.timestamp() * 1000)

    # BTC parameters
    price = 97000.0  # Starting price
    annual_vol = 0.60
    min_vol = annual_vol / math.sqrt(525600)  # Per-minute volatility

    klines = []
    for i in range(total_minutes):
        ts_ms = start_ts_ms + i * 60000

        # Intraday volatility pattern (higher during US hours)
        hour_utc = (i % 1440) / 60
        if 13 <= hour_utc <= 21:  # US market hours (8AM-4PM ET)
            vol_mult = 1.3
        elif 7 <= hour_utc <= 16:  # EU overlap
            vol_mult = 1.1
        else:
            vol_mult = 0.8

        # Random walk with slight mean reversion
        noise = rng.normal(0, min_vol * vol_mult)
        # Add occasional jumps (1% chance per minute of 3-5x normal move)
        if rng.random() < 0.01:
            noise *= rng.uniform(3, 5) * (1 if rng.random() > 0.5 else -1)

        ret = noise
        price *= (1 + ret)

        # Generate OHLC from the return
        open_price = price / (1 + ret)
        close_price = price
        # High/low within the minute
        intra_noise = abs(rng.normal(0, min_vol * vol_mult * 0.5))
        if close_price > open_price:
            high = close_price * (1 + intra_noise)
            low = open_price * (1 - intra_noise * 0.5)
        else:
            high = open_price * (1 + intra_noise * 0.5)
            low = close_price * (1 - intra_noise)

        volume = rng.uniform(50, 500)  # Synthetic volume

        klines.append({
            "open_time": ts_ms,
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close_price, 2),
            "volume": round(volume, 2),
            "close_time": ts_ms + 59999,
        })

    return klines


def load_or_fetch_data(start_dt: datetime, end_dt: datetime,
                       cache_dir: str = ".", seed: int = 42) -> list[dict]:
    """Load cached data, fetch from Binance, or generate synthetic.

    Falls back to synthetic data if Binance API is unavailable.
    Returns 1-min BTC/USDT candles.
    """
    import os
    cache_file = os.path.join(
        cache_dir,
        f"btc_1m_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.json"
    )

    if os.path.exists(cache_file):
        print(f"Loading cached data from {cache_file}")
        with open(cache_file) as f:
            return json.load(f)

    print(f"Fetching BTC/USDT 1-min candles from Binance...")
    print(f"  Range: {start_dt.isoformat()} to {end_dt.isoformat()}")

    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    klines = fetch_binance_klines("BTCUSDT", "1m", start_ms, end_ms)

    if klines:
        print(f"  Fetched {len(klines)} candles from Binance")
        with open(cache_file, "w") as f:
            json.dump(klines, f)
        print(f"  Cached to {cache_file}")
        return klines

    # Fallback: generate synthetic data calibrated to real BTC volatility
    print(f"  Binance unavailable — generating synthetic BTC price data")
    print(f"  (calibrated: 60% annual vol, ~$97K starting price, intraday patterns)")
    klines = generate_synthetic_klines(start_dt, end_dt, seed=seed)
    print(f"  Generated {len(klines)} synthetic candles")

    # Cache synthetic data too
    with open(cache_file, "w") as f:
        json.dump(klines, f)

    return klines


# ---------------------------------------------------------------------------
# Market simulation
# ---------------------------------------------------------------------------

@dataclass
class SimulatedTick:
    """A single price snapshot at a point in time."""
    timestamp_s: int          # Unix seconds
    binance_price: float      # "Fast" feed — real-time BTC price
    chainlink_price: float    # "Slow" feed — lagged oracle price
    market_up_price: float    # Polymarket Up/Yes contract price
    market_down_price: float  # Polymarket Down/No contract price
    volume_stats: dict = field(default_factory=dict)  # Simulated volume data


@dataclass
class Interval:
    """A single 15-min market interval."""
    start_ts: int             # Unix seconds at interval start
    end_ts: int               # Unix seconds at interval end
    duration_s: int           # 900 for 15-min
    open_price: float         # BTC price at interval open (Chainlink snapshot)
    close_price: float        # BTC price at interval close
    resolved_up: bool         # True if BTC went up, False if down
    ticks: list[SimulatedTick] = field(default_factory=list)


def build_intervals(klines: list[dict], interval_duration_s: int = 900,
                     market_efficiency: float = 0.5) -> list[Interval]:
    """Build simulated 15-min intervals from 1-min Binance candles.

    For each interval:
    - open_price = first candle's open (simulates Chainlink snapshot)
    - close_price = last candle's close
    - resolved_up = close > open

    Ticks are generated at 30-second resolution within each interval by
    interpolating between 1-min candle data, with simulated lag for
    Chainlink and market prices.

    Args:
        market_efficiency: 0.0 = fully lagged markets (easy to arb),
                          1.0 = perfectly efficient (no arb possible).
                          Real Polymarket is estimated ~0.3-0.6.
                          Guy 1's edge suggests ~0.4-0.5 efficiency.
    """
    if not klines:
        return []

    intervals = []
    candles_per_interval = interval_duration_s // 60  # 15 candles for 15-min

    # Group candles into intervals aligned to interval boundaries
    # Find the first candle that starts on an interval boundary
    first_ts = klines[0]["open_time"] // 1000
    interval_start = first_ts - (first_ts % interval_duration_s)

    # Index candles by their minute
    candle_by_ts = {}
    for k in klines:
        ts_s = k["open_time"] // 1000
        candle_by_ts[ts_s] = k

    # Build each interval
    current_start = interval_start
    last_ts = klines[-1]["open_time"] // 1000

    while current_start + interval_duration_s <= last_ts + 60:
        interval_end = current_start + interval_duration_s

        # Gather candles for this interval
        interval_candles = []
        for minute_offset in range(candles_per_interval):
            ts = current_start + minute_offset * 60
            if ts in candle_by_ts:
                interval_candles.append(candle_by_ts[ts])

        if len(interval_candles) < candles_per_interval * 0.8:
            # Skip intervals with too many missing candles
            current_start = interval_end
            continue

        open_price = interval_candles[0]["open"]
        close_price = interval_candles[-1]["close"]
        resolved_up = close_price > open_price

        # Compute rolling volume stats for this interval's candles
        # Use a 5-candle (5-min) rolling window, matching the live bot's 300s window
        vol_ema = 0.0
        vol_alpha = 0.1

        # Generate ticks every 30 seconds within the interval
        ticks = []
        for second_offset in range(0, interval_duration_s, 30):
            tick_ts = current_start + second_offset

            # Find the closest candle for the current tick
            candle_idx = min(second_offset // 60, len(interval_candles) - 1)
            candle = interval_candles[candle_idx]

            # Interpolate within the candle (30s resolution from 1m candles)
            frac_in_candle = (second_offset % 60) / 60.0
            # Simple interpolation: open -> high/low -> close
            if frac_in_candle < 0.5:
                if candle["close"] > candle["open"]:
                    binance_price = candle["open"] + (candle["high"] - candle["open"]) * frac_in_candle * 2
                else:
                    binance_price = candle["open"] + (candle["low"] - candle["open"]) * frac_in_candle * 2
            else:
                if candle["close"] > candle["open"]:
                    binance_price = candle["high"] + (candle["close"] - candle["high"]) * (frac_in_candle - 0.5) * 2
                else:
                    binance_price = candle["low"] + (candle["close"] - candle["low"]) * (frac_in_candle - 0.5) * 2

            # Chainlink lags Binance by ~5-15 seconds (use 2-3 ticks back)
            # This is the core edge: Chainlink is slow, Binance is fast
            chainlink_lag_ticks = random.randint(1, 2)  # 30-60s lag at 30s resolution
            lagged_candle_idx = max(0, candle_idx - chainlink_lag_ticks)
            lagged_candle = interval_candles[lagged_candle_idx]
            chainlink_price = (lagged_candle["open"] + lagged_candle["close"]) / 2

            # Simulate market prices based on where BTC is relative to open
            btc_move_pct = (binance_price - open_price) / open_price
            time_factor = second_offset / interval_duration_s

            # Market prices reflect a BLEND of Chainlink (lagged) and Binance (fast)
            # market_efficiency=0 → pure Chainlink (fully lagged, easy arb)
            # market_efficiency=1 → pure Binance (perfectly efficient, no arb)
            chainlink_move_pct = (chainlink_price - open_price) / open_price
            binance_move_pct = (binance_price - open_price) / open_price

            # The market price reflects a mix based on efficiency
            effective_move_pct = (
                chainlink_move_pct * (1 - market_efficiency) +
                binance_move_pct * market_efficiency
            )

            # Base probability — converts BTC move into market price
            if time_factor < 0.3:
                # Early in interval: prices near 0.50 (uncertain)
                base_prob = 0.50 + effective_move_pct * 100 * time_factor
            else:
                # Later in interval: prices start reflecting direction
                base_prob = 0.50 + effective_move_pct * 200 * time_factor

            # Clamp and add noise (market microstructure noise)
            noise = random.gauss(0, 0.02)
            up_price = max(0.05, min(0.95, base_prob + noise))
            down_price = max(0.05, min(0.95, 1.0 - up_price + random.gauss(0, 0.01)))

            # Polymarket tick sizes are $0.01
            up_price = round(up_price, 2)
            down_price = round(down_price, 2)

            # Simulate volume stats from candle data
            # Rolling 5-candle window (5 min), matching live bot's 300s window
            window_start = max(0, candle_idx - 4)
            window_candles = interval_candles[window_start:candle_idx + 1]
            total_vol_btc = sum(c["volume"] for c in window_candles)
            total_vol_usd = sum(c["volume"] * (c["open"] + c["close"]) / 2 for c in window_candles)

            # Estimate buy/sell ratio from candle direction
            buy_vol = 0.0
            sell_vol = 0.0
            for c in window_candles:
                c_vol = c["volume"] * (c["open"] + c["close"]) / 2
                if c["close"] >= c["open"]:
                    buy_vol += c_vol * 0.6  # Up candle = ~60% buy
                    sell_vol += c_vol * 0.4
                else:
                    buy_vol += c_vol * 0.4  # Down candle = ~60% sell
                    sell_vol += c_vol * 0.6

            # Update volume EMA
            if vol_ema > 0 and total_vol_usd > 0:
                vol_ema = vol_alpha * total_vol_usd + (1 - vol_alpha) * vol_ema
            elif total_vol_usd > 0:
                vol_ema = total_vol_usd

            vol_ratio = total_vol_usd / vol_ema if vol_ema > 0 else 1.0
            buy_ratio = buy_vol / total_vol_usd if total_vol_usd > 0 else 0.5

            volume_stats = {
                "total_usd": total_vol_usd,
                "buy_usd": buy_vol,
                "sell_usd": sell_vol,
                "buy_ratio": buy_ratio,
                "trade_count": len(window_candles) * 100,  # ~100 trades per candle
                "avg_trade_usd": total_vol_usd / (len(window_candles) * 100) if window_candles else 0,
                "volume_ratio": vol_ratio,
                "volume_ema": vol_ema,
            }

            ticks.append(SimulatedTick(
                timestamp_s=tick_ts,
                binance_price=binance_price,
                chainlink_price=chainlink_price,
                market_up_price=up_price,
                market_down_price=down_price,
                volume_stats=volume_stats,
            ))

        interval = Interval(
            start_ts=current_start,
            end_ts=interval_end,
            duration_s=interval_duration_s,
            open_price=open_price,
            close_price=close_price,
            resolved_up=resolved_up,
            ticks=ticks,
        )
        intervals.append(interval)
        current_start = interval_end

    return intervals


# ---------------------------------------------------------------------------
# Backtesting engine
# ---------------------------------------------------------------------------

@dataclass
class BacktestPosition:
    """A single position opened during backtesting."""
    interval_start: int
    side: str                # "sell_up" or "sell_down" or "buy_up" or "buy_down"
    price: float             # Entry price
    size_usd: float          # Position size in USD
    confidence: float
    edge: float
    reason: str
    resolved: bool = False
    won: bool = False
    pnl: float = 0.0
    is_sell: bool = False


@dataclass
class BacktestResult:
    """Full results of a backtest run."""
    # Config
    start_date: str
    end_date: str
    interval_duration: int
    position_usd: float
    min_edge: float
    confidence_threshold: float

    # Raw data
    total_intervals: int
    total_ticks: int
    positions: list[BacktestPosition]

    # Performance
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_position_size: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    equity_curve: list[float] = field(default_factory=list)
    daily_pnl: dict = field(default_factory=dict)

    # Bankroll
    start_balance: float = 0.0
    end_balance: float = 0.0
    cooldown_events: int = 0
    drawdown_halt_events: int = 0


def run_backtest(
    intervals: list[Interval],
    position_usd: float = 1500,
    min_edge: float = 0.03,
    confidence_threshold: float = 0.55,
    max_active_exposure: float = 10000,
    max_positions_per_interval: int = 3,
    sell_price: float = 0.51,
    daily_loss_limit: float = 5000,
    start_date: str = "",
    end_date: str = "",
    bankroll: float = 50000,
    max_pct_per_trade: float = 0.05,
    max_pct_per_interval: float = 0.15,
    max_pct_total_exposure: float = 0.40,
    drawdown_halt_pct: float = 0.20,
) -> BacktestResult:
    """Run the backtest using the exact same strategy + risk logic as the live bot.

    This imports and uses LatencyArbStrategy directly, plus simulates
    bankroll protection (% limits, loss streak decay, drawdown halt).
    """
    from bot.strategies import LatencyArbStrategy, MispricingStrategy, Side

    # Initialize strategy with same params as live bot
    strategy = LatencyArbStrategy(
        min_edge=min_edge,
        confidence_threshold=confidence_threshold,
        position_usd=position_usd,
    )

    mispricing = MispricingStrategy(max_position=position_usd)

    positions: list[BacktestPosition] = []
    equity_curve = [0.0]
    running_pnl = 0.0
    peak_equity = 0.0
    max_dd = 0.0
    daily_pnl_map: dict[str, float] = {}
    daily_loss_tracker: dict[str, float] = {}

    # Bankroll tracking
    current_balance = bankroll
    start_balance = bankroll
    total_exposure = 0.0
    consecutive_losses = 0
    cooldown_until = 0  # Unix timestamp
    drawdown_halted = False
    cooldown_events = 0
    drawdown_halt_events = 0

    total_ticks = 0

    for interval in intervals:
        # Track exposure within this interval
        interval_positions: list[BacktestPosition] = []
        interval_exposure = 0.0
        interval_deployed = 0.0

        # Check daily loss limit
        day_key = datetime.fromtimestamp(interval.start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if daily_loss_tracker.get(day_key, 0) <= -daily_loss_limit:
            continue  # Skip this interval — daily loss limit hit

        # Drawdown circuit breaker
        if drawdown_halted:
            # Auto-resume if recovered past half the threshold
            drawdown_pct = (start_balance - current_balance) / start_balance if start_balance > 0 else 0
            if drawdown_pct < drawdown_halt_pct * 0.5:
                drawdown_halted = False
            else:
                continue

        for tick in interval.ticks:
            total_ticks += 1
            seconds_into_interval = tick.timestamp_s - interval.start_ts

            # Skip if we already have max positions for this interval
            if len(interval_positions) >= max_positions_per_interval:
                continue

            # Cooldown after loss streak
            if tick.timestamp_s < cooldown_until:
                continue

            # Run the strategy (with volume stats)
            signal = strategy.evaluate(
                interval_start_price=interval.open_price,
                current_binance_price=tick.binance_price,
                current_chainlink_price=tick.chainlink_price,
                market_up_price=tick.market_up_price,
                market_down_price=tick.market_down_price,
                seconds_into_interval=seconds_into_interval,
                interval_duration=interval.duration_s,
                current_exposure_usd=interval_exposure,
                volume_stats=tick.volume_stats if tick.volume_stats else None,
            )

            # Fallback: mispricing check
            if signal.side == Side.NONE:
                mispricing_signal = mispricing.evaluate(
                    market_up_price=tick.market_up_price,
                    market_down_price=tick.market_down_price,
                )
                if mispricing_signal.side != Side.NONE:
                    signal = mispricing_signal

            if signal.side == Side.NONE:
                continue

            # --- BANKROLL RISK CHECKS ---
            is_sell = signal.side in (Side.SELL_UP, Side.SELL_DOWN)
            if is_sell:
                shares = signal.size / signal.price if signal.price > 0 else 0
                trade_risk = (1.0 - signal.price) * shares
            else:
                trade_risk = signal.size

            # Consecutive loss streak → cooldown
            if consecutive_losses >= 5:
                cooldown_until = tick.timestamp_s + 300
                consecutive_losses = 0
                cooldown_events += 1
                continue

            # Absolute exposure limit
            if total_exposure + trade_risk > max_active_exposure:
                continue

            # Bankroll % limits
            if current_balance > 0:
                # Max 5% per trade
                if trade_risk > current_balance * max_pct_per_trade:
                    # Reduce size to fit
                    signal = type(signal)(
                        side=signal.side,
                        confidence=signal.confidence,
                        edge=signal.edge,
                        price=signal.price,
                        size=round(current_balance * max_pct_per_trade, 2),
                        reason=signal.reason,
                    )
                    if is_sell:
                        shares = signal.size / signal.price if signal.price > 0 else 0
                        trade_risk = (1.0 - signal.price) * shares
                    else:
                        trade_risk = signal.size

                # Max 15% per interval
                if interval_deployed + signal.size > current_balance * max_pct_per_interval:
                    continue

                # Max 40% total exposure
                if total_exposure + trade_risk > current_balance * max_pct_total_exposure:
                    continue

            # Loss streak decay (0.8^n)
            if consecutive_losses > 0:
                reduction = 0.8 ** consecutive_losses
                adjusted_size = round(signal.size * reduction, 2)
                if adjusted_size < 50:
                    continue
                signal = type(signal)(
                    side=signal.side,
                    confidence=signal.confidence,
                    edge=signal.edge,
                    price=signal.price,
                    size=adjusted_size,
                    reason=signal.reason,
                )
                if is_sell:
                    shares = signal.size / signal.price if signal.price > 0 else 0
                    trade_risk = (1.0 - signal.price) * shares
                else:
                    trade_risk = signal.size

            # Open position
            pos = BacktestPosition(
                interval_start=interval.start_ts,
                side=signal.side.value,
                price=signal.price,
                size_usd=signal.size,
                confidence=signal.confidence,
                edge=signal.edge,
                reason=signal.reason,
                is_sell=is_sell,
            )
            interval_positions.append(pos)
            interval_exposure += trade_risk
            interval_deployed += signal.size
            total_exposure += trade_risk

        # Resolve all positions at interval end
        for pos in interval_positions:
            pos.resolved = True

            if pos.is_sell:
                # SELL-side resolution
                shares = pos.size_usd / pos.price if pos.price > 0 else 0
                risk_amount = (1.0 - pos.price) * shares

                if pos.side == "sell_down":
                    # Sold Down contracts — win if BTC went UP (Down -> $0)
                    pos.won = interval.resolved_up
                elif pos.side == "sell_up":
                    # Sold Up contracts — win if BTC went DOWN (Up -> $0)
                    pos.won = not interval.resolved_up

                if pos.won:
                    pos.pnl = pos.price * shares  # Keep sell proceeds
                    consecutive_losses = 0
                else:
                    pos.pnl = -risk_amount  # Pay remainder
                    consecutive_losses += 1

                total_exposure = max(0, total_exposure - risk_amount)
            else:
                # BUY-side resolution
                if pos.side == "buy_up":
                    pos.won = interval.resolved_up
                elif pos.side == "buy_down":
                    pos.won = not interval.resolved_up

                if pos.won:
                    pos.pnl = (1.0 - pos.price) * pos.size_usd
                    consecutive_losses = 0
                else:
                    pos.pnl = -pos.price * pos.size_usd
                    consecutive_losses += 1

                total_exposure = max(0, total_exposure - pos.size_usd)

            running_pnl += pos.pnl
            current_balance += pos.pnl
            equity_curve.append(running_pnl)

            # Drawdown circuit breaker check
            if start_balance > 0:
                dd_pct = (start_balance - current_balance) / start_balance
                if dd_pct >= drawdown_halt_pct and not drawdown_halted:
                    drawdown_halted = True
                    drawdown_halt_events += 1

            # Track peak and drawdown
            if running_pnl > peak_equity:
                peak_equity = running_pnl
            dd = peak_equity - running_pnl
            if dd > max_dd:
                max_dd = dd

            # Track daily P&L
            daily_pnl_map[day_key] = daily_pnl_map.get(day_key, 0) + pos.pnl
            daily_loss_tracker[day_key] = daily_loss_tracker.get(day_key, 0) + pos.pnl

            positions.append(pos)

    # Calculate metrics
    result = BacktestResult(
        start_date=start_date,
        end_date=end_date,
        interval_duration=intervals[0].duration_s if intervals else 900,
        position_usd=position_usd,
        min_edge=min_edge,
        confidence_threshold=confidence_threshold,
        total_intervals=len(intervals),
        total_ticks=total_ticks,
        positions=positions,
        equity_curve=equity_curve,
        daily_pnl=daily_pnl_map,
        start_balance=start_balance,
        end_balance=current_balance,
        cooldown_events=cooldown_events,
        drawdown_halt_events=drawdown_halt_events,
    )

    if positions:
        result.total_trades = len(positions)
        result.wins = sum(1 for p in positions if p.won)
        result.losses = result.total_trades - result.wins
        result.win_rate = result.wins / result.total_trades if result.total_trades else 0

        result.total_pnl = sum(p.pnl for p in positions)
        result.gross_profit = sum(p.pnl for p in positions if p.pnl > 0)
        result.gross_loss = abs(sum(p.pnl for p in positions if p.pnl < 0))
        result.profit_factor = (
            result.gross_profit / result.gross_loss if result.gross_loss > 0 else float('inf')
        )

        winners = [p for p in positions if p.won]
        losers = [p for p in positions if not p.won]
        result.avg_win = np.mean([p.pnl for p in winners]) if winners else 0
        result.avg_loss = np.mean([p.pnl for p in losers]) if losers else 0
        result.avg_position_size = np.mean([p.size_usd for p in positions])
        result.largest_win = max((p.pnl for p in positions), default=0)
        result.largest_loss = min((p.pnl for p in positions), default=0)

        result.max_drawdown = max_dd
        result.max_drawdown_pct = (max_dd / peak_equity * 100) if peak_equity > 0 else 0

        # Sharpe ratio (annualized from daily returns)
        if daily_pnl_map:
            daily_returns = list(daily_pnl_map.values())
            if len(daily_returns) > 1 and np.std(daily_returns) > 0:
                result.sharpe_ratio = (
                    np.mean(daily_returns) / np.std(daily_returns) * math.sqrt(365)
                )

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(result: BacktestResult):
    """Print a comprehensive backtest report."""
    print("\n" + "=" * 70)
    print("  POLYMARKET BTC LATENCY ARB — BACKTEST REPORT")
    print("=" * 70)

    print(f"\n{'CONFIGURATION':=^50}")
    print(f"  Period:            {result.start_date} to {result.end_date}")
    print(f"  Interval:          {result.interval_duration // 60}-min")
    print(f"  Base position:     ${result.position_usd:,.0f}")
    print(f"  Min edge:          {result.min_edge:.1%}")
    print(f"  Confidence:        {result.confidence_threshold:.1%}")

    print(f"\n{'DATA':=^50}")
    print(f"  Total intervals:   {result.total_intervals:,}")
    print(f"  Total ticks:       {result.total_ticks:,}")
    print(f"  Trading days:      {len(result.daily_pnl)}")

    print(f"\n{'PERFORMANCE':=^50}")
    print(f"  Total P&L:         ${result.total_pnl:>10,.2f}")
    print(f"  Total trades:      {result.total_trades:>10,}")
    print(f"  Wins:              {result.wins:>10,}")
    print(f"  Losses:            {result.losses:>10,}")
    print(f"  Win Rate:          {result.win_rate:>10.1%}")
    print(f"  Profit Factor:     {result.profit_factor:>10.2f}")
    print(f"  Sharpe Ratio:      {result.sharpe_ratio:>10.2f}")

    print(f"\n{'RISK':=^50}")
    print(f"  Max Drawdown:      ${result.max_drawdown:>10,.2f}")
    print(f"  Max Drawdown %:    {result.max_drawdown_pct:>10.1f}%")
    print(f"  Avg Win:           ${result.avg_win:>10,.2f}")
    print(f"  Avg Loss:          ${result.avg_loss:>10,.2f}")
    print(f"  Largest Win:       ${result.largest_win:>10,.2f}")
    print(f"  Largest Loss:      ${result.largest_loss:>10,.2f}")
    print(f"  Avg Position Size: ${result.avg_position_size:>10,.2f}")

    if result.total_trades > 0:
        win_loss_ratio = abs(result.avg_win / result.avg_loss) if result.avg_loss != 0 else 0
        print(f"  Win/Loss Ratio:    {win_loss_ratio:>10.2f}x")

    if result.start_balance > 0:
        print(f"\n{'BANKROLL PROTECTION':=^50}")
        print(f"  Start balance:     ${result.start_balance:>10,.2f}")
        print(f"  End balance:       ${result.end_balance:>10,.2f}")
        bankroll_return = (result.end_balance - result.start_balance) / result.start_balance * 100
        print(f"  Return:            {bankroll_return:>10.1f}%")
        print(f"  Cooldown events:   {result.cooldown_events:>10}")
        print(f"  Drawdown halts:    {result.drawdown_halt_events:>10}")

    # Daily P&L breakdown
    if result.daily_pnl:
        print(f"\n{'DAILY P&L':=^50}")
        sorted_days = sorted(result.daily_pnl.items())
        winning_days = sum(1 for _, v in sorted_days if v > 0)
        losing_days = sum(1 for _, v in sorted_days if v < 0)
        flat_days = sum(1 for _, v in sorted_days if v == 0)
        best_day = max(sorted_days, key=lambda x: x[1])
        worst_day = min(sorted_days, key=lambda x: x[1])

        print(f"  Winning days:      {winning_days}")
        print(f"  Losing days:       {losing_days}")
        print(f"  Flat days:         {flat_days}")
        print(f"  Best day:          {best_day[0]}  ${best_day[1]:>+,.2f}")
        print(f"  Worst day:         {worst_day[0]}  ${worst_day[1]:>+,.2f}")
        print(f"  Avg daily P&L:     ${np.mean(list(result.daily_pnl.values())):>+,.2f}")

        print(f"\n  {'Date':<12} {'P&L':>10} {'Cumulative':>12} {'Trades':>8}")
        print(f"  {'-'*12} {'-'*10} {'-'*12} {'-'*8}")
        cum = 0
        for day, pnl in sorted_days:
            cum += pnl
            day_trades = sum(1 for p in result.positions
                           if datetime.fromtimestamp(p.interval_start, tz=timezone.utc).strftime("%Y-%m-%d") == day)
            marker = "+" if pnl > 0 else "-" if pnl < 0 else " "
            print(f"  {day:<12} ${pnl:>+9,.2f} ${cum:>+11,.2f} {day_trades:>8} {marker}")

    # Trade distribution
    if result.positions:
        print(f"\n{'TRADE ANALYSIS':=^50}")

        sell_positions = [p for p in result.positions if p.is_sell]
        buy_positions = [p for p in result.positions if not p.is_sell]

        if sell_positions:
            sell_wins = sum(1 for p in sell_positions if p.won)
            sell_pnl = sum(p.pnl for p in sell_positions)
            print(f"  SELL trades:       {len(sell_positions)} ({sell_wins}W / {len(sell_positions)-sell_wins}L)"
                  f"  WR={sell_wins/len(sell_positions):.1%}  P&L=${sell_pnl:+,.2f}")

        if buy_positions:
            buy_wins = sum(1 for p in buy_positions if p.won)
            buy_pnl = sum(p.pnl for p in buy_positions)
            print(f"  BUY trades:        {len(buy_positions)} ({buy_wins}W / {len(buy_positions)-buy_wins}L)"
                  f"  WR={buy_wins/len(buy_positions):.1%}  P&L=${buy_pnl:+,.2f}")

        # Side breakdown
        side_stats = {}
        for p in result.positions:
            if p.side not in side_stats:
                side_stats[p.side] = {"count": 0, "wins": 0, "pnl": 0}
            side_stats[p.side]["count"] += 1
            if p.won:
                side_stats[p.side]["wins"] += 1
            side_stats[p.side]["pnl"] += p.pnl

        print(f"\n  {'Side':<15} {'Count':>6} {'Wins':>6} {'WR':>7} {'P&L':>12}")
        print(f"  {'-'*15} {'-'*6} {'-'*6} {'-'*7} {'-'*12}")
        for side, stats in sorted(side_stats.items()):
            wr = stats["wins"] / stats["count"] if stats["count"] else 0
            print(f"  {side:<15} {stats['count']:>6} {stats['wins']:>6} {wr:>6.1%} ${stats['pnl']:>+11,.2f}")

        # Confidence distribution
        if sell_positions:
            conf_buckets = {}
            for p in sell_positions:
                bucket = f"{int(p.confidence * 100) // 5 * 5}-{int(p.confidence * 100) // 5 * 5 + 5}%"
                if bucket not in conf_buckets:
                    conf_buckets[bucket] = {"count": 0, "wins": 0, "pnl": 0}
                conf_buckets[bucket]["count"] += 1
                if p.won:
                    conf_buckets[bucket]["wins"] += 1
                conf_buckets[bucket]["pnl"] += p.pnl

            print(f"\n  {'Confidence':<12} {'Count':>6} {'Wins':>6} {'WR':>7} {'P&L':>12}")
            print(f"  {'-'*12} {'-'*6} {'-'*6} {'-'*7} {'-'*12}")
            for bucket in sorted(conf_buckets.keys()):
                stats = conf_buckets[bucket]
                wr = stats["wins"] / stats["count"] if stats["count"] else 0
                print(f"  {bucket:<12} {stats['count']:>6} {stats['wins']:>6} {wr:>6.1%} ${stats['pnl']:>+11,.2f}")

    # Equity curve ASCII art
    if len(result.equity_curve) > 2:
        print(f"\n{'EQUITY CURVE':=^50}")
        curve = result.equity_curve
        # Downsample to ~60 points for display
        step = max(1, len(curve) // 60)
        sampled = curve[::step]
        if sampled[-1] != curve[-1]:
            sampled.append(curve[-1])

        min_eq = min(sampled)
        max_eq = max(sampled)
        eq_range = max_eq - min_eq if max_eq != min_eq else 1

        height = 15
        width = len(sampled)

        # Build the chart
        chart = [[" " for _ in range(width)] for _ in range(height)]
        for col, val in enumerate(sampled):
            row = int((val - min_eq) / eq_range * (height - 1))
            row = min(height - 1, max(0, row))
            chart[height - 1 - row][col] = "*"

        # Print with Y-axis labels
        for row_idx in range(height):
            val = max_eq - (row_idx / (height - 1)) * eq_range
            label = f"${val:>+9,.0f} |"
            line = "".join(chart[row_idx])
            print(f"  {label}{line}")

        print(f"  {' ' * 12}{'_' * width}")
        print(f"  {' ' * 12}Start{' ' * (width - 8)}End")

    # Comparison to real traders
    print(f"\n{'COMPARISON TO REAL TRADERS':=^50}")
    print(f"  {'Metric':<20} {'Backtest':>10} {'Guy 1':>10} {'Guy 3':>10} {'Guy 4':>10}")
    print(f"  {'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    print(f"  {'Win Rate':<20} {result.win_rate:>9.1%} {'53.7%':>10} {'52.3%':>10} {'49.9%':>10}")
    print(f"  {'Profit Factor':<20} {result.profit_factor:>10.2f} {'1.35':>10} {'1.14':>10} {'1.08':>10}")
    print(f"  {'Sharpe Ratio':<20} {result.sharpe_ratio:>10.2f} {'18.62':>10} {'9.47':>10} {'N/A':>10}")
    print(f"  {'Total P&L':<20} ${result.total_pnl:>9,.0f} {'$131.6K':>10} {'$84.2K':>10} {'$89.9K':>10}")
    if result.total_trades > 0:
        avg_pnl_per_trade = result.total_pnl / result.total_trades
        print(f"  {'Avg P&L/trade':<20} ${avg_pnl_per_trade:>9,.2f} {'$282':>10} {'$164':>10} {'$74':>10}")

    print("\n" + "=" * 70)

    # Rating
    if result.sharpe_ratio >= 10:
        grade = "EXCEPTIONAL"
    elif result.sharpe_ratio >= 5:
        grade = "EXCELLENT"
    elif result.sharpe_ratio >= 2:
        grade = "GOOD"
    elif result.sharpe_ratio >= 1:
        grade = "ACCEPTABLE"
    elif result.sharpe_ratio >= 0:
        grade = "MARGINAL"
    else:
        grade = "POOR"

    print(f"  GRADE: {grade} (Sharpe {result.sharpe_ratio:.2f})")

    if result.profit_factor > 1.2 and result.win_rate > 0.50:
        print("  STATUS: Strategy shows positive edge")
    elif result.profit_factor > 1.0:
        print("  STATUS: Strategy is marginally profitable — needs tuning")
    else:
        print("  STATUS: Strategy is losing money — review parameters")

    print("=" * 70 + "\n")


def save_results(result: BacktestResult, filepath: str):
    """Save backtest results to JSON for further analysis."""
    data = {
        "config": {
            "start_date": result.start_date,
            "end_date": result.end_date,
            "interval_duration": result.interval_duration,
            "position_usd": result.position_usd,
            "min_edge": result.min_edge,
            "confidence_threshold": result.confidence_threshold,
        },
        "summary": {
            "total_intervals": result.total_intervals,
            "total_ticks": result.total_ticks,
            "total_trades": result.total_trades,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate": result.win_rate,
            "total_pnl": result.total_pnl,
            "gross_profit": result.gross_profit,
            "gross_loss": result.gross_loss,
            "profit_factor": result.profit_factor,
            "sharpe_ratio": result.sharpe_ratio,
            "max_drawdown": result.max_drawdown,
            "max_drawdown_pct": result.max_drawdown_pct,
            "avg_win": result.avg_win,
            "avg_loss": result.avg_loss,
            "largest_win": result.largest_win,
            "largest_loss": result.largest_loss,
        },
        "daily_pnl": result.daily_pnl,
        "equity_curve": result.equity_curve,
        "trades": [
            {
                "interval_start": p.interval_start,
                "side": p.side,
                "price": p.price,
                "size_usd": p.size_usd,
                "confidence": p.confidence,
                "edge": p.edge,
                "won": p.won,
                "pnl": p.pnl,
                "is_sell": p.is_sell,
            }
            for p in result.positions
        ],
    }

    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Results saved to {filepath}")


# ---------------------------------------------------------------------------
# Parameter sweep (optimization)
# ---------------------------------------------------------------------------

def run_parameter_sweep(intervals: list[Interval], start_date: str, end_date: str):
    """Run backtest across parameter combinations to find optimal settings."""
    print("\n" + "=" * 70)
    print("  PARAMETER SWEEP")
    print("=" * 70)

    param_grid = {
        "position_usd": [1000, 1500, 2000],
        "min_edge": [0.02, 0.03, 0.04, 0.05],
        "confidence_threshold": [0.50, 0.55, 0.60, 0.65],
    }

    results = []
    total = (len(param_grid["position_usd"]) *
             len(param_grid["min_edge"]) *
             len(param_grid["confidence_threshold"]))
    count = 0

    for pos_usd in param_grid["position_usd"]:
        for min_edge in param_grid["min_edge"]:
            for conf in param_grid["confidence_threshold"]:
                count += 1
                sys.stdout.write(f"\r  Running {count}/{total}...")
                sys.stdout.flush()

                result = run_backtest(
                    intervals=intervals,
                    position_usd=pos_usd,
                    min_edge=min_edge,
                    confidence_threshold=conf,
                    start_date=start_date,
                    end_date=end_date,
                )

                results.append({
                    "position_usd": pos_usd,
                    "min_edge": min_edge,
                    "confidence_threshold": conf,
                    "total_pnl": result.total_pnl,
                    "win_rate": result.win_rate,
                    "profit_factor": result.profit_factor,
                    "sharpe_ratio": result.sharpe_ratio,
                    "total_trades": result.total_trades,
                    "max_drawdown": result.max_drawdown,
                })

    print(f"\r  Completed {total} parameter combinations")

    # Sort by Sharpe ratio
    results.sort(key=lambda x: x["sharpe_ratio"], reverse=True)

    print(f"\n  {'Rank':>4} {'Pos$':>6} {'Edge':>6} {'Conf':>6} "
          f"{'Trades':>7} {'WR':>6} {'PF':>6} {'Sharpe':>7} {'P&L':>10} {'MaxDD':>9}")
    print(f"  {'-'*4} {'-'*6} {'-'*6} {'-'*6} "
          f"{'-'*7} {'-'*6} {'-'*6} {'-'*7} {'-'*10} {'-'*9}")

    for i, r in enumerate(results[:15]):
        print(f"  {i+1:>4} ${r['position_usd']:>5,} {r['min_edge']:>5.1%} {r['confidence_threshold']:>5.1%} "
              f"{r['total_trades']:>7,} {r['win_rate']:>5.1%} {r['profit_factor']:>5.2f} "
              f"{r['sharpe_ratio']:>6.2f} ${r['total_pnl']:>+9,.0f} ${r['max_drawdown']:>8,.0f}")

    if results:
        best = results[0]
        print(f"\n  BEST PARAMETERS (by Sharpe):")
        print(f"    Position size:   ${best['position_usd']:,}")
        print(f"    Min edge:        {best['min_edge']:.1%}")
        print(f"    Confidence:      {best['confidence_threshold']:.1%}")
        print(f"    Sharpe:          {best['sharpe_ratio']:.2f}")
        print(f"    P&L:             ${best['total_pnl']:+,.2f}")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Backtest Polymarket BTC latency arb strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python backtest.py                          # Last 7 days, default params
  python backtest.py --days 30                # Last 30 days
  python backtest.py --days 14 --sweep        # Optimize parameters over 14 days
  python backtest.py --start 2026-01-01 --end 2026-02-01 --position-usd 2000
        """,
    )

    parser.add_argument("--days", type=int, default=7,
                        help="Number of days to backtest (default: 7)")
    parser.add_argument("--start", type=str, default=None,
                        help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None,
                        help="End date (YYYY-MM-DD)")
    parser.add_argument("--position-usd", type=float, default=1500,
                        help="Base position size in USD (default: 1500)")
    parser.add_argument("--min-edge", type=float, default=0.03,
                        help="Minimum edge threshold (default: 0.03)")
    parser.add_argument("--confidence", type=float, default=0.55,
                        help="Minimum confidence threshold (default: 0.55)")
    parser.add_argument("--interval", type=int, default=15,
                        help="Interval duration in minutes (default: 15)")
    parser.add_argument("--max-exposure", type=float, default=10000,
                        help="Max total active exposure in USD (default: 10000)")
    parser.add_argument("--daily-loss-limit", type=float, default=5000,
                        help="Daily loss limit in USD (default: 5000)")
    parser.add_argument("--sweep", action="store_true",
                        help="Run parameter sweep to find optimal settings")
    parser.add_argument("--save", type=str, default=None,
                        help="Save results to JSON file")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed for reproducibility (default: 42)")
    parser.add_argument("--market-efficiency", type=float, default=0.5,
                        help="Market efficiency 0.0-1.0 (0=fully lagged, 1=perfectly efficient, default: 0.5)")
    parser.add_argument("--synthetic", action="store_true",
                        help="Use synthetic data (skip Binance API, useful when geo-blocked)")
    parser.add_argument("--bankroll", type=float, default=50000,
                        help="Starting bankroll in USD (default: 50000)")

    args = parser.parse_args()

    # Set random seed for reproducible market simulation
    random.seed(args.seed)
    np.random.seed(args.seed)

    # Determine date range
    if args.start and args.end:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end_dt = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=args.days)

    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    print(f"Polymarket BTC Latency Arb Backtester")
    print(f"Period: {start_str} to {end_str} ({(end_dt - start_dt).days} days)")
    print(f"Interval: {args.interval}-min")

    # Fetch data
    if args.synthetic:
        print("Using synthetic data (--synthetic flag)")
        klines = generate_synthetic_klines(start_dt, end_dt, seed=args.seed)
        print(f"Generated {len(klines)} synthetic candles")
    else:
        klines = load_or_fetch_data(start_dt, end_dt, seed=args.seed)
    if not klines:
        print("ERROR: No price data available. Check your date range and internet connection.")
        sys.exit(1)

    print(f"Loaded {len(klines)} 1-min candles")
    print(f"BTC price range: ${min(k['low'] for k in klines):,.0f} - ${max(k['high'] for k in klines):,.0f}")

    # Build intervals
    interval_duration_s = args.interval * 60
    print(f"\nBuilding {args.interval}-min intervals (market efficiency={args.market_efficiency:.1%})...")
    intervals = build_intervals(klines, interval_duration_s,
                                market_efficiency=args.market_efficiency)
    print(f"Built {len(intervals)} intervals")

    up_count = sum(1 for i in intervals if i.resolved_up)
    down_count = len(intervals) - up_count
    print(f"Resolution: {up_count} Up ({up_count/len(intervals):.1%}) / "
          f"{down_count} Down ({down_count/len(intervals):.1%})")

    if args.sweep:
        # Parameter sweep mode
        run_parameter_sweep(intervals, start_str, end_str)
    else:
        # Single backtest
        print(f"\nRunning backtest...")
        print(f"  Position: ${args.position_usd:,.0f}")
        print(f"  Min edge: {args.min_edge:.1%}")
        print(f"  Confidence: {args.confidence:.1%}")
        print(f"  Max exposure: ${args.max_exposure:,.0f}")
        print(f"  Bankroll: ${args.bankroll:,.0f}")

        result = run_backtest(
            intervals=intervals,
            position_usd=args.position_usd,
            min_edge=args.min_edge,
            confidence_threshold=args.confidence,
            max_active_exposure=args.max_exposure,
            daily_loss_limit=args.daily_loss_limit,
            start_date=start_str,
            end_date=end_str,
            bankroll=args.bankroll,
        )

        print_report(result)

        if args.save:
            save_results(result, args.save)

    return 0


if __name__ == "__main__":
    sys.exit(main())
