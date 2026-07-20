"""
DST feature builder for the Edge scoring engine.
opponent_implied_total is the single strongest input per edge_formula_nfl.md —
the market's team-total estimate does most of the work for points-allowed tier
and takeaway upside.
"""

from scoring.stats_utils import ewma, regressed_rate


LEAGUE_MEAN_TURNOVER_WORTHY_RATE = 0.03   # per opponent offensive play, rough placeholder
TURNOVER_RATE_K = 80


def build_dst_features(
    opponent_implied_total: float,
    team_sack_rate_history: list[float],
    opponent_sack_rate_allowed_history: list[float],
    opponent_turnovers_history: list[int],
    opponent_plays_history: list[int],
    opponent_baseline_yards_allowed_per_game: float,
    half_life: float = 2.5,
) -> dict:
    """
    opponent_implied_total: the opposing offense's de-vig'd implied point total
      (from vegas_features.team_implied_total, called with the opponent's team_id).
    opponent_baseline_yards_allowed_per_game: derived from opponent's baseline
      offensive efficiency (not raw season yardage, to avoid garbage-time
      yardage inflation skewing this — computed upstream, passed in here).
    """
    team_sack_rate = ewma(team_sack_rate_history, half_life)
    opponent_sack_rate_allowed = ewma(opponent_sack_rate_allowed_history, half_life)
    sack_matchup = team_sack_rate * opponent_sack_rate_allowed / max(team_sack_rate, 0.0001) \
        if team_sack_rate > 0 else opponent_sack_rate_allowed
    # Simplified: sack matchup as the average of team's own rate and how much
    # this opponent tends to allow, rather than a pure multiplicative blend —
    # avoids over-amplifying when either side has a small/noisy sample.
    sack_matchup = (team_sack_rate + opponent_sack_rate_allowed) / 2

    total_turnovers = sum(opponent_turnovers_history) if opponent_turnovers_history else 0
    total_plays = sum(opponent_plays_history) if opponent_plays_history else 0
    opponent_turnover_worthy_rate = regressed_rate(
        event_count=total_turnovers,
        attempt_count=total_plays,
        league_mean_rate=LEAGUE_MEAN_TURNOVER_WORTHY_RATE,
        k=TURNOVER_RATE_K,
    )

    return {
        "opponent_implied_total": opponent_implied_total,
        "sack_matchup": sack_matchup,
        "opponent_turnover_worthy_rate": opponent_turnover_worthy_rate,
        "opponent_yards_allowed_tier_input": opponent_baseline_yards_allowed_per_game,
    }