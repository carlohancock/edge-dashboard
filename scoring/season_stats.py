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
    "qb_rush_td": 50,  # Empirically selected via odd/even 2025 split (n=37, per-carry denominator, median prior); k=50-100 plateau is FLAT (~0.7% RMSE spread) so k is identifiable but NOT sharply determined; revisit with 2025→2026 outcome pairs.
    "qb_int": 300,        # weekly INT_RATE_K = 150
    "rb_rush_td": 200,    # weekly RUSH_TD_RATE_K = 100
    # RB receiving-TD rate tested via odd/even 2025 split (n=52 RBs, >= 8 targets in
    # both halves) across k in [0,15,30,60,90,120,180,250]. With the league-mean prior,
    # RMSE improved monotonically toward k=250 and STILL lost to the prior*opportunities
    # baseline (1.062 vs 1.043); Spearman was weak (0.31-0.41). The sample-median prior
    # collapsed to 0.000 because over half the sample scored zero receiving TDs in odd
    # weeks. Conclusion: RB receiving-TD rate carries essentially no predictive signal —
    # k raised to 250 to shrink the player's own noisy rate to near-zero influence rather
    # than pretending k=120 is meaningful. Flagged: this term is unidentified, not fitted.
    "rb_rec_td": 250,
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

# Single tunable knob for volume/efficiency/per-game-rate shrinkage (Task 3).
# TD-rate shrinkage keeps its own SEASON_TD_RATE_K table above — not controlled here.
# Override temporarily (e.g. calibration review script) to compare strengths before committing.
SHRINKAGE_STRENGTH = 6.0  # locked via top-30 RB Spearman vs 2025 actuals (see PROJECT_LOG Phase 5 calibration)

# Fraction trimmed from each tail when building position-role baselines (Task 2).
BASELINE_TRIM_FRACTION = 0.10

# QB rush-TD prior percentile for season_regressed_rate(..., "qb_rush_td").
# p25 was chosen to anchor pocket passers near zero but empirically underperformed
# the median on the 2025 odd/even validation split; median (p50) used instead.
QB_RUSH_TD_PER_CARRY_PRIOR_PCTL = 0.50


def season_regressed_stat(
    observed: float,
    baseline: float,
    games_played: int,
    shrinkage_strength: float | None = None,
) -> float:
    """
    Empirical-Bayes shrinkage for a scalar stat (volume, efficiency, or per-game rate).

    Weight on the player's observed value grows with 2025 games played (thin samples
    like Murray's 5 GP pull hard toward baseline). Effective pseudo-count also grows
    with relative distance from baseline so outlier seasons (McCaffrey carry volume)
    regress more than median-starter seasons — same spirit as season_regressed_rate()
    but generalized beyond count/attempt TD rates.

    Formula (posterior-mean form): (observed * n + k_eff * baseline) / (n + k_eff)
    where n = games_played and k_eff = shrinkage_strength * (1 + relative_distance).
    """
    if shrinkage_strength is None:
        shrinkage_strength = SHRINKAGE_STRENGTH

    n = max(games_played, 0)
    if n == 0:
        return baseline

    scale = max(abs(baseline), 1e-6)
    relative_distance = abs(observed - baseline) / scale
    k_eff = shrinkage_strength * (1.0 + relative_distance)

    return (observed * n + k_eff * baseline) / (n + k_eff)


