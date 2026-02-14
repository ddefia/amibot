"""Order execution layer using the Polymarket CLOB API via py-clob-client."""

import logging
from dataclasses import dataclass

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY

from bot.strategies import Signal, Side

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    success: bool
    order_id: str | None
    status: str
    error: str | None = None


class Executor:
    """Handles order placement, cancellation, and position tracking."""

    def __init__(self, config):
        self.config = config
        self.client = ClobClient(
            host=config.clob_host,
            key=config.private_key,
            chain_id=config.chain_id,
        )
        self._init_credentials()
        self.open_orders: dict[str, dict] = {}

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

    def place_order(
        self,
        signal: Signal,
        token_id: str,
        post_only: bool = True,
    ) -> OrderResult:
        """Place a limit order based on a strategy signal.

        Args:
            signal: The trading signal with side, price, and size
            token_id: The ERC-1155 token ID for the outcome to buy
            post_only: If True, order must be maker-only (no taker fees)
        """
        if signal.side == Side.NONE:
            return OrderResult(False, None, "no_signal", "Signal is NONE")

        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=signal.price,
                size=signal.size,
                side=BUY,
            )

            from py_clob_client.clob_types import OrderType

            # Use GTC (Good-Till-Cancelled) for maker orders
            resp = self.client.create_and_post_order(
                order_args,
                OrderType.GTC,
            )

            if not resp:
                return OrderResult(False, None, "empty_response", "No response")

            order_id = resp.get("orderID") or resp.get("orderId", "")
            status = resp.get("status", "unknown")
            success = resp.get("success", False)

            if success:
                self.open_orders[order_id] = {
                    "signal": signal,
                    "token_id": token_id,
                    "status": status,
                }
                logger.info(
                    "Order placed: %s %s @ $%.2f x %.1f shares (id=%s)",
                    signal.side.value,
                    token_id[:12],
                    signal.price,
                    signal.size,
                    order_id[:12],
                )

            return OrderResult(
                success=success,
                order_id=order_id,
                status=status,
                error=resp.get("errorMsg"),
            )

        except Exception as e:
            logger.exception("Order placement failed")
            return OrderResult(False, None, "error", str(e))

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a specific order."""
        try:
            self.client.cancel(order_id=order_id)
            self.open_orders.pop(order_id, None)
            logger.info("Cancelled order %s", order_id[:12])
            return True
        except Exception:
            logger.exception("Failed to cancel order %s", order_id[:12])
            return False

    def cancel_all(self) -> bool:
        """Cancel all open orders."""
        try:
            self.client.cancel_all()
            self.open_orders.clear()
            logger.info("Cancelled all open orders")
            return True
        except Exception:
            logger.exception("Failed to cancel all orders")
            return False

    def get_balance(self) -> float | None:
        """Get available USDC balance."""
        try:
            result = self.client.get_balance_allowance(asset_type="COLLATERAL")
            return float(result.get("balance", 0))
        except Exception:
            logger.exception("Failed to get balance")
            return None

    def get_open_orders(self) -> list[dict]:
        """Fetch current open orders from the CLOB."""
        try:
            return self.client.get_orders() or []
        except Exception:
            logger.exception("Failed to get open orders")
            return []
