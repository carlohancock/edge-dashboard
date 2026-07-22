"""
QB feature builder for the Edge scoring engine.
Computes projected passing/rushing stats for a QB in a given week, per
edge_formula_nfl.md. Takes raw historical stat lists as input (position-agnostic
about data source) so it can be tested independently of the live pipeline.
"""

from scoring.stats_utils import ewma, regressed_rate
from scoring.vegas_features import game_script


# Reasonable v1 starting constants (to be replaced by regression-fit values
# once real outcome data exists — see Phase 5 calibration plan)
BETA_SCRIPT_QB_ATTEMPTS = 0.15   # how much game_script shifts attempt volume
LEAGUE_MEAN_TD_RATE = 0.045      # ~4.5% of attempts result in a passing TD, league-wide
LEAGUE_MEAN_INT_RATE = 0.022     # ~2.2% of attempts result in an INT, league-wide
TD_RATE_K = 150                  # shrinkage pseudo-count for pass TD rate
QB_RUSH_TD_PER_GAME_K = 8        # weekly mirror of season qb_rush_td_per_game k=15; tunable (Phase 5)
INT_RATE_K = 150                 # shrinkage pseudo-count for INT rate


def build_qb_features(
    pass_attempts_history: list[float],
    pass_yards_history: list[float],
    pass_td_history: list[int],
    pass_int_history: list[int],
    rush_attempts_history: list[float],
    rush_yards_history: list[float],
    team_spread: float,
    opponent_pass_def_factor: float = 1.0,
    half_life: float = 2.5,
    rush_td_history: list[int] | None = None,
) -> dict:
    """
    Computes projected QB features for one upcoming game.

    All *_history lists should be in chronological order (oldest first, most
    recent last), one entry per past game.

    opponent_pass_def_factor: ratio of opponent's pass yards allowed vs. league
    average (1.0 = league average defense, >1.0 = defense allows more than
    average / is worse, <1.0 = stingier than average).

    Returns a dict of projected features, ready to feed into the points calculator.
    """
    baseline_attempts = ewma(pass_attempts_history, half_life)
    total_attempts = sum(pass_attempts_history) if pass_attempts_history else 0
    total_pass_yards = sum(pass_yards_history) if pass_yards_history else 0
    total_pass_tds = sum(pass_td_history) if pass_td_history else 0
    total_pass_ints = sum(pass_int_history) if pass_int_history else 0

    g = game_script(team_spread)
    adj_attempts = baseline_attempts * (1 + BETA_SCRIPT_QB_ATTEMPTS * g)

    baseline_ypa = (total_pass_yards / total_attempts) if total_attempts > 0 else 0.0
    ypa_matchup = baseline_ypa * opponent_pass_def_factor

    td_rate = regressed_rate(
        event_count=total_pass_tds,
        attempt_count=total_attempts,
        league_mean_rate=LEAGUE_MEAN_TD_RATE,
        k=TD_RATE_K,
    )
    int_rate = regressed_rate(
        event_count=total_pass_ints,
        attempt_count=total_attempts,
        league_mean_rate=LEAGUE_MEAN_INT_RATE,
        k=INT_RATE_K,
    )

    proj_pass_yards = adj_attempts * ypa_matchup
    proj_pass_tds = adj_attempts * td_rate
    proj_pass_ints = adj_attempts * int_rate

    # Rushing sub-term (mobile QBs) — simple EWMA-based projection, no
    # game-script adjustment applied here since rushing volume for a QB is
    # more about individual running style than team game-flow.
    baseline_rush_attempts = ewma(rush_attempts_history, half_life)
    total_rush_attempts = sum(rush_attempts_history) if rush_attempts_history else 0
    total_rush_yards = sum(rush_yards_history) if rush_yards_history else 0
    total_rush_tds = sum(rush_td_history) if rush_td_history else 0
    ypc = (total_rush_yards / total_rush_attempts) if total_rush_attempts > 0 else 0.0
    proj_rush_yards = baseline_rush_attempts * ypc

    games_in_window = len(rush_attempts_history)
    # Prior loaded from 2025 draft-pool p25 at runtime in Draft Edge; weekly uses the
    # same documented fallback (0.0 on 2025 pool) until a live baseline is wired here.
    rush_td_per_game_prior = 0.0
    rush_tds_per_game = regressed_rate(
        event_count=total_rush_tds,
        attempt_count=games_in_window,
        league_mean_rate=rush_td_per_game_prior,
        k=QB_RUSH_TD_PER_GAME_K,
    )
    proj_rush_tds = rush_tds_per_game

    return {
        "proj_pass_attempts": adj_attempts,
        "proj_pass_yards": proj_pass_yards,
        "proj_pass_tds": proj_pass_tds,
        "proj_pass_ints": proj_pass_ints,
        "proj_rush_attempts": baseline_rush_attempts,
        "proj_rush_yards": proj_rush_yards,
        "proj_rush_tds": proj_rush_tds,
        "game_script": g,
    }
