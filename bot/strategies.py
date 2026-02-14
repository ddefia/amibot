"""Trading strategies for BTC 5-min Polymarket markets.

Implements the key strategies observed from successful traders:
1. Latency Arbitrage — exploit the gap between Binance and Chainlink prices
2. Mispricing — buy when market odds diverge from calculated probability
3. Market Making — provide liquidity on both sides, earn spread + rebates
"""

import time
import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class Side(Enum):
    BUY_UP = "buy_up"
    BUY_DOWN = "buy_down"
    NONE = "none"


@dataclass
class Signal:
    side: Side
    confidence: float  # 0.0 to 1.0
    edge: float  # expected profit per $1 risked
    price: float  # limit price to use
    size: float  # suggested position size in shares
    reason: str


class LatencyArbStrategy:
    """Core strategy: exploit the latency gap between Binance (fast) and
    Chainlink (resolution source, slightly slower).

    Tuned to match Guy 1's winning approach from trader analysis:
    - Only trade 180-240 seconds into the 5-min interval
    - Only buy at 48-65 cent prices (near-even odds with slight lean)
    - Small positions (~4-5 shares) with quarter-Kelly sizing
    - Require minimum 3% edge before entering

    Guy 1 stats: 54% win rate, $131K profit, 1.35 profit factor,
    max 6 consecutive losses."""

    # Guy 1's trading window (98% of his trades fall here)
    ENTRY_WINDOW_START = 180  # 3 minutes into interval
    ENTRY_WINDOW_END = 240    # 4 minutes into interval

    # Guy 1's price range (98% of trades at 50-65c)
    MIN_ENTRY_PRICE = 0.48
    MAX_ENTRY_PRICE = 0.65

    def __init__(
        self,
        min_edge: float = 0.03,
        max_position: float = 5.0,
        confidence_threshold: float = 0.55,
    ):
        self.min_edge = min_edge
        self.max_position = max_position
        self.confidence_threshold = confidence_threshold

    def evaluate(
        self,
        interval_start_price: float,
        current_binance_price: float,
        current_chainlink_price: float,
        market_up_price: float,
        market_down_price: float,
        seconds_into_interval: int,
    ) -> Signal:
        """Evaluate whether there's a tradeable latency arb opportunity.

        Args:
            interval_start_price: BTC price at the start of this 5-min interval
                                  (from Chainlink oracle)
            current_binance_price: Latest BTC price from Binance
            current_chainlink_price: Latest BTC price from Chainlink RTDS
            market_up_price: Current Polymarket price for "Up" shares
            market_down_price: Current Polymarket price for "Down" shares
            seconds_into_interval: How far into the 5-min window we are (0-300)
        """
        # TIMING GATE: Guy 1 only trades at 180-240 seconds
        if seconds_into_interval < self.ENTRY_WINDOW_START:
            return Signal(Side.NONE, 0, 0, 0, 0,
                          f"Too early ({seconds_into_interval}s < {self.ENTRY_WINDOW_START}s)")
        if seconds_into_interval > self.ENTRY_WINDOW_END:
            return Signal(Side.NONE, 0, 0, 0, 0,
                          f"Too late ({seconds_into_interval}s > {self.ENTRY_WINDOW_END}s)")

        # Calculate price movement from interval start
        binance_delta = current_binance_price - interval_start_price
        binance_pct_move = binance_delta / interval_start_price

        # Also check Chainlink movement for confirmation
        chainlink_delta = current_chainlink_price - interval_start_price

        # Both feeds should agree on direction for higher confidence
        feeds_agree = (binance_delta > 0) == (chainlink_delta > 0)

        time_factor = seconds_into_interval / 300.0
        move_magnitude = abs(binance_pct_move) * 10000  # in bps

        # At 180-240s, even small moves are meaningful
        if move_magnitude < 3:  # < 3 bps — truly flat
            return Signal(Side.NONE, 0, 0, 0, 0, "Move too small (<3bps)")

        # Confidence model tuned for the 180-240s window
        # At this point in the interval, moves are ~70-80% predictive
        raw_confidence = min(0.95, 0.50 + (move_magnitude / 80) * time_factor)

        # Boost confidence when both price feeds agree
        if feeds_agree:
            raw_confidence = min(0.97, raw_confidence + 0.05)
        else:
            raw_confidence *= 0.85  # reduce if feeds disagree

        if binance_delta > 0:
            true_prob = raw_confidence
            market_prob = market_up_price
            side = Side.BUY_UP
            target_price = market_up_price
        else:
            true_prob = raw_confidence
            market_prob = market_down_price
            side = Side.BUY_DOWN
            target_price = market_down_price

        # PRICE GATE: Guy 1 only buys at 48-65 cents
        if target_price < self.MIN_ENTRY_PRICE:
            return Signal(Side.NONE, raw_confidence, 0, 0, 0,
                          f"Price ${target_price:.2f} below min ${self.MIN_ENTRY_PRICE}")
        if target_price > self.MAX_ENTRY_PRICE:
            return Signal(Side.NONE, raw_confidence, 0, 0, 0,
                          f"Price ${target_price:.2f} above max ${self.MAX_ENTRY_PRICE}")

        # Edge = true probability - market price
        edge = true_prob - market_prob

        if edge < self.min_edge:
            return Signal(
                Side.NONE, raw_confidence, edge, 0, 0,
                f"Edge {edge:.3f} below threshold {self.min_edge}",
            )

        if raw_confidence < self.confidence_threshold:
            return Signal(
                Side.NONE, raw_confidence, edge, 0, 0,
                f"Confidence {raw_confidence:.3f} below threshold",
            )

        # Kelly criterion for position sizing (quarter-Kelly like Guy 1)
        b = (1.0 / target_price) - 1  # decimal odds
        p = true_prob
        q = 1 - p
        kelly_fraction = max(0, (b * p - q) / b) if b > 0 else 0
        size = min(self.max_position, self.max_position * kelly_fraction * 0.25)

        if size < 1:
            return Signal(Side.NONE, raw_confidence, edge, 0, 0, "Size too small")

        return Signal(
            side=side,
            confidence=raw_confidence,
            edge=edge,
            price=target_price,
            size=round(size, 2),
            reason=(
                f"BTC {'UP' if binance_delta > 0 else 'DOWN'} "
                f"{move_magnitude:.1f}bps | "
                f"prob={true_prob:.1%} vs mkt={market_prob:.1%} | "
                f"edge={edge:.1%} | "
                f"feeds={'AGREE' if feeds_agree else 'DISAGREE'} | "
                f"t={seconds_into_interval}s"
            ),
        )


