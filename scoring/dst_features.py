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
    def_safeties, def_tds, special_teams_tds, fumble_recovery_tds

Explicitly excluded, and why:
  - fumble_recovery_own: recovering YOUR OWN team's fumble is not a takeaway.
    (nflverse's own docs note this field "is not exclusive to defense" --
    it can happen on offense/special teams too.)
  - "blocked_kick" (worth 3 pts in our league rules): see Phase 4.10 decision
    below -- deliberately NOT scored in v1, not an oversight.

--- Phase 4.10 DST review (resolved the two PARKED NOTES items) ---

**fumble_recovery_tds -- RESOLVED, now scored (was previously excluded).**
The original v1 exclusion reasoning ("ambiguous overlap risk with def_tds")
was investigated and REFUTED by checking nflverse's actual stat-aggregation
source (nflfastR's calculate_stats.R): `def_tds` and `special_teams_tds` are
both built from a shared `td_ids()` list that EXPLICITLY EXCLUDES stat_ids
56/58/60/62 (own- and opp-fumble-recovery TDs), with a source comment
confirming they're "separately counted in fumble_recovery_tds". So
`fumble_recovery_tds` is a disjoint, purely-additive counter -- zero overlap
risk with def_tds/special_teams_tds, confirmed by definition, not just by
absence-of-evidence.

Separately, the *practical* undercount was confirmed directly against the
544-row 2025 DEF backfill: of the 18 team-weeks with `fumble_recovery_tds` >
0, 14 (78%) had `def_tds == 0` that same week -- i.e. def_tds/special_teams_tds
were silently missing the large majority of fumble-return defensive TDs.

Note on why there's no `fumble_recovery_own`-based gate here (the originally
proposed fix): per nflverse's stat_id semantics, EVERY fumble-recovery TD
(stat 56/58 own, or 60/62 opp) already increments fumble_recovery_own or
fumble_recovery_opp respectively as a SUPERSET -- but those two columns are
weekly aggregates dominated by ordinary, non-scoring recoveries (the whole
season's `fumble_recovery_own` sum is 246 across 544 team-weeks, vs. only 19
total `fumble_recovery_tds`). Gating "only count fumble_recovery_tds when
fumble_recovery_own == 0" would incorrectly zero out real defensive
fumble-return-TD credit any week a team also recovered an unrelated fumble
of its own (common) -- the opposite of the intended fix. A true own-fumble-
recovered-for-a-TD (stat 56/58, functionally an offensive scoring play, not
a defensive one) is a vanishingly rare, near-freak occurrence in real NFL
play; that small residual mis-attribution risk is accepted rather than
building a gate that would misfire far more often than it corrects.
`fumble_recovery_tds` is now folded into `proj_def_st_tds` unconditionally
(see `own_fumble_recovery_td_history` param below), same as def_tds/
special_teams_tds, and reported separately in the output dict for
factor_breakdown auditability.

This also makes fumble-return TDs consistent with how INT-return TDs are
already (and correctly) double-credited here: an interception pick-six
scores both the 2-pt takeaway (via def_interceptions, which itself already
includes stat_id 26 = INT-return-TD) AND the 6-pt TD (via def_tds, whose
td_ids() list includes 26) -- 8 total. Folding fumble_recovery_tds into
proj_def_st_tds makes a fumble-return TD score the same way: 2 pts via
fumble_recovery_opp (already in own_takeaways_history upstream) + 6 pts via
proj_def_st_tds. Not a new inconsistency -- deliberately matching existing
behavior.

**Blocked kicks -- CONFIRMED skip-for-v1 (deliberate, not an oversight).**
Verified against the 544-row 2025 DEF backfill: team-weeks with a blocked
kick credited (fg_blocked + pt_blocked only, matching how this was
originally estimated) = 31/544 = 5.7% -- matches the prior estimate almost
exactly. Including pat_blocked and gwfg_blocked (a tagged subset of
fg_blocked for game-winning-FG situations, not a separate event type) =
43/544 = 7.9%. Either way: low-frequency (~1 in 13-18 team-weeks) and low
point value (+3 in league_scoring_rules.py) relative to the added
complexity of correctly attributing it -- a block always shows up on the
OPPONENT's own box score (their kick got blocked), not this team's, so
scoring it here would require the same opponent-row join already used for
the sack/turnover matchup factors below, just to capture ~0.15-0.25
expected points/week league-wide. DECISION: skip for v1. Revisit only if a
future season's data shows materially higher frequency, or once the
opponent-row plumbing needed for something higher-value makes the marginal
cost of adding this trivial.

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
    own_fumble_recovery_td_history: list[float],
    own_safeties_history: list[float],
    opponent_sacks_suffered_history: list[float],
    opponent_giveaways_history: list[float],
    opponent_yards_history: list[float],
    half_life: float = 2.5,
) -> dict:
    """
    own_*_history: THIS defense's own last-N-games production, oldest-first
      (from ITS OWN DEF player_game_stats rows).
    own_fumble_recovery_td_history: raw `fumble_recovery_tds` per game (see
      Phase 4.10 note above) -- kept as its own input, separate from
      own_def_st_tds_history (def_tds + special_teams_tds, combined by the
      caller), so its contribution stays visible in factor_breakdown rather
      than being silently pre-summed before it reaches this function.
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
    own_fumble_recovery_td_pg = ewma(own_fumble_recovery_td_history, half_life)
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
        # def_tds/special_teams_tds + fumble_recovery_tds (Phase 4.10) --
        # see module docstring for why these are safely additive (disjoint
        # nflverse stat_ids, verified against source). Fumble-return TD
        # sub-component reported separately below for factor_breakdown
        # auditability even though both feed the same "touchdown" scoring
        # rule (points_calculator.py deliberately untouched).
        "proj_def_st_tds": own_def_st_tds_pg + own_fumble_recovery_td_pg,
        "proj_fumble_recovery_tds": own_fumble_recovery_td_pg,
        "proj_safeties": own_safeties_pg,
    }
