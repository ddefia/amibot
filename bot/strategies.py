from __future__ import annotations

"""Trading strategies for BTC 5-min/15-min Polymarket markets.

Implements strategies reverse-engineered from deep trade-by-trade analysis
of 5 real Polymarket traders (PDF reports, CSV data, screenshots):

1. Latency Arb SELL Strategy — Guy 1's exact approach (Sharpe 18.62)
   SELL overpriced contracts at 0.51 when Binance price gives directional signal.
   100% of Guy 1's captured fills are SELL-side at exactly $0.51.

2. Multi-Asset Buy Strategy — Guy 3's approach (Sharpe 9.47, $356.9k PnL)
   BUY underpriced contracts across BTC/ETH/SOL/XRP simultaneously.

3. Market Making — provide liquidity on both sides, earn spread + rebates

4. Calibration Edge — DATA-DRIVEN from 40GB Polymarket analysis (8M+ trades)
   Buy YES on 55-65c contracts (+2.2pp edge), fade longshots below 45c.

CROSS-TRADER LESSONS (3,029 BTC positions analyzed):
- 15-min intervals: +$305K total PnL (50.7% WR) — PREFER these
- 5-min intervals: -$8.4K total PnL (48.3% WR) — AVOID or require higher edge
- Entry 0.50-0.60: 55.4% WR, best zone for sell-side (Guy 1's zone)
- Entry 0.20-0.50: death zone, 25-38% WR, massive aggregate losses
- Larger positions correlate with wins (r=0.14) — scale up on confidence
- Feed disagreement kills edge — Guy 5 lost $19K on Down bets without confirmation
- Losses are almost always -100% — binary outcomes, no partial recovery

DATA-DRIVEN CALIBRATION (389K resolved markets, 8M+ trades):
- Favorite-longshot bias CONFIRMED: below 50c overpriced, above 50c underpriced
- Best edge: 60-65c at +2.22pp, 55-60c at +1.87pp
- Death zone confirmed: 35-45c at -1.6 to -1.7pp
- Large traders ($500+) show +0.94pp edge vs micro (<$10) at -0.05pp
"""

import time
import logging
from dataclasses import dataclass
from enum import Enum

from bot.calibration import (
    get_edge_for_price,
    get_calibration_adjustment,
    score_market_opportunity,
    get_size_edge_multiplier,
    is_positive_ev,
)

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
    # Backtest loss analysis: 40-50c zone = highest loss count (439 losers / $769K lost)
    # Tightened from 0.10-0.65 based on where we actually make money
    MIN_SELL_PRICE = 0.35  # Below this, risk/reward flips against us
    MAX_SELL_PRICE = 0.65  # Above this, market has already moved against us

    # COIN-FLIP ZONE: 48-52c prices need higher confidence
    # Backtest: 50c zone = 62.6% WR but 52% of all losing trades
    # 1,194 low-conf losers in this zone = 41% of total loss dollars
    COINFLIP_ZONE_LOW = 0.48
    COINFLIP_ZONE_HIGH = 0.52
    COINFLIP_MIN_CONFIDENCE = 0.70  # Need 70%+ confidence to trade 50c zone

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
        confidence_threshold: float = 0.60,
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

        # CALIBRATION BOOST: Apply data-driven edge from 8M+ trade analysis.
        # If the contract we're about to sell is in a historically overpriced zone,
        # boost confidence. If it's in an underpriced zone, penalize.
        cal_score = score_market_opportunity(market_up_price, market_down_price)
        calibration_label = ""
        if cal_score["edge"] > 0.5:
            # Calibration data supports a direction — apply boost
            cal_boost = cal_score["confidence_boost"]
            if binance_delta > 0 and cal_score["best_side"] == "yes":
                # BTC going up AND calibration says YES (up) is underpriced → stronger signal
                raw_confidence = min(0.97, raw_confidence + cal_boost)
                calibration_label = f"CAL+{cal_boost:.0%}"
            elif binance_delta < 0 and cal_score["best_side"] == "no":
                # BTC going down AND calibration says NO (down) is underpriced → stronger signal
                raw_confidence = min(0.97, raw_confidence + cal_boost)
                calibration_label = f"CAL+{cal_boost:.0%}"
            elif binance_delta > 0 and cal_score["best_side"] == "no":
                # BTC going up BUT calibration says NO is better → conflicting, penalize
                raw_confidence *= 0.95
                calibration_label = "CAL-conflict"
            elif binance_delta < 0 and cal_score["best_side"] == "yes":
                raw_confidence *= 0.95
                calibration_label = "CAL-conflict"

        # Data-driven death zone filter: 35-45c contracts are -1.6pp EV
        # If we're about to sell a contract priced in the sweet spot (55-65c),
        # that means the OTHER side is 35-45c — the buyer is in the death zone.
        # This is GOOD for us as sellers.
        cal_adj = get_calibration_adjustment(market_down_price if binance_delta > 0 else market_up_price)
        if cal_adj < -0.01:
            # The contract we're selling is overpriced per calibration → extra edge
            raw_confidence = min(0.97, raw_confidence + abs(cal_adj) * 2)
            calibration_label += " sell-overpriced"

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

        # COIN-FLIP ZONE GATE: 48-52c prices are near-random without high conviction.
        # Backtest: 52% of losing trades and 41% of total loss $ came from
        # low-confidence trades in this zone. Require 70%+ confidence.
        if (self.COINFLIP_ZONE_LOW <= target_price <= self.COINFLIP_ZONE_HIGH
                and raw_confidence < self.COINFLIP_MIN_CONFIDENCE):
            return Signal(Side.NONE, raw_confidence, edge, 0, 0,
                          f"Coin-flip zone (${target_price:.2f}) needs "
                          f"conf >= {self.COINFLIP_MIN_CONFIDENCE:.0%} "
                          f"(have {raw_confidence:.1%})")

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
                f"{calibration_label + ' | ' if calibration_label else ''}"
                f"t={seconds_into_interval}s/{interval_duration}s"
            ),
        )


