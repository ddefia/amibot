"""Trading strategies for BTC 5-min/15-min Polymarket markets.

Implements strategies reverse-engineered from deep trade-by-trade analysis
of 5 real Polymarket traders (PDF reports, CSV data, screenshots):

1. Latency Arb SELL Strategy — Guy 1's exact approach (Sharpe 18.62)
   SELL overpriced contracts at 0.51 when Binance price gives directional signal.
   100% of Guy 1's captured fills are SELL-side at exactly $0.51.

2. Multi-Asset Buy Strategy — Guy 3's approach (Sharpe 9.47, $356.9k PnL)
   BUY underpriced contracts across BTC/ETH/SOL/XRP simultaneously.

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
    SELL_UP = "sell_up"      # Sell "Up/Yes" contracts (short Up)
    SELL_DOWN = "sell_down"  # Sell "Down/No" contracts (short Down)
    NONE = "none"


@dataclass
class Signal:
    side: Side
    confidence: float  # 0.0 to 1.0
    edge: float  # expected profit per $1 risked
    price: float  # limit price to use
    size: float  # suggested position size in USD
    reason: str


class LatencyArbStrategy:
    """Core strategy: SELL overpriced contracts using Binance price as lead signal.

    Reverse-engineered from Guy 1 (dys123) — the best risk-adjusted trader:
    - Sharpe Ratio: 18.62 (Exceptional)
    - ALL-TIME P&L: $131.6k (Rank #801)
    - Win Rate: 53.7% across 467 positions
    - Profit Factor: 1.35
    - 100% BTC markets, 100% SELL-side fills at $0.51
    - Position sizes: $1,000-$2,000 (70% of positions)
    - Active exposure: only $2,837 at any time
    - 99.6% on 15-min intervals

    HOW IT WORKS:
    Guy 1 sells contracts that are about to become worthless:
    - If Binance shows BTC going UP → the "Down/No" contract is overpriced
      → SELL "Down" at 0.51, collect $0.51, pay $0.00 at resolution = $0.51 profit
    - If Binance shows BTC going DOWN → the "Up/Yes" contract is overpriced
      → SELL "Up" at 0.51, collect $0.51, pay $0.00 at resolution = $0.51 profit

    The key edge: Polymarket market prices lag Binance by seconds.
    By the time the market adjusts, Guy 1 has already sold at stale prices."""

    # Timing window — trades in the second half of intervals
    # 15-min intervals: trade at 600-780s (10-13 min mark)
    # 5-min intervals: trade at 180-260s (3-4.3 min mark)
    ENTRY_WINDOW_15M_START = 600   # 10 min into 15-min interval
    ENTRY_WINDOW_15M_END = 780     # 13 min into 15-min interval
    ENTRY_WINDOW_5M_START = 180    # 3 min into 5-min interval
    ENTRY_WINDOW_5M_END = 260      # 4.3 min into 5-min interval

    # Guy 1's exact price: 97.6% of fills at $0.51
    SELL_PRICE = 0.51

    # Price gate — only sell when contract is in this range
    MIN_SELL_PRICE = 0.48  # Don't sell below this (too cheap = risky)
    MAX_SELL_PRICE = 0.55  # Don't sell above this (too expensive = market already moved)

    # Position sizing from Guy 1's data
    DEFAULT_POSITION_USD = 1500    # $1,500 per position (median ~$1,200)
    MAX_POSITION_USD = 5000        # Hard cap per single market
    MAX_ACTIVE_EXPOSURE = 10000    # Total across all active positions

    def __init__(
        self,
        min_edge: float = 0.03,
        confidence_threshold: float = 0.55,
        position_usd: float = 1500,
    ):
        self.min_edge = min_edge
        self.confidence_threshold = confidence_threshold
        self.position_usd = min(position_usd, self.MAX_POSITION_USD)

    def evaluate(
        self,
        interval_start_price: float,
        current_binance_price: float,
        current_chainlink_price: float,
        market_up_price: float,
        market_down_price: float,
        seconds_into_interval: int,
        interval_duration: int = 900,
        current_exposure_usd: float = 0,
    ) -> Signal:
        """Evaluate whether there's a tradeable latency arb opportunity.

        Args:
            interval_start_price: BTC price at the start of this interval
                                  (from Chainlink oracle snapshot)
            current_binance_price: Latest BTC price from Binance (fast feed)
            current_chainlink_price: Latest BTC price from Chainlink RTDS
            market_up_price: Current Polymarket price for "Up/Yes" shares
            market_down_price: Current Polymarket price for "Down/No" shares
            seconds_into_interval: How far into the interval we are
            interval_duration: Total interval duration in seconds (300 or 900)
            current_exposure_usd: Current total active position exposure in USD
        """
        # EXPOSURE GATE: Don't exceed max active exposure (Guy 1 keeps it ~$2,837)
        if current_exposure_usd >= self.MAX_ACTIVE_EXPOSURE:
            return Signal(Side.NONE, 0, 0, 0, 0,
                          f"Exposure ${current_exposure_usd:.0f} >= max ${self.MAX_ACTIVE_EXPOSURE}")

        # TIMING GATE: Select window based on interval duration
        if interval_duration >= 900:  # 15-min interval
            window_start = self.ENTRY_WINDOW_15M_START
            window_end = self.ENTRY_WINDOW_15M_END
        else:  # 5-min interval
            window_start = self.ENTRY_WINDOW_5M_START
            window_end = self.ENTRY_WINDOW_5M_END

        if seconds_into_interval < window_start:
            return Signal(Side.NONE, 0, 0, 0, 0,
                          f"Too early ({seconds_into_interval}s < {window_start}s)")
        if seconds_into_interval > window_end:
            return Signal(Side.NONE, 0, 0, 0, 0,
                          f"Too late ({seconds_into_interval}s > {window_end}s)")

        # Calculate directional signal from Binance (fast feed)
        binance_delta = current_binance_price - interval_start_price
        binance_pct_move = binance_delta / interval_start_price

        # Chainlink confirmation
        chainlink_delta = current_chainlink_price - interval_start_price
        feeds_agree = (binance_delta > 0) == (chainlink_delta > 0)

        time_factor = seconds_into_interval / interval_duration
        move_magnitude = abs(binance_pct_move) * 10000  # in bps

        # Need at least 3bps movement to have directional conviction
        if move_magnitude < 3:
            return Signal(Side.NONE, 0, 0, 0, 0, "Move too small (<3bps)")

        # Confidence model — later in interval = higher predictive power
        raw_confidence = min(0.95, 0.50 + (move_magnitude / 80) * time_factor)

        # Dual feed confirmation bonus
        if feeds_agree:
            raw_confidence = min(0.97, raw_confidence + 0.05)
        else:
            raw_confidence *= 0.85

        # SELL-SIDE LOGIC (Guy 1's actual approach):
        # Sell the contract on the LOSING side (the one about to go to $0)
        if binance_delta > 0:
            # BTC going UP → "Down/No" contract will be worthless → SELL it
            side = Side.SELL_DOWN
            target_price = market_down_price
            # Edge = probability contract goes to 0 × sell price
            # If we sell at 0.51 and it resolves to 0, we keep $0.51
            edge = raw_confidence * target_price - (1 - raw_confidence) * (1 - target_price)
        else:
            # BTC going DOWN → "Up/Yes" contract will be worthless → SELL it
            side = Side.SELL_UP
            target_price = market_up_price
            edge = raw_confidence * target_price - (1 - raw_confidence) * (1 - target_price)

        # PRICE GATE: Only sell in the sweet spot (Guy 1: 97.6% at 0.51)
        if target_price < self.MIN_SELL_PRICE:
            return Signal(Side.NONE, raw_confidence, edge, 0, 0,
                          f"Price ${target_price:.2f} too low to sell (min ${self.MIN_SELL_PRICE})")
        if target_price > self.MAX_SELL_PRICE:
            return Signal(Side.NONE, raw_confidence, edge, 0, 0,
                          f"Price ${target_price:.2f} too high to sell (max ${self.MAX_SELL_PRICE})")

        # EDGE GATE
        if edge < self.min_edge:
            return Signal(Side.NONE, raw_confidence, edge, 0, 0,
                          f"Edge {edge:.3f} below threshold {self.min_edge}")

        # CONFIDENCE GATE
        if raw_confidence < self.confidence_threshold:
            return Signal(Side.NONE, raw_confidence, edge, 0, 0,
                          f"Confidence {raw_confidence:.3f} below threshold")

        # Position sizing — fixed USD amount like Guy 1
        # Scale down if remaining exposure budget is small
        remaining_budget = self.MAX_ACTIVE_EXPOSURE - current_exposure_usd
        size_usd = min(self.position_usd, remaining_budget)

        if size_usd < 100:
            return Signal(Side.NONE, raw_confidence, edge, 0, 0, "Size too small")

        # Use the SELL_PRICE (0.51) as the limit price, not the current market price
        sell_price = self.SELL_PRICE

        return Signal(
            side=side,
            confidence=raw_confidence,
            edge=edge,
            price=sell_price,
            size=round(size_usd, 2),
            reason=(
                f"SELL {'Up' if side == Side.SELL_UP else 'Down'} @ ${sell_price} | "
                f"BTC {'UP' if binance_delta > 0 else 'DOWN'} "
                f"{move_magnitude:.1f}bps | "
                f"conf={raw_confidence:.1%} edge={edge:.1%} | "
                f"feeds={'AGREE' if feeds_agree else 'DISAGREE'} | "
                f"t={seconds_into_interval}s/{interval_duration}s"
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
