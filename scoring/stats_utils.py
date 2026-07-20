"""
Statistical utility functions for the Edge scoring engine.
Shared across all positions — EWMA for volume stats, shrinkage regression for TD/turnover rates.
"""

from typing import Sequence


def ewma(values: Sequence[float], half_life: float = 2.5) -> float:
    """
    Exponentially-weighted moving average.
    `values` should be in chronological order, oldest first, most recent last.
    `half_life` controls how quickly older games lose weight (in games).
    Returns the EWMA-weighted average, with the most recent game weighted highest.

    A half-life of 2.5 means a game 2.5 games ago has half the weight of the most recent game.
    """
    if not values:
        return 0.0

    n = len(values)
    # decay factor per game, derived from half-life
    decay = 0.5 ** (1 / half_life)

    weights = [decay ** (n - 1 - i) for i in range(n)]
    weighted_sum = sum(v * w for v, w in zip(values, weights))
    weight_total = sum(weights)

    return weighted_sum / weight_total if weight_total > 0 else 0.0


def regressed_rate(
    event_count: int,
    attempt_count: int,
    league_mean_rate: float,
    k: float = 10.0,
) -> float:
    """
    Shrinkage/regression-to-mean for a rate stat (e.g. TD rate, INT rate, turnover-worthy rate).

    event_count: raw count of the event (e.g. TDs thrown)
    attempt_count: raw count of attempts/opportunities (e.g. pass attempts)
    league_mean_rate: the league-average rate for this stat (e.g. 0.045 for QB TD rate)
    k: pseudo-count — how many league-average attempts worth of skepticism to apply.
       Higher k = more regression toward league mean (use higher k for low-volume situations,
       e.g. goal-line-only backs; lower k for high-volume passers).

    Formula: (event_count + k * league_mean_rate) / (attempt_count + k)
    """
    if attempt_count < 0 or k < 0:
        raise ValueError("attempt_count and k must be non-negative")

    return (event_count + k * league_mean_rate) / (attempt_count + k)


def bucket_bonus(value: float, tiers: list[dict]) -> float:
    """
    Given a stat value (e.g. passing yards = 320) and a list of tier dicts like:
        [{"min": 300, "max": 399, "bonus": 1}, {"min": 400, "max": None, "bonus": 2}]
    returns the bonus points for whichever tier the value falls into, or 0 if none match.
    A `max` of None means "no upper bound" (e.g. 400+).
    """
    for tier in tiers:
        lo = tier["min"]
        hi = tier["max"]
        if value >= lo and (hi is None or value <= hi):
            return tier["bonus"]
    return 0.0


def tiered_points(value: float, tiers: list[dict]) -> float:
    """
    Same shape as bucket_bonus, but for tiers keyed by 'points' instead of 'bonus'
    (used for kicker FG makes/misses and DST points/yards-allowed tiers, which are
    mutually-exclusive point tiers rather than additive bonuses).
    """
    for tier in tiers:
        lo = tier["min"]
        hi = tier["max"]
        if value >= lo and (hi is None or value <= hi):
            return tier["points"]
    return 0.0