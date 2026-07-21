"""
Season-level aggregation for the Draft Edge scoring engine.

Weekly Edge projects ONE specific upcoming game from a trailing EWMA window
over the last few games -- recency matters, and there's a real "next game"
to target. Draft Edge is a different statistical problem: it's July 2026,
zero 2026 games exist, and the thing being projected is an entire season
that hasn't started. There is no trailing window to weight -- 2025 is one
single, already-finished, complete sample, and it is aggregated as SEASON
TOTALS/RATES here, not EWMA'd. (2026 context -- team, depth chart -- comes
from the live `players` row, read in scoring/draft_edge_features.py, not
from any game history; this module only touches 2025 `player_game_stats`.)

Deliberately does NOT reuse compute_edge_scores.py's `_get_upcoming_game` /
`_get_game_for_period` -- those pick a single game row; nothing here ever
selects a game, it sums an entire season of them.
"""

from __future__ import annotations

from scoring.stats_utils import regressed_rate

# Same nflverse key names used by the weekly engine (verified against real
# 2025 data -- see compute_edge_scores.py's module docstring). Summed across
# the whole season here instead of EWMA'd over a trailing window.
SKILL_STAT_KEYS = [
    "attempts", "passing_yards", "passing_tds", "passing_interceptions",
    "carries", "rushing_yards", "rushing_tds",
    "targets", "receptions", "receiving_yards", "receiving_tds", "receiving_air_yards",
    "fg_att", "fg_made", "pat_att",
]

GAMES_IN_SEASON = 17  # NFL regular-season games per team, 2025 and 2026 alike

# ---- Season-level shrinkage constants for TD rate (Task 1) ----
#
# Why these aren't just the weekly k values, reconsidered rather than
# blindly reused:
#
# The weekly k's (qb_features.py TD_RATE_K=150/INT_RATE_K=150,
# rb_features.py RUSH_TD_RATE_K=100/REC_TD_RATE_K=60,
# wr_te_features.py REC_TD_RATE_K=60) were tuned against a ~5-GAME TRAILING
# WINDOW. At that sample size, k is mostly doing WITHIN-SEASON noise
# smoothing -- guarding against a real player's hot/cold 3-5 week
# TD-rate stretch mid-season, where the EWMA recency-weighting is already
# doing some of that work too.
#
# A season-level Draft Edge projection is a different problem: we are using
# ONE fully-realized 2025 season as the ENTIRE evidence base to project a
# DIFFERENT, not-yet-played 2026 season -- structurally closer to a
# year-over-year forecasting problem (Marcel-the-Monkey-style projection
# systems, which explicitly regress a single year of stats hard toward the
# mean/career rate) than to in-season smoothing. TD rate especially is
# heavily red-zone-luck-driven, and it's a well-known result in projection
# systems that even a full season's TD count is a noisy predictor of the
# FOLLOWING season's rate. So the right amount of regression here is
# GENUINELY STRONGER than "whatever the weekly k implies at a bigger
# denominator" -- not just the same k automatically diluted by more
# attempts.
#
# v1 choice: roughly double each position's weekly k, as an explicit,
# documented forecasting-uncertainty premium on top of the pure
# within-season noise-smoothing the weekly k's were tuned for. This is NOT
# derived from data (there is no 2026 outcome data yet to fit it against
# -- that is exactly the Phase 5 regression-calibration gap flagged
# throughout this project); it's a reasoned starting point, same spirit as
# every other "reasonable v1 starting constant" already in this codebase
# (BETA_SCRIPT_* in qb/rb/wr_te_features.py, etc.). Revisit once real
# 2025-to-2026 outcome pairs exist to fit k empirically.
SEASON_TD_RATE_K = {
    "qb_pass_td": 300,    # weekly TD_RATE_K = 150
    "qb_int": 300,        # weekly INT_RATE_K = 150
    "rb_rush_td": 200,    # weekly RUSH_TD_RATE_K = 100
    "rb_rec_td": 120,     # weekly REC_TD_RATE_K = 60
    "wr_te_rec_td": 120,  # weekly REC_TD_RATE_K = 60
}