def _trimmed_mean(values: list[float], trim_fraction: float = BASELINE_TRIM_FRACTION) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    trim = int(len(sorted_vals) * trim_fraction)
    if len(sorted_vals) - 2 * trim < 1:
        return sorted_vals[len(sorted_vals) // 2]
    trimmed = sorted_vals[trim : len(sorted_vals) - trim]
    return sum(trimmed) / len(trimmed)


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """Linear-interpolated percentile on a pre-sorted list (0.0–1.0)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = percentile * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


def compute_qb_rush_td_per_carry_prior(
    observed_by_player: list[tuple[str, int | None, dict[str, float]]],
    percentile: float = QB_RUSH_TD_PER_CARRY_PRIOR_PCTL,
) -> float:
    """
    Data-derived prior for QB rush TDs per carry (season_regressed_rate denominator =
    carries, not games). Default: p50 (median) of 2025 draft-pool QBs with
    MIN_SEASON_GAMES+ and carries > 0. Zero-carry QBs are excluded from the
    sample so they do not drag the prior down.
    """
    values = sorted(
        metrics["rush_tds_per_carry"]
        for position, _rank, metrics in observed_by_player
        if position == "QB" and "rush_tds_per_carry" in metrics
    )
    if not values:
        return 0.0
    return _percentile(values, percentile)


def _baseline_key(position: str, depth_chart_rank: int | None, metric: str) -> tuple[str, int | None, str]:
    return (position, depth_chart_rank, metric)


def _lookup_baseline(
    baselines: dict[tuple[str, int | None, str], float],
    position: str,
    depth_chart_rank: int | None,
    metric: str,
    fallback: float,
) -> float:
    """Rank-specific baseline, then position-wide (rank=None), then caller fallback."""
    if depth_chart_rank is not None:
        val = baselines.get(_baseline_key(position, depth_chart_rank, metric))
        if val is not None:
            return val
    val = baselines.get(_baseline_key(position, None, metric))
    return val if val is not None else fallback


def collect_raw_observed_stats(
    season_totals: dict,
    position: str,
    share_denominator_team_totals: dict | None = None,
) -> dict[str, float]:
    """
    Raw 2025 observed rates/volumes BEFORE any shrinkage — used only to build
    position-role baselines, never as projection inputs directly.
    """
    games = season_totals["games_played"]
    if games == 0:
        return {}

    if position == "QB":
        attempts = season_totals["attempts"]
        rush_attempts = season_totals["carries"]
        metrics: dict[str, float] = {
            "attempts_per_game": attempts / games,
            "rush_attempts_per_game": rush_attempts / games,
            "ypa": (season_totals["passing_yards"] / attempts) if attempts > 0 else 0.0,
            "ypc": (season_totals["rushing_yards"] / rush_attempts) if rush_attempts > 0 else 0.0,
        }
        if rush_attempts > 0:
            metrics["rush_tds_per_carry"] = season_totals["rushing_tds"] / rush_attempts
        return metrics

    if position == "RB":
        carries = season_totals["carries"]
        targets = season_totals["targets"]
        receptions = season_totals["receptions"]
        rec_yards = season_totals["receiving_yards"]
        team_carries = share_denominator_team_totals.get("carries") if share_denominator_team_totals else None
        team_attempts = share_denominator_team_totals.get("attempts") if share_denominator_team_totals else None
        share_gp = share_denominator_team_totals.get("games_played") or 0 if share_denominator_team_totals else 0
        team_carries_season = (
            (team_carries / share_gp) * GAMES_IN_SEASON if team_carries and share_gp else None
        )
        team_attempts_season = (
            (team_attempts / share_gp) * GAMES_IN_SEASON if team_attempts and share_gp else None
        )
        return {
            "carries": carries,
            "targets": targets,
            "ypc": (season_totals["rushing_yards"] / carries) if carries > 0 else 0.0,
            "catch_rate": (receptions / targets) if targets > 0 else 0.0,
            "ypt": (rec_yards / targets) if targets > 0 else 0.0,
            "rush_share": (carries / team_carries_season) if team_carries_season else 0.0,
            "target_share": (targets / team_attempts_season) if team_attempts_season else 0.0,
        }

    if position in ("WR", "TE"):
        targets = season_totals["targets"]
        receptions = season_totals["receptions"]
        rec_yards = season_totals["receiving_yards"]
        team_attempts = share_denominator_team_totals.get("attempts") if share_denominator_team_totals else None
        share_gp = share_denominator_team_totals.get("games_played") or 0 if share_denominator_team_totals else 0
        team_attempts_season = (
            (team_attempts / share_gp) * GAMES_IN_SEASON if team_attempts and share_gp else None
        )
        return {
            "targets": targets,
            "catch_rate": (receptions / targets) if targets > 0 else 0.0,
            "ypt": (rec_yards / targets) if targets > 0 else 0.0,
            "target_share": (targets / team_attempts_season) if team_attempts_season else 0.0,
        }

    if position == "K":
        fg_att = season_totals["fg_att"]
        fg_made = season_totals["fg_made"]
        pat_att = season_totals["pat_att"]
        return {
            "fg_att_per_game": fg_att / games,
            "pat_att_per_game": pat_att / games,
            "fg_accuracy": (fg_made / fg_att) if fg_att > 0 else 0.80,
        }

    return {}


def build_position_role_baselines(
    observed_by_player: list[tuple[str, int | None, dict[str, float]]],
) -> dict[tuple[str, int | None, str], float]:
    """
    Robust position-role baselines for shrinkage targets (Task 2).

    Computed from trimmed means (10% each tail) of 2025 RAW observed season stats
    among players with games_played >= MIN_SEASON_GAMES, grouped by position and
    depth_chart_rank. Position-wide (rank=None) fallbacks use the same trimmed mean
    across all ranks at that position. NOT derived from projected fantasy points or
    from the unregressed Draft Edge outputs being corrected — only from prior-season
    counting/rate stats, so outlier projections cannot contaminate their own baseline.
    """
    # bucket[(position, rank)][metric] -> list of raw observed values
    by_rank: dict[tuple[str, int | None], dict[str, list[float]]] = {}
    by_position: dict[str, dict[str, list[float]]] = {}

    for position, rank, metrics in observed_by_player:
        for metric, value in metrics.items():
            by_rank.setdefault((position, rank), {}).setdefault(metric, []).append(value)
            by_position.setdefault(position, {}).setdefault(metric, []).append(value)

    baselines: dict[tuple[str, int | None, str], float] = {}
    for (position, rank), metric_lists in by_rank.items():
        for metric, values in metric_lists.items():
            mean = _trimmed_mean(values)
            if mean is not None:
                baselines[(position, rank, metric)] = mean

    for position, metric_lists in by_position.items():
        for metric, values in metric_lists.items():
            mean = _trimmed_mean(values)
            if mean is not None:
                baselines[(position, None, metric)] = mean

    return baselines


PAGE_SIZE = 1000  # PostgREST's default per-request row cap


def fetch_season_games_by_player(client, season: int) -> dict[str, list[dict]]:
    """
    All player_game_stats rows for `season`, grouped by player_id and sorted
    oldest-first by week. Paginated so we never hit PostgREST's silent 1000-row
    truncation. Used by compute_draft_edge to avoid one HTTP round-trip per
    draft-pool player (~991 calls × 2 passes = the main source of review timeouts).
    """
    by_player: dict[str, list[dict]] = {}
    offset = 0
    while True:
        result = (
            client.table("player_game_stats")
            .select("player_id, stats, game_id, team_id, games!inner(season, week_or_date)")
            .eq("games.season", season)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = result.data or []
        for row in rows:
            by_player.setdefault(row["player_id"], []).append(row)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    for player_rows in by_player.values():
        player_rows.sort(key=lambda r: int(r["games"]["week_or_date"]))
    return by_player


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
