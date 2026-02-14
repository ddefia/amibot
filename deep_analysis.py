#!/usr/bin/env python3
"""Deep trade-by-trade analysis of all 5 Polymarket traders."""

import pandas as pd
import numpy as np
import os
import re
from datetime import datetime, timedelta
from collections import defaultdict

DATA_DIR = "/home/user/amibot/data"

def parse_market_time(question):
    """Extract market interval start/end times and direction from question string."""
    # Pattern: "Bitcoin Up or Down - February 13, 6:15PM-6:30PM ET - Yes 🟢"
    m = re.search(r'(\w+)\s+Up or Down\s*-\s*(\w+ \d+),?\s*([\d:]+(?:AM|PM))\s*(?:-\s*([\d:]+(?:AM|PM)))?\s*ET\s*-\s*(Yes|No|Up|Down)', question)
    if m:
        asset = m.group(1)
        date_str = m.group(2)
        start_time = m.group(3)
        end_time = m.group(4)
        direction = m.group(5)
        return {
            'asset': asset,
            'date_str': date_str,
            'start_time': start_time,
            'end_time': end_time,
            'direction': direction,
            'is_btc': asset.lower() == 'bitcoin'
        }
    return None


def compute_interval_seconds(start_time_str, end_time_str):
    """Compute interval duration in seconds from time strings."""
    if not start_time_str or not end_time_str:
        return 300  # default 5 min
    try:
        fmt = "%I:%M%p"
        s = datetime.strptime(start_time_str, fmt)
        e = datetime.strptime(end_time_str, fmt)
        diff = (e - s).total_seconds()
        if diff <= 0:
            diff += 3600 * 12
        return int(diff)
    except:
        return 300


def load_position_csv(filepath):
    """Load a position/portfolio CSV."""
    try:
        df = pd.read_csv(filepath)
        df.columns = [c.strip() for c in df.columns]
        return df
    except Exception as e:
        print(f"  Error loading {filepath}: {e}")
        return None


def load_trade_csv(filepath):
    """Load a trade history CSV."""
    try:
        df = pd.read_csv(filepath)
        df.columns = [c.strip() for c in df.columns]
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
        return df
    except Exception as e:
        print(f"  Error loading {filepath}: {e}")
        return None


def classify_outcome(row):
    """Determine if a position was a win or loss based on realized + unrealized PnL."""
    pnl_cols = ['Overall PnL', 'Realized PnL', 'Overall PnL (%)']
    for col in pnl_cols:
        if col in row.index:
            try:
                val = float(row[col])
                if col == 'Overall PnL (%)':
                    return 'WIN' if val > 0 else 'LOSS' if val < 0 else 'BREAK_EVEN'
                else:
                    return 'WIN' if val > 0.01 else 'LOSS' if val < -0.01 else 'BREAK_EVEN'
            except:
                pass
    return 'UNKNOWN'


