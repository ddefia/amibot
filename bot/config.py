"""Bot configuration loaded from environment variables.

Defaults calibrated to Guy 1 (dys123): Sharpe 18.62, $131.6K P&L,
53.7% WR, $1,000-$2,000 positions, ~$2,837 active exposure.
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_float(key: str, default: str) -> float:
    return float(os.getenv(key, default))


def _env_int(key: str, default: str) -> int:
    return int(os.getenv(key, default))


def _env_bool(key: str, default: str) -> bool:
    return os.getenv(key, default).lower() in ("true", "1", "yes")


@dataclass
class Config:
    # Polymarket
    clob_host: str = field(default_factory=lambda: _env("CLOB_HOST", "https://clob.polymarket.com"))
    gamma_host: str = field(default_factory=lambda: _env("GAMMA_HOST", "https://gamma-api.polymarket.com"))
    chain_id: int = field(default_factory=lambda: _env_int("CHAIN_ID", "137"))
    private_key: str = field(default_factory=lambda: _env("PRIVATE_KEY", ""))

    # API credentials (auto-derived if empty)
    api_key: str = field(default_factory=lambda: _env("POLY_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: _env("POLY_API_SECRET", ""))
    passphrase: str = field(default_factory=lambda: _env("POLY_PASSPHRASE", ""))

    # Strategy — calibrated to Guy 1's real performance
    strategy: str = field(default_factory=lambda: _env("STRATEGY", "latency_arb"))
    position_usd: float = field(default_factory=lambda: _env_float("POSITION_USD", "1500"))
    max_position_size: float = field(default_factory=lambda: _env_float("MAX_POSITION_SIZE", "2000"))
    max_exposure_usdc: float = field(default_factory=lambda: _env_float("MAX_EXPOSURE_USDC", "10000"))
    min_edge_threshold: float = field(default_factory=lambda: _env_float("MIN_EDGE_THRESHOLD", "0.03"))
    confidence_threshold: float = field(default_factory=lambda: _env_float("CONFIDENCE_THRESHOLD", "0.55"))
    maker_only: bool = field(default_factory=lambda: _env_bool("MAKER_ONLY", "true"))
    prefer_15min: bool = field(default_factory=lambda: _env_bool("PREFER_15MIN", "true"))

    # Risk management
    max_consecutive_losses: int = field(default_factory=lambda: _env_int("MAX_CONSECUTIVE_LOSSES", "5"))
    daily_loss_limit_usdc: float = field(default_factory=lambda: _env_float("DAILY_LOSS_LIMIT_USDC", "5000"))
    cooldown_after_loss_streak_seconds: int = field(
        default_factory=lambda: _env_int("COOLDOWN_AFTER_LOSS_STREAK_SECONDS", "300")
    )
    max_positions_per_interval: int = field(
        default_factory=lambda: _env_int("MAX_POSITIONS_PER_INTERVAL", "3")
    )

    # Execution
    fill_check_interval_seconds: float = field(
        default_factory=lambda: _env_float("FILL_CHECK_INTERVAL", "5")
    )
    order_timeout_seconds: float = field(
        default_factory=lambda: _env_float("ORDER_TIMEOUT", "60")
    )

    # Operating mode
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", "false"))

    # WebSocket endpoints
    clob_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    rtds_ws: str = "wss://ws-live-data.polymarket.com"

    # Logging
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: _env("LOG_FILE", "bot.log"))

    # Persistence
    trade_log_file: str = field(default_factory=lambda: _env("TRADE_LOG", "trades.jsonl"))

    def validate(self) -> list[str]:
        """Validate config, return list of errors (empty = OK)."""
        errors = []
        if not self.private_key and not self.dry_run:
            errors.append("PRIVATE_KEY is required (or set DRY_RUN=true)")
        if self.private_key and not self.dry_run:
            key = self.private_key.replace("0x", "")
            if len(key) != 64:
                errors.append(f"PRIVATE_KEY should be 64 hex chars, got {len(key)}")
            try:
                int(key, 16)
            except ValueError:
                errors.append("PRIVATE_KEY is not valid hex")
        if self.position_usd <= 0:
            errors.append(f"POSITION_USD must be positive, got {self.position_usd}")
        if self.max_exposure_usdc < self.position_usd:
            errors.append(f"MAX_EXPOSURE ({self.max_exposure_usdc}) < POSITION_USD ({self.position_usd})")
        if not 0 < self.min_edge_threshold < 1:
            errors.append(f"MIN_EDGE_THRESHOLD must be 0-1, got {self.min_edge_threshold}")
        if not 0 < self.confidence_threshold < 1:
            errors.append(f"CONFIDENCE_THRESHOLD must be 0-1, got {self.confidence_threshold}")
        return errors