LEAGUE_MEAN_PASS_TD_RATE = 0.045
LEAGUE_MEAN_INT_RATE = 0.022
LEAGUE_MEAN_RUSH_TD_RATE = 0.045
LEAGUE_MEAN_RB_REC_TD_RATE = 0.055
LEAGUE_MEAN_WR_TE_REC_TD_RATE = 0.06

# Below these season attempt/target counts, efficiency rates (ypc/ypt/catch
# rate -- NOT the shrinkage-regressed TD rate, which handles its own small
# sample problem via `k` above) get a `low_sample` flag in factor_breakdown
# rather than being silently trusted. v1 thresholds, tunable.
MIN_SEASON_SAMPLE = {
    "pass_attempts": 100,
    "carries": 40,
    "targets": 25,
}
MIN_SEASON_GAMES = 6  # fewer games played than this in 2025 -> low_sample, regardless of volume


def fetch_player_season_games(client, player_id: str, season: int) -> list[dict]:
    """
    All of a player's player_game_stats rows for `season`, oldest-first by
    week. Joins on the PER-GAME team_id backfilled in Phase 4.8, not
    players.team_id (which is the player's CURRENT/2026 team -- wrong for
    anyone who changed teams at any point, in-season or since).
    """
    result = (
        client.table("player_game_stats")
        .select("stats, game_id, team_id, games!inner(season, week_or_date)")
        .eq("player_id", player_id)
        .eq("games.season", season)
        .execute()
    )
    rows = result.data or []
    rows.sort(key=lambda r: int(r["games"]["week_or_date"]))
    return rows


def aggregate_skill_season_totals(rows: list[dict]) -> dict:
    """
    Sum raw counting stats across a season's worth of game rows (works for
    both skill-player rows and a team's own DEF-row season, since they
    share the same nflverse column set -- see dst_features.py's docstring).
    Also returns games_played and the distinct team_ids the player recorded
    a stat line for (used upstream to detect in-season trades / offseason
    team changes -- see draft_edge_features.py).
    """
    totals: dict = {key: 0.0 for key in SKILL_STAT_KEYS}
    for row in rows:
        stats = row.get("stats") or {}
        for key in SKILL_STAT_KEYS:
            totals[key] += stats.get(key) or 0
    totals["games_played"] = len(rows)
    totals["team_ids_seen"] = [row["team_id"] for row in rows if row.get("team_id")]
    return totals


def fetch_team_season_totals(client, def_player_id: str | None, season: int) -> dict:
    """
    A team's own season-total rush/pass attempt volume, read off its DEF
    row's season stats (the "DEF row = team's own full box score" trick
    used throughout the weekly engine). Season SUM, not EWMA -- there is no
    recency concept across a season that has not been played yet.
    """
    if not def_player_id:
        return {"carries": None, "attempts": None, "games_played": 0}
    rows = fetch_player_season_games(client, def_player_id, season)
    totals = aggregate_skill_season_totals(rows)
    return {
        "carries": totals["carries"],
        "attempts": totals["attempts"],
        "games_played": totals["games_played"],
    }


def season_regressed_rate(event_count: float, attempt_count: float, league_mean_rate: float, rate_key: str) -> float:
    """Shrinkage regression (scoring/stats_utils.regressed_rate) using the season-level k's above."""
    k = SEASON_TD_RATE_K[rate_key]
    return regressed_rate(event_count, attempt_count, league_mean_rate, k=k)


def primary_team_id(team_ids_seen: list[str]) -> str | None:
    """The team a player recorded the most 2025 game-stat rows for (mode), or None if they have no history."""
    if not team_ids_seen:
        return None
    counts: dict[str, int] = {}
    for tid in team_ids_seen:
        counts[tid] = counts.get(tid, 0) + 1
    return max(counts, key=lambda tid: counts[tid])