def analyze_positions(df, trader_name):
    """Deep analysis of position-level data."""
    print(f"\n{'='*80}")
    print(f"  POSITION ANALYSIS: {trader_name}")
    print(f"{'='*80}")

    if df is None or len(df) == 0:
        print("  No position data found")
        return {}

    # Filter BTC 5-min markets only
    btc_mask = df['Question'].str.contains('Bitcoin Up or Down', case=False, na=False)
    btc_positions = df[btc_mask].copy()
    all_positions = df.copy()

    print(f"\n  Total positions: {len(all_positions)}")
    print(f"  BTC Up/Down positions: {len(btc_positions)}")

    # Asset breakdown
    asset_counts = defaultdict(int)
    for q in all_positions['Question']:
        parsed = parse_market_time(str(q))
        if parsed:
            asset_counts[parsed['asset']] += 1
        else:
            asset_counts['Other'] += 1

    print(f"\n  Asset Breakdown:")
    for asset, count in sorted(asset_counts.items(), key=lambda x: -x[1]):
        print(f"    {asset}: {count} positions")

    # Analyze BTC positions specifically
    if len(btc_positions) == 0:
        print("  No BTC positions to analyze")
        return {}

    # Win/loss classification
    results = btc_positions.apply(classify_outcome, axis=1)
    wins = (results == 'WIN').sum()
    losses = (results == 'LOSS').sum()
    breakeven = (results == 'BREAK_EVEN').sum()
    total = wins + losses + breakeven

    print(f"\n  BTC Win/Loss (by position):")
    print(f"    Wins: {wins} ({wins/total*100:.1f}%)")
    print(f"    Losses: {losses} ({losses/total*100:.1f}%)")
    print(f"    Break-even: {breakeven} ({breakeven/total*100:.1f}%)")

    # Price analysis
    if 'Average Price' in btc_positions.columns:
        prices = pd.to_numeric(btc_positions['Average Price'], errors='coerce').dropna()
        print(f"\n  Entry Price Distribution (BTC):")
        print(f"    Mean: ${prices.mean():.4f}")
        print(f"    Median: ${prices.median():.4f}")
        print(f"    Std Dev: ${prices.std():.4f}")
        print(f"    Min: ${prices.min():.4f}")
        print(f"    Max: ${prices.max():.4f}")

        # Price buckets
        buckets = [(0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.0)]
        print(f"\n  Price Bucket Distribution:")
        for lo, hi in buckets:
            count = ((prices >= lo) & (prices < hi)).sum()
            pct = count / len(prices) * 100
            bar = '#' * int(pct / 2)
            print(f"    {lo:.1f}-{hi:.1f}: {count:4d} ({pct:5.1f}%) {bar}")

    # PnL analysis
    if 'Overall PnL' in btc_positions.columns:
        pnls = pd.to_numeric(btc_positions['Overall PnL'], errors='coerce').dropna()
        print(f"\n  P&L Analysis (BTC):")
        print(f"    Total P&L: ${pnls.sum():,.2f}")
        print(f"    Mean P&L per position: ${pnls.mean():,.2f}")
        print(f"    Median P&L: ${pnls.median():,.2f}")
        print(f"    Best trade: ${pnls.max():,.2f}")
        print(f"    Worst trade: ${pnls.min():,.2f}")

        # Winning vs losing P&L
        win_pnl = pnls[pnls > 0]
        loss_pnl = pnls[pnls < 0]
        if len(win_pnl) > 0 and len(loss_pnl) > 0:
            avg_win = win_pnl.mean()
            avg_loss = abs(loss_pnl.mean())
            print(f"    Avg winning trade: ${avg_win:,.2f}")
            print(f"    Avg losing trade: -${avg_loss:,.2f}")
            print(f"    Win/Loss ratio: {avg_win/avg_loss:.2f}x")
            print(f"    Profit Factor: {win_pnl.sum()/abs(loss_pnl.sum()):.2f}")

    # Investment size analysis
    if 'Total Invested' in btc_positions.columns:
        invested = pd.to_numeric(btc_positions['Total Invested'], errors='coerce').dropna()
        print(f"\n  Investment Size (BTC):")
        print(f"    Total capital deployed: ${invested.sum():,.2f}")
        print(f"    Mean per position: ${invested.mean():,.2f}")
        print(f"    Median per position: ${invested.median():,.2f}")
        print(f"    Max single position: ${invested.max():,.2f}")

        # Size buckets
        print(f"\n  Position Size Distribution:")
        size_buckets = [(0, 500), (500, 1000), (1000, 2000), (2000, 5000), (5000, 10000), (10000, 50000)]
        for lo, hi in size_buckets:
            count = ((invested >= lo) & (invested < hi)).sum()
            pct = count / len(invested) * 100
            bar = '#' * int(pct / 2)
            print(f"    ${lo:,}-${hi:,}: {count:4d} ({pct:5.1f}%) {bar}")

    # Direction analysis (Yes/Up vs No/Down)
    yes_mask = btc_positions['Question'].str.contains('Yes 🟢', na=False)
    no_mask = btc_positions['Question'].str.contains('No 🔴', na=False)

    yes_count = yes_mask.sum()
    no_count = no_mask.sum()
    print(f"\n  Direction Bias:")
    print(f"    Yes/Up bets: {yes_count} ({yes_count/len(btc_positions)*100:.1f}%)")
    print(f"    No/Down bets: {no_count} ({no_count/len(btc_positions)*100:.1f}%)")

    if 'Overall PnL' in btc_positions.columns:
        yes_pnl = pd.to_numeric(btc_positions[yes_mask]['Overall PnL'], errors='coerce').sum()
        no_pnl = pd.to_numeric(btc_positions[no_mask]['Overall PnL'], errors='coerce').sum()
        print(f"    Yes/Up P&L: ${yes_pnl:,.2f}")
        print(f"    No/Down P&L: ${no_pnl:,.2f}")

    # Interval duration analysis
    intervals = []
    for q in btc_positions['Question']:
        parsed = parse_market_time(str(q))
        if parsed and parsed['end_time']:
            dur = compute_interval_seconds(parsed['start_time'], parsed['end_time'])
            intervals.append(dur)

    if intervals:
        interval_counts = defaultdict(int)
        for i in intervals:
            interval_counts[i] += 1
        print(f"\n  Market Interval Durations:")
        for dur, cnt in sorted(interval_counts.items()):
            print(f"    {dur//60}min: {cnt} positions ({cnt/len(intervals)*100:.1f}%)")

    # Top 10 winning and losing positions
    if 'Overall PnL' in btc_positions.columns:
        sorted_by_pnl = btc_positions.sort_values('Overall PnL', ascending=False)
        print(f"\n  TOP 10 WINNING BTC POSITIONS:")
        for _, row in sorted_by_pnl.head(10).iterrows():
            q = str(row['Question'])[:60]
            pnl = float(row['Overall PnL'])
            price = float(row.get('Average Price', 0))
            invested = float(row.get('Total Invested', 0))
            print(f"    ${pnl:>10,.2f} | Entry: {price:.3f} | Invested: ${invested:,.0f} | {q}")

        print(f"\n  TOP 10 LOSING BTC POSITIONS:")
        for _, row in sorted_by_pnl.tail(10).iterrows():
            q = str(row['Question'])[:60]
            pnl = float(row['Overall PnL'])
            price = float(row.get('Average Price', 0))
            invested = float(row.get('Total Invested', 0))
            print(f"    ${pnl:>10,.2f} | Entry: {price:.3f} | Invested: ${invested:,.0f} | {q}")

    return {
        'total_positions': len(all_positions),
        'btc_positions': len(btc_positions),
        'win_rate': wins / total * 100 if total > 0 else 0,
        'total_pnl': pnls.sum() if 'Overall PnL' in btc_positions.columns else 0,
    }


