"""
Kicker feature builder for the Edge scoring engine.
Intentionally simple v1 per edge_formula_nfl.md — kicker is the noisiest
position, not worth over-engineering yet. Distance mix uses season averages
(V2 flag: refine with drive-level red-zone-stall data).
"""

from scoring.stats_utils import ewma


def build_kicker_features(
    fg_attempts_history: list[float],
    fg_made_history: list[float],
    pat_attempts_history: list[float],
    team_implied_total: float,
    league_avg_implied_total: float = 22.0,
    half_life: float = 2.5,
) -> dict:
    """
    team_implied_total: this team's de-vig'd implied point total for the game
      (from vegas_features.team_implied_total).
    league_avg_implied_total: rough league-average team implied total, used to
      scale this kicker's baseline FG/PAT volume up or down based on how
      high-scoring this specific matchup is projected to be.
    """
    baseline_fg_attempts = ewma(fg_attempts_history, half_life)
    baseline_pat_attempts = ewma(pat_attempts_history, half_life)

    total_fg_attempts = sum(fg_attempts_history) if fg_attempts_history else 0
    total_fg_made = sum(fg_made_history) if fg_made_history else 0
    fg_accuracy = (total_fg_made / total_fg_attempts) if total_fg_attempts > 0 else 0.80

    # Scale baseline volume by how this game's implied total compares to league average
    scoring_factor = team_implied_total / league_avg_implied_total if league_avg_implied_total > 0 else 1.0

    proj_fg_attempts = baseline_fg_attempts * scoring_factor
    proj_fg_made = proj_fg_attempts * fg_accuracy
    proj_pat_attempts = baseline_pat_attempts * scoring_factor

    return {
        "proj_fg_attempts": proj_fg_attempts,
        "proj_fg_made": proj_fg_made,
        "proj_pat_attempts": proj_pat_attempts,
        "fg_accuracy": fg_accuracy,
    }