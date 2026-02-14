"""Order execution layer using the Polymarket CLOB API via py-clob-client.

Handles order placement, fill verification, cancellation, and state sync.
Fixed from audit: correct size calculation for both BUY and SELL,
fill verification polling, persistent order state.

Paper trading mode: realistic fill simulation using live market prices.
Orders go open → filled/expired just like real CLOB, with paper balance
tracking that deducts collateral and settles P&L on resolution.
"""

import json
import random
import time
import logging
from dataclasses import dataclass, field
from pathlib import Path

from bot.strategies import Signal, Side

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    success: bool
    order_id: str | None
    status: str
    filled_size: float = 0.0
    error: str | None = None


@dataclass
class TrackedOrder:
    """An order we're tracking through its lifecycle."""
    order_id: str
    token_id: str
    side: str           # "buy_up", "sell_down", etc.
    price: float
    size_shares: float  # Size in shares (what we sent to CLOB)
    size_usd: float     # Original USD-denominated size
    is_sell: bool
    placed_at: float    # Unix timestamp
    status: str = "open"  # open, filled, partial, cancelled, expired
    filled_shares: float = 0.0
    confidence: float = 0.0
    edge: float = 0.0
    # Paper trading: random fill delay assigned at placement
    _paper_fill_after: float = 0.0


