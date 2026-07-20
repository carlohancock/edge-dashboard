"""
WR/TE feature builder for the Edge scoring engine.
Single usage stream (targets) per edge_formula_nfl.md, with aDOT tracked
separately to distinguish possession-receiver floor from deep-threat variance.
"""

from scoring.stats_utils import ewma, regressed_rate
from scoring.vegas_features import game_script


BETA_SCRIPT_TEAM_VOLUME = 0.15
LEAGUE_MEAN_REC_TD_RATE = 0.06   # slightly higher than RB's per-target rate — WR/TE see more red-zone/end-zone targets on average
REC_TD_RATE_K = 60


def build_wr_te_features(
    target_share_history: list[float],
    team_pass_attempts_baseline: float,
    targets_history: list[float],
    receptions_history: list[float],
    rec_yards_history: list[float],
    rec_td_history: list[int],
    adot_history: list[float],
    team_spread: float,
    opponent_pass_def_factor: float = 1.0,
    half_life: float = 2.5,
) -> dict:
    """
    adot_history: average depth of target per game (yards downfield at the
      moment of the target, not yards after catch) — tracked as its own EWMA
      to characterize a player's role (possession vs. deep threat), not used
      directly in the point projection but returned for downstream use
      (e.g. as a variance/risk indicator for Trade Edge later).
    """
    g = game_script(team_spread)
    team_pass_attempts_proj = team_pass_attempts_baseline * (1 + BETA_SCRIPT_TEAM_VOLUME * g)

    target_share = ewma(target_share_history, half_life)
    proj_targets = target_share * team_pass_attempts_proj

    total_targets = sum(targets_history) if targets_history else 0
    total_receptions = sum(receptions_history) if receptions_history else 0
    total_rec_yards = sum(rec_yards_history) if rec_yards_history else 0
    total_rec_tds = sum(rec_td_history) if rec_td_history else 0

    catch_rate = (total_receptions / total_targets) if total_targets > 0 else 0.0
    ypt = (total_rec_yards / total_targets) if total_targets > 0 else 0.0
    ypt_matchup = ypt * opponent_pass_def_factor

    proj_receptions = proj_targets * catch_rate
    proj_rec_yards = proj_targets * ypt_matchup

    rec_td_rate = regressed_rate(
        event_count=total_rec_tds,
        attempt_count=total_targets,
        league_mean_rate=LEAGUE_MEAN_REC_TD_RATE,
        k=REC_TD_RATE_K,
    )
    proj_rec_tds = proj_targets * rec_td_rate

    adot = ewma(adot_history, half_life)

    return {
        "proj_targets": proj_targets,
        "proj_receptions": proj_receptions,
        "proj_rec_yards": proj_rec_yards,
        "proj_rec_tds": proj_rec_tds,
        "adot": adot,
        "game_script": g,
    }