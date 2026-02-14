"""CLI tool to analyze trader data and extract strategy insights.

Usage:
    python analyze.py /path/to/polymarket-bot-data/

The data directory should contain subfolders for each trader with CSV/JSON files.
"""

import sys
import json
import logging

from analysis.trader_analyzer import TraderAnalyzer

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def main():
    if len(sys.argv) < 2:
        print("Usage: python analyze.py <data_directory>")
        print()
        print("Example:")
        print("  python analyze.py ~/Downloads/polymarket-bot/")
        print()
        print("The data directory should contain subfolders for each trader")
        print("with CSV or JSON trade data files.")
        sys.exit(1)

    data_dir = sys.argv[1]
    analyzer = TraderAnalyzer(data_dir)
    traders = analyzer.load_all()

    if not traders:
        print(f"No trader data found in {data_dir}")
        print("Expected structure:")
        print(f"  {data_dir}/")
        print(f"    trader_1/")
        print(f"      trades.csv")
        print(f"    trader_2/")
        print(f"      trades.csv")
        sys.exit(1)

    print("=" * 70)
    print("  POLYMARKET TRADER ANALYSIS REPORT")
    print("=" * 70)

    # Individual analysis
    for name in traders:
        print(f"\n{'─' * 60}")
        print(f"  Trader: {name}")
        print(f"{'─' * 60}")

        analysis = analyzer.analyze_trader(name)
        strategy = analyzer.classify_strategy(name)
        params = analyzer.extract_strategy_params(name)

        print(f"  Strategy Classification: {strategy}")
        print(f"  Total Trades: {analysis.get('total_trades', 'N/A')}")
        print(f"  Win Rate: {analysis.get('win_rate', 0):.1%}")
        print(f"  Total P&L: ${analysis.get('total_pnl', 0):.2f}")
        print(f"  Avg Win: ${analysis.get('avg_win', 0):.2f}")
        print(f"  Avg Loss: ${analysis.get('avg_loss', 0):.2f}")
        print(f"  Profit Factor: {analysis.get('profit_factor', 0):.2f}")
        print(f"  Sharpe Ratio: {analysis.get('sharpe', 0):.3f}")
        print(f"  Avg Position Size: {analysis.get('avg_size', 'N/A')}")
        print(f"  Avg Entry Price: ${analysis.get('avg_entry_price', 0):.3f}")
        print(f"  % Entries < $0.35: {analysis.get('pct_entries_below_35c', 0):.1%}")

        timing = analysis.get("entry_timing_distribution")
        if timing:
            print(f"  Entry Timing (within 5-min interval):")
            print(f"    0-60s:   {timing.get('first_60s', 0):.1%}")
            print(f"    60-120s: {timing.get('60_120s', 0):.1%}")
            print(f"    120-180s:{timing.get('120_180s', 0):.1%}")
            print(f"    180-240s:{timing.get('180_240s', 0):.1%}")
            print(f"    240-300s:{timing.get('last_60s', 0):.1%}")

        print(f"\n  Suggested Bot Parameters:")
        for k, v in params.items():
            print(f"    {k}: {v}")

    # Comparison table
    print(f"\n{'=' * 70}")
    print("  TRADER COMPARISON")
    print(f"{'=' * 70}")

    comparison = analyzer.compare_traders()
    if not comparison.empty:
        cols = ["name", "total_trades", "win_rate", "total_pnl", "profit_factor", "avg_size"]
        available = [c for c in cols if c in comparison.columns]
        print(comparison[available].to_string(index=False))

    # Best trader's parameters
    print(f"\n{'=' * 70}")
    print("  RECOMMENDED BOT CONFIGURATION")
    print(f"{'=' * 70}")

    best_traders = [
        name for name in traders
        if analyzer.analyze_trader(name).get("win_rate", 0) > 0.6
    ]

    if best_traders:
        best = best_traders[0]
        params = analyzer.extract_strategy_params(best)
        print(f"\n  Based on top trader '{best}':")
        print(f"  Copy these to your .env file:\n")
        print(f"  STRATEGY={params.get('strategy_type', 'latency_arb')}")
        print(f"  MAX_POSITION_SIZE={params.get('suggested_position_size', 100)}")
        print(f"  MIN_EDGE_THRESHOLD=0.05")
        print(f"  MAKER_ONLY=true")
    else:
        print("\n  No profitable traders found — using default config")


if __name__ == "__main__":
    main()
