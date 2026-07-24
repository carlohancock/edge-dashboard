"""
Draft Priority Score (DPS) review — read-only comparison vs ADP and Draft Edge.

  QB/RB/TE: DPS_i = ADP_i - (lambda_pos * Delta_i)
            Delta_i = 0.44 * Z_xTD_i + 0.56 * Z_Role_i

  WR: DPS_i = ADP_i - (LAMBDA_WR * Delta_i)
      Delta_i = 0.20 * Z_xTD_i + 0.50 * Z_Role_i + 0.30 * Z_Avail_i

All v1 constants below are hand-set starting values, flagged for empirical
fit against 2025→2026 outcome pairs — same status as SHRINKAGE_STRENGTH in
season_stats.py. No database writes.
"""

from __future__ import annotations

import statistics
import sys

from config.supabase_client import get_supabase_client
from scoring.compute_draft_edge import (
    PERIOD,
    SCORE_TYPE,
    _get_def_player_by_team,
    fetch_players_by_names,
    load_draft_edge_context,
)
from scoring.draft_edge_features import (
    DEFAULT_QB_GAMES_ESTIMATE,
    DEFAULT_TE_TARGET_SHARE_PRIOR,
    DEFAULT_TEAM_PASS_ATTEMPTS_SEASON,
    DEFAULT_TEAM_RUSH_ATTEMPTS_SEASON,
    QB_GAMES_ESTIMATE_PRIOR,
    TE_TARGET_SHARE_PRIOR,
    _depth_chart_tier,
    _team_season_attempts,
)
from scoring.points_calculator import calculate_rb_points
from scoring.season_stats import (
    LEAGUE_MEAN_PASS_TD_RATE,
    LEAGUE_MEAN_RB_REC_TD_RATE,
    LEAGUE_MEAN_RUSH_TD_RATE,
    LEAGUE_MEAN_WR_TE_REC_TD_RATE,
    MIN_SEASON_GAMES,
    MIN_SEASON_SAMPLE,
    _lookup_baseline,
    aggregate_skill_season_totals,
    primary_team_id,
    season_regressed_rate,
)

# ---- v1 hand-set constants (flagged for empirical fit) ----
DELTA_XTD_WEIGHT = 0.44
DELTA_ROLE_WEIGHT = 0.56
# lambda_QB selected from a 4.0/8.0/12.0 sweep: QB ADP spacing is wide enough that
# Delta cannot meaningfully reorder the board at any reasonable lambda; 8.0 captures
# the consistent movers (Daniels, Murray, Darnold) without the top-end churn seen at
# 12.0. Flagged for empirical fit alongside the other lambdas.
# lambda_RB selected from a 3.0/5.0/8.0/12.0 sweep: movement scales smoothly and the
# same players move in the same direction at every lambda (stable signal, not churn).
# 8.0 leaves the elite tier (Bijan/Gibbs/McCaffrey) intact while capturing meaningful
# mid-round moves (Hampton +3, Henry -4, Taylor -3, Barkley +2). 12.0 was rejected
# because it reorders the top 5, and the RB points curve is steepest there — a 3-spot
# fade in round 1 costs far more than the same move at RB25. Flagged for empirical fit
# alongside the other lambdas.
# lambda_WR selected from a 2/3/4/5/6/8 sweep (Set C weights 0.20/0.50/0.30):
# WR ADP is denser than QB; same Delta moves more rank spots at higher lambda.
# 4.0 keeps the elite tier intact (only JSN/Lamb λ-sensitive); 8.0 over-promotes
# Lamb and distorts JSN. Flagged for empirical fit with 2025→2026 outcomes.
LAMBDA_POS = {"QB": 8.0, "RB": 8.0, "WR": 4.0, "TE": 6.0}
LAMBDA_WR = 4.0
CONTEXT_CHANGED_MULTIPLIER = 0.85
GAMES_NORMALIZER = 16.0
# ---- WR DPS spec (Phase 5.1 — read-only prototype) ----
WR_BETA_TGT = 0.034          # FITTED — not sharply identified (odd 0.030 / even 0.038)
WR_BETA_AY = 0.0017          # FITTED — not sharply identified (odd 0.0018 / even 0.0016)
WR_WOPR_SHRINK_K = 2.5       # FITTED — not sharply identified (odd k=1.5 / even k=3.0, flat curve)
WR_WOPR_PG_MEDIAN = {        # FITTED — 2025 medians, >= 4 GP
    1: 0.6856,
    2: 0.5017,
    3: 0.3306,
    4: 0.2078,
}
WR_WOPR_PG_DEFAULT = 0.1387  # rank 5+ / null
WR_PROJ_GAMES = 17.0         # HAND-SET — flat across ranks; WR2/WR3 play full seasons
DELTA_WR_XTD_WEIGHT = 0.20   # HAND-SET (swept A–F; components orthogonal, r < 0.03)
DELTA_WR_ROLE_WEIGHT = 0.50  # HAND-SET (swept)
DELTA_WR_AVAIL_WEIGHT = 0.30 # HAND-SET (swept)
# RB weighted-opportunity share priors by depth_chart_rank — derived from 2025
# median RB WO share by depth_chart_rank (n=110 RBs, >= 4 games, RB-only
# backfield denominator, W_TARGET=2.51). The prior hand-set table
# {1: 0.65, 2: 0.35, 3: 0.15, 4: 0.05} summed to 1.20 vs an empirical median
# sum of 1.031 — that ~17% surplus systematically inflated Role Shift positive
# for nearly every RB. Known limitations: (a) shares are grouped by CURRENT
# (2026) depth_chart_rank against 2025 usage, since no 2025 depth-chart snapshot
# exists — promoted/demoted players contaminate their bucket, likely biasing the
# rank-1 median slightly low; (b) within-rank dispersion is wide (rank 1:
# p10=0.32, p90=0.77), so a single median is a coarse prior. Flagged for revisit.
RB_WO_SHARE_PRIOR = {1: 0.603, 2: 0.292, 3: 0.09, 4: 0.046}
DEFAULT_RB_WO_SHARE_PRIOR = 0.046
MIN_RB_CARRIES_W_TARGET = 50
W_TARGET_COMPARISON = 1.5

