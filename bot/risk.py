"""Risk management: position sizing, loss limits, and cooldowns."""

import time
import logging
from dataclasses import dataclass, field

from bot.strategies import Signal

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    timestamp: float
    side: str
    price: float
    size: float
    pnl: float  # realized P&L (0 until resolved)
    resolved: bool = False


class RiskManager:
    """Enforces risk limits and tracks performance."""

    def __init__(self, config):
        self.config = config
        self.trades: list[TradeRecord] = []
        self.consecutive_losses: int = 0
        self.daily_pnl: float = 0.0
        self.day_start: float = time.time()
        self.cooldown_until: float = 0.0
        self.total_exposure: float = 0.0

    def check_allowed(self, signal: Signal) -> tuple[bool, str]:
        """Check if a trade is allowed under current risk limits.

        Returns (allowed, reason)."""
        now = time.time()

        # Reset daily P&L at midnight
        if now - self.day_start > 86400:
            self.daily_pnl = 0.0
            self.day_start = now
            logger.info("Daily P&L reset")

        # Cooldown after loss streak
        if now < self.cooldown_until:
            remaining = int(self.cooldown_until - now)
            return False, f"Cooldown active ({remaining}s remaining)"

        # Daily loss limit
        if self.daily_pnl <= -self.config.daily_loss_limit_usdc:
            return False, f"Daily loss limit hit (${self.daily_pnl:.2f})"

        # Consecutive loss limit
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            self.cooldown_until = now + self.config.cooldown_after_loss_streak_seconds
            self.consecutive_losses = 0
            return False, (
                f"Consecutive loss limit hit, "
                f"cooling down {self.config.cooldown_after_loss_streak_seconds}s"
            )

        # Max exposure check
        trade_cost = signal.price * signal.size
        if self.total_exposure + trade_cost > self.config.max_exposure_usdc:
            return False, (
                f"Would exceed max exposure "
                f"(${self.total_exposure:.2f} + ${trade_cost:.2f} "
                f"> ${self.config.max_exposure_usdc:.2f})"
            )

        return True, "OK"

    def adjust_size(self, signal: Signal) -> float:
        """Adjust position size based on risk state. Returns adjusted size."""
        size = signal.size

        # Reduce size after consecutive losses
        if self.consecutive_losses > 0:
            reduction = 0.8 ** self.consecutive_losses
            size *= reduction
            logger.info(
                "Size reduced %.1f%% due to %d consecutive losses",
                (1 - reduction) * 100,
                self.consecutive_losses,
            )

        # Cap to remaining exposure room
        remaining_room = self.config.max_exposure_usdc - self.total_exposure
        max_shares = remaining_room / signal.price if signal.price > 0 else 0
        size = min(size, max_shares)

        return max(1, round(size, 2))

    def record_trade(self, signal: Signal):
        """Record a trade entry."""
        trade = TradeRecord(
            timestamp=time.time(),
            side=signal.side.value,
            price=signal.price,
            size=signal.size,
            pnl=0.0,
        )
        self.trades.append(trade)
        self.total_exposure += signal.price * signal.size

    def record_resolution(self, trade_index: int, won: bool):
        """Record the outcome of a resolved trade."""
        if trade_index >= len(self.trades):
            return

        trade = self.trades[trade_index]
        if trade.resolved:
            return

        trade.resolved = True
        cost = trade.price * trade.size
        self.total_exposure -= cost

        if won:
            trade.pnl = (1.0 - trade.price) * trade.size  # profit per share
            self.consecutive_losses = 0
        else:
            trade.pnl = -cost
            self.consecutive_losses += 1

        self.daily_pnl += trade.pnl
        logger.info(
            "Trade resolved: %s, P&L=$%.2f, Daily=$%.2f, Streak=%d",
            "WIN" if won else "LOSS",
            trade.pnl,
            self.daily_pnl,
            self.consecutive_losses,
        )

    def get_stats(self) -> dict:
        """Get current risk/performance statistics."""
        resolved = [t for t in self.trades if t.resolved]
        wins = sum(1 for t in resolved if t.pnl > 0)
        losses = sum(1 for t in resolved if t.pnl <= 0)
        total_pnl = sum(t.pnl for t in resolved)

        return {
            "total_trades": len(self.trades),
            "resolved": len(resolved),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / len(resolved) if resolved else 0,
            "total_pnl": total_pnl,
            "daily_pnl": self.daily_pnl,
            "consecutive_losses": self.consecutive_losses,
            "current_exposure": self.total_exposure,
        }
