"""Risk management: position sizing, loss limits, and cooldowns.

CROSS-TRADER LESSONS APPLIED:
- Losses are almost always -100% in binary markets (no partial recovery)
- 5-min intervals are net losers — tighter risk limits on these
- Feed disagreement + Down bets = Guy 5's $19K loss pattern
- Sell-side exposure = (1 - sell_price) x shares (what you pay if wrong)
- Size reduction after consecutive losses (0.8^n exponential decay)
"""

import time
import logging
from dataclasses import dataclass, field

from bot.strategies import Signal, Side

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    timestamp: float
    side: str
    price: float
    size: float  # In USD
    pnl: float  # Realized P&L (0 until resolved)
    resolved: bool = False
    is_sell: bool = False


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

        Returns (allowed, reason).
        """
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
        if self.daily_pnl < -self.config.daily_loss_limit_usdc:
            return False, f"Daily loss limit hit (${self.daily_pnl:.2f})"

        # Consecutive loss limit → trigger cooldown
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            self.cooldown_until = now + self.config.cooldown_after_loss_streak_seconds
            self.consecutive_losses = 0
            return False, (
                f"Consecutive loss limit hit, "
                f"cooling down {self.config.cooldown_after_loss_streak_seconds}s"
            )

        # Max exposure check
        trade_risk = self._calculate_risk(signal)

        if self.total_exposure + trade_risk > self.config.max_exposure_usdc:
            return False, (
                f"Would exceed max exposure "
                f"(${self.total_exposure:.2f} + ${trade_risk:.2f} "
                f"> ${self.config.max_exposure_usdc:.2f})"
            )

        return True, "OK"

    def adjust_size(self, signal: Signal) -> float:
        """Adjust position size based on risk state.

        Reduces size after consecutive losses (0.8^n decay).
        Caps to remaining exposure room.
        Returns adjusted size in USD.
        """
        size = signal.size

        # Reduce size after consecutive losses
        if self.consecutive_losses > 0:
            reduction = 0.8 ** self.consecutive_losses
            size *= reduction
            logger.info(
                "Size reduced %.1f%% due to %d consecutive losses ($%.0f → $%.0f)",
                (1 - reduction) * 100,
                self.consecutive_losses,
                signal.size,
                size,
            )

        # Cap to remaining exposure room
        remaining_room = self.config.max_exposure_usdc - self.total_exposure
        if signal.price > 0:
            is_sell = signal.side in (Side.SELL_UP, Side.SELL_DOWN)
            if is_sell:
                # Sell risk per USD = (1 - price) / price
                max_usd = remaining_room * signal.price / (1 - signal.price) if signal.price < 1 else remaining_room
            else:
                max_usd = remaining_room / signal.price if signal.price > 0 else remaining_room
            size = min(size, max_usd)

        # Minimum viable position
        if size < 50:
            return 0.0

        return round(size, 2)

    def record_trade(self, signal: Signal):
        """Record a trade entry."""
        is_sell = signal.side in (Side.SELL_UP, Side.SELL_DOWN)
        trade = TradeRecord(
            timestamp=time.time(),
            side=signal.side.value,
            price=signal.price,
            size=signal.size,
            pnl=0.0,
            is_sell=is_sell,
        )
        self.trades.append(trade)

        # Add to exposure
        self.total_exposure += self._calculate_risk(signal)

    def record_resolution(self, trade_index: int, won: bool):
        """Record the outcome of a resolved trade."""
        if trade_index >= len(self.trades):
            return

        trade = self.trades[trade_index]
        if trade.resolved:
            return

        trade.resolved = True

        if trade.is_sell:
            # Sell-side: signal.size is in USD, shares = size / price
            shares = trade.size / trade.price if trade.price > 0 else 0
            risk_amount = (1.0 - trade.price) * shares
            self.total_exposure = max(0, self.total_exposure - risk_amount)
            if won:
                trade.pnl = trade.price * shares  # Keep proceeds
                self.consecutive_losses = 0
            else:
                trade.pnl = -risk_amount  # Pay remainder
                self.consecutive_losses += 1
        else:
            # Buy-side: cost is price * (size / price) = size
            self.total_exposure = max(0, self.total_exposure - trade.size)
            if won:
                shares = trade.size / trade.price if trade.price > 0 else 0
                trade.pnl = (1.0 - trade.price) * shares  # Shares resolve to $1
                self.consecutive_losses = 0
            else:
                trade.pnl = -trade.size  # Lose the cost
                self.consecutive_losses += 1

        self.daily_pnl += trade.pnl
        logger.info(
            "Trade resolved: %s | P&L=$%+.2f | Daily=$%+.2f | Streak=%d",
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
            "total_pnl": round(total_pnl, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "consecutive_losses": self.consecutive_losses,
            "current_exposure": round(self.total_exposure, 2),
        }

    def _calculate_risk(self, signal: Signal) -> float:
        """Calculate the risk (potential loss) of a signal."""
        is_sell = signal.side in (Side.SELL_UP, Side.SELL_DOWN)
        if is_sell:
            # Sell risk: if contract resolves to $1, we pay (1-price) per share
            shares = signal.size / signal.price if signal.price > 0 else 0
            return (1.0 - signal.price) * shares
        else:
            # Buy risk: we paid price * shares = signal.size (since signal.size is in USD)
            return signal.size
