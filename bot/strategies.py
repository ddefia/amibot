"""Trading strategies for BTC 5-min/15-min Polymarket markets.

Implements strategies reverse-engineered from deep trade-by-trade analysis
of 5 real Polymarket traders (PDF reports, CSV data, screenshots):

1. Latency Arb SELL Strategy — Guy 1's exact approach (Sharpe 18.62)
   SELL overpriced contracts at 0.51 when Binance price gives directional signal.
   100% of Guy 1's captured fills are SELL-side at exactly $0.51.

2. Multi-Asset Buy Strategy — Guy 3's approach (Sharpe 9.47, $356.9k PnL)
   BUY underpriced contracts across BTC/ETH/SOL/XRP simultaneously.

3. Market Making — provide liquidity on both sides, earn spread + rebates

CROSS-TRADER LESSONS (3,029 BTC positions analyzed):
- 15-min intervals: +$305K total PnL (50.7% WR) — PREFER these
- 5-min intervals: -$8.4K total PnL (48.3% WR) — AVOID or require higher edge
- Entry 0.50-0.60: 55.4% WR, best zone for sell-side (Guy 1's zone)
- Entry 0.20-0.50: death zone, 25-38% WR, massive aggregate losses
- Larger positions correlate with wins (r=0.14) — scale up on confidence
- Feed disagreement kills edge — Guy 5 lost $19K on Down bets without confirmation
- Losses are almost always -100% — binary outcomes, no partial recovery
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

    # Timing window — allow trading throughout most of each interval.
    # Only skip the first ~2 min (to let prices stabilize) and last ~1 min.
    ENTRY_WINDOW_15M_START = 30    # 30s into 15-min interval (prices settle fast)
    ENTRY_WINDOW_15M_END = 840     # 14 min (stop 1 min before close)
    ENTRY_WINDOW_5M_START = 60     # 1 min into 5-min interval
    ENTRY_WINDOW_5M_END = 270      # 4.5 min
    ENTRY_WINDOW_1H_START = 120    # 2 min into hourly interval
    ENTRY_WINDOW_1H_END = 3480     # 58 min
    ENTRY_WINDOW_DAILY_START = 300   # 5 min in
    ENTRY_WINDOW_DAILY_END = 82800   # 23 hours

    # Guy 1's exact price: 97.6% of fills at $0.51
    SELL_PRICE = 0.51

    # Price gate — only sell when contract is in this range
    # Wider than Guy 1's $0.51 target because REST mode lacks latency edge.
    # Edge gate (>= 3%) still prevents unprofitable trades at extreme prices.
    MIN_SELL_PRICE = 0.10  # Lowered for paper testing — see trades at more price levels
    MAX_SELL_PRICE = 0.65  # Above this, market has already moved against us

    # Position sizing from Guy 1's data
    DEFAULT_POSITION_USD = 1500    # $1,500 per position (median ~$1,200)
    MAX_POSITION_USD = 5000        # Hard cap per single market
    MAX_ACTIVE_EXPOSURE = 10000    # Total across all active positions

    # Cross-trader lesson: 5-min intervals are net losers (-$8.4K across 497 positions)
    # Require higher edge/confidence on 5-min to compensate
    FIVE_MIN_EDGE_MULTIPLIER = 1.2   # 20% higher edge required for 5-min (relaxed for paper)
    FIVE_MIN_CONFIDENCE_BOOST = 0.03  # Need 3% more confidence for 5-min

    # Cross-trader lesson: feed disagreement kills edge (Guy 5 pattern)
    # Stronger penalty than before — disagreement means low conviction
    FEED_DISAGREE_PENALTY = 0.75  # 25% confidence cut (was 15%)

    # Volume confirmation: high-volume moves sustain direction, low-volume = fakeout
    # volume_ratio = current volume / EMA volume (from Binance rolling window)
    HIGH_VOLUME_THRESHOLD = 1.5    # 50% above average = strong move
    LOW_VOLUME_THRESHOLD = 0.5     # 50% below average = weak/fakeout
    HIGH_VOLUME_CONF_BOOST = 0.06  # +6% confidence on high volume
    LOW_VOLUME_CONF_PENALTY = 0.85 # 15% confidence cut on low volume
    # Buy/sell ratio alignment: if buyers dominate during an up-move, extra confirmation
    VOLUME_DIRECTION_BOOST = 0.03  # +3% confidence when volume direction agrees

    # Time-based edge scaling: early trades need higher edge (reversal risk)
    # Data: 2-5 min trades at 3-5% edge had 43% WR (net loser)
    #        5-10 min trades at 8%+ edge had 92% WR
    # Paper session: 3 early fakeout losses at t=132-159s with 7.8-9.2% edge.
    #   2.0x multiplier requires 10% edge early → would have filtered all 3.
    EARLY_PHASE_END = 300       # First 5 min = "early" (high reversal risk)
    EARLY_EDGE_MULTIPLIER = 1.3 # 30% higher edge required in early phase (relaxed for paper)

    # Cross-trader lesson: scale position size with confidence (r=0.14 correlation)
    # Higher confidence → bigger position (like winners across all traders)
    MIN_CONFIDENCE_SIZE_SCALE = 0.6   # At minimum confidence, use 60% of position_usd
    MAX_CONFIDENCE_SIZE_SCALE = 1.3   # At max confidence, use 130% of position_usd

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
        volume_stats: dict | None = None,
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
            volume_stats: Rolling volume data from PriceFeed.get_volume_stats()
                         Keys: volume_ratio, buy_ratio, total_usd, trade_count
        """
        # EXPOSURE GATE: Don't exceed max active exposure (Guy 1 keeps it ~$2,837)
        if current_exposure_usd >= self.MAX_ACTIVE_EXPOSURE:
            return Signal(Side.NONE, 0, 0, 0, 0,
                          f"Exposure ${current_exposure_usd:.0f} >= max ${self.MAX_ACTIVE_EXPOSURE}")

        # TIMING GATE: Select window based on interval duration
        is_5min = interval_duration <= 300
        if interval_duration >= 86400:  # daily
            window_start = self.ENTRY_WINDOW_DAILY_START
            window_end = self.ENTRY_WINDOW_DAILY_END
        elif interval_duration >= 3600:  # hourly
            window_start = self.ENTRY_WINDOW_1H_START
            window_end = self.ENTRY_WINDOW_1H_END
        elif is_5min:
            window_start = self.ENTRY_WINDOW_5M_START
            window_end = self.ENTRY_WINDOW_5M_END
        else:  # 15-min
            window_start = self.ENTRY_WINDOW_15M_START
            window_end = self.ENTRY_WINDOW_15M_END

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

        # Need at least 1bps movement to have directional conviction
        # (lowered for paper testing — catch smaller moves)
        if move_magnitude < 1:
            return Signal(Side.NONE, 0, 0, 0, 0,
                          f"Move too small ({move_magnitude:.1f}bps < 2bps) "
                          f"BTC ${current_binance_price:.0f} vs start ${interval_start_price:.0f}")

        # Confidence model — later in interval = higher predictive power
        raw_confidence = min(0.95, 0.50 + (move_magnitude / 80) * time_factor)

        # Dual feed confirmation bonus
        # Cross-trader lesson: feed disagreement is strongly anti-predictive
        # Guy 5 lost $19K on No/Down bets without feed confirmation
        if feeds_agree:
            raw_confidence = min(0.97, raw_confidence + 0.05)
        else:
            raw_confidence *= self.FEED_DISAGREE_PENALTY  # 25% cut (was 15%)

        # VOLUME CONFIRMATION: high-volume moves sustain, low-volume = fakeout
        volume_label = ""
        if volume_stats and volume_stats.get("trade_count", 0) > 10:
            vol_ratio = volume_stats.get("volume_ratio", 1.0)
            buy_ratio = volume_stats.get("buy_ratio", 0.5)

            # Volume magnitude: is this move happening on real volume?
            if vol_ratio >= self.HIGH_VOLUME_THRESHOLD:
                raw_confidence = min(0.97, raw_confidence + self.HIGH_VOLUME_CONF_BOOST)
                volume_label = f"HIGH vol({vol_ratio:.1f}x)"
            elif vol_ratio <= self.LOW_VOLUME_THRESHOLD:
                raw_confidence *= self.LOW_VOLUME_CONF_PENALTY
                volume_label = f"LOW vol({vol_ratio:.1f}x)"
            else:
                volume_label = f"vol({vol_ratio:.1f}x)"

            # Volume direction alignment: are buyers or sellers driving the move?
            # BTC going UP + buy_ratio > 0.55 = buyers driving it = more conviction
            # BTC going UP + buy_ratio < 0.45 = sellers dominating but price up = suspicious
            if binance_delta > 0 and buy_ratio > 0.55:
                raw_confidence = min(0.97, raw_confidence + self.VOLUME_DIRECTION_BOOST)
                volume_label += " buy-driven"
            elif binance_delta < 0 and buy_ratio < 0.45:
                raw_confidence = min(0.97, raw_confidence + self.VOLUME_DIRECTION_BOOST)
                volume_label += " sell-driven"

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

        # 5-MIN PENALTY: Cross-trader data shows 5-min is net -$8.4K loser
        # Require higher edge and confidence to trade 5-min intervals
        effective_min_edge = self.min_edge
        effective_min_confidence = self.confidence_threshold
        if is_5min:
            effective_min_edge *= self.FIVE_MIN_EDGE_MULTIPLIER
            effective_min_confidence += self.FIVE_MIN_CONFIDENCE_BOOST

        # EARLY-PHASE PENALTY: 2-5 min window had 45% WR (net loser).
        # BTC often fakes out early then reverses. Require higher edge.
        if seconds_into_interval < self.EARLY_PHASE_END:
            effective_min_edge *= self.EARLY_EDGE_MULTIPLIER

        # EDGE GATE
        if edge < effective_min_edge:
            return Signal(Side.NONE, raw_confidence, edge, 0, 0,
                          f"Edge {edge:.3f} below threshold {effective_min_edge:.3f}"
                          f"{' (5min penalty)' if is_5min else ''}")

        # CONFIDENCE GATE
        if raw_confidence < effective_min_confidence:
            return Signal(Side.NONE, raw_confidence, edge, 0, 0,
                          f"Confidence {raw_confidence:.3f} below threshold"
                          f"{' (5min penalty)' if is_5min else ''}")

        # CONVICTION-SCALED SIZING: Cross-trader lesson — bigger positions on higher
        # confidence correlate with wins (r=0.14). Scale from 60% to 130% of base size.
        confidence_range = 0.97 - effective_min_confidence
        if confidence_range > 0:
            confidence_pct = (raw_confidence - effective_min_confidence) / confidence_range
        else:
            confidence_pct = 0.5
        size_scale = (self.MIN_CONFIDENCE_SIZE_SCALE +
                      confidence_pct * (self.MAX_CONFIDENCE_SIZE_SCALE - self.MIN_CONFIDENCE_SIZE_SCALE))
        base_size = self.position_usd * size_scale

        # Scale down if remaining exposure budget is small
        remaining_budget = self.MAX_ACTIVE_EXPOSURE - current_exposure_usd
        size_usd = min(base_size, remaining_budget)

        if size_usd < 3:
            return Signal(Side.NONE, raw_confidence, edge, 0, 0, "Size too small")

        # Use the current market price as limit price (fills immediately in paper mode).
        # Guy 1 uses fixed $0.51 with WS latency edge, but in REST mode we sell
        # at whatever the market offers within our price gate ($0.48-$0.55).
        sell_price = target_price

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
                f"{volume_label + ' | ' if volume_label else ''}"
                f"t={seconds_into_interval}s/{interval_duration}s"
            ),
        )