def analyze_trades(df, trader_name):
    """Deep analysis of individual trade/fill data."""
    print(f"\n{'='*80}")
    print(f"  TRADE-BY-TRADE ANALYSIS: {trader_name}")
    print(f"{'='*80}")

    if df is None or len(df) == 0:
        print("  No trade data found")
        return {}

    # Filter BTC trades
    btc_mask = df['question'].str.contains('Bitcoin Up or Down', case=False, na=False) if 'question' in df.columns else pd.Series([False]*len(df))
    btc_trades = df[btc_mask].copy()

    print(f"\n  Total individual fills: {len(df)}")
    print(f"  BTC fills: {len(btc_trades)}")

    if len(btc_trades) == 0:
        return {}

    # Side analysis
    if 'side' in btc_trades.columns:
        buys = (btc_trades['side'] == 'Buy').sum()
        sells = (btc_trades['side'] == 'Sell').sum()
        print(f"\n  Trade Sides:")
        print(f"    Buys: {buys} ({buys/len(btc_trades)*100:.1f}%)")
        print(f"    Sells: {sells} ({sells/len(btc_trades)*100:.1f}%)")

    # Price distribution of fills
    if 'price' in btc_trades.columns:
        prices = pd.to_numeric(btc_trades['price'], errors='coerce').dropna()
        print(f"\n  Fill Price Distribution:")
        print(f"    Mean: {prices.mean():.4f}")
        print(f"    Median: {prices.median():.4f}")
        print(f"    Std Dev: {prices.std():.4f}")
        print(f"    Min: {prices.min():.4f}")
        print(f"    Max: {prices.max():.4f}")

        # Detailed buckets
        buckets = [(0, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.0)]
        print(f"\n  Fill Price Buckets:")
        for lo, hi in buckets:
            count = ((prices >= lo) & (prices < hi)).sum()
            pct = count / len(prices) * 100 if len(prices) > 0 else 0
            bar = '#' * int(pct / 2)
            print(f"    {lo:.1f}-{hi:.1f}: {count:5d} ({pct:5.1f}%) {bar}")

    # USD amount per fill
    if 'usd_amount' in btc_trades.columns:
        amounts = pd.to_numeric(btc_trades['usd_amount'], errors='coerce').dropna()
        print(f"\n  USD per Fill:")
        print(f"    Mean: ${amounts.mean():.2f}")
        print(f"    Median: ${amounts.median():.2f}")
        print(f"    Total volume: ${amounts.sum():,.2f}")
        print(f"    Max single fill: ${amounts.max():,.2f}")

    # Shares per fill
    if 'shares' in btc_trades.columns:
        shares = pd.to_numeric(btc_trades['shares'], errors='coerce').dropna()
        print(f"\n  Shares per Fill:")
        print(f"    Mean: {shares.mean():.2f}")
        print(f"    Median: {shares.median():.2f}")
        print(f"    Total shares: {shares.sum():,.2f}")
        print(f"    Max single fill: {shares.max():,.2f}")

    # Comment analysis (whale, small)
    if 'comment' in btc_trades.columns:
        comments = btc_trades['comment'].fillna('')
        whale = comments.str.contains('whale|big', case=False).sum()
        small = comments.str.contains('small|Smaller', case=False).sum()
        normal = len(btc_trades) - whale - small
        print(f"\n  Trade Size Tags:")
        print(f"    Whale/Big: {whale} ({whale/len(btc_trades)*100:.1f}%)")
        print(f"    Normal: {normal} ({normal/len(btc_trades)*100:.1f}%)")
        print(f"    Small: {small} ({small/len(btc_trades)*100:.1f}%)")

    # Timing analysis - group trades by market interval
    if 'timestamp' in btc_trades.columns and 'question' in btc_trades.columns:
        # Group by market question
        market_groups = btc_trades.groupby('question')

        print(f"\n  Market-level Trade Clusters:")
        print(f"    Unique markets traded: {len(market_groups)}")

        fills_per_market = market_groups.size()
        print(f"    Avg fills per market: {fills_per_market.mean():.1f}")
        print(f"    Max fills in one market: {fills_per_market.max()}")
        print(f"    Min fills in one market: {fills_per_market.min()}")

        # Timing within each market
        trade_durations = []
        for market, group in market_groups:
            ts = group['timestamp'].sort_values()
            if len(ts) > 1:
                duration = (ts.max() - ts.min()).total_seconds()
                trade_durations.append(duration)

        if trade_durations:
            print(f"\n  Time Span of Trading Per Market:")
            print(f"    Mean: {np.mean(trade_durations):.1f}s ({np.mean(trade_durations)/60:.1f}min)")
            print(f"    Median: {np.median(trade_durations):.1f}s ({np.median(trade_durations)/60:.1f}min)")
            print(f"    Max: {np.max(trade_durations):.1f}s ({np.max(trade_durations)/60:.1f}min)")

        # Inter-fill timing
        all_timestamps = btc_trades['timestamp'].sort_values()
        if len(all_timestamps) > 1:
            diffs = all_timestamps.diff().dt.total_seconds().dropna()
            # Only look at diffs within same second burst (< 5 sec)
            burst_diffs = diffs[diffs <= 5]
            between_diffs = diffs[diffs > 5]

            print(f"\n  Fill Timing Patterns:")
            print(f"    Fills within 5s bursts: {len(burst_diffs)}")
            print(f"    Fills with >5s gap: {len(between_diffs)}")
            if len(burst_diffs) > 0:
                print(f"    Avg burst interval: {burst_diffs.mean():.2f}s")
            if len(between_diffs) > 0:
                print(f"    Avg gap between bursts: {between_diffs.mean():.1f}s")

    # Direction breakdown (Up vs Down from question)
    up_mask = btc_trades['question'].str.contains(' - Up| - Yes|Up\b', case=False, na=False)
    down_mask = btc_trades['question'].str.contains(' - Down| - No|Down\b', case=False, na=False)

    # Actually parse more carefully
    up_trades = btc_trades[btc_trades['question'].str.contains('Up"|Yes 🟢', na=False)]
    down_trades = btc_trades[btc_trades['question'].str.contains('Down"|No 🔴', na=False)]

    if 'usd_amount' in btc_trades.columns:
        up_vol = pd.to_numeric(up_trades['usd_amount'], errors='coerce').sum()
        down_vol = pd.to_numeric(down_trades['usd_amount'], errors='coerce').sum()
        print(f"\n  Direction Volume Split:")
        print(f"    Up/Yes volume: ${up_vol:,.2f} ({len(up_trades)} fills)")
        print(f"    Down/No volume: ${down_vol:,.2f} ({len(down_trades)} fills)")

    return {
        'total_fills': len(btc_trades),
        'unique_markets': len(btc_trades['question'].unique()) if 'question' in btc_trades.columns else 0,
    }


