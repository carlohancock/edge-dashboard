"""
Draft Priority Score (DPS) — shared computation for review + production write.

QB/RB: DPS = ADP − (lambda_pos × Delta), Delta = 0.44·Z_xTD + 0.56·Z_Role
WR:    DPS = ADP − (LAMBDA_WR × Delta),
       Delta = 0.20·Z_xTD + 0.50·Z_Role + 0.30·Z_Avail

Production no-adjustment positions (TE / K / DEF): Delta = 0, DPS = ADP.

Constants are settled — do not re-fit here. See docs/edge_formula_draft_edge.md.
"""

from __future__ import annotations

import statistics

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

# ---- settled constants (do not re-fit) ----
DELTA_XTD_WEIGHT = 0.44
DELTA_ROLE_WEIGHT = 0.56
# lambda_QB=8.0, lambda_RB=8.0 from position sweeps; see draft_priority_review history.
LAMBDA_POS = {"QB": 8.0, "RB": 8.0, "WR": 4.0, "TE": 6.0}
LAMBDA_WR = 4.0
CONTEXT_CHANGED_MULTIPLIER = 0.85
GAMES_NORMALIZER = 16.0

WR_BETA_TGT = 0.034
WR_BETA_AY = 0.0017
WR_WOPR_SHRINK_K = 2.5
WR_WOPR_PG_MEDIAN = {
    1: 0.6856,
    2: 0.5017,
    3: 0.3306,
    4: 0.2078,
}
WR_WOPR_PG_DEFAULT = 0.1387
WR_PROJ_GAMES = 17.0
DELTA_WR_XTD_WEIGHT = 0.20
DELTA_WR_ROLE_WEIGHT = 0.50
DELTA_WR_AVAIL_WEIGHT = 0.30

RB_WO_SHARE_PRIOR = {1: 0.603, 2: 0.292, 3: 0.09, 4: 0.046}
DEFAULT_RB_WO_SHARE_PRIOR = 0.046
MIN_RB_CARRIES_W_TARGET = 50
W_TARGET_COMPARISON = 1.5

# Review-path positions (includes legacy TE model for identical review output).
DPS_POSITIONS = ("QB", "RB", "WR", "TE")
# Production model positions (full Δ).
MODEL_POSITIONS = ("QB", "RB", "WR")
# Production no-adjustment (Δ = 0). DEF is the players.position for DST.
NO_ADJUSTMENT_POSITIONS = frozenset({"TE", "K", "DEF"})
PRODUCTION_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")

PAGE_SIZE = 1000


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
    positions: tuple[str, ...] = DPS_POSITIONS,
) -> tuple[list[dict], int, dict[str, int]]:
    excluded_no_adp = 0
    no_data_counts: dict[str, int] = {p: 0 for p in positions}
    records: list[dict] = []
    missing_inputs: list[str] = []

    for player in context.players:
        position = player["position"]
        if position not in positions:
            continue

        pid = player["player_id"]
        adp = context.adp_by_player.get(pid)
        if adp is None:
            excluded_no_adp += 1
            continue

        name = player.get("full_name")
        if not name:
            missing_inputs.append(f"{pid}: missing full_name (substituted '-')")
            name = "-"

        rows = context.season_games_by_player.get(pid, [])
        season_totals = aggregate_skill_season_totals(rows)
        if season_totals["games_played"] == 0:
            no_data_counts[position] += 1
            records.append({
                "player_id": pid,
                "name": name,
                "position": position,
                "adp": adp,
                "xtd_raw": 0.0,
                "role_raw": 0.0,
                "avail_raw": 0.0,
                "context_changed": False,
                "low_sample": False,
                "no_2025_data": True,
                "no_adjustment": False,
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
            "name": name,
            "position": position,
            "adp": adp,
            "xtd_raw": xtd_raw,
            "role_raw": role_raw,
            "avail_raw": avail_raw,
            "context_changed": context_changed,
            "low_sample": _low_sample(position, season_totals),
            "no_2025_data": False,
            "no_adjustment": False,
            "_player": player,
            "_season_totals": season_totals,
            "_share_team_totals": share_team_totals,
            "_primary_2025_team": primary_2025_team,
        })

    return records, excluded_no_adp, no_data_counts, missing_inputs


