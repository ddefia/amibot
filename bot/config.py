"""Bot configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Polymarket
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137
    private_key: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))

    # API credentials (auto-derived if empty)
    api_key: str = field(default_factory=lambda: os.getenv("POLY_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("POLY_API_SECRET", ""))
    passphrase: str = field(default_factory=lambda: os.getenv("POLY_PASSPHRASE", ""))

    # Strategy
    strategy: str = field(
        default_factory=lambda: os.getenv("STRATEGY", "latency_arb")
    )
    max_position_size: float = field(
        default_factory=lambda: float(os.getenv("MAX_POSITION_SIZE", "100"))
    )
    max_exposure_usdc: float = field(
        default_factory=lambda: float(os.getenv("MAX_EXPOSURE_USDC", "500"))
    )
    min_edge_threshold: float = field(
        default_factory=lambda: float(os.getenv("MIN_EDGE_THRESHOLD", "0.05"))
    )
    maker_only: bool = field(
        default_factory=lambda: os.getenv("MAKER_ONLY", "true").lower() == "true"
    )

    # Risk management
    max_consecutive_losses: int = field(
        default_factory=lambda: int(os.getenv("MAX_CONSECUTIVE_LOSSES", "5"))
    )
    daily_loss_limit_usdc: float = field(
        default_factory=lambda: float(os.getenv("DAILY_LOSS_LIMIT_USDC", "200"))
    )
    cooldown_after_loss_streak_seconds: int = field(
        default_factory=lambda: int(
            os.getenv("COOLDOWN_AFTER_LOSS_STREAK_SECONDS", "300")
        )
    )

    # WebSocket endpoints
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    rtds_ws: str = "wss://ws-live-data.polymarket.com"

    # Logging
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO")
    )
    log_file: str = field(
        default_factory=lambda: os.getenv("LOG_FILE", "bot.log")
    )