DPS_POSITIONS = ("QB", "RB", "WR", "TE")
PAGE_SIZE = 1000

ADP_SPOTLIGHT_NAMES = (
    "Ja'Marr Chase",
    "Bijan Robinson",
    "Josh Allen",
    "Lamar Jackson",
    "Puka Nacua",
    "Brock Bowers",
    "Saquon Barkley",
)
# User prompt typo alias — lookup optional
ADP_SPOTLIGHT_ALIASES = {"Brock Bowders": "Brock Bowers"}


def _fetch_all_adp_rows(client) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        result = (
            client.table("adp")
            .select("player_id, adp_value, fetched_at")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def _fetch_draft_edge_ranks(client) -> dict[str, int]:
    """player_id -> positional_rank from stored draft_edge scores."""
    ranks: dict[str, int] = {}
    offset = 0
    while True:
        result = (
            client.table("edge_scores")
            .select("player_id, positional_rank")
            .eq("score_type", SCORE_TYPE)
            .eq("period", PERIOD)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = result.data or []
        for row in batch:
            if row.get("positional_rank") is not None:
                ranks[row["player_id"]] = row["positional_rank"]
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return ranks


def _z_scores(values: list[float]) -> list[float]:
    if len(values) <= 1:
        return [0.0] * len(values)
    mu = statistics.mean(values)
    sigma = statistics.pstdev(values)
    if sigma == 0:
        return [0.0] * len(values)
    return [(v - mu) / sigma for v in values]


def _context_multiplier(context_changed: bool) -> float:
    return CONTEXT_CHANGED_MULTIPLIER if context_changed else 1.0


def _build_team_game_stats(
    season_games_by_player: dict[str, list[dict]],
    def_player_by_team: dict[str, str],
) -> dict[tuple[str, str], dict]:
    """team_id + game_id -> {targets, receiving_air_yards} from each team's DEF row."""
    team_game: dict[tuple[str, str], dict] = {}
    for team_id, def_player_id in def_player_by_team.items():
        for row in season_games_by_player.get(def_player_id, []):
            stats = row.get("stats") or {}
            team_game[(team_id, row["game_id"])] = {
                "targets": stats.get("targets") or 0,
                "receiving_air_yards": stats.get("receiving_air_yards") or 0,
            }
    return team_game


def _compute_wopr_pg(game_rows: list[dict], team_game_stats: dict[tuple[str, str], dict]) -> float:
    """
    Per-game WOPR restricted to games the player appeared in, then averaged.

    For each player game row: WOPR_game = 1.5×(player_targets/team_targets_that_game)
    + 0.7×(player_air_yards/team_air_yards_that_game), where team volume comes from
    the team's DEF-row box score for the same game_id (not season totals). WOPR_pg is
    the arithmetic mean across those games — exact per-game team restriction, not a
    season-total ratio approximation.
    """
    woprs: list[float] = []
    for row in game_rows:
        team_id = row.get("team_id")
        game_id = row.get("game_id")
        if not team_id or not game_id:
            continue
        team = team_game_stats.get((team_id, game_id))
        if team is None:
            continue
        stats = row.get("stats") or {}
        player_targets = stats.get("targets") or 0
        player_air_yards = stats.get("receiving_air_yards") or 0
        team_targets = team["targets"]
        team_air_yards = team["receiving_air_yards"]
        target_share = (player_targets / team_targets) if team_targets else 0.0
        air_share = (player_air_yards / team_air_yards) if team_air_yards else 0.0
        woprs.append(1.5 * target_share + 0.7 * air_share)
    return statistics.mean(woprs) if woprs else 0.0


def _compute_wr_xtd_delta(season_totals: dict) -> float:
    expected = (
        WR_BETA_TGT * season_totals["targets"]
        + WR_BETA_AY * season_totals["receiving_air_yards"]
    )
    return expected - season_totals["receiving_tds"]


def _compute_wr_role_shift(
    player: dict,
    season_totals: dict,
    game_rows: list[dict],
    team_game_stats: dict[tuple[str, str], dict],
    context_changed: bool,
) -> float:
    gp = season_totals["games_played"]
    rank_median = _depth_chart_tier(
        player.get("depth_chart_rank"),
        WR_WOPR_PG_MEDIAN,
        WR_WOPR_PG_DEFAULT,
    )
    wopr_pg = _compute_wopr_pg(game_rows, team_game_stats)
    shrunk_wopr_pg = (gp * wopr_pg + WR_WOPR_SHRINK_K * rank_median) / (gp + WR_WOPR_SHRINK_K)
    return (rank_median - shrunk_wopr_pg) * _context_multiplier(context_changed)


def _compute_wr_availability(season_totals: dict) -> float:
    return (WR_PROJ_GAMES - season_totals["games_played"]) / GAMES_NORMALIZER


def _build_team_targets_by_team(
    season_games_by_player: dict[str, list[dict]],
    def_player_by_team: dict[str, str],
) -> dict[str, float]:
    """Season-normalized team targets from each team's DEF row."""
    by_team: dict[str, float] = {}
    for team_id, def_player_id in def_player_by_team.items():
        rows = season_games_by_player.get(def_player_id, [])
        totals = aggregate_skill_season_totals(rows)
        by_team[team_id] = _team_season_attempts(
            {"targets": totals["targets"], "games_played": totals["games_played"]},
            "targets",
            DEFAULT_TEAM_PASS_ATTEMPTS_SEASON,
        )
    return by_team


def _team_rb_wo_denominator(
    share_team_totals: dict,
    team_id: str | None,
    team_targets_by_team: dict[str, float],
    w_target: float,
) -> float:
    team_carries = _team_season_attempts(
        share_team_totals, "carries", DEFAULT_TEAM_RUSH_ATTEMPTS_SEASON,
    )
    team_targets = team_targets_by_team.get(team_id or "", 0.0)
    if not team_targets:
        team_targets = _team_season_attempts(
            share_team_totals, "targets", DEFAULT_TEAM_PASS_ATTEMPTS_SEASON,
        )
    return team_carries + w_target * team_targets


def _player_rb_wo(season_totals: dict, w_target: float) -> float:
    return season_totals["carries"] + w_target * season_totals["targets"]


def _derive_w_target(context) -> tuple[float, float, float, int]:
    """
    W_TARGET = (points per target) / (points per carry) across RBs with
    >= MIN_RB_CARRIES_W_TARGET carries in 2025, under league scoring rules.
    """
    total_carries = 0.0
    total_targets = 0.0
    total_rush_pts = 0.0
    total_rec_pts = 0.0
    rb_count = 0

    for player in context.players:
        if player["position"] != "RB":
            continue
        rows = context.season_games_by_player.get(player["player_id"], [])
        totals = aggregate_skill_season_totals(rows)
        if totals["games_played"] == 0 or totals["carries"] < MIN_RB_CARRIES_W_TARGET:
            continue

        rush_pts = calculate_rb_points({
            "proj_rush_yards": totals["rushing_yards"],
            "proj_rush_tds": totals["rushing_tds"],
            "proj_rec_yards": 0.0,
            "proj_receptions": 0.0,
            "proj_rec_tds": 0.0,
        })
        rec_pts = calculate_rb_points({
            "proj_rush_yards": 0.0,
            "proj_rush_tds": 0.0,
            "proj_rec_yards": totals["receiving_yards"],
            "proj_receptions": totals["receptions"],
            "proj_rec_tds": totals["receiving_tds"],
        })

        total_carries += totals["carries"]
        total_targets += totals["targets"]
        total_rush_pts += rush_pts
        total_rec_pts += rec_pts
        rb_count += 1

    points_per_carry = total_rush_pts / total_carries if total_carries else 0.0
    points_per_target = total_rec_pts / total_targets if total_targets else 0.0
    w_target = points_per_target / points_per_carry if points_per_carry else 0.0
    return points_per_carry, points_per_target, w_target, rb_count


def _low_sample(position: str, season_totals: dict) -> bool:
    gp = season_totals["games_played"]
    if gp < MIN_SEASON_GAMES:
        return True
    if position == "QB":
        return season_totals["attempts"] < MIN_SEASON_SAMPLE["pass_attempts"]
    if position == "RB":
        return (season_totals["carries"] + season_totals["targets"]) < MIN_SEASON_SAMPLE["carries"]
    if position in ("WR", "TE"):
        return season_totals["targets"] < MIN_SEASON_SAMPLE["targets"]
    return False


def _compute_xtd_delta(
    position: str,
    season_totals: dict,
    baselines: dict,
) -> float:
    gp = season_totals["games_played"]
    if position == "QB":
        attempts = season_totals["attempts"]
        pass_tds = season_totals["passing_tds"]
        rush_tds = season_totals["rushing_tds"]
        td_rate = season_regressed_rate(pass_tds, attempts, LEAGUE_MEAN_PASS_TD_RATE, "qb_pass_td")
        rush_prior = _lookup_baseline(baselines, "QB", None, "rush_tds_per_carry_prior", 0.0)
        carries = season_totals["carries"]
        rush_td_pc = season_regressed_rate(rush_tds, carries, rush_prior, "qb_rush_td")
        expected = attempts * td_rate + carries * rush_td_pc
        actual = pass_tds + rush_tds
        return expected - actual

    if position == "RB":
        carries = season_totals["carries"]
        targets = season_totals["targets"]
        rush_tds = season_totals["rushing_tds"]
        rec_tds = season_totals["receiving_tds"]
        rush_td_rate = season_regressed_rate(rush_tds, carries, LEAGUE_MEAN_RUSH_TD_RATE, "rb_rush_td")
        rec_td_rate = season_regressed_rate(rec_tds, targets, LEAGUE_MEAN_RB_REC_TD_RATE, "rb_rec_td")
        expected = carries * rush_td_rate + targets * rec_td_rate
        actual = rush_tds + rec_tds
        return expected - actual

    if position == "WR":
        return _compute_wr_xtd_delta(season_totals)

    if position == "TE":
        targets = season_totals["targets"]
        rec_tds = season_totals["receiving_tds"]
        rec_td_rate = season_regressed_rate(rec_tds, targets, LEAGUE_MEAN_WR_TE_REC_TD_RATE, "wr_te_rec_td")
        expected = targets * rec_td_rate
        return expected - rec_tds

    return 0.0


def _compute_role_shift(
    player: dict,
    season_totals: dict,
    share_team_totals: dict,
    context_changed: bool,
    *,
    w_target: float = 1.0,
    team_targets_by_team: dict[str, float] | None = None,
    primary_2025_team: str | None = None,
    game_rows: list[dict] | None = None,
    team_game_stats: dict[tuple[str, str], dict] | None = None,
) -> float:
    position = player["position"]
    depth_chart_rank = player.get("depth_chart_rank")
    gp = season_totals["games_played"]
    mult = _context_multiplier(context_changed)
    team_targets_by_team = team_targets_by_team or {}

    if position == "QB":
        proj_games = _depth_chart_tier(depth_chart_rank, QB_GAMES_ESTIMATE_PRIOR, DEFAULT_QB_GAMES_ESTIMATE)
        return ((proj_games - gp) / GAMES_NORMALIZER) * mult

    if position == "RB":
        team_wo = _team_rb_wo_denominator(
            share_team_totals, primary_2025_team, team_targets_by_team, w_target,
        )
        player_wo = _player_rb_wo(season_totals, w_target)
        observed = (player_wo / team_wo) if team_wo else 0.0
        expected = _depth_chart_tier(depth_chart_rank, RB_WO_SHARE_PRIOR, DEFAULT_RB_WO_SHARE_PRIOR)
        return (expected - observed) * mult

    if position == "WR":
        return _compute_wr_role_shift(
            player,
            season_totals,
            game_rows or [],
            team_game_stats or {},
            context_changed,
        )

    if position == "TE":
        share_team_attempts = _team_season_attempts(
            share_team_totals, "attempts", DEFAULT_TEAM_PASS_ATTEMPTS_SEASON,
        )
        observed = (season_totals["targets"] / share_team_attempts) if share_team_attempts else 0.0
        expected = _depth_chart_tier(depth_chart_rank, TE_TARGET_SHARE_PRIOR, DEFAULT_TE_TARGET_SHARE_PRIOR)
        return (expected - observed) * mult

    return 0.0


def _build_player_records(
    context,
    *,
    w_target: float,
    team_targets_by_team: dict[str, float],
    team_game_stats: dict[tuple[str, str], dict],
) -> tuple[list[dict], int, dict[str, int]]:
    excluded_no_adp = 0
    no_data_counts: dict[str, int] = {p: 0 for p in DPS_POSITIONS}
    records: list[dict] = []

    for player in context.players:
        position = player["position"]
        if position not in DPS_POSITIONS:
            continue

        pid = player["player_id"]
        adp = context.adp_by_player.get(pid)
        if adp is None:
            excluded_no_adp += 1
            continue

        rows = context.season_games_by_player.get(pid, [])
        season_totals = aggregate_skill_season_totals(rows)
        if season_totals["games_played"] == 0:
            no_data_counts[position] += 1
            records.append({
                "player_id": pid,
                "name": player["full_name"],
                "position": position,
                "adp": adp,
                "xtd_raw": 0.0,
                "role_raw": 0.0,
                "avail_raw": 0.0,
                "context_changed": False,
                "low_sample": False,
                "no_2025_data": True,
            })
            continue

        primary_2025_team = primary_team_id(season_totals["team_ids_seen"])
        current_team = player["team_id"]
        context_changed = bool(primary_2025_team and current_team and primary_2025_team != current_team)
        share_team_totals = context.team_totals_by_team.get(primary_2025_team, {})

        xtd_raw = _compute_xtd_delta(position, season_totals, context.baselines)
        role_raw = _compute_role_shift(
            player,
            season_totals,
            share_team_totals,
            context_changed,
            w_target=w_target,
            team_targets_by_team=team_targets_by_team,
            primary_2025_team=primary_2025_team,
            game_rows=rows,
            team_game_stats=team_game_stats,
        )
        avail_raw = _compute_wr_availability(season_totals) if position == "WR" else 0.0

        records.append({
            "player_id": pid,
            "name": player["full_name"],
            "position": position,
            "adp": adp,
            "xtd_raw": xtd_raw,
            "role_raw": role_raw,
            "avail_raw": avail_raw,
            "context_changed": context_changed,
            "low_sample": _low_sample(position, season_totals),
            "no_2025_data": False,
            "_player": player,
            "_season_totals": season_totals,
            "_share_team_totals": share_team_totals,
            "_primary_2025_team": primary_2025_team,
        })

    return records, excluded_no_adp, no_data_counts


def _recompute_rb_role_shifts(
    records: list[dict],
    *,
    w_target: float,
    team_targets_by_team: dict[str, float],
) -> None:
    for rec in records:
        if rec["position"] != "RB" or rec.get("no_2025_data"):
            continue
        rec["role_raw"] = _compute_role_shift(
            rec["_player"],
            rec["_season_totals"],
            rec["_share_team_totals"],
            rec["context_changed"],
            w_target=w_target,
            team_targets_by_team=team_targets_by_team,
            primary_2025_team=rec["_primary_2025_team"],
        )


def _apply_z_scores_and_dps(records: list[dict]) -> dict[str, int]:
    """Z-score components within position (2025-data players only); compute DPS."""
    by_pos: dict[str, list[dict]] = {p: [] for p in DPS_POSITIONS}
    for rec in records:
        by_pos[rec["position"]].append(rec)

    sample_sizes: dict[str, int] = {}
    for position in DPS_POSITIONS:
        group = by_pos[position]
        with_data = [r for r in group if not r.get("no_2025_data")]
        no_data = [r for r in group if r.get("no_2025_data")]
        sample_sizes[position] = len(with_data)

        if with_data:
            xtd_vals = [r["xtd_raw"] for r in with_data]
            role_vals = [r["role_raw"] for r in with_data]
            xtd_sigma = statistics.pstdev(xtd_vals) if len(xtd_vals) > 1 else 0.0
            role_sigma = statistics.pstdev(role_vals) if len(role_vals) > 1 else 0.0
            if xtd_sigma == 0:
                print(f"  WARNING: {position} xTD_Delta sigma == 0 — Z_xTD set to 0 for all")
            if role_sigma == 0:
                print(f"  WARNING: {position} Role_Shift sigma == 0 — Z_Role set to 0 for all")

            z_xtd = _z_scores(xtd_vals)
            z_role = _z_scores(role_vals)

            if position == "WR":
                avail_vals = [r["avail_raw"] for r in with_data]
                avail_sigma = statistics.pstdev(avail_vals) if len(avail_vals) > 1 else 0.0
                if avail_sigma == 0:
                    print("  WARNING: WR Availability sigma == 0 — Z_Avail set to 0 for all")
                z_avail = _z_scores(avail_vals)
                lam = LAMBDA_WR
                for rec, zx, zr, za in zip(with_data, z_xtd, z_role, z_avail):
                    delta = (
                        DELTA_WR_XTD_WEIGHT * zx
                        + DELTA_WR_ROLE_WEIGHT * zr
                        + DELTA_WR_AVAIL_WEIGHT * za
                    )
                    rec["z_xtd"] = zx
                    rec["z_role"] = zr
                    rec["z_avail"] = za
                    rec["delta"] = delta
                    rec["dps"] = rec["adp"] - lam * delta
            else:
                lam = LAMBDA_POS[position]
                for rec, zx, zr in zip(with_data, z_xtd, z_role):
                    delta = DELTA_XTD_WEIGHT * zx + DELTA_ROLE_WEIGHT * zr
                    rec["z_xtd"] = zx
                    rec["z_role"] = zr
                    rec["z_avail"] = 0.0
                    rec["delta"] = delta
                    rec["dps"] = rec["adp"] - lam * delta

        for rec in no_data:
            rec["z_xtd"] = 0.0
            rec["z_role"] = 0.0
            rec["z_avail"] = 0.0
            rec["delta"] = 0.0
            rec["dps"] = rec["adp"]

    return sample_sizes


def _rank_by_adp(records: list[dict]) -> None:
    by_pos: dict[str, list[dict]] = {p: [] for p in DPS_POSITIONS}
    for rec in records:
        by_pos[rec["position"]].append(rec)

    for position in DPS_POSITIONS:
        group = sorted(by_pos[position], key=lambda r: r["adp"])
        for rank, rec in enumerate(group, start=1):
            rec["adp_rank"] = rank


def _rank_by_dps(records: list[dict]) -> None:
    by_pos: dict[str, list[dict]] = {p: [] for p in DPS_POSITIONS}
    for rec in records:
        by_pos[rec["position"]].append(rec)

    for position in DPS_POSITIONS:
        group = sorted(by_pos[position], key=lambda r: (r["dps"], r["adp"]))
        for rank, rec in enumerate(group, start=1):
            rec["dps_rank"] = rank
            rec["rank_move"] = rec["adp_rank"] - rank


def _flags_str(rec: dict) -> str:
    flags = []
    if rec.get("no_2025_data"):
        flags.append("no_2025_data")
    if rec["context_changed"]:
        flags.append("context_changed")
    if rec["low_sample"]:
        flags.append("low_sample")
    return ",".join(flags) if flags else "-"


def _board_header(position: str) -> str:
    if position == "WR":
        return (
            f"{'DPS#':>4} | {'Player':<26} | {'ADP#':>4} | {'DE#':>4} | {'ADP':>6} | "
            f"{'DPS':>7} | {'Delta':>6} | {'Z_xTD':>6} | {'Z_Role':>6} | {'Z_Avail':>7} | "
            f"{'Move':>5} | Flags"
        )
    return (
        f"{'DPS#':>4} | {'Player':<26} | {'ADP#':>4} | {'DE#':>4} | {'ADP':>6} | "
        f"{'DPS':>7} | {'Delta':>6} | {'Z_xTD':>6} | {'Z_Role':>6} | {'Move':>5} | Flags"
    )


def _format_board_row(rec: dict, position: str, draft_edge_ranks: dict[str, int]) -> str:
    de_rank = draft_edge_ranks.get(rec["player_id"])
    de_str = str(de_rank) if de_rank is not None else "-"
    if position == "WR":
        return (
            f"{rec['dps_rank']:>4} | {rec['name']:<26} | {rec['adp_rank']:>4} | {de_str:>4} | "
            f"{rec['adp']:>6.1f} | {rec['dps']:>7.2f} | {rec['delta']:>6.3f} | "
            f"{rec['z_xtd']:>6.2f} | {rec['z_role']:>6.2f} | {rec['z_avail']:>7.2f} | "
            f"{rec['rank_move']:>+5} | {_flags_str(rec)}"
        )
    return (
        f"{rec['dps_rank']:>4} | {rec['name']:<26} | {rec['adp_rank']:>4} | {de_str:>4} | "
        f"{rec['adp']:>6.1f} | {rec['dps']:>7.2f} | {rec['delta']:>6.3f} | "
        f"{rec['z_xtd']:>6.2f} | {rec['z_role']:>6.2f} | {rec['rank_move']:>+5} | "
        f"{_flags_str(rec)}"
    )


def _print_board_table(
    records: list[dict],
    position: str,
    draft_edge_ranks: dict[str, int],
    *,
    title: str,
    top_n: int = 30,
) -> None:
    group = [r for r in records if r["position"] == position]
    group.sort(key=lambda r: r["dps_rank"])

    header = _board_header(position)
    print(f"\n{'=' * len(header)}")
    print(title)
    print(header)
    print("-" * len(header))

    for rec in group[:top_n]:
        print(_format_board_row(rec, position, draft_edge_ranks))


def _print_no_2025_data_summary(records: list[dict], no_data_counts: dict[str, int]) -> None:
    print("\nno_2025_data players added (Delta=0, DPS=ADP, excluded from z-score sample):")
    for position in DPS_POSITIONS:
        print(f"  {position}: {no_data_counts.get(position, 0)}")

    no_data = [r for r in records if r.get("no_2025_data")]
    top5 = sorted(no_data, key=lambda r: r["adp"])[:5]
    print("\nTop 5 no_2025_data players by ADP (where they land on DPS board):")
    if not top5:
        print("  (none)")
        return
    for rec in top5:
        print(
            f"  {rec['name']:<26} ADP={rec['adp']:>6.1f}  ADP#{rec['adp_rank']:>3}  "
            f"DPS#{rec['dps_rank']:>3}  move={rec['rank_move']:>+3}"
        )


def _print_position_board(
    records: list[dict],
    position: str,
    draft_edge_ranks: dict[str, int],
    sample_n: int,
    *,
    top_n: int = 30,
) -> None:
    group = [r for r in records if r["position"] == position]
    group.sort(key=lambda r: r["dps_rank"])

    header = _board_header(position)
    print(f"\n{'=' * len(header)}")
    print(f"{position} — top {top_n} by DPS (lower DPS = draft earlier)")
    print(header)
    print("-" * len(header))

    for rec in group[:top_n]:
        print(_format_board_row(rec, position, draft_edge_ranks))

    _print_movers(records, position, n=5)
    print(f"\n{position} z-score sample: n={sample_n}")


def _print_movers(records: list[dict], position: str, *, n: int = 5) -> None:
    group = [r for r in records if r["position"] == position]

    risers = sorted(group, key=lambda r: r["rank_move"], reverse=True)[:n]
    fallers = sorted(group, key=lambda r: r["rank_move"])[:n]

    print(f"\n{position} — top {n} RISERS vs ADP (rank_move = ADP# - DPS#)")
    for rec in risers:
        print(
            f"  {rec['name']:<26} ADP#{rec['adp_rank']:>3} -> DPS#{rec['dps_rank']:>3} "
            f"move={rec['rank_move']:>+3}  DPS={rec['dps']:.2f}  flags={_flags_str(rec)}"
        )

    print(f"\n{position} — top {n} FALLERS vs ADP")
    for rec in fallers:
        print(
            f"  {rec['name']:<26} ADP#{rec['adp_rank']:>3} -> DPS#{rec['dps_rank']:>3} "
            f"move={rec['rank_move']:>+3}  DPS={rec['dps']:.2f}  flags={_flags_str(rec)}"
        )


def _print_adp_freshness(client, adp_rows: list[dict], context) -> None:
    fetched = [r["fetched_at"] for r in adp_rows if r.get("fetched_at")]
    print("ADP FRESHNESS CHECK")
    print("-" * 40)
    print(f"  adp row count (all sources/history): {len(adp_rows)}")
    print(f"  adp players (latest per player_id):  {len(context.adp_by_player)}")
    if fetched:
        print(f"  fetched_at min: {min(fetched)}")
        print(f"  fetched_at max: {max(fetched)}")
    else:
        print("  fetched_at: (none)")

    # Spotlight sanity names
    lookup_names = list(ADP_SPOTLIGHT_NAMES)
    optional = [ADP_SPOTLIGHT_ALIASES["Brock Bowders"]]
    try:
        by_name = fetch_players_by_names(client, lookup_names)
    except RuntimeError as exc:
        print(f"  Spotlight lookup error: {exc}")
        by_name = {}

    print("\n  Spotlight ADP values (latest per player):")
    for name in ADP_SPOTLIGHT_NAMES:
        player = by_name.get(name)
        if not player:
            print(f"    {name:<22} NOT FOUND")
            continue
        adp = context.adp_by_player.get(player["player_id"])
        adp_str = f"{adp:.1f}" if adp is not None else "no ADP row"
        print(f"    {name:<22} adp_value={adp_str}")

    # Also note Brock Bowders alias if user meant Bowers
    print("    (prompt alias 'Brock Bowders' -> Brock Bowers)")


def main() -> None:
    positions_filter: tuple[str, ...] | None = None
    if len(sys.argv) > 1:
        arg = sys.argv[1].upper()
        if arg in DPS_POSITIONS:
            positions_filter = (arg,)

    print("Draft Priority Score review — read-only, no database writes.\n")

    client = get_supabase_client()
    context = load_draft_edge_context(client)
    def_player_by_team = _get_def_player_by_team(client)
    team_targets_by_team = _build_team_targets_by_team(
        context.season_games_by_player, def_player_by_team,
    )
    team_game_stats = _build_team_game_stats(
        context.season_games_by_player, def_player_by_team,
    )

    ppc, ppt, w_target_derived, w_target_rb_n = _derive_w_target(context)
    print("RB W_TARGET derivation (>= 50 carries in 2025, full PPR scoring rules)")
    print("-" * 60)
    print(f"  RBs in sample:     {w_target_rb_n}")
    print(f"  points/carry:      {ppc:.4f}")
    print(f"  points/target:     {ppt:.4f}")
    print(f"  W_TARGET (derived): {w_target_derived:.4f}")

    records, excluded_no_adp, no_data_counts = _build_player_records(
        context,
        w_target=w_target_derived,
        team_targets_by_team=team_targets_by_team,
        team_game_stats=team_game_stats,
    )
    print(f"\nExcluded (draft pool, no ADP): {excluded_no_adp}")

    draft_edge_ranks = _fetch_draft_edge_ranks(client)

    sample_sizes = _apply_z_scores_and_dps(records)
    _rank_by_adp(records)
    _rank_by_dps(records)

    positions = positions_filter or DPS_POSITIONS

    if "RB" in positions:
        _print_board_table(
            records,
            "RB",
            draft_edge_ranks,
            title=f"RB — top 30 by DPS (W_TARGET={w_target_derived:.4f}, lower DPS = draft earlier)",
        )
        _print_no_2025_data_summary(records, no_data_counts)

        _recompute_rb_role_shifts(
            records, w_target=W_TARGET_COMPARISON, team_targets_by_team=team_targets_by_team,
        )
        _apply_z_scores_and_dps(records)
        _rank_by_dps(records)
        _print_board_table(
            records,
            "RB",
            draft_edge_ranks,
            title=f"RB — top 30 by DPS (W_TARGET={W_TARGET_COMPARISON} comparison, lower DPS = draft earlier)",
        )
        print(f"\nRB z-score sample (2025-data only): n={sample_sizes.get('RB', 0)}")

    for position in positions:
        if position == "RB":
            continue
        _print_position_board(
            records, position, draft_edge_ranks, sample_sizes.get(position, 0),
        )

    if "RB" not in positions:
        _print_no_2025_data_summary(records, no_data_counts)


if __name__ == "__main__":
    main()
