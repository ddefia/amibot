"""Entry point for the Polymarket BTC 5-min trading bot."""

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
    setup_logging(config)

    if not config.private_key:
        print("ERROR: PRIVATE_KEY not set in .env file")
        print("Copy .env.example to .env and add your Polygon wallet private key")
        sys.exit(1)

    engine = BotEngine(config)

    # Graceful shutdown on Ctrl+C
    def shutdown(sig, frame):
        print("\nShutting down...")
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("=" * 60)
    print("  Polymarket BTC 5-Min Trading Bot")
    print(f"  Strategy: {config.strategy}")
    print(f"  Max Position: {config.max_position_size} shares")
    print(f"  Max Exposure: ${config.max_exposure_usdc} USDC")
    print(f"  Min Edge: {config.min_edge_threshold * 100}%")
    print(f"  Maker Only: {config.maker_only}")
    print("=" * 60)

    asyncio.run(engine.run())


if __name__ == "__main__":
    main()
