"""Entry point for the Polymarket BTC trading bot.

Usage:
    python run.py              # Live trading (requires PRIVATE_KEY in .env)
    DRY_RUN=true python run.py # Paper trading (no real orders)
"""

import asyncio
import logging
import signal
import sys

from bot.config import Config
from bot.engine import BotEngine


def setup_logging(config: Config):
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]

    if config.log_file:
        handlers.append(logging.FileHandler(config.log_file))

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format=fmt,
        handlers=handlers,
    )


def main():
    config = Config()

    # Validate configuration
    errors = config.validate()
    if errors:
        print("Configuration errors:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    setup_logging(config)

    engine = BotEngine(config)

    # Graceful shutdown on Ctrl+C
    def shutdown(sig, frame):
        print("\nShutting down...")
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    mode = "DRY RUN" if config.dry_run else "LIVE"
    print("=" * 60)
    print(f"  Polymarket BTC Latency Arb Bot [{mode}]")
    print(f"  Strategy: Guy 1 SELL-side ({config.strategy})")
    print(f"  Position Size: ${config.position_usd:,.0f}")
    print(f"  Max Exposure: ${config.max_exposure_usdc:,.0f}")
    print(f"  Min Edge: {config.min_edge_threshold:.1%}")
    print(f"  Confidence: {config.confidence_threshold:.1%}")
    print(f"  Prefer 15-min: {config.prefer_15min}")
    print(f"  Max Positions/Interval: {config.max_positions_per_interval}")
    print(f"  Daily Loss Limit: ${config.daily_loss_limit_usdc:,.0f}")
    print(f"  Trade Log: {config.trade_log_file}")
    print("=" * 60)

    if config.dry_run:
        print("  PAPER TRADING — real data, simulated orders")
        print(f"  Paper Balance: ${config.paper_balance:,.0f}")
        print(f"  Fill Delay: {config.paper_fill_delay_min:.1f}-{config.paper_fill_delay_max:.1f}s")
        print(f"  Fill Rate: {config.paper_fill_rate:.0%} (liquidity sim)")
        print("  Orders: open → fill/expire (real lifecycle)")
        print("  Set DRY_RUN=false in .env for live trading")
        print("=" * 60)

    asyncio.run(engine.run())


if __name__ == "__main__":
    main()
