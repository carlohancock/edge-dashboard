"""
RB feature builder for the Edge scoring engine.
Splits rushing and receiving into two independent usage streams per
edge_formula_nfl.md, so a receiving back's role isn't diluted by declining
rush share (or vice versa).
"""

from scoring.stats_utils import ewma, regressed_rate
from scoring.vegas_features import game_script


BETA_SCRIPT_TEAM_VOLUME = 0.15   # how much game_script shifts team rush/pass volume
LEAGUE_MEAN_RUSH_TD_RATE = 0.045   # per carry
LEAGUE_MEAN_REC_TD_RATE = 0.055    # per target (receiving TDs are rarer per-target than rush TDs per-carry, but targets are fewer than carries league-wide, so the two aren't directly comparable — kept separate per the doc)
RUSH_TD_RATE_K = 100
REC_TD_RATE_K = 60   # lower k than rushing since red-zone receiving TDs are a smaller, higher-variance sample per player


def build_rb_features(
    rush_share_history: list[float],
    target_share_history: list[float],
    team_rush_attempts_baseline: float,
    team_pass_attempts_baseline: float,
    rush_yards_history: list[float],
    rush_attempts_history: list[float],
    rec_yards_history: list[float],
    receptions_history: list[float],
    targets_history: list[float],
    rush_td_history: list[int],
    rec_td_history: list[int],
    team_spread: float,
    opponent_run_def_factor: float = 1.0,
    opponent_pass_def_factor: float = 1.0,
    half_life: float = 2.5,
) -> dict:
    """
    rush_share_history / target_share_history: this player's share of team
      rush attempts / pass targets, per game (e.g. 0.45 = 45% of team carries).
    team_rush_attempts_baseline / team_pass_attempts_baseline: team-level EWMA
      baseline volume (computed elsewhere, at the team level, then passed in here).
    """
    g = game_script(team_spread)

    # Team-level volume shifts in opposite directions with game-script
    team_rush_attempts_proj = team_rush_attempts_baseline * (1 - BETA_SCRIPT_TEAM_VOLUME * g)
    team_pass_attempts_proj = team_pass_attempts_baseline * (1 + BETA_SCRIPT_TEAM_VOLUME * g)

    # Player's own role, tracked independently
    rush_share = ewma(rush_share_history, half_life)
    target_share = ewma(target_share_history, half_life)

    proj_carries = rush_share * team_rush_attempts_proj
    proj_targets = target_share * team_pass_attempts_proj

    # Efficiency
    total_rush_attempts = sum(rush_attempts_history) if rush_attempts_history else 0
    total_rush_yards = sum(rush_yards_history) if rush_yards_history else 0
    ypc = (total_rush_yards / total_rush_attempts) if total_rush_attempts > 0 else 0.0
    ypc_matchup = ypc * opponent_run_def_factor

    total_targets = sum(targets_history) if targets_history else 0
    total_receptions = sum(receptions_history) if receptions_history else 0
    total_rec_yards = sum(rec_yards_history) if rec_yards_history else 0
    catch_rate = (total_receptions / total_targets) if total_targets > 0 else 0.0
    ypt = (total_rec_yards / total_targets) if total_targets > 0 else 0.0
    ypt_matchup = ypt * opponent_pass_def_factor

    proj_rush_yards = proj_carries * ypc_matchup
    proj_receptions = proj_targets * catch_rate
    proj_rec_yards = proj_targets * ypt_matchup

    # TD rates, shrinkage-regressed, split rushing vs receiving
    total_rush_tds = sum(rush_td_history) if rush_td_history else 0
    total_rec_tds = sum(rec_td_history) if rec_td_history else 0

    rush_td_rate = regressed_rate(
        event_count=total_rush_tds,
        attempt_count=total_rush_attempts,
        league_mean_rate=LEAGUE_MEAN_RUSH_TD_RATE,
        k=RUSH_TD_RATE_K,
    )
    rec_td_rate = regressed_rate(
        event_count=total_rec_tds,
        attempt_count=total_targets,
        league_mean_rate=LEAGUE_MEAN_REC_TD_RATE,
        k=REC_TD_RATE_K,
    )

    proj_rush_tds = proj_carries * rush_td_rate
    proj_rec_tds = proj_targets * rec_td_rate

    return {
        "proj_carries": proj_carries,
        "proj_rush_yards": proj_rush_yards,
        "proj_rush_tds": proj_rush_tds,
        "proj_targets": proj_targets,
        "proj_receptions": proj_receptions,
        "proj_rec_yards": proj_rec_yards,
        "proj_rec_tds": proj_rec_tds,
        "game_script": g,
    }