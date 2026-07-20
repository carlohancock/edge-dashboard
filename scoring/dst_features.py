"""
DST feature builder for the Edge scoring engine.

v1 subset of nflverse's team-week box score used for defense scoring (see
PROJECT_LOG.md Step 3 gotcha #4): a DEF row in `player_game_stats` holds that
TEAM's FULL box score (offense + defense combined), since nflverse has no
standalone defense-only weekly file. Of the ~127 raw keys on a DEF row, only
the following are true defense/special-teams production -- everything else
(passing_yards, carries, receiving_yards, etc. on a DEF row) is that team's
OWN OFFENSE, not "yards/points allowed", and must never be used for scoring
this defense:

    def_sacks, def_interceptions, fumble_recovery_opp, def_fumbles_forced,
    def_safeties, def_tds, special_teams_tds

Explicitly excluded, and why:
  - fumble_recovery_own: recovering YOUR OWN team's fumble is not a takeaway.
    (nflverse's own docs note this field "is not exclusive to defense" --
    it can happen on offense/special teams too.)
  - fumble_recovery_tds: ambiguous overlap risk with def_tds (a defensive
    fumble-return TD may already be counted there; a special-teams fumble
    recovery TD may not be "defense" at all). Excluded to avoid double- or
    mis-counting; all return/defensive TDs are captured via def_tds +
    special_teams_tds instead.
  - "blocked_kick" (worth 3 pts in our league rules): no such stat exists in
    this file. fg_blocked/pt_blocked describe THIS team's OWN kicks getting
    blocked (bad for them) -- not blocks made by their defense. Not
    scorable from this data source in v1; contributes 0 points.

opponent_implied_total (Vegas, from `games`) remains the primary
points-allowed input, unchanged from the original design in edge_formula_nfl.md.
"""

from scoring.stats_utils import ewma


# Rough league-average placeholders, pending Phase 5 regression calibration.
LEAGUE_AVG_SACKS_ALLOWED_PER_GAME = 2.2       # opponent's own sacks-suffered/game
LEAGUE_AVG_GIVEAWAYS_PER_GAME = 1.2           # opponent's own (INT thrown + fumbles lost)/game
LEAGUE_AVG_TEAM_YARDS_PER_GAME = 340.0        # fallback when the opponent has no game history yet


def build_dst_features(
    opponent_implied_total: float,
    own_sacks_history: list[float],
    own_takeaways_history: list[float],
    own_forced_fumbles_history: list[float],
    own_def_st_tds_history: list[float],
    own_safeties_history: list[float],
    opponent_sacks_suffered_history: list[float],
    opponent_giveaways_history: list[float],
    opponent_yards_history: list[float],
    half_life: float = 2.5,
) -> dict:
    """
    own_*_history: THIS defense's own last-N-games production, oldest-first
      (from ITS OWN DEF player_game_stats rows).
    opponent_*_history: the UPCOMING opponent's own last-N-games OFFENSIVE
      performance, oldest-first (from the OPPONENT's own DEF player_game_stats
      rows -- again, those rows are that team's own full box score). Used to
      scale this defense's expected sacks/takeaways by how sack-/turnover-prone
      this specific opponent has actually been recently.
    opponent_yards_history: opponent's own (passing_yards + rushing_yards) per
      game -- a proxy for total yards this defense is likely to allow, per the
      original v1 design note ("derived from opponent's baseline offensive
      efficiency, not raw season yardage, to avoid garbage-time inflation").

    Empty opponent_* lists (e.g. week 1, no history yet) fall back to
    league-average placeholders so the matchup multiplier is neutral (1.0)
    rather than incorrectly zeroing out the projection.
    """
    own_sacks_pg = ewma(own_sacks_history, half_life)
    own_takeaways_pg = ewma(own_takeaways_history, half_life)
    own_forced_fumbles_pg = ewma(own_forced_fumbles_history, half_life)
    own_def_st_tds_pg = ewma(own_def_st_tds_history, half_life)
    own_safeties_pg = ewma(own_safeties_history, half_life)

    opp_sacks_suffered_pg = (
        ewma(opponent_sacks_suffered_history, half_life)
        if opponent_sacks_suffered_history
        else LEAGUE_AVG_SACKS_ALLOWED_PER_GAME
    )
    opp_giveaways_pg = (
        ewma(opponent_giveaways_history, half_life)
        if opponent_giveaways_history
        else LEAGUE_AVG_GIVEAWAYS_PER_GAME
    )
    opponent_yards_allowed_proxy = (
        ewma(opponent_yards_history, half_life)
        if opponent_yards_history
        else LEAGUE_AVG_TEAM_YARDS_PER_GAME
    )

    sack_matchup_factor = opp_sacks_suffered_pg / LEAGUE_AVG_SACKS_ALLOWED_PER_GAME
    turnover_matchup_factor = opp_giveaways_pg / LEAGUE_AVG_GIVEAWAYS_PER_GAME

    proj_sacks = own_sacks_pg * sack_matchup_factor
    proj_takeaways = own_takeaways_pg * turnover_matchup_factor

    return {
        "opponent_implied_total": opponent_implied_total,
        "opponent_yards_allowed_proxy": opponent_yards_allowed_proxy,
        "proj_sacks": proj_sacks,
        "proj_takeaways": proj_takeaways,
        "proj_forced_fumbles": own_forced_fumbles_pg,
        "proj_def_st_tds": own_def_st_tds_pg,
        "proj_safeties": own_safeties_pg,
    }