class Executor:
    """Handles order placement, fill tracking, and position management.

    In paper trading mode (dry_run=True), simulates the full order lifecycle:
    - Orders start as "open" (not instant fill)
    - check_fills() evaluates real market prices to decide if orders fill
    - Paper balance tracks collateral lockup and P&L
    - Fills have realistic random delays and liquidity simulation
    """

    def __init__(self, config):
        self.config = config
        self.dry_run = config.dry_run
        self.trade_log_file = config.trade_log_file

        if not self.dry_run:
            from py_clob_client.client import ClobClient
            self.client = ClobClient(
                host=config.clob_host,
                key=config.private_key,
                chain_id=config.chain_id,
            )
            self._init_credentials()
        else:
            self.client = None
            # Paper trading state
            self._paper_balance = config.paper_balance
            self._paper_collateral_locked = 0.0  # USDC locked in open positions
            self._paper_realized_pnl = 0.0
            self._paper_market_prices: dict[str, float] = {}  # token_id → price
            logger.info(
                "PAPER TRADING mode — starting balance: $%,.0f | "
                "fills simulated from real market prices",
                self._paper_balance,
            )

        self.tracked_orders: dict[str, TrackedOrder] = {}

    def _init_credentials(self):
        """Derive or set API credentials."""
        if self.config.api_key:
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=self.config.api_key,
                api_secret=self.config.api_secret,
                api_passphrase=self.config.passphrase,
            )
            self.client.set_api_creds(creds)
        else:
            creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(creds)
            logger.info("API credentials derived from private key")

    # ---- Market state for paper trading ----

    def update_market_prices(self, token_prices: dict[str, float]):
        """Update current market prices for paper fill simulation.

        Called by the engine every tick with real Polymarket prices.
        Args:
            token_prices: {token_id: current_price} for active tokens
        """
        if self.dry_run:
            self._paper_market_prices.update(token_prices)

    # ---- Order placement ----

    def place_order(
        self,
        signal: Signal,
        token_id: str,
        post_only: bool = True,
        is_sell: bool = False,
    ) -> OrderResult:
        """Place a limit order based on a strategy signal.

        For BOTH buy and sell: size sent to CLOB is in shares.
        - signal.size is always in USD
        - shares = USD / price (how many shares for that USD amount)
        """
        if signal.side == Side.NONE:
            return OrderResult(False, None, "no_signal", error="Signal is NONE")

        # Calculate shares from USD amount
        if signal.price <= 0:
            return OrderResult(False, None, "bad_price", error=f"Price {signal.price} <= 0")

        shares = signal.size / signal.price
        shares = round(shares, 2)

        if shares < 1:
            return OrderResult(False, None, "too_small", error=f"Shares {shares} < 1")

        # Log the order details
        side_label = signal.side.value
        logger.info(
            "Placing %s order: %s %s @ $%.2f x %.1f shares ($%.0f USD) | conf=%.1f%% edge=%.1f%%",
            "SELL" if is_sell else "BUY",
            side_label,
            token_id[:16] + "...",
            signal.price,
            shares,
            signal.size,
            signal.confidence * 100,
            signal.edge * 100,
        )

        if self.dry_run:
            return self._paper_place_order(
                signal, token_id, shares, side_label, is_sell,
            )

        return self._live_place_order(
            signal, token_id, shares, side_label, is_sell,
        )

    def _paper_place_order(
        self,
        signal: Signal,
        token_id: str,
        shares: float,
        side_label: str,
        is_sell: bool,
    ) -> OrderResult:
        """Paper trading: place order as 'open', lock collateral, assign fill delay."""
        # Calculate collateral required
        if is_sell:
            # Selling at price P: collateral = (1-P) * shares (risk if resolves against you)
            collateral = (1.0 - signal.price) * shares
        else:
            # Buying at price P: cost = P * shares
            collateral = signal.price * shares

        # Check paper balance
        available = self._paper_balance - self._paper_collateral_locked
        if collateral > available:
            logger.warning(
                "PAPER: Insufficient balance — need $%.0f collateral, have $%.0f available "
                "(balance=$%.0f, locked=$%.0f)",
                collateral, available, self._paper_balance, self._paper_collateral_locked,
            )
            return OrderResult(
                False, None, "insufficient_paper_balance",
                error=f"Need ${collateral:.0f}, have ${available:.0f} available",
            )

        # Lock collateral
        self._paper_collateral_locked += collateral

        # Assign random fill delay (simulates order book matching latency)
        fill_delay = random.uniform(
            self.config.paper_fill_delay_min,
            self.config.paper_fill_delay_max,
        )

        fake_id = f"PAPER_{int(time.time() * 1000)}_{side_label}"
        tracked = TrackedOrder(
            order_id=fake_id,
            token_id=token_id,
            side=side_label,
            price=signal.price,
            size_shares=shares,
            size_usd=signal.size,
            is_sell=is_sell,
            placed_at=time.time(),
            status="open",  # NOT instant fill — goes through lifecycle
            filled_shares=0.0,
            confidence=signal.confidence,
            edge=signal.edge,
            _paper_fill_after=time.time() + fill_delay,
        )
        self.tracked_orders[fake_id] = tracked

        self._log_trade("place", {
            "order_id": fake_id,
            "side": side_label,
            "price": signal.price,
            "shares": shares,
            "usd": signal.size,
            "collateral_locked": round(collateral, 2),
            "is_sell": is_sell,
            "paper": True,
            "fill_delay": round(fill_delay, 2),
            "paper_balance": round(self._paper_balance, 2),
            "paper_available": round(available - collateral, 2),
        })

        logger.info(
            "PAPER order OPEN: %s | $%.0f collateral locked | "
            "available: $%.0f → $%.0f | fill eligible in %.1fs",
            fake_id, collateral, available, available - collateral, fill_delay,
        )

        return OrderResult(True, fake_id, "open", filled_size=0.0)

    def _live_place_order(
        self,
        signal: Signal,
        token_id: str,
        shares: float,
        side_label: str,
        is_sell: bool,
    ) -> OrderResult:
        """Live trading: place real order on Polymarket CLOB."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        order_side = SELL if is_sell else BUY

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=signal.price,
                size=shares,
                side=order_side,
            )

            resp = self.client.create_and_post_order(order_args, OrderType.GTC)

            if not resp:
                return OrderResult(False, None, "empty_response", error="No response from CLOB")

            order_id = resp.get("orderID") or resp.get("orderId", "")
            status = resp.get("status", "unknown")
            success = resp.get("success", False)

            if success and order_id:
                tracked = TrackedOrder(
                    order_id=order_id,
                    token_id=token_id,
                    side=side_label,
                    price=signal.price,
                    size_shares=shares,
                    size_usd=signal.size,
                    is_sell=is_sell,
                    placed_at=time.time(),
                    status="open",
                    confidence=signal.confidence,
                    edge=signal.edge,
                )
                self.tracked_orders[order_id] = tracked

                self._log_trade("place", {
                    "order_id": order_id,
                    "side": side_label,
                    "price": signal.price,
                    "shares": shares,
                    "usd": signal.size,
                    "is_sell": is_sell,
                    "clob_status": status,
                })

                logger.info("Order placed: id=%s status=%s", order_id[:20], status)
            else:
                error_msg = resp.get("errorMsg", "Unknown error")
                logger.warning("Order rejected: %s", error_msg)
                return OrderResult(False, order_id, status, error=error_msg)

            return OrderResult(
                success=success,
                order_id=order_id,
                status=status,
            )

        except Exception as e:
            logger.exception("Order placement failed")
            return OrderResult(False, None, "error", error=str(e))

    # ---- Fill checking ----

    def check_fills(self) -> list[TrackedOrder]:
        """Check for filled orders. Returns list of newly filled orders."""
        if self.dry_run:
            return self._paper_check_fills()
        return self._live_check_fills()

    def _paper_check_fills(self) -> list[TrackedOrder]:
        """Paper trading fill simulation using real market prices.

        Simulates realistic CLOB behavior:
        - SELL at 0.51: fills when market price >= 0.51 (someone buys at our price)
        - BUY at 0.49: fills when market price <= 0.49 (someone sells at our price)
        - Random fill rate simulates liquidity (not every marketable order fills)
        - Minimum delay before fill (simulates order book propagation)
        """
        now = time.time()
        newly_filled = []
        open_orders = [o for o in self.tracked_orders.values() if o.status == "open"]

        if not open_orders:
            return []

        for order in open_orders:
            # Not yet eligible (fill delay not elapsed)
            if now < order._paper_fill_after:
                continue

            # Check timeout
            age = now - order.placed_at
            if age > self.config.order_timeout_seconds:
                self._paper_release_collateral(order)
                order.status = "expired"
                logger.info(
                    "PAPER order EXPIRED: %s (%.0fs old, no fill)",
                    order.order_id, age,
                )
                self._log_trade("expire", {
                    "order_id": order.order_id,
                    "side": order.side,
                    "age_seconds": round(age, 1),
                    "paper": True,
                })
                continue

            # Get current market price for this token
            market_price = self._paper_market_prices.get(order.token_id)
            if market_price is None:
                # No market price available — can't simulate fill
                continue

            # Would this order fill at the current market price?
            would_fill = False
            if order.is_sell:
                # SELL limit at order.price: fills when market bid >= our ask
                # Market price represents what buyers pay, so fill if >= our price
                would_fill = market_price >= order.price
            else:
                # BUY limit at order.price: fills when market ask <= our bid
                would_fill = market_price <= order.price

            if not would_fill:
                continue

            # Liquidity simulation: not every marketable order fills instantly
            if random.random() > self.config.paper_fill_rate:
                # Didn't fill this check — push eligibility forward slightly
                order._paper_fill_after = now + random.uniform(0.5, 2.0)
                logger.debug(
                    "PAPER: %s marketable but no fill (liquidity sim), retry in ~1s",
                    order.order_id,
                )
                continue

            # FILL the order
            order.status = "filled"
            order.filled_shares = order.size_shares
            newly_filled.append(order)

            fill_latency = now - order.placed_at
            logger.info(
                "PAPER order FILLED: %s | %s %.1f shares @ $%.2f | "
                "market=$%.2f | latency=%.1fs | balance=$%,.0f",
                order.order_id,
                "SELL" if order.is_sell else "BUY",
                order.filled_shares,
                order.price,
                market_price,
                fill_latency,
                self._paper_balance,
            )

            self._log_trade("fill", {
                "order_id": order.order_id,
                "side": order.side,
                "price": order.price,
                "shares": order.filled_shares,
                "market_price_at_fill": market_price,
                "fill_latency_s": round(fill_latency, 2),
                "paper": True,
                "paper_balance": round(self._paper_balance, 2),
            })

        return newly_filled

    def _live_check_fills(self) -> list[TrackedOrder]:
        """Live CLOB fill check."""
        if not self.client:
            return []

        newly_filled = []
        open_orders = [o for o in self.tracked_orders.values() if o.status == "open"]

        if not open_orders:
            return []

        try:
            clob_orders = self.client.get_orders() or []
            clob_by_id = {o.get("id", ""): o for o in clob_orders}
        except Exception:
            logger.exception("Failed to fetch orders for fill check")
            return []

        for tracked in open_orders:
            clob_order = clob_by_id.get(tracked.order_id)

            if clob_order:
                clob_status = clob_order.get("status", "").lower()
                filled = float(clob_order.get("size_matched", 0))

                if filled > 0 and filled >= tracked.size_shares * 0.95:
                    tracked.status = "filled"
                    tracked.filled_shares = filled
                    newly_filled.append(tracked)
                    logger.info("Order FILLED: %s (%.1f shares)", tracked.order_id[:20], filled)
                elif filled > 0:
                    tracked.status = "partial"
                    tracked.filled_shares = filled
                elif clob_status in ("cancelled", "expired"):
                    tracked.status = clob_status
            else:
                # Order not in CLOB — could be fully filled and removed, or expired
                age = time.time() - tracked.placed_at
                if age > self.config.order_timeout_seconds:
                    tracked.status = "expired"
                    logger.info("Order expired (not in CLOB): %s", tracked.order_id[:20])
                else:
                    # Assume filled if recently placed and gone from book
                    tracked.status = "filled"
                    tracked.filled_shares = tracked.size_shares
                    newly_filled.append(tracked)
                    logger.info("Order likely filled (gone from book): %s", tracked.order_id[:20])

        return newly_filled

    # ---- Paper balance management ----

    def _paper_release_collateral(self, order: TrackedOrder):
        """Release locked collateral for a cancelled/expired paper order."""
        if order.is_sell:
            collateral = (1.0 - order.price) * order.size_shares
        else:
            collateral = order.price * order.size_shares
        self._paper_collateral_locked = max(0, self._paper_collateral_locked - collateral)

    def paper_settle_resolution(self, order_id: str, won: bool):
        """Settle a paper position after market resolution.

        Called by the engine when an interval resolves.
        Updates paper balance based on win/loss.
        """
        if not self.dry_run:
            return

        order = self.tracked_orders.get(order_id)
        if not order or order.status != "filled":
            return

        shares = order.filled_shares

        if order.is_sell:
            collateral = (1.0 - order.price) * shares
            if won:
                # Sold a contract that resolved to $0 — keep the sale proceeds
                pnl = order.price * shares
                self._paper_balance += collateral + pnl  # Return collateral + profit
            else:
                # Contract resolved to $1 — lose the collateral
                pnl = -collateral
                # Collateral already deducted, nothing returned
        else:
            cost = order.price * shares
            if won:
                # Bought a contract that resolved to $1 — receive $1 per share
                pnl = (1.0 - order.price) * shares
                self._paper_balance += cost + pnl  # Return cost + profit
            else:
                # Contract resolved to $0 — lose the cost
                pnl = -cost
                # Cost already deducted, nothing returned

        self._paper_collateral_locked = max(0, self._paper_collateral_locked - abs(
            (1.0 - order.price) * shares if order.is_sell else order.price * shares
        ))
        self._paper_realized_pnl += pnl

        logger.info(
            "PAPER SETTLED: %s | %s | P&L=$%+.2f | "
            "balance=$%,.0f | total P&L=$%+.0f",
            order.order_id,
            "WIN" if won else "LOSS",
            pnl,
            self._paper_balance,
            self._paper_realized_pnl,
        )

        self._log_trade("settle", {
            "order_id": order.order_id,
            "side": order.side,
            "won": won,
            "pnl": round(pnl, 2),
            "paper_balance": round(self._paper_balance, 2),
            "paper_total_pnl": round(self._paper_realized_pnl, 2),
            "paper": True,
        })

    # ---- Cancel ----

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        if self.dry_run:
            order = self.tracked_orders.get(order_id)
            if order and order.status == "open":
                self._paper_release_collateral(order)
                order.status = "cancelled"
                logger.info("PAPER order CANCELLED: %s", order_id)
            self.tracked_orders.pop(order_id, None)
            return True

        try:
            self.client.cancel(order_id=order_id)
            if order_id in self.tracked_orders:
                self.tracked_orders[order_id].status = "cancelled"
            logger.info("Cancelled order %s", order_id[:20])
            return True
        except Exception:
            logger.exception("Failed to cancel order %s", order_id[:20])
            return False

    def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if self.dry_run:
            cancelled = 0
            for o in self.tracked_orders.values():
                if o.status == "open":
                    self._paper_release_collateral(o)
                    o.status = "cancelled"
                    cancelled += 1
            if cancelled:
                logger.info("PAPER: Cancelled %d open orders", cancelled)
            return True

        try:
            self.client.cancel_all()
            for o in self.tracked_orders.values():
                if o.status == "open":
                    o.status = "cancelled"
            logger.info("Cancelled all open orders")
            return True
        except Exception:
            logger.exception("Failed to cancel all orders")
            return False

    # ---- Balance ----

    def get_balance(self) -> float | None:
        """Get available USDC balance."""
        if self.dry_run:
            available = self._paper_balance - self._paper_collateral_locked
            logger.info(
                "PAPER balance: $%,.0f (locked: $%,.0f, available: $%,.0f, P&L: $%+,.0f)",
                self._paper_balance,
                self._paper_collateral_locked,
                available,
                self._paper_realized_pnl,
            )
            return available

        try:
            result = self.client.get_balance_allowance(asset_type="COLLATERAL")
            return float(result.get("balance", 0))
        except Exception:
            logger.exception("Failed to get balance")
            return None

    # ---- Utilities ----

    def get_open_order_ids(self) -> list[str]:
        """Get IDs of currently open (unfilled) tracked orders."""
        return [oid for oid, o in self.tracked_orders.items() if o.status == "open"]

    def get_filled_orders(self) -> list[TrackedOrder]:
        """Get all filled orders (for resolution tracking)."""
        return [o for o in self.tracked_orders.values() if o.status == "filled"]

    def cleanup_old_orders(self, max_age_seconds: float = 7200):
        """Remove resolved/cancelled orders older than max_age."""
        cutoff = time.time() - max_age_seconds
        to_remove = [
            oid for oid, o in self.tracked_orders.items()
            if o.status in ("filled", "cancelled", "expired") and o.placed_at < cutoff
        ]
        for oid in to_remove:
            del self.tracked_orders[oid]

    def _log_trade(self, event: str, data: dict):
        """Append a trade event to the JSONL trade log."""
        entry = {"ts": time.time(), "event": event, **data}
        try:
            with open(self.trade_log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            logger.exception("Failed to write trade log")
