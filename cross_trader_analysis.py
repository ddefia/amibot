#!/usr/bin/env python3
"""
Cross-Trader BTC Analysis for Polymarket
=========================================
Analyzes BTC-specific trading patterns across 5 traders to extract
lessons for a trading bot. Focuses on positions and trade history CSVs.
"""

import csv
import re
import os
from collections import defaultdict
from datetime import datetime

# ── File mapping ──────────────────────────────────────────────────────────────
BASE = "/home/user/amibot/data"

POSITIONS_FILES = {
    "Guy 1": os.path.join(BASE, "guy 1", "2026-02-13T23-291_export.csv"),
    "Guy 2": os.path.join(BASE, "guy 2", "2026-02-13T23-31_export.csv"),
    "Guy 3": os.path.join(BASE, "guy 3", "2026-02-13T23-33_export.csv"),
    "Guy 4": os.path.join(BASE, "guy 4", "2026-02-13T23-35_export.csv"),
    "Guy 5": os.path.join(BASE, "guy 5", "2026-02-13T23-38_export.csv"),
}

TRADE_HISTORY_FILES = {
    "Guy 1": os.path.join(BASE, "guy 1", "2026-02-13T23-29_export.csv"),
    "Guy 2": os.path.join(BASE, "guy 2", "2026-02-113T23-32_export.csv"),
    "Guy 3": os.path.join(BASE, "guy 3", "2026-02-13T23-331_export.csv"),
    "Guy 5": os.path.join(BASE, "guy 5", "2026-102-13T23-38_export.csv"),
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_btc(question: str) -> bool:
    """Check if a question/market is about Bitcoin."""
    q = question.lower()
    return "bitcoin" in q or "btc" in q


def parse_direction_from_question(question: str):
    """
    Parse the direction (Yes/Up vs No/Down) from the question text.
    Positions CSV uses 'Yes' / 'No' with emoji.
    Trade history CSV uses 'Up' / 'Down' in the question text.
    Returns tuple: (direction_label, raw_text)
      direction_label is one of: 'Yes/Up', 'No/Down', 'Unknown'
    """
    q = question.strip()
    # Positions format: ends with "- Yes" or "- No" (with emoji)
    if "- Yes" in q or "Yes 🟢" in q:
        return "Yes/Up", "Yes"
    if "- No" in q or "No 🔴" in q:
        return "No/Down", "No"
    # Trade history format: question text contains "Up" or "Down"
    if "- Up" in q or "- up" in q:
        return "Yes/Up", "Up"
    if "- Down" in q or "- down" in q:
        return "No/Down", "Down"
    return "Unknown", ""


def parse_time_interval(question: str):
    """
    Extract the time interval from the question.
    Examples:
      'Bitcoin Up or Down - February 6, 12:45PM-1:00PM ET' -> 15 min
      'Bitcoin Up or Down - February 13, 4:00PM-8:00PM ET' -> 240 min
      'Bitcoin Up or Down - February 13, 6PM ET' -> point-in-time (0 min marker)
      'Bitcoin Up or Down - February 13, 5:50PM-5:55PM ET' -> 5 min
    Returns interval in minutes, or None if unparseable.
    """
    q = question.strip()

    # Pattern for range: HH:MMAM/PM-HH:MMAM/PM
    range_pat = re.compile(
        r'(\d{1,2}):(\d{2})(AM|PM)\s*-\s*(\d{1,2}):(\d{2})(AM|PM)',
        re.IGNORECASE
    )
    m = range_pat.search(q)
    if m:
        h1, m1, ap1 = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        h2, m2, ap2 = int(m.group(4)), int(m.group(5)), m.group(6).upper()
        t1 = to_minutes(h1, m1, ap1)
        t2 = to_minutes(h2, m2, ap2)
        diff = t2 - t1
        if diff < 0:
            diff += 24 * 60  # crosses midnight
        return diff

    # Pattern for point-in-time: just "6PM ET" without range
    point_pat = re.compile(r'(\d{1,2})(AM|PM)\s+ET', re.IGNORECASE)
    m = point_pat.search(q)
    if m:
        return 0  # point-in-time, no interval

    return None


def to_minutes(h, m, ap):
    if ap == 'AM':
        if h == 12:
            h = 0
    else:  # PM
        if h != 12:
            h += 12
    return h * 60 + m


def classify_interval(minutes):
    if minutes is None:
        return "unknown"
    if minutes == 0:
        return "point-in-time"
    if minutes <= 5:
        return "5-min"
    if minutes <= 15:
        return "15-min"
    if minutes <= 30:
        return "30-min"
    if minutes <= 60:
        return "hourly"
    return "multi-hour"


def price_bucket(price):
    """Bucket entry prices into ranges."""
    if price < 0.10:
        return "<0.10"
    elif price < 0.20:
        return "0.10-0.20"
    elif price < 0.30:
        return "0.20-0.30"
    elif price < 0.40:
        return "0.30-0.40"
    elif price < 0.50:
        return "0.40-0.50"
    elif price < 0.60:
        return "0.50-0.60"
    elif price < 0.70:
        return "0.60-0.70"
    elif price < 0.80:
        return "0.70-0.80"
    elif price < 0.90:
        return "0.80-0.90"
    else:
        return "0.90-1.00"


def size_bucket(invested):
    """Bucket position sizes (total invested in USD)."""
    if invested < 100:
        return "<$100"
    elif invested < 500:
        return "$100-$500"
    elif invested < 1000:
        return "$500-$1K"
    elif invested < 2000:
        return "$1K-$2K"
    elif invested < 5000:
        return "$2K-$5K"
    elif invested < 10000:
        return "$5K-$10K"
    else:
        return "$10K+"


# ── Data Loading ──────────────────────────────────────────────────────────────

def load_positions(filepath, trader_name):
    """Load BTC positions from a positions CSV."""
    positions = []
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            question = row.get('Question', '')
            if not is_btc(question):
                continue
            try:
                avg_price = float(row.get('Average Price', 0))
                current_price = float(row.get('Current Price', 0))
                realized_pnl = float(row.get('Realized PnL', 0))
                total_invested = float(row.get('Total Invested', 0))
                unrealized_pnl = float(row.get('Unrealized PnL', 0))
                overall_pnl = float(row.get('Overall PnL', 0))
                overall_pnl_pct = float(row.get('Overall PnL (%)', 0))
                position_size = float(row.get('Position', 0))
            except (ValueError, TypeError):
                continue

            direction, raw_dir = parse_direction_from_question(question)
            interval = parse_time_interval(question)
            interval_label = classify_interval(interval)

            positions.append({
                'trader': trader_name,
                'question': question,
                'direction': direction,
                'raw_direction': raw_dir,
                'avg_price': avg_price,
                'current_price': current_price,
                'realized_pnl': realized_pnl,
                'total_invested': total_invested,
                'unrealized_pnl': unrealized_pnl,
                'overall_pnl': overall_pnl,
                'overall_pnl_pct': overall_pnl_pct,
                'position_size': position_size,
                'interval_minutes': interval,
                'interval_label': interval_label,
                'is_winner': overall_pnl > 0,
                'price_bucket': price_bucket(avg_price),
                'size_bucket': size_bucket(total_invested),
            })
    return positions


def load_trade_history(filepath, trader_name):
    """Load BTC trades from a trade history CSV."""
    trades = []
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            question = row.get('question', '')
            if not is_btc(question):
                continue
            try:
                price = float(row.get('price', 0))
                usd_amount = float(row.get('usd_amount', 0))
                shares = float(row.get('shares', 0))
            except (ValueError, TypeError):
                continue

            side = row.get('side', '').strip()
            comment = row.get('comment', '').strip()
            timestamp = row.get('timestamp', '')
            direction, raw_dir = parse_direction_from_question(question)
            interval = parse_time_interval(question)
            interval_label = classify_interval(interval)

            trades.append({
                'trader': trader_name,
                'question': question,
                'side': side,  # Buy or Sell
                'price': price,
                'usd_amount': usd_amount,
                'shares': shares,
                'comment': comment,
                'timestamp': timestamp,
                'direction': direction,
                'raw_direction': raw_dir,
                'interval_minutes': interval,
                'interval_label': interval_label,
                'is_whale': '🐳' in comment,
                'is_small': '💧' in comment,
            })
    return trades


# ── Analysis Functions ────────────────────────────────────────────────────────

def print_header(title):
    width = 80
    print()
    print("=" * width)
    print(f"  {title}")
    print("=" * width)


def print_subheader(title):
    print(f"\n--- {title} ---")


def analyze_direction_performance(positions):
    """Q1: Yes/Up vs No/Down performance."""
    print_header("1. DIRECTION ANALYSIS: Yes/Up vs No/Down Performance")

    # Overall
    by_dir = defaultdict(list)
    for p in positions:
        by_dir[p['direction']].append(p)

    for d in ['Yes/Up', 'No/Down', 'Unknown']:
        group = by_dir.get(d, [])
        if not group:
            continue
        n = len(group)
        wins = sum(1 for p in group if p['is_winner'])
        losses = n - wins
        win_rate = wins / n * 100 if n else 0
        total_pnl = sum(p['overall_pnl'] for p in group)
        avg_pnl = total_pnl / n if n else 0
        avg_pnl_pct = sum(p['overall_pnl_pct'] for p in group) / n * 100 if n else 0
        total_invested = sum(p['total_invested'] for p in group)
        avg_invested = total_invested / n if n else 0
        median_pnl_pct = sorted([p['overall_pnl_pct'] for p in group])[n // 2] * 100

        print(f"\n  {d}:")
        print(f"    Positions: {n}  |  Wins: {wins}  |  Losses: {losses}  |  Win Rate: {win_rate:.1f}%")
        print(f"    Total PnL: ${total_pnl:,.2f}  |  Avg PnL: ${avg_pnl:,.2f}")
        print(f"    Avg PnL%: {avg_pnl_pct:.2f}%  |  Median PnL%: {median_pnl_pct:.2f}%")
        print(f"    Avg Position Size (invested): ${avg_invested:,.2f}")

    # Per trader breakdown
    print_subheader("Per-Trader Direction Breakdown")
    traders = sorted(set(p['trader'] for p in positions))
    for trader in traders:
        tp = [p for p in positions if p['trader'] == trader]
        print(f"\n  {trader}:")
        for d in ['Yes/Up', 'No/Down']:
            group = [p for p in tp if p['direction'] == d]
            if not group:
                continue
            n = len(group)
            wins = sum(1 for p in group if p['is_winner'])
            win_rate = wins / n * 100 if n else 0
            total_pnl = sum(p['overall_pnl'] for p in group)
            avg_pnl_pct = sum(p['overall_pnl_pct'] for p in group) / n * 100 if n else 0
            print(f"    {d}: {n} positions, WR={win_rate:.1f}%, Total PnL=${total_pnl:,.2f}, Avg PnL%={avg_pnl_pct:.1f}%")


def analyze_price_buckets(positions):
    """Q2: Entry price distribution - what ranges win vs lose."""
    print_header("2. ENTRY PRICE ANALYSIS: What Price Ranges Win vs Lose")

    buckets = defaultdict(list)
    for p in positions:
        buckets[p['price_bucket']].append(p)

    # Sort buckets by price
    bucket_order = ["<0.10", "0.10-0.20", "0.20-0.30", "0.30-0.40", "0.40-0.50",
                     "0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00"]

    print(f"\n  {'Price Bucket':<14} {'Count':>6} {'Wins':>6} {'Losses':>6} {'WinRate':>8} {'Avg PnL%':>10} {'Med PnL%':>10} {'Total PnL':>14}")
    print("  " + "-" * 84)
    for bucket in bucket_order:
        group = buckets.get(bucket, [])
        if not group:
            continue
        n = len(group)
        wins = sum(1 for p in group if p['is_winner'])
        losses = n - wins
        win_rate = wins / n * 100 if n else 0
        avg_pnl_pct = sum(p['overall_pnl_pct'] for p in group) / n * 100 if n else 0
        median_pnl_pct = sorted([p['overall_pnl_pct'] for p in group])[n // 2] * 100
        total_pnl = sum(p['overall_pnl'] for p in group)
        print(f"  {bucket:<14} {n:>6} {wins:>6} {losses:>6} {win_rate:>7.1f}% {avg_pnl_pct:>9.1f}% {median_pnl_pct:>9.1f}% ${total_pnl:>12,.2f}")

    # Now split by direction within price buckets
    print_subheader("Price Buckets by Direction")
    for direction in ['Yes/Up', 'No/Down']:
        print(f"\n  Direction: {direction}")
        print(f"  {'Price Bucket':<14} {'Count':>6} {'WinRate':>8} {'Avg PnL%':>10} {'Total PnL':>14}")
        print("  " + "-" * 60)
        for bucket in bucket_order:
            group = [p for p in buckets.get(bucket, []) if p['direction'] == direction]
            if not group:
                continue
            n = len(group)
            wins = sum(1 for p in group if p['is_winner'])
            win_rate = wins / n * 100 if n else 0
            avg_pnl_pct = sum(p['overall_pnl_pct'] for p in group) / n * 100 if n else 0
            total_pnl = sum(p['overall_pnl'] for p in group)
            print(f"  {bucket:<14} {n:>6} {win_rate:>7.1f}% {avg_pnl_pct:>9.1f}% ${total_pnl:>12,.2f}")


def analyze_position_sizing(positions):
    """Q3: Position sizing vs win rate."""
    print_header("3. POSITION SIZING ANALYSIS: Does Size Correlate with Win Rate?")

    size_order = ["<$100", "$100-$500", "$500-$1K", "$1K-$2K", "$2K-$5K", "$5K-$10K", "$10K+"]
    buckets = defaultdict(list)
    for p in positions:
        buckets[p['size_bucket']].append(p)

    print(f"\n  {'Size Bucket':<14} {'Count':>6} {'Wins':>6} {'Losses':>6} {'WinRate':>8} {'Avg PnL%':>10} {'Avg Invested':>14} {'Total PnL':>14}")
    print("  " + "-" * 96)
    for bucket in size_order:
        group = buckets.get(bucket, [])
        if not group:
            continue
        n = len(group)
        wins = sum(1 for p in group if p['is_winner'])
        losses = n - wins
        win_rate = wins / n * 100 if n else 0
        avg_pnl_pct = sum(p['overall_pnl_pct'] for p in group) / n * 100 if n else 0
        avg_invested = sum(p['total_invested'] for p in group) / n if n else 0
        total_pnl = sum(p['overall_pnl'] for p in group)
        print(f"  {bucket:<14} {n:>6} {wins:>6} {losses:>6} {win_rate:>7.1f}% {avg_pnl_pct:>9.1f}% ${avg_invested:>12,.2f} ${total_pnl:>12,.2f}")

    # Correlation: compute simple Pearson-like metric
    invested_list = [p['total_invested'] for p in positions]
    pnl_pct_list = [p['overall_pnl_pct'] for p in positions]
    winner_list = [1 if p['is_winner'] else 0 for p in positions]
    n = len(positions)
    if n > 1:
        mean_inv = sum(invested_list) / n
        mean_wr = sum(winner_list) / n
        mean_pnl = sum(pnl_pct_list) / n

        cov_inv_wr = sum((invested_list[i] - mean_inv) * (winner_list[i] - mean_wr) for i in range(n)) / n
        std_inv = (sum((x - mean_inv) ** 2 for x in invested_list) / n) ** 0.5
        std_wr = (sum((x - mean_wr) ** 2 for x in winner_list) / n) ** 0.5

        corr_inv_wr = cov_inv_wr / (std_inv * std_wr) if std_inv * std_wr > 0 else 0

        cov_inv_pnl = sum((invested_list[i] - mean_inv) * (pnl_pct_list[i] - mean_pnl) for i in range(n)) / n
        std_pnl = (sum((x - mean_pnl) ** 2 for x in pnl_pct_list) / n) ** 0.5
        corr_inv_pnl = cov_inv_pnl / (std_inv * std_pnl) if std_inv * std_pnl > 0 else 0

        print(f"\n  Correlation (Invested vs Win): r = {corr_inv_wr:.4f}")
        print(f"  Correlation (Invested vs PnL%): r = {corr_inv_pnl:.4f}")
        if abs(corr_inv_wr) < 0.1:
            print("  -> Weak/no correlation between position size and winning")
        elif corr_inv_wr > 0:
            print("  -> Positive correlation: larger positions tend to win more")
        else:
            print("  -> Negative correlation: larger positions tend to lose more")


def analyze_time_intervals(positions):
    """Q4: 5-min vs 15-min vs hourly performance."""
    print_header("4. TIME INTERVAL ANALYSIS: 5-min vs 15-min vs Hourly")

    interval_order = ["5-min", "15-min", "30-min", "hourly", "multi-hour", "point-in-time", "unknown"]
    buckets = defaultdict(list)
    for p in positions:
        buckets[p['interval_label']].append(p)

    print(f"\n  {'Interval':<16} {'Count':>6} {'Wins':>6} {'Losses':>6} {'WinRate':>8} {'Avg PnL%':>10} {'Med PnL%':>10} {'Total PnL':>14}")
    print("  " + "-" * 90)
    for interval in interval_order:
        group = buckets.get(interval, [])
        if not group:
            continue
        n = len(group)
        wins = sum(1 for p in group if p['is_winner'])
        losses = n - wins
        win_rate = wins / n * 100 if n else 0
        avg_pnl_pct = sum(p['overall_pnl_pct'] for p in group) / n * 100 if n else 0
        median_pnl_pct = sorted([p['overall_pnl_pct'] for p in group])[n // 2] * 100
        total_pnl = sum(p['overall_pnl'] for p in group)
        print(f"  {interval:<16} {n:>6} {wins:>6} {losses:>6} {win_rate:>7.1f}% {avg_pnl_pct:>9.1f}% {median_pnl_pct:>9.1f}% ${total_pnl:>12,.2f}")

    # Direction split per interval
    print_subheader("Interval by Direction")
    for direction in ['Yes/Up', 'No/Down']:
        print(f"\n  Direction: {direction}")
        print(f"  {'Interval':<16} {'Count':>6} {'WinRate':>8} {'Avg PnL%':>10} {'Total PnL':>14}")
        print("  " + "-" * 62)
        for interval in interval_order:
            group = [p for p in buckets.get(interval, []) if p['direction'] == direction]
            if not group:
                continue
            n = len(group)
            wins = sum(1 for p in group if p['is_winner'])
            win_rate = wins / n * 100 if n else 0
            avg_pnl_pct = sum(p['overall_pnl_pct'] for p in group) / n * 100 if n else 0
            total_pnl = sum(p['overall_pnl'] for p in group)
            print(f"  {interval:<16} {n:>6} {win_rate:>7.1f}% {avg_pnl_pct:>9.1f}% ${total_pnl:>12,.2f}")


def analyze_losing_patterns(positions):
    """Q5: What patterns do LOSING BTC trades share?"""
    print_header("5. LOSING TRADE PATTERNS: What Causes Losses")

    losers = [p for p in positions if not p['is_winner']]
    winners = [p for p in positions if p['is_winner']]

    print(f"\n  Total BTC Positions: {len(positions)}")
    print(f"  Winners: {len(winners)} ({len(winners)/len(positions)*100:.1f}%)")
    print(f"  Losers:  {len(losers)} ({len(losers)/len(positions)*100:.1f}%)")
    print(f"  Break-even (PnL=0): {sum(1 for p in positions if p['overall_pnl'] == 0)}")

    # Compare avg entry price
    if losers and winners:
        avg_entry_win = sum(p['avg_price'] for p in winners) / len(winners)
        avg_entry_lose = sum(p['avg_price'] for p in losers) / len(losers)
        print(f"\n  Avg Entry Price - Winners: {avg_entry_win:.4f}")
        print(f"  Avg Entry Price - Losers:  {avg_entry_lose:.4f}")

        # Direction split
        yes_losers = [p for p in losers if p['direction'] == 'Yes/Up']
        no_losers = [p for p in losers if p['direction'] == 'No/Down']
        yes_winners = [p for p in winners if p['direction'] == 'Yes/Up']
        no_winners = [p for p in winners if p['direction'] == 'No/Down']
        print(f"\n  Loser Direction Split:")
        print(f"    Yes/Up losses:  {len(yes_losers)} ({len(yes_losers)/len(losers)*100:.1f}% of all losses)")
        print(f"    No/Down losses: {len(no_losers)} ({len(no_losers)/len(losers)*100:.1f}% of all losses)")
        print(f"  Winner Direction Split:")
        print(f"    Yes/Up wins:    {len(yes_winners)} ({len(yes_winners)/len(winners)*100:.1f}% of all wins)")
        print(f"    No/Down wins:   {len(no_winners)} ({len(no_winners)/len(winners)*100:.1f}% of all wins)")

        # Avg invested
        avg_inv_win = sum(p['total_invested'] for p in winners) / len(winners)
        avg_inv_lose = sum(p['total_invested'] for p in losers) / len(losers)
        print(f"\n  Avg Total Invested - Winners: ${avg_inv_win:,.2f}")
        print(f"  Avg Total Invested - Losers:  ${avg_inv_lose:,.2f}")

        # Interval split for losers
        print_subheader("Loser Interval Distribution")
        interval_counts = defaultdict(int)
        for p in losers:
            interval_counts[p['interval_label']] += 1
        for k, v in sorted(interval_counts.items(), key=lambda x: -x[1]):
            pct = v / len(losers) * 100
            print(f"    {k}: {v} ({pct:.1f}%)")

        # Biggest losers
        print_subheader("Top 15 Biggest BTC Losses (by $ PnL)")
        sorted_losers = sorted(losers, key=lambda p: p['overall_pnl'])[:15]
        for i, p in enumerate(sorted_losers, 1):
            print(f"  {i:>2}. [{p['trader']}] PnL=${p['overall_pnl']:,.2f} ({p['overall_pnl_pct']*100:.1f}%) "
                  f"| Entry={p['avg_price']:.3f} | Dir={p['direction']} | Int={p['interval_label']} "
                  f"| Invested=${p['total_invested']:,.2f}")

        # High entry price losers
        print_subheader("Losers with Entry Price > 0.65 (bought expensive)")
        expensive_losers = [p for p in losers if p['avg_price'] > 0.65]
        print(f"  Count: {len(expensive_losers)} out of {len(losers)} total losers ({len(expensive_losers)/len(losers)*100:.1f}%)")
        if expensive_losers:
            avg_loss = sum(p['overall_pnl_pct'] for p in expensive_losers) / len(expensive_losers) * 100
            print(f"  Avg PnL%: {avg_loss:.1f}%")
            dir_counts = defaultdict(int)
            for p in expensive_losers:
                dir_counts[p['direction']] += 1
            for d, c in dir_counts.items():
                print(f"    {d}: {c}")


def analyze_winning_patterns(positions):
    """Q6: What patterns do WINNING BTC trades share?"""
    print_header("6. WINNING TRADE PATTERNS: What Makes Winners")

    winners = [p for p in positions if p['is_winner']]
    if not winners:
        print("  No winning positions found.")
        return

    # Direction
    yes_winners = [p for p in winners if p['direction'] == 'Yes/Up']
    no_winners = [p for p in winners if p['direction'] == 'No/Down']
    print(f"\n  Total Winners: {len(winners)}")
    print(f"    Yes/Up wins:  {len(yes_winners)} ({len(yes_winners)/len(winners)*100:.1f}%)")
    print(f"    No/Down wins: {len(no_winners)} ({len(no_winners)/len(winners)*100:.1f}%)")

    # Top winners
    print_subheader("Top 15 Biggest BTC Wins (by $ PnL)")
    sorted_winners = sorted(winners, key=lambda p: -p['overall_pnl'])[:15]
    for i, p in enumerate(sorted_winners, 1):
        print(f"  {i:>2}. [{p['trader']}] PnL=${p['overall_pnl']:,.2f} ({p['overall_pnl_pct']*100:.1f}%) "
              f"| Entry={p['avg_price']:.3f} | Dir={p['direction']} | Int={p['interval_label']} "
              f"| Invested=${p['total_invested']:,.2f}")

    # Cheap entries that won big
    print_subheader("Winners with Entry Price < 0.40 (bought cheap)")
    cheap_winners = [p for p in winners if p['avg_price'] < 0.40]
    print(f"  Count: {len(cheap_winners)} out of {len(winners)} total winners ({len(cheap_winners)/len(winners)*100:.1f}%)")
    if cheap_winners:
        avg_pnl = sum(p['overall_pnl_pct'] for p in cheap_winners) / len(cheap_winners) * 100
        print(f"  Avg PnL%: {avg_pnl:.1f}%")
        avg_inv = sum(p['total_invested'] for p in cheap_winners) / len(cheap_winners)
        print(f"  Avg Invested: ${avg_inv:,.2f}")

    # No/Down winners detail
    print_subheader("No/Down Winners Analysis")
    if no_winners:
        avg_entry = sum(p['avg_price'] for p in no_winners) / len(no_winners)
        avg_pnl_pct = sum(p['overall_pnl_pct'] for p in no_winners) / len(no_winners) * 100
        avg_invested = sum(p['total_invested'] for p in no_winners) / len(no_winners)
        print(f"  Count: {len(no_winners)}")
        print(f"  Avg Entry Price: {avg_entry:.4f}")
        print(f"  Avg PnL%: {avg_pnl_pct:.1f}%")
        print(f"  Avg Invested: ${avg_invested:,.2f}")


def analyze_trade_history(all_trades):
    """Additional analysis from trade history data."""
    print_header("7. TRADE HISTORY ANALYSIS (Individual Trade Level)")

    if not all_trades:
        print("  No trade history data found.")
        return

    # Buy vs Sell distribution
    buys = [t for t in all_trades if t['side'] == 'Buy']
    sells = [t for t in all_trades if t['side'] == 'Sell']
    print(f"\n  Total BTC trades in history: {len(all_trades)}")
    print(f"  Buys: {len(buys)} ({len(buys)/len(all_trades)*100:.1f}%)")
    print(f"  Sells: {len(sells)} ({len(sells)/len(all_trades)*100:.1f}%)")

    # Price distribution of buys
    if buys:
        buy_prices = [t['price'] for t in buys]
        buy_prices.sort()
        avg_buy_price = sum(buy_prices) / len(buy_prices)
        median_buy_price = buy_prices[len(buy_prices) // 2]
        print(f"\n  Buy Price Stats:")
        print(f"    Min: {min(buy_prices):.4f}  |  Max: {max(buy_prices):.4f}")
        print(f"    Avg: {avg_buy_price:.4f}  |  Median: {median_buy_price:.4f}")

    if sells:
        sell_prices = [t['price'] for t in sells]
        sell_prices.sort()
        avg_sell_price = sum(sell_prices) / len(sell_prices)
        median_sell_price = sell_prices[len(sell_prices) // 2]
        print(f"\n  Sell Price Stats:")
        print(f"    Min: {min(sell_prices):.4f}  |  Max: {max(sell_prices):.4f}")
        print(f"    Avg: {avg_sell_price:.4f}  |  Median: {median_sell_price:.4f}")

    # Direction in trade history
    print_subheader("Trade History by Direction")
    for direction in ['Yes/Up', 'No/Down']:
        group = [t for t in all_trades if t['direction'] == direction]
        if not group:
            continue
        buy_group = [t for t in group if t['side'] == 'Buy']
        sell_group = [t for t in group if t['side'] == 'Sell']
        total_buy_usd = sum(t['usd_amount'] for t in buy_group)
        total_sell_usd = sum(t['usd_amount'] for t in sell_group)
        print(f"\n  {direction}:")
        print(f"    Buys: {len(buy_group)} trades, ${total_buy_usd:,.2f} total")
        print(f"    Sells: {len(sell_group)} trades, ${total_sell_usd:,.2f} total")

    # Whale vs small trades
    whales = [t for t in all_trades if t['is_whale']]
    smalls = [t for t in all_trades if t['is_small']]
    print(f"\n  Whale trades (big): {len(whales)}")
    print(f"  Small trades: {len(smalls)}")
    print(f"  Normal trades: {len(all_trades) - len(whales) - len(smalls)}")

    if whales:
        avg_whale_usd = sum(t['usd_amount'] for t in whales) / len(whales)
        print(f"  Avg whale trade size: ${avg_whale_usd:,.2f}")
    if smalls:
        avg_small_usd = sum(t['usd_amount'] for t in smalls) / len(smalls)
        print(f"  Avg small trade size: ${avg_small_usd:,.2f}")

    # Per-trader summary
    print_subheader("Per-Trader Trade History Summary")
    traders = sorted(set(t['trader'] for t in all_trades))
    for trader in traders:
        tt = [t for t in all_trades if t['trader'] == trader]
        buys_t = [t for t in tt if t['side'] == 'Buy']
        sells_t = [t for t in tt if t['side'] == 'Sell']
        total_usd = sum(t['usd_amount'] for t in tt)
        up_trades = [t for t in tt if t['direction'] == 'Yes/Up']
        down_trades = [t for t in tt if t['direction'] == 'No/Down']
        print(f"\n  {trader}: {len(tt)} BTC trades, ${total_usd:,.2f} volume")
        print(f"    Buy: {len(buys_t)} | Sell: {len(sells_t)}")
        print(f"    Up/Yes: {len(up_trades)} | Down/No: {len(down_trades)}")


def per_trader_summary(positions):
    """Summary table per trader."""
    print_header("8. PER-TRADER SUMMARY")

    traders = sorted(set(p['trader'] for p in positions))

    print(f"\n  {'Trader':<10} {'BTC Pos':>8} {'Wins':>6} {'Losses':>6} {'WR%':>6} {'Total PnL':>14} {'Avg PnL%':>10} {'Avg Entry':>10} {'Avg Inv':>12}")
    print("  " + "-" * 92)

    for trader in traders:
        tp = [p for p in positions if p['trader'] == trader]
        n = len(tp)
        wins = sum(1 for p in tp if p['is_winner'])
        losses = n - wins
        wr = wins / n * 100 if n else 0
        total_pnl = sum(p['overall_pnl'] for p in tp)
        avg_pnl_pct = sum(p['overall_pnl_pct'] for p in tp) / n * 100 if n else 0
        avg_entry = sum(p['avg_price'] for p in tp) / n if n else 0
        avg_inv = sum(p['total_invested'] for p in tp) / n if n else 0
        print(f"  {trader:<10} {n:>8} {wins:>6} {losses:>6} {wr:>5.1f}% ${total_pnl:>12,.2f} {avg_pnl_pct:>9.1f}% {avg_entry:>9.4f} ${avg_inv:>10,.2f}")


def synthesize_findings(positions, all_trades):
    """Final synthesis answering all key questions."""
    print_header("9. SYNTHESIZED FINDINGS & KEY TAKEAWAYS")

    all_pos = positions
    yes_pos = [p for p in all_pos if p['direction'] == 'Yes/Up']
    no_pos = [p for p in all_pos if p['direction'] == 'No/Down']
    winners = [p for p in all_pos if p['is_winner']]
    losers = [p for p in all_pos if not p['is_winner']]

    # Q1: No/Down vs Yes/Up
    print_subheader("Q1: Are No/Down bets more profitable than Yes/Up?")
    if yes_pos:
        yes_wr = sum(1 for p in yes_pos if p['is_winner']) / len(yes_pos) * 100
        yes_avg_pnl = sum(p['overall_pnl_pct'] for p in yes_pos) / len(yes_pos) * 100
        yes_total = sum(p['overall_pnl'] for p in yes_pos)
    else:
        yes_wr = yes_avg_pnl = yes_total = 0
    if no_pos:
        no_wr = sum(1 for p in no_pos if p['is_winner']) / len(no_pos) * 100
        no_avg_pnl = sum(p['overall_pnl_pct'] for p in no_pos) / len(no_pos) * 100
        no_total = sum(p['overall_pnl'] for p in no_pos)
    else:
        no_wr = no_avg_pnl = no_total = 0

    print(f"  Yes/Up:  WR={yes_wr:.1f}%, Avg PnL%={yes_avg_pnl:.1f}%, Total PnL=${yes_total:,.2f} ({len(yes_pos)} positions)")
    print(f"  No/Down: WR={no_wr:.1f}%, Avg PnL%={no_avg_pnl:.1f}%, Total PnL=${no_total:,.2f} ({len(no_pos)} positions)")
    if no_wr > yes_wr:
        print(f"  ANSWER: YES - No/Down has higher win rate ({no_wr:.1f}% vs {yes_wr:.1f}%)")
    else:
        print(f"  ANSWER: NO - Yes/Up has higher win rate ({yes_wr:.1f}% vs {no_wr:.1f}%)")
    if no_avg_pnl > yes_avg_pnl:
        print(f"  No/Down has higher avg PnL% ({no_avg_pnl:.1f}% vs {yes_avg_pnl:.1f}%)")
    else:
        print(f"  Yes/Up has higher avg PnL% ({yes_avg_pnl:.1f}% vs {no_avg_pnl:.1f}%)")

    # Q2: Profitable vs unprofitable price ranges
    print_subheader("Q2: Profitable vs Unprofitable Entry Price Ranges")
    bucket_order = ["<0.10", "0.10-0.20", "0.20-0.30", "0.30-0.40", "0.40-0.50",
                     "0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00"]
    buckets = defaultdict(list)
    for p in all_pos:
        buckets[p['price_bucket']].append(p)

    profitable_buckets = []
    unprofitable_buckets = []
    for bucket in bucket_order:
        group = buckets.get(bucket, [])
        if len(group) < 3:
            continue
        avg_pnl = sum(p['overall_pnl_pct'] for p in group) / len(group) * 100
        wr = sum(1 for p in group if p['is_winner']) / len(group) * 100
        if avg_pnl > 0:
            profitable_buckets.append((bucket, wr, avg_pnl, len(group)))
        else:
            unprofitable_buckets.append((bucket, wr, avg_pnl, len(group)))

    print("  PROFITABLE ranges:")
    for b, wr, pnl, n in profitable_buckets:
        print(f"    {b}: WR={wr:.1f}%, Avg PnL%={pnl:.1f}% (n={n})")
    print("  UNPROFITABLE ranges:")
    for b, wr, pnl, n in unprofitable_buckets:
        print(f"    {b}: WR={wr:.1f}%, Avg PnL%={pnl:.1f}% (n={n})")

    # Q3: Position sizing
    print_subheader("Q3: Does Position Sizing Correlate with Win Rate?")
    invested_list = [p['total_invested'] for p in all_pos]
    winner_list = [1 if p['is_winner'] else 0 for p in all_pos]
    n = len(all_pos)
    if n > 1:
        mean_inv = sum(invested_list) / n
        mean_wr = sum(winner_list) / n
        cov = sum((invested_list[i] - mean_inv) * (winner_list[i] - mean_wr) for i in range(n)) / n
        std_inv = (sum((x - mean_inv) ** 2 for x in invested_list) / n) ** 0.5
        std_wr = (sum((x - mean_wr) ** 2 for x in winner_list) / n) ** 0.5
        corr = cov / (std_inv * std_wr) if std_inv * std_wr > 0 else 0
        print(f"  Correlation (Invested vs Win): r = {corr:.4f}")
        if abs(corr) < 0.1:
            print("  ANSWER: Weak/negligible correlation. Position size does not strongly predict wins.")
        elif corr > 0:
            print("  ANSWER: Positive correlation. Larger positions slightly more likely to win.")
        else:
            print("  ANSWER: Negative correlation. Larger positions slightly more likely to lose.")

    # Winner avg inv vs loser avg inv
    if winners and losers:
        avg_inv_w = sum(p['total_invested'] for p in winners) / len(winners)
        avg_inv_l = sum(p['total_invested'] for p in losers) / len(losers)
        print(f"  Avg Invested (Winners): ${avg_inv_w:,.2f}")
        print(f"  Avg Invested (Losers):  ${avg_inv_l:,.2f}")

    # Q4: 5-min vs 15-min
    print_subheader("Q4: Are 5-min Intervals Worse than 15-min?")
    five_min = [p for p in all_pos if p['interval_label'] == '5-min']
    fifteen_min = [p for p in all_pos if p['interval_label'] == '15-min']
    for label, group in [("5-min", five_min), ("15-min", fifteen_min)]:
        if group:
            wr = sum(1 for p in group if p['is_winner']) / len(group) * 100
            avg_pnl = sum(p['overall_pnl_pct'] for p in group) / len(group) * 100
            total_pnl = sum(p['overall_pnl'] for p in group)
            print(f"  {label}: WR={wr:.1f}%, Avg PnL%={avg_pnl:.1f}%, Total PnL=${total_pnl:,.2f} (n={len(group)})")
        else:
            print(f"  {label}: No data")

    if five_min and fifteen_min:
        wr5 = sum(1 for p in five_min if p['is_winner']) / len(five_min) * 100
        wr15 = sum(1 for p in fifteen_min if p['is_winner']) / len(fifteen_min) * 100
        pnl5 = sum(p['overall_pnl_pct'] for p in five_min) / len(five_min) * 100
        pnl15 = sum(p['overall_pnl_pct'] for p in fifteen_min) / len(fifteen_min) * 100
        if wr5 < wr15 and pnl5 < pnl15:
            print(f"  ANSWER: YES - 5-min is worse (WR: {wr5:.1f}% vs {wr15:.1f}%, PnL%: {pnl5:.1f}% vs {pnl15:.1f}%)")
        elif wr5 > wr15 and pnl5 > pnl15:
            print(f"  ANSWER: NO - 5-min is actually better (WR: {wr5:.1f}% vs {wr15:.1f}%, PnL%: {pnl5:.1f}% vs {pnl15:.1f}%)")
        else:
            print(f"  ANSWER: MIXED - 5-min WR={wr5:.1f}% vs 15-min WR={wr15:.1f}%; 5-min PnL%={pnl5:.1f}% vs 15-min PnL%={pnl15:.1f}%")

    # Q5 & Q6 summary
    print_subheader("Q5: Common Losing Trade Patterns")
    if losers:
        # Direction breakdown of losers
        yes_lose = [p for p in losers if p['direction'] == 'Yes/Up']
        no_lose = [p for p in losers if p['direction'] == 'No/Down']
        yes_pct = len(yes_lose) / len(losers) * 100
        no_pct = len(no_lose) / len(losers) * 100

        # Avg entry of losers
        avg_entry_lose = sum(p['avg_price'] for p in losers) / len(losers)
        avg_entry_win = sum(p['avg_price'] for p in winners) / len(winners) if winners else 0

        # Interval of losers
        interval_counts = defaultdict(int)
        for p in losers:
            interval_counts[p['interval_label']] += 1
        top_intervals = sorted(interval_counts.items(), key=lambda x: -x[1])

        print(f"  - {yes_pct:.0f}% of losses are Yes/Up bets, {no_pct:.0f}% are No/Down")
        print(f"  - Losers avg entry price: {avg_entry_lose:.4f} vs winners: {avg_entry_win:.4f}")
        print(f"  - Most common losing interval: {top_intervals[0][0]} ({top_intervals[0][1]} losses)")

        # High-entry losers
        expensive_losers = [p for p in losers if p['avg_price'] > 0.65]
        if expensive_losers:
            print(f"  - {len(expensive_losers)} losses ({len(expensive_losers)/len(losers)*100:.0f}%) had entry > 0.65 (bought expensive)")

        # Biggest single-position losses
        worst = sorted(losers, key=lambda p: p['overall_pnl'])[:5]
        print("  - Top 5 worst losses:")
        for p in worst:
            print(f"      [{p['trader']}] ${p['overall_pnl']:,.2f} | Entry={p['avg_price']:.3f} | {p['direction']} | {p['interval_label']}")

    print_subheader("Q6: Common Winning Trade Patterns")
    if winners:
        yes_win = [p for p in winners if p['direction'] == 'Yes/Up']
        no_win = [p for p in winners if p['direction'] == 'No/Down']
        yes_pct = len(yes_win) / len(winners) * 100
        no_pct = len(no_win) / len(winners) * 100

        avg_entry_win = sum(p['avg_price'] for p in winners) / len(winners)

        interval_counts = defaultdict(int)
        for p in winners:
            interval_counts[p['interval_label']] += 1
        top_intervals = sorted(interval_counts.items(), key=lambda x: -x[1])

        print(f"  - {yes_pct:.0f}% of wins are Yes/Up bets, {no_pct:.0f}% are No/Down")
        print(f"  - Winners avg entry price: {avg_entry_win:.4f}")
        print(f"  - Most common winning interval: {top_intervals[0][0]} ({top_intervals[0][1]} wins)")

        # Cheap winners
        cheap_winners = [p for p in winners if p['avg_price'] < 0.40]
        if cheap_winners:
            avg_ret = sum(p['overall_pnl_pct'] for p in cheap_winners) / len(cheap_winners) * 100
            print(f"  - {len(cheap_winners)} wins ({len(cheap_winners)/len(winners)*100:.0f}%) bought cheap (<0.40), avg return {avg_ret:.1f}%")

        best = sorted(winners, key=lambda p: -p['overall_pnl'])[:5]
        print("  - Top 5 best wins:")
        for p in best:
            print(f"      [{p['trader']}] ${p['overall_pnl']:,.2f} | Entry={p['avg_price']:.3f} | {p['direction']} | {p['interval_label']}")

    # Final bot recommendations
    print_header("10. BOT STRATEGY RECOMMENDATIONS")
    print("""
  Based on cross-trader BTC analysis:

  1. DIRECTION BIAS:
     - Track the win rate and avg PnL% for Yes/Up vs No/Down from the data above.
     - If No/Down shows higher win rate AND higher avg PnL%, favor No/Down positions.
     - The data from Guy 3 and Guy 4 suggests No/Down can be more profitable,
       but this needs to be validated against the full cross-trader dataset.

  2. ENTRY PRICE SWEET SPOTS:
     - Avoid buying at prices > 0.70 (expensive entries lose more often).
     - Look for entries in the 0.40-0.60 range which tend to have reasonable win rates.
     - Very cheap entries (<0.20) can have massive returns but very low win rates.

  3. POSITION SIZING:
     - Use the correlation data above to calibrate. If correlation is near-zero,
       size shouldn't affect win probability, so focus on risk management.
     - Do NOT scale up on losing patterns.

  4. TIME INTERVALS:
     - Compare 5-min vs 15-min performance above.
     - If 15-min shows better risk/reward, prefer it.
     - Multi-hour markets may have different dynamics (less noise).

  5. LOSS AVOIDANCE:
     - The biggest losses come from buying expensive (>0.65) and being wrong.
     - When entry price is high, use smaller position sizes.
     - Consider exiting early if price moves against you quickly.

  6. WIN MAXIMIZATION:
     - The biggest wins come from buying cheap (<0.45) when the bet resolves correctly.
     - No/Down bets at moderate prices (0.40-0.60) can generate consistent returns.
     - Multiple small wins compound better than swinging for home runs.
""")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 80)
    print("  CROSS-TRADER BTC ANALYSIS FOR POLYMARKET")
    print("  Analyzing 5 traders' BTC positions and trade history")
    print("=" * 80)

    # Load positions
    all_positions = []
    for trader, filepath in POSITIONS_FILES.items():
        positions = load_positions(filepath, trader)
        print(f"  Loaded {len(positions)} BTC positions from {trader}")
        all_positions.extend(positions)
    print(f"\n  Total BTC positions across all traders: {len(all_positions)}")

    # Load trade history
    all_trades = []
    for trader, filepath in TRADE_HISTORY_FILES.items():
        trades = load_trade_history(filepath, trader)
        print(f"  Loaded {len(trades)} BTC trades from {trader}")
        all_trades.extend(trades)
    print(f"  Total BTC trades in history: {len(all_trades)}")

    # Run all analyses
    per_trader_summary(all_positions)
    analyze_direction_performance(all_positions)
    analyze_price_buckets(all_positions)
    analyze_position_sizing(all_positions)
    analyze_time_intervals(all_positions)
    analyze_losing_patterns(all_positions)
    analyze_winning_patterns(all_positions)
    analyze_trade_history(all_trades)
    synthesize_findings(all_positions, all_trades)


if __name__ == "__main__":
    main()