def _build_no_adjustment_records(
    players: list[dict],
    adp_by_player: dict[str, float],
    season_games_by_player: dict[str, list[dict]],
    *,
    positions: frozenset[str] = NO_ADJUSTMENT_POSITIONS,
) -> tuple[list[dict], int, dict[str, int], list[str]]:
    """TE/K/DEF production records: Δ = 0, DPS = ADP."""
    excluded_no_adp = 0
    no_data_counts: dict[str, int] = {p: 0 for p in positions}
    records: list[dict] = []
    missing_inputs: list[str] = []

    for player in players:
        position = player["position"]
        if position not in positions:
            continue
        pid = player["player_id"]
        adp = adp_by_player.get(pid)
        if adp is None:
            excluded_no_adp += 1
            continue

        name = player.get("full_name")
        if not name:
            missing_inputs.append(f"{pid}: missing full_name (substituted '-')")
            name = "-"

        rows = season_games_by_player.get(pid, [])
        season_totals = aggregate_skill_season_totals(rows)
        gp = season_totals["games_played"]
        if gp == 0:
            no_data_counts[position] += 1
            records.append({
                "player_id": pid,
                "name": name,
                "position": position,
                "adp": adp,
                "xtd_raw": 0.0,
                "role_raw": 0.0,
                "avail_raw": 0.0,
                "z_xtd": 0.0,
                "z_role": 0.0,
                "z_avail": 0.0,
                "delta": 0.0,
                "dps": adp,
                "lambda_used": 0.0,
                "context_changed": False,
                "low_sample": False,
                "no_2025_data": True,
                "no_adjustment": True,
            })
            continue

        primary_2025_team = primary_team_id(season_totals["team_ids_seen"])
        current_team = player.get("team_id")
        context_changed = bool(primary_2025_team and current_team and primary_2025_team != current_team)

        records.append({
            "player_id": pid,
            "name": name,
            "position": position,
            "adp": adp,
            "xtd_raw": 0.0,
            "role_raw": 0.0,
            "avail_raw": 0.0,
            "z_xtd": 0.0,
            "z_role": 0.0,
            "z_avail": 0.0,
            "delta": 0.0,
            "dps": adp,
            "lambda_used": 0.0,
            "context_changed": context_changed,
            "low_sample": _low_sample(position, season_totals),
            "no_2025_data": False,
            "no_adjustment": True,
        })

    return records, excluded_no_adp, no_data_counts, missing_inputs


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


def _lambda_for(position: str) -> float:
    if position == "WR":
        return LAMBDA_WR
    if position in NO_ADJUSTMENT_POSITIONS:
        return 0.0
    return LAMBDA_POS.get(position, 0.0)


def _apply_z_scores_and_dps(
    records: list[dict],
    *,
    positions: tuple[str, ...] | None = None,
) -> dict[str, int]:
    """Z-score within position (2025-data only); compute DPS. No-adjustment → Δ=0."""
    positions = positions or DPS_POSITIONS
    by_pos: dict[str, list[dict]] = {p: [] for p in positions}
    for rec in records:
        if rec["position"] in by_pos:
            by_pos[rec["position"]].append(rec)

    sample_sizes: dict[str, int] = {}
    for position in positions:
        group = by_pos.get(position, [])
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
                    rec["lambda_used"] = lam
                    rec["dps"] = rec["adp"] - lam * delta
                    rec["no_adjustment"] = False
            else:
                # QB/RB (and review-path TE) two-term Δ.
                lam = LAMBDA_POS[position]
                for rec, zx, zr in zip(with_data, z_xtd, z_role):
                    delta = DELTA_XTD_WEIGHT * zx + DELTA_ROLE_WEIGHT * zr
                    rec["z_xtd"] = zx
                    rec["z_role"] = zr
                    rec["z_avail"] = 0.0
                    rec["delta"] = delta
                    rec["lambda_used"] = lam
                    rec["dps"] = rec["adp"] - lam * delta
                    rec["no_adjustment"] = False

        for rec in no_data:
            rec["z_xtd"] = 0.0
            rec["z_role"] = 0.0
            rec["z_avail"] = 0.0
            rec["delta"] = 0.0
            rec["lambda_used"] = _lambda_for(position)
            rec["dps"] = rec["adp"]
            rec["no_adjustment"] = False

    return sample_sizes