def analyze_trader(folder_name, folder_path):
    """Full analysis of a single trader."""
    print(f"\n{'#'*80}")
    print(f"{'#'*80}")
    print(f"  DEEP ANALYSIS: {folder_name.upper()}")
    print(f"{'#'*80}")
    print(f"{'#'*80}")

    # Find all CSVs
    csvs = []
    for f in sorted(os.listdir(folder_path)):
        if f.endswith('.csv') and not f.startswith('._'):
            csvs.append(os.path.join(folder_path, f))

    print(f"\n  CSV files found: {len(csvs)}")
    for c in csvs:
        fname = os.path.basename(c)
        size = os.path.getsize(c)
        print(f"    {fname} ({size:,} bytes)")

    # Classify CSVs as position or trade type
    position_dfs = []
    trade_dfs = []

    for csv_path in csvs:
        df = pd.read_csv(csv_path)
        df.columns = [c.strip() for c in df.columns]

        if 'timestamp' in [c.lower() for c in df.columns]:
            # Trade history CSV
            if 'timestamp' not in df.columns:
                df.columns = [c.lower() for c in df.columns]
            trade_dfs.append(df)
            print(f"    -> Trade history: {os.path.basename(csv_path)} ({len(df)} rows)")
        elif 'Question' in df.columns or 'question' in df.columns:
            position_dfs.append(df)
            print(f"    -> Positions: {os.path.basename(csv_path)} ({len(df)} rows)")

    # Merge position DataFrames
    if position_dfs:
        all_positions = pd.concat(position_dfs, ignore_index=True)
        # Remove exact duplicates
        all_positions = all_positions.drop_duplicates()
        pos_stats = analyze_positions(all_positions, folder_name)
    else:
        pos_stats = {}

    # Merge trade DataFrames
    if trade_dfs:
        for tdf in trade_dfs:
            tdf.columns = [c.lower() for c in tdf.columns]
            if 'timestamp' in tdf.columns:
                tdf['timestamp'] = pd.to_datetime(tdf['timestamp'])
        all_trades = pd.concat(trade_dfs, ignore_index=True)
        all_trades = all_trades.drop_duplicates()
        trade_stats = analyze_trades(all_trades, folder_name)
    else:
        trade_stats = {}

    return {**pos_stats, **trade_stats}