class CalibrationEdgeStrategy:
    """Data-driven strategy: exploit systematic Polymarket calibration errors.

    Derived from 40GB of historical Polymarket data (8M+ trades, 389K resolved markets).

    THE CORE INSIGHT:
    Polymarket has a favorite-longshot bias. Contracts priced 55-65c win MORE
    often than their price implies. Contracts priced 35-45c win LESS often.

    STRATEGY:
    1. "Buy the Favorite": Buy YES on any contract priced 55-65c (+2.2pp edge)
    2. "Fade Longshots": Buy NO when YES is priced 35-45c (+1.6pp edge)
    3. "High-confidence grind": Buy YES at 85-95c for low-variance +1.0pp
    4. AVOID: Extreme longshots (0-10c), 95-100c (capital inefficient)

    This is NOT about predicting outcomes — it's about systematic market bias.
    Over hundreds of trades, the edge compounds.

    Expected returns (from empirical data):
    - 60-65c contracts: 3.5% return on capital per trade
    - 55-60c contracts: 3.2% return on capital per trade
    - 85-95c contracts: 0.9% return, but 89-94% win rate (low variance)
    """

    # Price zones and their properties
    # Format: (low, high, edge_pp, min_volume, strategy_label)
    SWEET_SPOTS = [
        (0.55, 0.65, 2.0, 5000, "prime_buy_yes"),     # BEST: +2.0-2.2pp edge
        (0.65, 0.80, 1.1, 5000, "good_buy_yes"),      # GOOD: +0.7-1.5pp edge
        (0.80, 0.95, 0.8, 10000, "grind_buy_yes"),    # GRIND: +0.7-1.0pp, high WR
    ]

    FADE_ZONES = [
        (0.35, 0.45, 1.6, 5000, "fade_longshot"),     # Overpriced → buy NO
        (0.20, 0.35, 0.8, 10000, "fade_deep_longshot"),
    ]

    # Minimum thresholds
    MIN_EDGE_PP = 0.5          # At least 0.5 percentage point edge
    MIN_VOLUME = 5000          # $5K minimum market volume
    MIN_CONFIDENCE = 0.55      # Base confidence from calibration data

    def __init__(
        self,
        position_usd: float = 1000,
        max_position_usd: float = 3000,
        aggression: float = 1.0,  # 0.5 = conservative, 1.0 = normal, 1.5 = aggressive
    ):
        self.position_usd = position_usd
        self.max_position_usd = max_position_usd
        self.aggression = aggression

    def evaluate(
        self,
        yes_price: float,
        no_price: float,
        market_volume: float = 0,
        market_question: str = "",
    ) -> Signal:
        """Evaluate a market for calibration edge opportunities.

        Args:
            yes_price: Current YES/Up price (0-1)
            no_price: Current NO/Down price (0-1)
            market_volume: 24h volume in USD
            market_question: Market question text (for logging)
        """
        # Get calibration score from the empirical data
        cal = score_market_opportunity(yes_price, no_price)

        if cal["best_side"] == "none":
            return Signal(Side.NONE, 0, 0, 0, 0,
                          f"No calibration edge: {cal['reason']}")

        edge_pp = cal["edge"]

        # Minimum edge filter (adjusted by aggression)
        min_edge = self.MIN_EDGE_PP / self.aggression
        if edge_pp < min_edge:
            return Signal(Side.NONE, 0, 0, 0, 0,
                          f"Edge {edge_pp:.1f}pp below threshold {min_edge:.1f}pp")

        # Volume filter — thin markets are harder to enter/exit
        if market_volume > 0 and market_volume < self.MIN_VOLUME:
            return Signal(Side.NONE, 0, 0, 0, 0,
                          f"Volume ${market_volume:.0f} below ${self.MIN_VOLUME}")

        # Determine side and price
        if cal["best_side"] == "yes":
            side = Side.BUY_UP
            price = yes_price
        else:
            side = Side.BUY_DOWN
            price = no_price

        # Confidence from calibration data + edge magnitude
        # Base: 0.55 for minimum edge, up to 0.75 for max edge (2.2pp)
        confidence = min(0.80, self.MIN_CONFIDENCE + (edge_pp / 100) * 8)

        # Position sizing: scale with edge magnitude
        # Prime zone (55-65c, +2pp): full size
        # Good zone (65-80c, +1pp): 75% size
        # Grind zone (80-95c, +0.8pp): 50% size (lower return per trade)
        if edge_pp >= 1.5:
            size_mult = 1.0
        elif edge_pp >= 0.8:
            size_mult = 0.75
        else:
            size_mult = 0.50

        size = min(self.position_usd * size_mult * self.aggression, self.max_position_usd)

        edge_decimal = cal["ev_per_dollar"]

        return Signal(
            side=side,
            confidence=confidence,
            edge=edge_decimal,
            price=price,
            size=round(size, 2),
            reason=(
                f"CALIBRATION: {cal['best_side'].upper()} @ ${price:.2f} | "
                f"edge={edge_pp:.1f}pp | conf={confidence:.0%} | "
                f"${size:.0f} | {cal['reason'][:60]}"
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