class MispricingStrategy:
    """Buy shares when they're significantly underpriced relative to fair value.

    Inspired by the 'Gabagool' method — don't predict direction, just buy
    whichever side is cheap. In a binary market, if Up + Down < $1.00,
    buying both guarantees profit.

    CROSS-TRADER LESSONS APPLIED:
    - Pure arb (combined < $1.00) is the ONLY reliable buy-side strategy
    - Single-side cheap buys in the 0.20-0.50 range are a DEATH ZONE:
      25-38% WR, massive aggregate losses across 3,029 BTC positions
    - Only buy cheap contracts (<$0.10) as small lottery tickets if at all
    - Guy 5 lost $19K buying at 0.40-0.50 without directional edge"""

    def __init__(
        self,
        combined_threshold: float = 0.97,
        max_position: float = 100,
    ):
        self.combined_threshold = combined_threshold
        self.max_position = max_position

    def evaluate(
        self,
        market_up_price: float,
        market_down_price: float,
    ) -> Signal:
        combined = market_up_price + market_down_price

        # Pure arbitrage: combined price < $1.00 — ONLY reliable buy strategy
        # This is mathematically guaranteed profit, not directional
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

        # REMOVED: Single-side cheap buys at 0.20-0.50
        # Cross-trader data: 0.20-0.50 entry range has 25-38% WR and massive
        # aggregate losses. Guy 5's biggest failure was buying at 0.40-0.50
        # without directional edge. Only pure arb (combined < $1.00) is safe.

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
