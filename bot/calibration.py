"""Data-driven calibration edge from 40GB Polymarket historical analysis.

Derived from 8M+ trades across 389K resolved binary markets.
This module provides the empirical calibration curves that tell us
exactly where Polymarket prices are systematically wrong.

KEY FINDINGS (from /analysis/research_data/):
1. Favorite-longshot bias: contracts below 50c are overpriced, above 50c underpriced
2. Sweet spot: 55-65c contracts have +2.0-2.2pp edge (3.2-3.5% ROI)
3. Death zone: 20-45c contracts are systematically overpriced (-1.4 to -1.7% EV)
4. Extreme longshots (0-10c): negative EV, avoid buying
5. Large traders ($500+) show edge in 55-65c but noisy elsewhere
6. 95-100c: capital-inefficient despite positive EV (+0.4%)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceBucketEdge:
    """Empirical edge data for a price range."""
    price_low: float
    price_high: float
    actual_win_rate: float
    implied_prob: float  # midpoint of bucket
    edge_pp: float  # percentage points above/below fair
    ev_pct: float  # expected value per dollar
    sample_size: int
    recommendation: str  # "buy_yes", "buy_no", "avoid", "neutral"


# Raw calibration data from analysis of 8M+ Polymarket trades
# Each entry: (low, high, actual_win_rate, avg_price, edge_pp, ev_pct, count, recommendation)
CALIBRATION_TABLE = [
    PriceBucketEdge(0.00, 0.05, 0.0128, 0.0140, -0.12, -0.12, 821983, "avoid"),
    PriceBucketEdge(0.05, 0.10, 0.0733, 0.0781, -0.48, -0.48, 208487, "avoid"),
    PriceBucketEdge(0.10, 0.15, 0.1243, 0.1292, -0.49, -0.49, 178780, "avoid"),
    PriceBucketEdge(0.15, 0.20, 0.1751, 0.1796, -0.45, -0.45, 187539, "avoid"),
    PriceBucketEdge(0.20, 0.25, 0.2163, 0.2302, -1.39, -1.39, 192195, "fade"),      # overpriced → sell/buy_no
    PriceBucketEdge(0.25, 0.30, 0.2754, 0.2801, -0.47, -0.47, 208434, "avoid"),
    PriceBucketEdge(0.30, 0.35, 0.3236, 0.3297, -0.61, -0.61, 232326, "fade"),
    PriceBucketEdge(0.35, 0.40, 0.3622, 0.3796, -1.74, -1.74, 269116, "fade"),      # DEATH ZONE
    PriceBucketEdge(0.40, 0.45, 0.4140, 0.4299, -1.59, -1.59, 290098, "fade"),      # DEATH ZONE
    PriceBucketEdge(0.45, 0.50, 0.4736, 0.4823, -0.86, -0.86, 430697, "avoid"),
    PriceBucketEdge(0.50, 0.55, 0.5400, 0.5277, +1.23, +1.23, 349677, "buy_yes"),   # CROSSOVER
    PriceBucketEdge(0.55, 0.60, 0.5978, 0.5791, +1.87, +1.87, 284879, "buy_yes"),   # SWEET SPOT
    PriceBucketEdge(0.60, 0.65, 0.6511, 0.6290, +2.22, +2.22, 256135, "buy_yes"),   # BEST EDGE
    PriceBucketEdge(0.65, 0.70, 0.6928, 0.6779, +1.48, +1.48, 232166, "buy_yes"),
    PriceBucketEdge(0.70, 0.75, 0.7358, 0.7290, +0.68, +0.68, 209979, "buy_yes"),
    PriceBucketEdge(0.75, 0.80, 0.7931, 0.7800, +1.31, +1.31, 188289, "buy_yes"),
    PriceBucketEdge(0.80, 0.85, 0.8367, 0.8296, +0.71, +0.71, 175116, "buy_yes"),
    PriceBucketEdge(0.85, 0.90, 0.8895, 0.8795, +1.00, +1.00, 183525, "buy_yes"),
    PriceBucketEdge(0.90, 0.95, 0.9387, 0.9305, +0.81, +0.81, 212362, "buy_yes"),
    PriceBucketEdge(0.95, 1.00, 0.9908, 0.9867, +0.42, +0.42, 764487, "neutral"),   # capital inefficient
]

# Pre-built lookup for fast access
_BUCKET_LOOKUP: dict[int, PriceBucketEdge] = {}
for _b in CALIBRATION_TABLE:
    # Key by the lower bound in cents (0, 5, 10, ...)
    _BUCKET_LOOKUP[int(_b.price_low * 100)] = _b


def get_edge_for_price(price: float) -> PriceBucketEdge | None:
    """Look up the empirical edge for a given contract price (0.0 - 1.0).

    Returns the calibration data for the 5-cent bucket containing this price.
    """
    if price < 0 or price >= 1.0:
        return None
    bucket_key = int(price * 100) // 5 * 5
    return _BUCKET_LOOKUP.get(bucket_key)


def get_calibration_adjustment(price: float) -> float:
    """Get the calibration adjustment (actual_win_rate - implied_prob) for a price.

    Positive = market underprices this outcome (good to buy YES)
    Negative = market overprices this outcome (good to buy NO / avoid YES)

    Returns 0.0 if no data available.
    """
    bucket = get_edge_for_price(price)
    if bucket is None:
        return 0.0
    return bucket.actual_win_rate - bucket.implied_prob


def is_positive_ev(price: float) -> bool:
    """Check if buying YES at this price has positive expected value."""
    return get_calibration_adjustment(price) > 0


def get_ev_per_dollar(price: float) -> float:
    """Get the expected value per dollar risked at this price.

    Positive = expected profit. Negative = expected loss.
    """
    bucket = get_edge_for_price(price)
    if bucket is None:
        return 0.0
    return bucket.ev_pct / 100.0


def get_recommendation(price: float) -> str:
    """Get the trading recommendation for a price level.

    Returns: "buy_yes", "buy_no", "fade", "avoid", or "neutral"
    """
    bucket = get_edge_for_price(price)
    if bucket is None:
        return "avoid"
    return bucket.recommendation


def score_market_opportunity(yes_price: float, no_price: float) -> dict:
    """Score a binary market opportunity using calibration data.

    Returns a dict with:
    - best_side: "yes", "no", or "none"
    - edge: expected edge in percentage points
    - ev_per_dollar: expected return per dollar
    - confidence_boost: how much to boost/reduce confidence
    - reason: human-readable explanation
    """
    yes_bucket = get_edge_for_price(yes_price)
    no_bucket = get_edge_for_price(no_price)

    if yes_bucket is None or no_bucket is None:
        return {
            "best_side": "none", "edge": 0.0, "ev_per_dollar": 0.0,
            "confidence_boost": 0.0, "reason": "No calibration data",
        }

    yes_edge = yes_bucket.actual_win_rate - yes_bucket.implied_prob
    no_edge = no_bucket.actual_win_rate - no_bucket.implied_prob

    # For NO side: buying NO is equivalent to the market overpricing YES
    # If YES is overpriced (negative yes_edge), buying NO is profitable
    # The NO edge from buying NO at no_price:
    no_buy_edge = no_edge  # direct edge on the NO contract

    # But also: if YES is overpriced, that means NO is underpriced
    # We want to pick the side with the MOST mispricing in our favor
    yes_ev = yes_edge  # positive = good to buy YES
    no_ev = -yes_edge  # if YES overpriced, NO is underpriced by same amount

    # Pick the better side
    if yes_ev > no_ev and yes_ev > 0.005:  # At least 0.5pp edge
        # Confidence boost proportional to edge magnitude
        # 2.2pp edge (best) → +0.10 confidence boost
        # 0.5pp edge (minimum) → +0.02 confidence boost
        conf_boost = min(0.10, max(0.02, yes_ev * 4.5))
        return {
            "best_side": "yes",
            "edge": yes_ev * 100,  # in percentage points
            "ev_per_dollar": yes_ev,
            "confidence_boost": conf_boost,
            "reason": f"Calibration: YES at ${yes_price:.2f} has +{yes_ev*100:.1f}pp edge (win rate {yes_bucket.actual_win_rate:.1%} vs implied {yes_bucket.implied_prob:.1%})",
        }
    elif no_ev > yes_ev and no_ev > 0.005:
        conf_boost = min(0.10, max(0.02, no_ev * 4.5))
        return {
            "best_side": "no",
            "edge": no_ev * 100,
            "ev_per_dollar": no_ev,
            "confidence_boost": conf_boost,
            "reason": f"Calibration: YES at ${yes_price:.2f} is overpriced by {no_ev*100:.1f}pp → buy NO",
        }
    else:
        return {
            "best_side": "none",
            "edge": 0.0,
            "ev_per_dollar": 0.0,
            "confidence_boost": 0.0,
            "reason": f"No calibration edge at YES=${yes_price:.2f} (edge={yes_ev*100:.2f}pp)",
        }


# Trade size edge data: larger trades are slightly more informed
TRADE_SIZE_EDGE = {
    # size_bucket: (win_rate, edge_pp)
    "micro": (0.407, -0.05),    # < $10
    "small": (0.649, +0.43),    # $10-50
    "medium": (0.714, +0.53),   # $50-100
    "large": (0.711, +0.87),    # $100-500
    "whale": (0.746, +0.94),    # $500-1K
    "mega": (0.760, +0.82),     # $1K-5K
}


def get_size_edge_multiplier(trade_size_usd: float) -> float:
    """Get edge multiplier based on trade size.

    Larger trades are empirically more accurate. Returns a multiplier
    for confidence (1.0 = neutral, >1.0 = boost, <1.0 = penalty).

    Based on: trades >$100 have +0.87pp edge vs <$10 at -0.05pp.
    """
    if trade_size_usd < 10:
        return 0.98  # Slight penalty — micro trades are noise
    elif trade_size_usd < 50:
        return 1.00  # Neutral
    elif trade_size_usd < 100:
        return 1.01
    elif trade_size_usd < 500:
        return 1.02
    elif trade_size_usd < 1000:
        return 1.03
    else:
        return 1.02  # Slightly less for mega — noisy at extremes


# Volume distribution: where retail money concentrates
# High retail concentration = more mispricing opportunity
RETAIL_CONCENTRATION = {
    # price_range: pct_of_total_volume
    (0.00, 0.10): 1.22,   # 1.2% of volume — thin, avoid
    (0.10, 0.20): 1.52,   # thin
    (0.20, 0.30): 2.28,   # starting to get volume
    (0.30, 0.40): 5.22,   # decent volume, death zone
    (0.40, 0.50): 11.38,  # heavy retail, still death zone
    (0.50, 0.60): 10.81,  # crossover zone — good
    (0.60, 0.70): 9.19,   # SWEET SPOT — good volume + best edge
    (0.70, 0.80): 6.32,
    (0.80, 0.90): 7.79,
    (0.90, 1.00): 44.26,  # massive retail in "safe" bets
}
