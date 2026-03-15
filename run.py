"""Entry point for the Polymarket multi-strategy trading bot.

Runs the unified engine with all 4 strategies:
1. Oracle Lag (crypto up/down across BTC/ETH/SOL/XRP)
2. Arbitrage (YES+NO < $1.00 on any market)
3. Data Edge (sports/weather/news with faster data)
4. Market Making (spread + maker rebates)

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
from bot.unified_engine import UnifiedEngine

logger = logging.getLogger("bot.runner")

MAX_RESTARTS = 50
RESTART_DELAY_BASE = 5
RESTART_DELAY_MAX = 30


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
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def print_banner(config: Config):
    mode = "DRY RUN" if config.dry_run else "LIVE"
    print("=" * 60)
    if config.dry_run:
        print("  *** TESTING MODE — NO REAL MONEY AT RISK ***")
        print("=" * 60)
    print(f"  Polymarket Multi-Strategy Bot [{mode}]")
    print(f"  Strategies: Oracle Lag + Arb + Data Edge + MM")
    print(f"  Assets: BTC / ETH / SOL / XRP + all markets")
    print(f"  Position Size: ${config.position_usd:,.0f}")
    print(f"  Max Exposure: ${config.max_exposure_usdc:,.0f}")
    print(f"  Min Edge: {config.min_edge_threshold:.1%}")
    print(f"  Confidence: {config.confidence_threshold:.1%}")
    print(f"  Daily Loss Limit: ${config.daily_loss_limit_usdc:,.0f}")
    print(f"  Trade Log: {config.trade_log_file}")
    print("=" * 60)

    if config.dry_run:
        print("  PAPER TRADING — real data, simulated orders")
        print(f"  Paper Balance: ${config.paper_balance:,.0f}")
        print(f"  Fill Rate: {config.paper_fill_rate:.0%} (liquidity sim)")
        print("  Stats report every 15 min in bot.log")
        print("  Set DRY_RUN=false in .env for live trading")
        print("=" * 60)


def main():
    config = Config()

    errors = config.validate()
    if errors:
        print("Configuration errors:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    setup_logging(config)
    print_banner(config)

    shutdown_requested = False

    def shutdown(sig, frame):
        nonlocal shutdown_requested
        shutdown_requested = True
        print("\nShutdown signal received...")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    restart_count = 0
    consecutive_fast_crashes = 0

    while restart_count < MAX_RESTARTS and not shutdown_requested:
        start_time = time.time()

        try:
            engine = UnifiedEngine(config)

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

            if elapsed < 30:
                consecutive_fast_crashes += 1
            else:
                consecutive_fast_crashes = 0

            if consecutive_fast_crashes >= 5:
                logger.error("5 fast crashes in a row — waiting 60s")
                time.sleep(60)
                consecutive_fast_crashes = 0
                continue

            delay = min(RESTART_DELAY_BASE * restart_count, RESTART_DELAY_MAX)
            logger.info("Restarting in %ds...", delay)
            time.sleep(delay)
            continue

        elapsed = time.time() - start_time
        restart_count += 1
        logger.warning(
            "Bot exited after %.0fs — restarting (attempt %d/%d)",
            elapsed, restart_count, MAX_RESTARTS,
        )
        time.sleep(RESTART_DELAY_BASE)

    if restart_count >= MAX_RESTARTS:
        logger.error("Max restarts (%d) reached — giving up", MAX_RESTARTS)


if __name__ == "__main__":
    main()
