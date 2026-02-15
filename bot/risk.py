"""Risk management: position sizing, loss limits, cooldowns, and bankroll protection.

CROSS-TRADER LESSONS APPLIED:
- Losses are almost always -100% in binary markets (no partial recovery)
- 5-min intervals are net losers — tighter risk limits on these
- Feed disagreement + Down bets = Guy 5's $19K loss pattern
- Sell-side exposure = (1 - sell_price) x shares (what you pay if wrong)
- Size reduction after consecutive losses (0.8^n exponential decay)

BANKROLL PROTECTION:
- All sizing is percentage-of-bankroll, not fixed dollar amounts
- Pre-trade balance check before every order
- Max 5% of bankroll on any single trade
- Max 15% of bankroll deployed per interval
- Max 40% of bankroll in total active exposure
- Drawdown circuit breaker: halt if equity drops 20% from session start
- Periodic balance refresh (every 60s) to stay synced with reality
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
    """Enforces risk limits, bankroll protection, and tracks performance.

    Key principle: NEVER risk more than a percentage of actual bankroll.
    All dollar-amount limits are also capped by percentage-of-balance limits.
    """

    def __init__(self, config):
        self.config = config
        self.trades: list[TradeRecord] = []
        self.consecutive_losses: int = 0
        self.daily_pnl: float = 0.0
        self.day_start: float = time.time()
        self.cooldown_until: float = 0.0
        self.total_exposure: float = 0.0

        # Bankroll tracking
        self.current_balance: float = 0.0      # Last known USDC balance
        self.session_start_balance: float = 0.0 # Balance when bot started
        self.balance_last_checked: float = 0.0  # When we last refreshed
        self.interval_deployed: float = 0.0     # Deployed this interval
        self._drawdown_halted: bool = False

    def set_balance(self, balance: float):
        """Update the known balance. Called on startup and periodically."""
        self.current_balance = balance
        if self.session_start_balance == 0:
            self.session_start_balance = balance
            logger.info("Session start balance: $%.2f", balance)
        self.balance_last_checked = time.time()

        # Check drawdown circuit breaker
        if self.session_start_balance > 0:
            drawdown_pct = (self.session_start_balance - balance) / self.session_start_balance
            if drawdown_pct >= self.config.drawdown_halt_pct:
                if not self._drawdown_halted:
                    self._drawdown_halted = True
                    logger.warning(
                        "DRAWDOWN HALT: Balance dropped %.1f%% ($%.0f → $%.0f). "
                        "Trading suspended until manual restart.",
                        drawdown_pct * 100, self.session_start_balance, balance,
                    )
            elif self._drawdown_halted and drawdown_pct < self.config.drawdown_halt_pct * 0.5:
                # Auto-resume if recovered significantly
                self._drawdown_halted = False
                logger.info("Drawdown recovered, resuming trading")

    def needs_balance_refresh(self) -> bool:
        """Check if it's time to re-fetch the actual balance."""
        return (time.time() - self.balance_last_checked) > self.config.balance_refresh_interval

    def reset_interval_deployed(self):
        """Reset per-interval capital tracking (called at interval boundaries)."""
        self.interval_deployed = 0.0

    def check_allowed(self, signal: Signal) -> tuple[bool, str]:
        """Check if a trade is allowed under current risk limits.

        Enforces both absolute AND percentage-of-bankroll limits.
        Returns (allowed, reason).
        """
        now = time.time()

        # Reset daily P&L at midnight
        if now - self.day_start > 86400:
            self.daily_pnl = 0.0
            self.day_start = now
            logger.info("Daily P&L reset")

        # Drawdown circuit breaker
        if self._drawdown_halted:
            return False, "DRAWDOWN HALT — trading suspended"

        # Cooldown after loss streak
        if now < self.cooldown_until:
            remaining = int(self.cooldown_until - now)
            return False, f"Cooldown active ({remaining}s remaining)"

        # Daily loss limit (absolute)
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

        trade_risk = self._calculate_risk(signal)

        # --- ABSOLUTE LIMITS ---
        if self.total_exposure + trade_risk > self.config.max_exposure_usdc:
            return False, (
                f"Would exceed max exposure "
                f"(${self.total_exposure:.2f} + ${trade_risk:.2f} "
                f"> ${self.config.max_exposure_usdc:.2f})"
            )

        # --- BANKROLL-BASED LIMITS ---
        if self.current_balance > 0:
            # Max single trade: 5% of bankroll
            max_trade_usd = self.current_balance * self.config.max_pct_per_trade
            if trade_risk > max_trade_usd:
                return False, (
                    f"Trade risk ${trade_risk:.0f} exceeds "
                    f"{self.config.max_pct_per_trade:.0%} of bankroll "
                    f"(${max_trade_usd:.0f} of ${self.current_balance:.0f})"
                )

            # Max per interval: 15% of bankroll
            max_interval_usd = self.current_balance * self.config.max_pct_per_interval
            if self.interval_deployed + signal.size > max_interval_usd:
                return False, (
                    f"Would exceed per-interval limit "
                    f"(${self.interval_deployed:.0f} + ${signal.size:.0f} "
                    f"> {self.config.max_pct_per_interval:.0%} of ${self.current_balance:.0f})"
                )

            # Max total exposure: 40% of bankroll
            max_total_usd = self.current_balance * self.config.max_pct_total_exposure
            if self.total_exposure + trade_risk > max_total_usd:
                return False, (
                    f"Would exceed bankroll exposure limit "
                    f"(${self.total_exposure:.0f} + ${trade_risk:.0f} "
                    f"> {self.config.max_pct_total_exposure:.0%} of ${self.current_balance:.0f})"
                )

        return True, "OK"

    def adjust_size(self, signal: Signal) -> float:
        """Adjust position size based on risk state AND bankroll.

        Applies:
        1. Loss streak decay (0.8^n)
        2. Remaining exposure room
        3. Bankroll percentage cap (max 5% per trade)
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

        # Cap to remaining absolute exposure room
        remaining_room = self.config.max_exposure_usdc - self.total_exposure
        if signal.price > 0:
            is_sell = signal.side in (Side.SELL_UP, Side.SELL_DOWN)
            if is_sell:
                max_usd = remaining_room * signal.price / (1 - signal.price) if signal.price < 1 else remaining_room
            else:
                max_usd = remaining_room / signal.price if signal.price > 0 else remaining_room
            size = min(size, max_usd)

        # Cap to bankroll percentage
        if self.current_balance > 0:
            max_from_bankroll = self.current_balance * self.config.max_pct_per_trade
            if size > max_from_bankroll:
                logger.info("Size capped by bankroll: $%.0f → $%.0f (%.0f%% of $%.0f)",
                            size, max_from_bankroll,
                            self.config.max_pct_per_trade * 100, self.current_balance)
                size = max_from_bankroll

        # Minimum viable position (low floor for live testing with small balances)
        if size < 5:
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

        risk = self._calculate_risk(signal)
        self.total_exposure += risk
        self.interval_deployed += signal.size

    def record_resolution(self, trade_index: int, won: bool):
        """Record the outcome of a resolved trade."""
        if trade_index >= len(self.trades):
            return

        trade = self.trades[trade_index]
        if trade.resolved:
            return

        trade.resolved = True

        if trade.is_sell:
            shares = trade.size / trade.price if trade.price > 0 else 0
            risk_amount = (1.0 - trade.price) * shares
            self.total_exposure = max(0, self.total_exposure - risk_amount)
            if won:
                trade.pnl = trade.price * shares
                self.consecutive_losses = 0
            else:
                trade.pnl = -risk_amount
                self.consecutive_losses += 1
        else:
            self.total_exposure = max(0, self.total_exposure - trade.size)
            if won:
                shares = trade.size / trade.price if trade.price > 0 else 0
                trade.pnl = (1.0 - trade.price) * shares
                self.consecutive_losses = 0
            else:
                trade.pnl = -trade.size
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
            "current_balance": round(self.current_balance, 2),
            "session_start_balance": round(self.session_start_balance, 2),
            "drawdown_halted": self._drawdown_halted,
        }

    def _calculate_risk(self, signal: Signal) -> float:
        """Calculate the risk (potential loss) of a signal."""
        is_sell = signal.side in (Side.SELL_UP, Side.SELL_DOWN)
        if is_sell:
            shares = signal.size / signal.price if signal.price > 0 else 0
            return (1.0 - signal.price) * shares
        else:
            return signal.size