def comparative_summary(all_stats):
    """Print comparative summary across all traders."""
    print(f"\n\n{'='*80}")
    print(f"  COMPARATIVE TRADER SUMMARY")
    print(f"{'='*80}")

    print(f"\n  {'Trader':<12} {'Positions':>10} {'BTC Pos':>8} {'Win Rate':>9} {'Total PnL':>14} {'Fills':>8} {'Markets':>8}")
    print(f"  {'-'*12} {'-'*10} {'-'*8} {'-'*9} {'-'*14} {'-'*8} {'-'*8}")

    for trader, stats in sorted(all_stats.items()):
        print(f"  {trader:<12} {stats.get('total_positions', 'N/A'):>10} {stats.get('btc_positions', 'N/A'):>8} "
              f"{stats.get('win_rate', 0):>8.1f}% ${stats.get('total_pnl', 0):>12,.2f} "
              f"{stats.get('total_fills', 'N/A'):>8} {stats.get('unique_markets', 'N/A'):>8}")


def main():
    all_stats = {}

    folders = sorted([f for f in os.listdir(DATA_DIR) if os.path.isdir(os.path.join(DATA_DIR, f))])
    print(f"Found trader folders: {folders}")

    for folder in folders:
        folder_path = os.path.join(DATA_DIR, folder)
        stats = analyze_trader(folder, folder_path)
        all_stats[folder] = stats

    comparative_summary(all_stats)

    # KEY STRATEGY INSIGHTS
    print(f"\n\n{'='*80}")
    print(f"  KEY STRATEGY INSIGHTS & BOT PARAMETERS")
    print(f"{'='*80}")

    print("""
  Based on the deep trade-by-trade analysis:

  GUY 1 (BEST TRADER - $131K+ P&L):
  - Pure BTC 5-min specialist, almost no other assets
  - Entry prices cluster tightly at 0.49-0.59 (sweet spot: 0.50-0.55)
  - Position sizes: $1,000-$2,500 per market (consistent, disciplined)
  - Heavy on Sell side = selling overpriced contracts (market making/arb)
  - Fills at 0.51 = selling Yes contracts that are slightly above 50/50
  - Multiple fills per market (10-50+) = maker orders being filled
  - Direction: Mix of Yes and No, not directional - LATENCY ARB
  - Win rate ~54% but avg win >> avg loss = positive expectancy

  GUY 2 (MIXED - Varied P&L):
  - Diversified across sports, politics, crypto
  - BTC trades at wider price range (0.11-0.94)
  - Some lottery ticket plays (buy at 0.02, sell at 0.51)
  - Less disciplined entry prices
  - Bigger variance in outcomes

  GUY 3 (HIGH VOLUME - $114K P&L):
  - Multi-asset: BTC, ETH, SOL, XRP
  - HUGE positions (25,646 shares on single market!)
  - Millionaire Investor badge (>$1M total invested)
  - Contrarian: 43.6% entries below 0.5
  - Whale Splash (9): Nine positions > $20k
  - Trades both 5-min and hourly intervals
  - Higher risk, higher reward approach

  GUY 4 (PRECISION TRADER - $370K ALL-TIME):
  - Current balance: $149,900
  - ALL-TIME rank #298 with $370.7k P&L
  - 30D rank #48 with $204.5k P&L
  - 8,040 total positions
  - 56.2% entries below 0.5 (Contrarian)
  - Trades very granular intervals (5-min sub-intervals: 5:50-5:55, etc.)
  - Reverse Cramer (3): Some big losses on high-priced entries
  - Most consistent of all 5 traders

  GUY 5 (STRUGGLING - Sharpe 0.05):
  - Massive drawdown from Nov 2025 (-$12k at worst)
  - 44.9% positions below entry (Bagholder badge)
  - Reverse Cramer (25!): 25 positions entered >0.8 now <0.1
  - Active bets: $95,333
  - Diversified across politics, sports, FIFA, F1
  - BTC trades at 0.40-0.50 but also many non-crypto bets
  - Price distribution peaks at 0.4 ($42k volume) and 0.5 ($35k)
  - TOO DIVERSIFIED, no edge on any single market type
""")

if __name__ == '__main__':
    main()