def _rank_by_adp(records: list[dict], positions: tuple[str, ...] | None = None) -> None:
    positions = positions or DPS_POSITIONS
    by_pos: dict[str, list[dict]] = {p: [] for p in positions}
    for rec in records:
        if rec["position"] in by_pos:
            by_pos[rec["position"]].append(rec)

    for position in positions:
        group = sorted(by_pos[position], key=lambda r: r["adp"])
        for rank, rec in enumerate(group, start=1):
            rec["adp_rank"] = rank


def _rank_by_dps(records: list[dict], positions: tuple[str, ...] | None = None) -> None:
    positions = positions or DPS_POSITIONS
    by_pos: dict[str, list[dict]] = {p: [] for p in positions}
    for rec in records:
        if rec["position"] in by_pos:
            by_pos[rec["position"]].append(rec)

    for position in positions:
        group = sorted(by_pos[position], key=lambda r: (r["dps"], r["adp"]))
        for rank, rec in enumerate(group, start=1):
            rec["dps_rank"] = rank
            rec["rank_move"] = rec["adp_rank"] - rank


def _rank_overall_by_dps(records: list[dict]) -> None:
    ordered = sorted(records, key=lambda r: (r["dps"], r["adp"], r["name"]))
    for rank, rec in enumerate(ordered, start=1):
        rec["overall_rank"] = rank


def _flags_str(rec: dict) -> str:
    flags = []
    if rec.get("no_2025_data"):
        flags.append("no_2025_data")
    if rec.get("context_changed"):
        flags.append("context_changed")
    if rec.get("low_sample"):
        flags.append("low_sample")
    if rec.get("no_adjustment"):
        flags.append("no_adjustment")
    return ",".join(flags) if flags else "-"


def build_factor_breakdown(rec: dict) -> dict:
    """factor_breakdown payload for edge_scores draft_edge rows."""
    no_adj = bool(rec.get("no_adjustment") or rec["position"] in NO_ADJUSTMENT_POSITIONS)
    dps_val = rec["adp"] if no_adj else round(rec["dps"], 4)
    return {
        "dps": dps_val,
        "adp": rec["adp"],
        "adp_positional_rank": rec["adp_rank"],
        "delta": 0.0 if no_adj else round(rec["delta"], 6),
        "overall_rank": rec["overall_rank"],
        "z_xtd": round(rec["z_xtd"], 6),
        "z_role": round(rec["z_role"], 6),
        "z_avail": round(rec["z_avail"], 6),
        "xtd_delta": round(rec["xtd_raw"], 6),
        "role_shift_raw": round(rec["role_raw"], 6),
        "availability_raw": round(rec["avail_raw"], 6),
        "lambda_used": 0.0 if no_adj else rec.get("lambda_used", _lambda_for(rec["position"])),
        "no_2025_data": bool(rec.get("no_2025_data")),
        "low_sample": bool(rec.get("low_sample")),
        "context_changed": bool(rec.get("context_changed")),
        "no_adjustment": no_adj,
        "position": rec["position"],
        "full_name": rec["name"],
    }
