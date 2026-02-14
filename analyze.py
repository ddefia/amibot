"""CLI tool to analyze trader data and extract strategy insights.

Handles CSVs, PDFs, screenshots, JSON, Excel, and text files.

Usage:
    python analyze.py /path/to/polymarket-bot-data/
    python analyze.py /path/to/polymarket-bot-data/ --report output.txt
    python analyze.py /path/to/polymarket-bot-data/ --trader "trader_1"
"""

import sys
import argparse
import logging

from analysis.trader_analyzer import TraderAnalyzer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    parser = argparse.ArgumentParser(
        description="Analyze Polymarket trader data (CSVs, PDFs, screenshots, etc.)"
    )
    parser.add_argument(
        "data_dir",
        help="Path to the folder containing trader subfolders",
    )
    parser.add_argument(
        "--report", "-r",
        help="Save the full report to a text file",
    )
    parser.add_argument(
        "--trader", "-t",
        help="Analyze a specific trader folder only",
    )
    parser.add_argument(
        "--text", action="store_true",
        help="Print all extracted text (from PDFs, OCR, notes)",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress info-level logging",
    )

    args = parser.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    analyzer = TraderAnalyzer(args.data_dir)
    traders = analyzer.load_all()

    if not traders:
        print(f"\nNo trader data found in {args.data_dir}")
        print("\nExpected structure (any mix of file types works):")
        print(f"  {args.data_dir}/")
        print(f"    trader_1/")
        print(f"      trades.csv")
        print(f"      account_statement.pdf")
        print(f"      pnl_screenshot.png")
        print(f"    trader_2/")
        print(f"      ...")
        sys.exit(1)

    # File inventory summary
    print("\n" + "=" * 70)
    print("  FILE INVENTORY")
    print("=" * 70)
    for name in traders:
        inv = analyzer.get_inventory(name)
        print(f"\n  {name}:")
        for ftype, count in inv.items():
            if count > 0 and ftype != "total":
                print(f"    {ftype:>8}: {count} files")
        print(f"    {'total':>8}: {inv.get('total', 0)} files")

    # Single trader mode
    if args.trader:
        if args.trader not in traders:
            print(f"\nTrader '{args.trader}' not found. Available: {list(traders.keys())}")
            sys.exit(1)
        analysis = analyzer.analyze_trader(args.trader)
        strategy = analyzer.classify_strategy(args.trader)
        params = analyzer.extract_strategy_params(args.trader)

        print(f"\n{'=' * 70}")
        print(f"  DETAILED ANALYSIS: {args.trader}")
        print(f"{'=' * 70}")
        _print_analysis(analysis, strategy, params)

        if args.text:
            text = analyzer.get_raw_text(args.trader)
            if text:
                print(f"\n{'─' * 60}")
                print(f"  EXTRACTED TEXT")
                print(f"{'─' * 60}")
                print(text[:5000])
        return

    # Full report
    report = analyzer.full_report()
    print(report)

    # Save report if requested
    if args.report:
        with open(args.report, "w") as f:
            f.write(report)
        print(f"\nReport saved to {args.report}")

    # Print extracted text if requested
    if args.text:
        print(f"\n{'=' * 70}")
        print("  ALL EXTRACTED TEXT (PDFs, Screenshots, Notes)")
        print(f"{'=' * 70}")
        for name in traders:
            text = analyzer.get_raw_text(name)
            if text:
                print(f"\n{'─' * 40} {name} {'─' * 40}")
                print(text[:3000])
                if len(text) > 3000:
                    print(f"  ... ({len(text) - 3000} more chars)")

    # Final recommendation
    print(f"\n{'=' * 70}")
    print("  RECOMMENDED BOT CONFIGURATION")
    print(f"{'=' * 70}")

    best_traders = sorted(
        [n for n in traders if analyzer.analyze_trader(n).get("win_rate", 0) > 0.5],
        key=lambda n: analyzer.analyze_trader(n).get("win_rate", 0),
        reverse=True,
    )

    if best_traders:
        best = best_traders[0]
        params = analyzer.extract_strategy_params(best)
        print(f"\n  Based on top trader '{best}':")
        print(f"\n  .env settings:")
        print(f"    STRATEGY={params.get('strategy_type', 'latency_arb')}")
        print(f"    MAX_POSITION_SIZE={params.get('suggested_position_size', 100)}")
        print(f"    MIN_EDGE_THRESHOLD=0.05")
        print(f"    MAKER_ONLY=true")
        print(f"\n  Confidence: {params.get('confidence_level', 'unknown')}")
        print(f"  Kelly Fraction: {params.get('kelly_fraction', 0.10)}")
    else:
        print("\n  No clearly profitable traders found — using default conservative config")


def _print_analysis(analysis, strategy, params):
    """Pretty-print a single trader analysis."""
    print(f"  Strategy: {strategy}")
    print(f"  Data Source: {analysis.get('data_source', 'unknown')}")
    print(f"  Total Trades: {analysis.get('total_trades', 'N/A')}")

    for key in ["win_rate", "total_pnl", "avg_win", "avg_loss", "profit_factor",
                 "sharpe", "max_win_streak", "max_loss_streak", "avg_size",
                 "median_size", "avg_entry_price", "total_fees"]:
        val = analysis.get(key)
        if val is not None:
            if key == "win_rate":
                print(f"  {key}: {val:.1%}")
            elif isinstance(val, float):
                print(f"  {key}: ${val:.2f}" if "pnl" in key or "fee" in key or "win" in key.lower() or "loss" in key.lower() else f"  {key}: {val:.3f}")
            else:
                print(f"  {key}: {val}")

    if "entry_price_distribution" in analysis:
        print(f"\n  Entry Price Distribution:")
        for bucket, pct in analysis["entry_price_distribution"].items():
            bar = "#" * int(pct * 40)
            print(f"    {bucket:>8}: {pct:5.1%} {bar}")

    if "entry_timing_distribution" in analysis:
        print(f"\n  Entry Timing (within 5-min interval):")
        for window, pct in analysis["entry_timing_distribution"].items():
            bar = "#" * int(pct * 40)
            print(f"    {window:>10}: {pct:5.1%} {bar}")

    print(f"\n  Recommended Bot Params:")
    for k, v in params.items():
        print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
