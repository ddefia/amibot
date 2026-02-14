"""Entry point for the Polymarket BTC trading bot.

Auto-restarts on crash with exponential backoff.

Usage:
    python run.py              # Live trading (requires PRIVATE_KEY in .env)
    DRY_RUN=true python run.py # Paper trading (no real orders)
"""

import asyncio
import logging
import signal
import sys
import time
import traceback

from bot.config import Config
from bot.engine import BotEngine

logger = logging.getLogger("bot.runner")

MAX_RESTARTS = 50  # Max total restarts before giving up
RESTART_DELAY_BASE = 5  # Initial restart delay (seconds)
RESTART_DELAY_MAX = 30  # Max restart delay (seconds)


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
    # Quiet down noisy HTTP request logging from httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def print_banner(config: Config):
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
    print_banner(config)

    # Graceful shutdown flag
    shutdown_requested = False

    def shutdown(sig, frame):
        nonlocal shutdown_requested
        shutdown_requested = True
        print("\nShutdown signal received...")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Auto-restart loop
    restart_count = 0
    consecutive_fast_crashes = 0

    while restart_count < MAX_RESTARTS and not shutdown_requested:
        start_time = time.time()

        try:
            engine = BotEngine(config)

            if restart_count > 0:
                logger.info("=== RESTARTING (attempt %d) ===", restart_count + 1)

            asyncio.run(engine.run())

        except SystemExit:
            break
        except KeyboardInterrupt:
            break
        except Exception:
            elapsed = time.time() - start_time
            restart_count += 1

            logger.error(
                "Bot crashed after %.0fs (restart %d/%d):\n%s",
                elapsed, restart_count, MAX_RESTARTS, traceback.format_exc(),
            )

            # Track fast crashes (< 30s) — if too many, increase delay
            if elapsed < 30:
                consecutive_fast_crashes += 1
            else:
                consecutive_fast_crashes = 0

            if consecutive_fast_crashes >= 5:
                logger.error(
                    "5 fast crashes in a row — waiting 60s before retry"
                )
                time.sleep(60)
                consecutive_fast_crashes = 0
                continue

            # Exponential backoff: 5s, 10s, 15s, ... max 30s
            delay = min(RESTART_DELAY_BASE * restart_count, RESTART_DELAY_MAX)
            logger.info("Restarting in %ds...", delay)
            time.sleep(delay)
            continue

        # Clean exit from asyncio.run — all tasks completed or returned
        # This shouldn't happen normally, but if it does, restart
        elapsed = time.time() - start_time
        restart_count += 1
        logger.warning(
            "Bot exited cleanly after %.0fs — restarting (attempt %d/%d)",
            elapsed, restart_count, MAX_RESTARTS,
        )
        time.sleep(RESTART_DELAY_BASE)

    if restart_count >= MAX_RESTARTS:
        logger.error("Max restarts (%d) reached — giving up", MAX_RESTARTS)


if __name__ == "__main__":
    main()