class MispricingStrategy:
    """Buy shares when they're significantly underpriced relative to fair value.

    Inspired by the 'Gabagool' method — don't predict direction, just buy
    whichever side is cheap. In a binary market, if Up + Down < $1.00,
    buying both guarantees profit."""

    def __init__(
        self,
        cheap_threshold: float = 0.35,
        combined_threshold: float = 0.97,
        max_position: float = 100,
    ):
        self.cheap_threshold = cheap_threshold
        self.combined_threshold = combined_threshold
        self.max_position = max_position

    def evaluate(
        self,
        market_up_price: float,
        market_down_price: float,
    ) -> Signal:
        combined = market_up_price + market_down_price

        # Pure arbitrage: combined price < $1.00
        if combined < self.combined_threshold:
            cheaper_side = (
                Side.BUY_UP if market_up_price <= market_down_price else Side.BUY_DOWN
            )
            price = min(market_up_price, market_down_price)
            edge = 1.0 - combined
            return Signal(
                side=cheaper_side,
                confidence=0.99,
                edge=edge,
                price=price,
                size=self.max_position,
                reason=f"Combined price ${combined:.3f} < $1.00, guaranteed edge ${edge:.3f}",
            )

        # Single-side value: one side trading very cheap
        if market_up_price < self.cheap_threshold:
            return Signal(
                side=Side.BUY_UP,
                confidence=0.6,
                edge=0.5 - market_up_price,  # rough edge estimate
                price=market_up_price,
                size=self.max_position * 0.5,
                reason=f"Up shares cheap at ${market_up_price:.3f}",
            )

        if market_down_price < self.cheap_threshold:
            return Signal(
                side=Side.BUY_DOWN,
                confidence=0.6,
                edge=0.5 - market_down_price,
                price=market_down_price,
                size=self.max_position * 0.5,
                reason=f"Down shares cheap at ${market_down_price:.3f}",
            )

        return Signal(Side.NONE, 0, 0, 0, 0, "No mispricing detected")


class MarketMakingStrategy:
    """Provide liquidity on both sides, capture spread and maker rebates.

    Places postOnly limit orders on both bid and ask. Earns the bid-ask
    spread on every fill, plus daily USDC maker rebates from Polymarket."""

    def __init__(
        self,
        spread_target: float = 0.04,  # 4 cent spread
        max_position: float = 100,
        max_inventory_imbalance: float = 200,
    ):
        self.spread_target = spread_target
        self.max_position = max_position
        self.max_inventory_imbalance = max_inventory_imbalance

    def get_quotes(
        self,
        midpoint: float,
        current_inventory_up: float = 0,
        current_inventory_down: float = 0,
    ) -> tuple[Signal, Signal]:
        """Generate bid and ask quotes around the midpoint.

        Returns (bid_signal, ask_signal) for the Up token.
        Skew quotes based on current inventory to reduce directional risk."""
        half_spread = self.spread_target / 2

        # Skew: if we're long Up, lower our Up bid and raise our Up ask
        net_inventory = current_inventory_up - current_inventory_down
        skew = net_inventory / self.max_inventory_imbalance * half_spread

        bid_price = max(0.01, midpoint - half_spread - skew)
        ask_price = min(0.99, midpoint + half_spread - skew)

        bid = Signal(
            side=Side.BUY_UP,
            confidence=0.5,
            edge=half_spread,
            price=round(bid_price, 2),
            size=self.max_position,
            reason=f"MM bid at ${bid_price:.2f} (mid=${midpoint:.2f}, skew={skew:.4f})",
        )

        ask = Signal(
            side=Side.BUY_DOWN,
            confidence=0.5,
            edge=half_spread,
            price=round(1 - ask_price, 2),  # Down price = 1 - Up price
            size=self.max_position,
            reason=f"MM ask at ${ask_price:.2f} (mid=${midpoint:.2f}, skew={skew:.4f})",
        )

        return bid, ask
