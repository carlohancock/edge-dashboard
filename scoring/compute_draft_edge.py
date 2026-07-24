"""
Draft Edge orchestration layer.

Season-long positional rankings for the upcoming draft via the Draft Priority
Score (DPS) architecture (docs/edge_formula_draft_edge.md):

  DPS = ADP − (λ_pos × Δ)

QB/RB/WR use the settled model Δ from scoring/draft_priority_score.py.
TE/K/DST (players.position == 'DEF') are finalized as Δ = 0 (DPS = ADP).

Writes to `edge_scores` with score_type='draft_edge'. Per the schema's
Draft Edge column comment, this is RANK-based, not a /100 score:
`score_value` is left null and `positional_rank` is populated. Reuses the
same UNIQUE(player_id, score_type, period) upsert path added in Phase 4.7
-- no schema change needed.

Population: players with an ADP row only. Legacy projected-points path is
superseded for this write; weekly Edge / Wire Edge are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from config.supabase_client import get_supabase_client
from scoring.draft_priority_score import (
    MODEL_POSITIONS,
    NO_ADJUSTMENT_POSITIONS,
    PRODUCTION_POSITIONS,
    _apply_z_scores_and_dps,
    _build_no_adjustment_records,
    _build_player_records,
    _build_team_game_stats,
    _build_team_targets_by_team,
    _derive_w_target,
    _rank_by_adp,
    _rank_by_dps,
    _rank_overall_by_dps,
    build_factor_breakdown,
)
from scoring.season_stats import (
    MIN_SEASON_GAMES,
    aggregate_skill_season_totals,
    build_position_role_baselines,
    collect_raw_observed_stats,
    compute_qb_rush_td_per_carry_prior,
    fetch_player_season_games,
    fetch_season_games_by_player,
    fetch_team_season_totals,
    primary_team_id,
)
from scoring.draft_edge_features import (
    QB_FACTOR_AUDIT_KEYS,
    build_draft_kicker_features,
    build_draft_qb_features,
    build_draft_rb_features,
    build_draft_wr_te_features,
)
from scoring.points_calculator import (
    calculate_kicker_points,
    calculate_observed_qb_season_points,
    calculate_qb_points,
    calculate_rb_points,
    calculate_wr_te_points,
)
from scoring.stats_utils import projected_to_observed_ratio

SEASON = 2026            # the season being projected/drafted for
BASE_SEASON = 2025       # the season the projection is BUILT FROM
DRAFT_POSITIONS = ("QB", "RB", "WR", "TE", "K")
PERIOD = f"{SEASON}-draftedge"
SCORE_TYPE = "draft_edge"

# Shrinkage-strength sweep used by run_shrinkage_calibration_review().
CALIBRATION_SHRINKAGE_STRENGTHS = {
    "low": 3.0,
    "moderate": 8.0,
    "high": 18.0,
}
CALIBRATION_SPOTLIGHT_NAMES = (
    "Christian McCaffrey",
    "Kyler Murray",
    "Jeremiyah Love",
    "Jadarian Price",
)
QB_SHRINKAGE_DIAGNOSTIC_NAMES = (
    "Lamar Jackson",
    "Josh Allen",
    "Matthew Stafford",
    "Jared Goff",
)

# If a no-historical-data player has no ADP either (undrafted rookie/UDFA
# with zero market signal at all), place them just below the position's
# worst REAL projection rather than fabricating a number from nothing.
NO_ADP_FLOOR_FACTOR = 0.5


def _get_def_player_by_team(client) -> dict[str, str]:
    """team_id -> DEF player_id (32 rows, well under any pagination cap). Local, tiny, and separate from compute_edge_scores.py's copy on purpose (see module docstring: this module doesn't share machinery with the weekly engine)."""
    result = (
        client.table("players")
        .select("player_id, team_id")
        .eq("sport", "nfl")
        .eq("position", "DEF")
        .execute()
    )
    return {row["team_id"]: row["player_id"] for row in (result.data or []) if row.get("team_id")}


PAGE_SIZE = 1000  # PostgREST's default per-request row cap -- must paginate explicitly past it


def _fetch_draft_pool(client) -> list[dict]:
    """
    Paginated on purpose: unpaginated .execute() calls silently truncate at
    PostgREST's default 1000-row cap with NO error -- the draft pool (991
    as of this writing) is currently just under that limit, which would
    make a truncation bug invisible until the player pool grows past 1000
    and rankings silently go missing players with no error printed. Same
    PAGE_SIZE pattern already used in pipeline/seed_players.py etc.
    """
    players: list[dict] = []
    offset = 0
    while True:
        result = (
            client.table("players")
            .select("player_id, full_name, position, team_id, depth_chart_rank")
            .eq("sport", "nfl")
            .in_("position", DRAFT_POSITIONS)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = result.data or []
        players.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return players


def _fetch_adp_by_player(client) -> dict[str, float]:
    rows: list[dict] = []
    offset = 0
    while True:
        result = client.table("adp").select("player_id, adp_value, fetched_at").range(offset, offset + PAGE_SIZE - 1).execute()
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    latest: dict[str, tuple[str, float]] = {}
    for row in rows:
        pid = row["player_id"]
        fetched_at = row.get("fetched_at") or ""
        if pid not in latest or fetched_at > latest[pid][0]:
            latest[pid] = (fetched_at, row["adp_value"])
    return {pid: adp for pid, (_, adp) in latest.items()}


def _team_season_totals_by_team(
    client,
    def_player_by_team: dict[str, str],
    season_games_by_player: dict[str, list[dict]] | None = None,
) -> dict[str, dict]:
    """2025 season rush/pass attempt totals for every team, keyed by team_id (used as the shared RB/WR/TE volume baseline)."""
    totals: dict[str, dict] = {}
    for team_id, def_player_id in def_player_by_team.items():
        if season_games_by_player is not None:
            rows = season_games_by_player.get(def_player_id, [])
            agg = aggregate_skill_season_totals(rows)
            totals[team_id] = {
                "carries": agg["carries"],
                "attempts": agg["attempts"],
                "games_played": agg["games_played"],
            }
        else:
            totals[team_id] = fetch_team_season_totals(client, def_player_id, BASE_SEASON)
    return totals


@dataclass
class DraftEdgeContext:
    """Pre-fetched inputs shared across shrinkage-strength sweeps (calibration review)."""

    players: list[dict]
    team_totals_by_team: dict[str, dict]
    adp_by_player: dict[str, float]
    season_games_by_player: dict[str, list[dict]]
    baselines: dict


def load_draft_edge_context(client) -> DraftEdgeContext:
    """Fetch draft-pool + 2025 season data once; reuse for multiple projection passes."""
    players = _fetch_draft_pool(client)
    def_player_by_team = _get_def_player_by_team(client)
    season_games_by_player = fetch_season_games_by_player(client, BASE_SEASON)
    team_totals_by_team = _team_season_totals_by_team(
        client, def_player_by_team, season_games_by_player,
    )
    adp_by_player = _fetch_adp_by_player(client)
    baselines = _build_role_baselines(
        client, players, team_totals_by_team, season_games_by_player,
    )
    return DraftEdgeContext(
        players=players,
        team_totals_by_team=team_totals_by_team,
        adp_by_player=adp_by_player,
        season_games_by_player=season_games_by_player,
        baselines=baselines,
    )


def _empty_team_totals() -> dict:
    return {"carries": None, "attempts": None, "games_played": 0}


def compute_player_draft_projection(
    client,
    player: dict,
    team_totals_by_team: dict[str, dict],
    baselines: dict | None = None,
    shrinkage_strength: float | None = None,
    season_rows: list[dict] | None = None,
) -> dict | None:
    """
    Returns {"points": float, "features": dict} for a player WITH 2025
    history, or None if the player has zero 2025 stats (routed to the
    ADP-anchored placeholder pass in compute_and_write_draft_edge instead
    -- see module docstring's Task 2 rookie/no-data handling).
    """
    position = player["position"]
    current_team_id = player["team_id"]
    depth_chart_rank = player.get("depth_chart_rank")

    if season_rows is None:
        rows = fetch_player_season_games(client, player["player_id"], BASE_SEASON)
    else:
        rows = season_rows
    season_totals = aggregate_skill_season_totals(rows)
    if season_totals["games_played"] == 0:
        return None

    primary_2025_team_id = primary_team_id(season_totals["team_ids_seen"])
    context_changed = bool(primary_2025_team_id and current_team_id and primary_2025_team_id != current_team_id)

    if position == "QB":
        features = build_draft_qb_features(
            season_totals, depth_chart_rank, context_changed, baselines, shrinkage_strength,
        )
        points = calculate_qb_points(features)

    elif position == "RB":
        current_team_totals = team_totals_by_team.get(current_team_id, _empty_team_totals())
        share_team_totals = team_totals_by_team.get(primary_2025_team_id, _empty_team_totals())
        features = build_draft_rb_features(
            season_totals, current_team_totals, share_team_totals, depth_chart_rank, context_changed,
            baselines, shrinkage_strength,
        )
        points = calculate_rb_points(features)

    elif position in ("WR", "TE"):
        current_team_totals = team_totals_by_team.get(current_team_id, _empty_team_totals())
        share_team_totals = team_totals_by_team.get(primary_2025_team_id, _empty_team_totals())
        features = build_draft_wr_te_features(
            season_totals, current_team_totals, share_team_totals, depth_chart_rank, context_changed, position,
            baselines, shrinkage_strength,
        )
        points = calculate_wr_te_points(features)

    elif position == "K":
        features = build_draft_kicker_features(
            season_totals, depth_chart_rank, context_changed, baselines, shrinkage_strength,
        )
        points = calculate_kicker_points(features)

    else:
        return None

    return {"points": points, "features": features}


def _interpolate_placeholder_points(known_adp_points: list[tuple[float, float]], target_adp: float) -> float:
    """
    known_adp_points: (adp_value, points) pairs for REAL (non-placeholder)
    projections at this position, sorted ascending by adp_value. Simple
    linear interpolation by market ADP -- deliberately not fancier than
    this (Task 2/3's "don't fabricate a projection from nothing": the
    number comes entirely from the market's own ranking plus OTHER
    players' real model output, never from a made-up assumption about the
    specific player).
    """
    if not known_adp_points:
        return 0.0
    if target_adp <= known_adp_points[0][0]:
        return known_adp_points[0][1]
    if target_adp >= known_adp_points[-1][0]:
        return known_adp_points[-1][1]
    for (adp_lo, pts_lo), (adp_hi, pts_hi) in zip(known_adp_points, known_adp_points[1:]):
        if adp_lo <= target_adp <= adp_hi:
            if adp_hi == adp_lo:
                return pts_lo
            frac = (target_adp - adp_lo) / (adp_hi - adp_lo)
            return pts_lo + frac * (pts_hi - pts_lo)
    return known_adp_points[-1][1]


def _build_placeholder_projection(
    position: str,
    adp_value: float | None,
    real_points_by_position: dict[str, list[tuple[float, float]]],
) -> dict:
    known_points = real_points_by_position.get(position, [])
    if adp_value is not None and known_points:
        points = _interpolate_placeholder_points(known_points, adp_value)
    elif known_points:
        # No ADP either -- park below the worst real projection, don't invent a number from nothing.
        points = known_points[-1][1] * NO_ADP_FLOOR_FACTOR
    else:
        points = 1.0  # degenerate case: no real projections exist at this position at all

    return {
        "points": points,
        "features": {
            "no_historical_data": True,
            "context_changed": False,
            "low_sample": True,
            "placeholder_method": "adp_interpolation" if (adp_value is not None and known_points) else "floor_below_worst_real_projection",
        },
    }


def _build_role_baselines(
    client,
    players: list[dict],
    team_totals_by_team: dict[str, dict],
    season_games_by_player: dict[str, list[dict]] | None = None,
) -> dict:
    """Collect 2025 raw observed stats (MIN_SEASON_GAMES+) and build trimmed-mean baselines."""
    observed_samples: list[tuple[str, int | None, dict[str, float]]] = []

    for player in players:
        if season_games_by_player is not None:
            rows = season_games_by_player.get(player["player_id"], [])
        else:
            rows = fetch_player_season_games(client, player["player_id"], BASE_SEASON)
        season_totals = aggregate_skill_season_totals(rows)
        if season_totals["games_played"] < MIN_SEASON_GAMES:
            continue

        position = player["position"]
        primary_2025_team_id = primary_team_id(season_totals["team_ids_seen"])
        share_team_totals = team_totals_by_team.get(primary_2025_team_id, _empty_team_totals())

        raw = collect_raw_observed_stats(season_totals, position, share_team_totals)
        if raw:
            observed_samples.append((position, player.get("depth_chart_rank"), raw))

    baselines = build_position_role_baselines(observed_samples)
    baselines[("QB", None, "rush_tds_per_carry_prior")] = compute_qb_rush_td_per_carry_prior(
        observed_samples,
    )
    return baselines


def compute_draft_edge_projections(
    shrinkage_strength: float | None = None,
    context: DraftEdgeContext | None = None,
) -> tuple[list[dict], dict[str, dict], dict[str, float]]:
    """
    Full Draft Edge compute pass without writing to edge_scores.
    Returns (players, all_projections_by_pid, adp_by_player).

    Pass a pre-loaded `context` to skip Supabase fetches (used by the
    shrinkage calibration review, which sweeps three strengths).
    """
    if context is None:
        client = get_supabase_client()
        context = load_draft_edge_context(client)
    else:
        client = None

    players = context.players
    team_totals_by_team = context.team_totals_by_team
    adp_by_player = context.adp_by_player
    season_games_by_player = context.season_games_by_player
    baselines = context.baselines

    real_by_pid: dict[str, dict] = {}
    placeholder_pids: list[str] = []
    for player in players:
        proj = compute_player_draft_projection(
            client,
            player,
            team_totals_by_team,
            baselines,
            shrinkage_strength,
            season_rows=season_games_by_player.get(player["player_id"], []),
        )
        if proj is None:
            placeholder_pids.append(player["player_id"])
        else:
            real_by_pid[player["player_id"]] = {**proj, "position": player["position"]}

    real_points_by_position: dict[str, list[tuple[float, float]]] = {}
    for pid, proj in real_by_pid.items():
        adp_value = adp_by_player.get(pid)
        if adp_value is not None:
            real_points_by_position.setdefault(proj["position"], []).append((adp_value, proj["points"]))
    for pts in real_points_by_position.values():
        pts.sort(key=lambda pair: pair[0])

    players_by_id = {p["player_id"]: p for p in players}
    all_projections: dict[str, dict] = dict(real_by_pid)
    for pid in placeholder_pids:
        position = players_by_id[pid]["position"]
        placeholder = _build_placeholder_projection(position, adp_by_player.get(pid), real_points_by_position)
        all_projections[pid] = {**placeholder, "position": position}

    return players, all_projections, adp_by_player


def _fetch_def_players(client) -> list[dict]:
    """DST rows live as players.position == 'DEF'."""
    result = (
        client.table("players")
        .select("player_id, full_name, position, team_id, depth_chart_rank")
        .eq("sport", "nfl")
        .eq("position", "DEF")
        .execute()
    )
    return result.data or []


def _count_draft_edge_rows(client) -> int:
    total = 0
    offset = 0
    while True:
        result = (
            client.table("edge_scores")
            .select("player_id")
            .eq("score_type", SCORE_TYPE)
            .eq("period", PERIOD)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = result.data or []
        total += len(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return total


def _fetch_draft_edge_player_ids(client) -> set[str]:
    ids: set[str] = set()
    offset = 0
    while True:
        result = (
            client.table("edge_scores")
            .select("player_id")
            .eq("score_type", SCORE_TYPE)
            .eq("period", PERIOD)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = result.data or []
        for row in batch:
            ids.add(row["player_id"])
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return ids


def compute_production_dps_records(client) -> tuple[list[dict], dict]:
    """
    Build production DPS records for all ADP-eligible players.
    Returns (records, meta) where meta holds sample sizes / exclusion counts.
    """
    context = load_draft_edge_context(client)
    def_players = _fetch_def_players(client)
    def_player_by_team = _get_def_player_by_team(client)

    # Merge DEF into a combined player list for no-adjustment path.
    # Context.players already has QB/RB/WR/TE/K.
    all_players_by_id = {p["player_id"]: p for p in context.players}
    for p in def_players:
        all_players_by_id[p["player_id"]] = p
    all_players = list(all_players_by_id.values())

    team_targets_by_team = _build_team_targets_by_team(
        context.season_games_by_player, def_player_by_team,
    )
    team_game_stats = _build_team_game_stats(
        context.season_games_by_player, def_player_by_team,
    )
    ppc, ppt, w_target, w_target_rb_n = _derive_w_target(context)

    print(f"ADP players (latest per player_id): n={len(context.adp_by_player)}")
    print(f"Draft-pool skill players loaded: n={len(context.players)}")
    print(f"DEF players loaded: n={len(def_players)}")
    print(f"RB W_TARGET (derived): {w_target:.4f}  (n_rb>=50 carries: {w_target_rb_n})")
    print(f"  points/carry={ppc:.4f}  points/target={ppt:.4f}")

    model_records, excl_model, no_data_model, missing_model = _build_player_records(
        context,
        w_target=w_target,
        team_targets_by_team=team_targets_by_team,
        team_game_stats=team_game_stats,
        positions=MODEL_POSITIONS,
    )
    print(f"Model positions (QB/RB/WR) with ADP: n={len(model_records)}")
    print(f"  excluded (in pool, no ADP): n={excl_model}")
    print(f"  no_2025_data: { {k: no_data_model.get(k, 0) for k in MODEL_POSITIONS} }")

    no_adj_records, excl_no_adj, no_data_no_adj, missing_no_adj = _build_no_adjustment_records(
        all_players,
        context.adp_by_player,
        context.season_games_by_player,
        positions=NO_ADJUSTMENT_POSITIONS,
    )
    print(f"No-adjustment positions (TE/K/DEF) with ADP: n={len(no_adj_records)}")
    print(f"  excluded (in pool, no ADP): n={excl_no_adj}")
    print(f"  no_2025_data: {dict(no_data_no_adj)}")

    missing_inputs = missing_model + missing_no_adj
    if missing_inputs:
        print(f"Missing inputs substituted: n={len(missing_inputs)}")
        for line in missing_inputs:
            print(f"  {line}")
    else:
        print("Missing inputs substituted: n=0")

    sample_sizes = _apply_z_scores_and_dps(model_records, positions=MODEL_POSITIONS)
    records = model_records + no_adj_records
    _rank_by_adp(records, positions=PRODUCTION_POSITIONS)
    _rank_by_dps(records, positions=PRODUCTION_POSITIONS)
    _rank_overall_by_dps(records)

    meta = {
        "w_target": w_target,
        "w_target_rb_n": w_target_rb_n,
        "sample_sizes": sample_sizes,
        "n_model": len(model_records),
        "n_no_adj": len(no_adj_records),
        "n_total": len(records),
        "adp_n": len(context.adp_by_player),
    }
    return records, meta


def compute_and_write_draft_edge() -> None:
    client = get_supabase_client()

    existing_before = _count_draft_edge_rows(client)
    print(f"Existing draft_edge rows for period={PERIOD}: n={existing_before}")

    records, meta = compute_production_dps_records(client)
    print(f"\nProduction DPS board size: n={meta['n_total']} "
          f"(model={meta['n_model']}, no_adjustment={meta['n_no_adj']})")

    computed_at = datetime.now(timezone.utc).isoformat()
    rows_to_upsert = []
    for rec in records:
        rows_to_upsert.append({
            "player_id": rec["player_id"],
            "score_type": SCORE_TYPE,
            "period": PERIOD,
            "score_value": None,
            "positional_rank": rec["dps_rank"],
            "factor_breakdown": build_factor_breakdown(rec),
            "computed_at": computed_at,
        })

    written_ids = {r["player_id"] for r in rows_to_upsert}

    if rows_to_upsert:
        # Upsert in chunks to stay under payload limits.
        chunk_size = 200
        for i in range(0, len(rows_to_upsert), chunk_size):
            chunk = rows_to_upsert[i:i + chunk_size]
            client.table("edge_scores").upsert(
                chunk, on_conflict="player_id,score_type,period"
            ).execute()

    print(f"Upserted draft_edge rows: n={len(rows_to_upsert)}")

    # Stale-row cleanup: delete legacy ADP-less (and any other) rows not in written set.
    existing_ids = _fetch_draft_edge_player_ids(client)
    stale_ids = sorted(existing_ids - written_ids)
    print(f"Stale draft_edge player_ids to delete: n={len(stale_ids)}")

    deleted_count = 0
    if stale_ids:
        chunk_size = 200
        for i in range(0, len(stale_ids), chunk_size):
            chunk = stale_ids[i:i + chunk_size]
            result = (
                client.table("edge_scores")
                .delete()
                .eq("score_type", SCORE_TYPE)
                .eq("period", PERIOD)
                .in_("player_id", chunk)
                .execute()
            )
            # Prefer actual returned rows when available; else fall back to requested chunk
            # and verify after.
            returned = result.data or []
            if returned:
                deleted_count += len(returned)
            else:
                deleted_count += len(chunk)

    # Verify deletes actually landed (Phase 3 game-seeding bug: do not trust pre-delete alone).
    remaining_ids = _fetch_draft_edge_player_ids(client)
    stale_remaining = remaining_ids - written_ids
    if stale_remaining:
        print(f"WARNING: stale rows still present after delete: n={len(stale_remaining)}")
        # Retry once
        retry = sorted(stale_remaining)
        for i in range(0, len(retry), 200):
            chunk = retry[i:i + 200]
            client.table("edge_scores").delete().eq("score_type", SCORE_TYPE).eq(
                "period", PERIOD
            ).in_("player_id", chunk).execute()
        remaining_ids = _fetch_draft_edge_player_ids(client)
        stale_remaining = remaining_ids - written_ids
        deleted_count = len(stale_ids)  # requested; verified below

    actually_deleted = len(stale_ids) - len(stale_remaining)
    print(f"Stale rows actually deleted: n={actually_deleted}")
    if stale_remaining:
        print(f"ERROR: {len(stale_remaining)} stale rows could not be deleted")

    total_after = _count_draft_edge_rows(client)
    print(f"Total draft_edge rows after cleanup: n={total_after}")

    _print_dps_write_verification(client, records, meta, total_after, written_ids)


def _print_dps_write_verification(
    client,
    records: list[dict],
    meta: dict,
    total_after: int,
    written_ids: set[str],
) -> None:
    """Eight verification blocks required by the production-write prompt."""
    from collections import Counter

    # ---- 1. Rows written by position + total ----
    print("\n" + "=" * 80)
    print("VERIFICATION 1 — rows written by position")
    by_pos = Counter(r["position"] for r in records)
    for pos in PRODUCTION_POSITIONS:
        print(f"  {pos}: n={by_pos.get(pos, 0)}")
    print(f"  TOTAL written: n={len(records)}")

    # ---- 2. Total rows after cleanup == written ----
    print("\n" + "=" * 80)
    print("VERIFICATION 2 — total rows for period after cleanup")
    print(f"  total_rows period={PERIOD}: n={total_after}")
    print(f"  rows written: n={len(records)}")
    print(f"  match: {total_after == len(records)}")

    # ---- 3. Upsert dedup ----
    print("\n" + "=" * 80)
    print("VERIFICATION 3 — upsert dedup (total_rows == distinct player_id)")
    distinct_ids = _fetch_draft_edge_player_ids(client)
    print(f"  total_rows: n={total_after}")
    print(f"  distinct(player_id): n={len(distinct_ids)}")
    print(f"  match: {total_after == len(distinct_ids)}")

    # Reload factor_breakdown from DB for remaining checks
    db_rows: list[dict] = []
    offset = 0
    while True:
        result = (
            client.table("edge_scores")
            .select("player_id, positional_rank, score_value, factor_breakdown")
            .eq("score_type", SCORE_TYPE)
            .eq("period", PERIOD)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = result.data or []
        db_rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    by_id = {r["player_id"]: r for r in db_rows}
    rec_by_id = {r["player_id"]: r for r in records}

    # ---- 4. Top 15 overall ----
    print("\n" + "=" * 80)
    print("VERIFICATION 4 — top 15 by overall_rank")
    top15 = sorted(records, key=lambda r: r["overall_rank"])[:15]
    hdr = f"{'OVR':>3} | {'Pos':>3} | {'Player':<26} | {'ADP':>6} | {'DPS':>7} | {'Pos#':>4}"
    print(hdr)
    print("-" * len(hdr))
    for r in top15:
        print(
            f"{r['overall_rank']:>3} | {r['position']:>3} | {r['name']:<26} | "
            f"{r['adp']:>6.1f} | {r['dps']:>7.2f} | {r['dps_rank']:>4}"
        )

    # ---- 5. Top 10 per position ----
    print("\n" + "=" * 80)
    print("VERIFICATION 5 — top 10 per position by positional_rank")
    for pos in PRODUCTION_POSITIONS:
        group = sorted(
            [r for r in records if r["position"] == pos],
            key=lambda r: r["dps_rank"],
        )[:10]
        print(f"\n  {pos} (n_pos={by_pos.get(pos, 0)})")
        if not group:
            print("    (none)")
            continue
        for r in group:
            print(
                f"    #{r['dps_rank']:<2} {r['name']:<26} ADP={r['adp']:>6.1f}  "
                f"DPS={r['dps']:>7.2f}  OVR={r['overall_rank']:>3}"
            )

    # ---- 6. WR verification anchor ----
    print("\n" + "=" * 80)
    print("VERIFICATION 6 — WR anchor (Nacua #1, Chase #2, Lamb #3, JSN #6)")
    wr = {r["name"]: r for r in records if r["position"] == "WR"}
    anchors = [
        ("Puka Nacua", 1),
        ("Ja'Marr Chase", 2),
        ("CeeDee Lamb", 3),
        ("Jaxon Smith-Njigba", 6),
    ]
    for name, expected in anchors:
        rec = wr.get(name)
        if not rec:
            print(f"  {name}: NOT FOUND")
            continue
        ok = rec["dps_rank"] == expected
        print(
            f"  {name}: DPS#={rec['dps_rank']} expected={expected}  "
            f"{'OK' if ok else 'MISMATCH'}  DPS={rec['dps']:.2f}"
        )

    # ---- 7. factor_breakdown null/empty count ----
    print("\n" + "=" * 80)
    print("VERIFICATION 7 — factor_breakdown null/empty count (must be 0)")
    null_empty = 0
    for row in db_rows:
        fb = row.get("factor_breakdown")
        if fb is None or fb == {} or fb == []:
            null_empty += 1
    print(f"  null/empty factor_breakdown: n={null_empty}")

    # ---- 8. TE/K/DEF delta=0 and dps==adp ----
    print("\n" + "=" * 80)
    print("VERIFICATION 8 — TE/K/DEF delta=0 and dps==adp")
    for pos in ("TE", "K", "DEF"):
        group = [r for r in records if r["position"] == pos]
        bad_delta = sum(1 for r in group if r["delta"] != 0.0)
        bad_dps = sum(1 for r in group if abs(r["dps"] - r["adp"]) > 1e-9)
        no_adj_flag = sum(1 for r in group if not r.get("no_adjustment"))
        print(
            f"  {pos}: n={len(group)}  delta!=0: {bad_delta}  "
            f"dps!=adp: {bad_dps}  missing no_adjustment flag: {no_adj_flag}"
        )
        # Cross-check DB
        db_bad = 0
        for r in group:
            row = by_id.get(r["player_id"])
            if not row:
                db_bad += 1
                continue
            fb = row.get("factor_breakdown") or {}
            if fb.get("delta") != 0 or fb.get("dps") != fb.get("adp"):
                db_bad += 1
        print(f"    DB mismatches (delta!=0 or dps!=adp): n={db_bad}")

    print("\nVERIFICATION COMPLETE")


def find_player_by_name(players: list[dict], name: str) -> dict | None:
    for player in players:
        if player["full_name"] == name:
            return player
    return None


def fetch_players_by_names(client, names: tuple[str, ...] | list[str]) -> dict[str, dict]:
    result = (
        client.table("players")
        .select("player_id, full_name, position, team_id, depth_chart_rank")
        .eq("sport", "nfl")
        .in_("full_name", list(names))
        .execute()
    )
    by_name = {row["full_name"]: row for row in (result.data or [])}
    missing = [name for name in names if name not in by_name]
    if missing:
        raise RuntimeError(f"Players not found: {', '.join(missing)}")
    return by_name


def fetch_draft_edge_rows(
    client,
    player_ids: list[str],
    *,
    period: str = PERIOD,
    score_type: str = SCORE_TYPE,
) -> dict[str, dict]:
    result = (
        client.table("edge_scores")
        .select("player_id, factor_breakdown, positional_rank, computed_at")
        .eq("score_type", score_type)
        .eq("period", period)
        .in_("player_id", player_ids)
        .execute()
    )
    by_player = {row["player_id"]: row for row in (result.data or [])}
    missing = [pid for pid in player_ids if pid not in by_player]
    if missing:
        raise RuntimeError(
            f"No {score_type} row for period={period}: {', '.join(missing)}"
        )
    return by_player


def rank_players_at_position(
    players: list[dict],
    all_projections: dict[str, dict],
    position: str,
) -> list[tuple[int, dict, dict]]:
    """Return (positional_rank, player, projection) tuples sorted by projected points."""
    players_by_id = {p["player_id"]: p for p in players}
    pids = [p["player_id"] for p in players if p["position"] == position]
    sorted_pids = sorted(pids, key=lambda pid: all_projections[pid]["points"], reverse=True)
    return [
        (rank + 1, players_by_id[pid], all_projections[pid])
        for rank, pid in enumerate(sorted_pids)
    ]


def rb10_25_points_slope(ranked_rbs: list[tuple[int, dict, dict]]) -> float:
    """Average projected-point drop per rank slot across RB10–RB25 (calibration tiebreaker)."""
    pts = [proj["points"] for rank, _, proj in ranked_rbs if 10 <= rank <= 25]
    if len(pts) < 2:
        return 0.0
    return (pts[0] - pts[-1]) / (len(pts) - 1)


def projection_feature_flags(features: dict) -> list[str]:
    flags = []
    if features.get("no_historical_data"):
        flags.append("PLACEHOLDER")
    if features.get("low_sample"):
        flags.append("low_sample")
    if features.get("context_changed"):
        flags.append("context_changed")
    return flags


def build_qb_shrinkage_diagnostic_rows(
    client,
    names: tuple[str, ...] | list[str] = QB_SHRINKAGE_DIAGNOSTIC_NAMES,
    *,
    base_season: int = BASE_SEASON,
    period: str = PERIOD,
) -> list[dict]:
    """
    Compare 2025 observed season totals vs stored Draft Edge factor_breakdown
    for named QBs. Pure read — no edge_scores writes.
    """
    players_by_name = fetch_players_by_names(client, names)
    player_ids = [players_by_name[name]["player_id"] for name in names]
    edge_rows = fetch_draft_edge_rows(client, player_ids, period=period)

    rows: list[dict] = []
    for name in names:
        player = players_by_name[name]
        player_id = player["player_id"]
        game_rows = fetch_player_season_games(client, player_id, base_season)
        totals = aggregate_skill_season_totals(game_rows)
        fb = edge_rows[player_id]["factor_breakdown"]

        proj_att = fb.get("proj_pass_attempts")
        proj_rush_yards = fb.get("proj_rush_yards")
        proj_rush_tds = fb.get("proj_rush_tds")
        proj_fp = fb.get("projected_points")

        rows.append({
            "name": name,
            "player_id": player_id,
            "totals": totals,
            "factor_breakdown": fb,
            "positional_rank": edge_rows[player_id].get("positional_rank"),
            "computed_at": edge_rows[player_id].get("computed_at"),
            "obs_pass_attempts": totals["attempts"],
            "obs_rush_yards": totals["rushing_yards"],
            "obs_rush_tds": totals["rushing_tds"],
            "obs_fp": calculate_observed_qb_season_points(totals),
            "proj_pass_attempts": proj_att,
            "proj_rush_yards": proj_rush_yards,
            "proj_rush_tds": proj_rush_tds,
            "proj_fp": proj_fp,
            "att_ratio": projected_to_observed_ratio(proj_att, totals["attempts"]),
            "rush_yards_ratio": projected_to_observed_ratio(proj_rush_yards, totals["rushing_yards"]),
            "rush_tds_ratio": projected_to_observed_ratio(proj_rush_tds, totals["rushing_tds"]),
        })
    return rows


def _format_ratio(ratio: float | None) -> str:
    if ratio is None:
        return "N/A"
    return f"{ratio:.3f}"


def print_qb_shrinkage_diagnostic(
    client=None,
    names: tuple[str, ...] | list[str] = QB_SHRINKAGE_DIAGNOSTIC_NAMES,
) -> list[dict]:
    """Print QB observed-vs-projected audit table; returns structured rows."""
    if client is None:
        client = get_supabase_client()
    diagnostic_rows = build_qb_shrinkage_diagnostic_rows(client, names)

    print(f"QB shrinkage diagnostic — {BASE_SEASON} observed vs Draft Edge ({PERIOD})")
    print("Read-only: no database writes.\n")

    header = (
        f"{'Name':<18} "
        f"{'Obs Att':>8} {'Proj Att':>8} {'Att Ratio':>9} "
        f"{'Obs RushY':>9} {'Proj RushY':>10} {'RushY Ratio':>11} "
        f"{'Obs RushTD':>10} {'Proj RushTD':>11} {'RushTD Ratio':>12} "
        f"{'Obs FP':>8} {'Proj FP':>8} {'GP':>4}"
    )
    print(header)
    print("-" * len(header))

    for row in diagnostic_rows:
        totals = row["totals"]
        proj_rush_tds = row["proj_rush_tds"]
        proj_fp = row["proj_fp"]
        print(
            f"{row['name']:<18} "
            f"{row['obs_pass_attempts']:>8.0f} {row['proj_pass_attempts']:>8.1f} "
            f"{_format_ratio(row['att_ratio']):>9} "
            f"{row['obs_rush_yards']:>9.0f} {row['proj_rush_yards']:>10.1f} "
            f"{_format_ratio(row['rush_yards_ratio']):>11} "
            f"{row['obs_rush_tds']:>10.0f} "
            f"{(f'{proj_rush_tds:.3f}' if proj_rush_tds is not None else 'N/A'):>11} "
            f"{_format_ratio(row['rush_tds_ratio']):>12} "
            f"{row['obs_fp']:>8.1f} {proj_fp if proj_fp is not None else 'N/A':>8} "
            f"{totals['games_played']:>4.0f}"
        )

    print("\n2025 observed season totals (player_game_stats, Phase 4.8 team_id backfill)")
    obs_header = (
        f"{'Name':<18} {'Pass Att':>8} {'Pass Yds':>9} {'Pass TD':>7} "
        f"{'Rush Att':>8} {'Rush Yds':>8} {'Rush TD':>7} {'GP':>4}"
    )
    print(obs_header)
    print("-" * len(obs_header))
    for row in diagnostic_rows:
        t = row["totals"]
        print(
            f"{row['name']:<18} "
            f"{t['attempts']:>8.0f} {t['passing_yards']:>9.0f} {t['passing_tds']:>7.0f} "
            f"{t['carries']:>8.0f} {t['rushing_yards']:>8.0f} {t['rushing_tds']:>7.0f} "
            f"{t['games_played']:>4.0f}"
        )

    print(f"\nDraft Edge factor_breakdown ({PERIOD}) — projections + shrinkage intermediates")
    proj_header = (
        f"{'Name':<18} {'Proj Att':>8} {'Proj Rush Att':>13} {'Proj Rush Yds':>13} "
        f"{'Proj Rush TDs':>13} {'Proj Pass Yds':>13} {'Proj Pass TDs':>13} "
        f"{'Pos Rank':>8}"
    )
    print(proj_header)
    print("-" * len(proj_header))
    for row in diagnostic_rows:
        fb = row["factor_breakdown"]
        proj_rush_tds = row["proj_rush_tds"]
        print(
            f"{row['name']:<18} "
            f"{fb.get('proj_pass_attempts', 0):>8.1f} "
            f"{fb.get('proj_rush_attempts', 0):>13.1f} "
            f"{fb.get('proj_rush_yards', 0):>13.1f} "
            f"{(f'{proj_rush_tds:.3f}' if proj_rush_tds is not None else 'N/A'):>13} "
            f"{fb.get('proj_pass_yards', 0):>13.1f} "
            f"{fb.get('proj_pass_tds', 0):>13.2f} "
            f"{row.get('positional_rank', 'N/A'):>8}"
        )

    print("\nShrinkage / audit fields stored in factor_breakdown")
    for row in diagnostic_rows:
        fb = row["factor_breakdown"]
        print(f"\n  {row['name']}:")
        for key in QB_FACTOR_AUDIT_KEYS:
            print(f"    {key}: {fb.get(key)}")
        print(f"    projected_points: {fb.get('projected_points')}")
        print(f"    computed_at: {row.get('computed_at')}")

    return diagnostic_rows


def run_shrinkage_calibration_review(
    client=None,
    strengths: dict[str, float] | None = None,
    spotlight_names: tuple[str, ...] = CALIBRATION_SPOTLIGHT_NAMES,
) -> None:
    """RB shrinkage sweep — read-only, no edge_scores writes."""
    if client is None:
        client = get_supabase_client()
    if strengths is None:
        strengths = CALIBRATION_SHRINKAGE_STRENGTHS

    print("Loading draft pool + 2025 season data (one-time fetch)...")
    context = load_draft_edge_context(client)
    print(f"Loaded {len(context.players)} players.\n")

    for label, strength in strengths.items():
        print(f"\n{'=' * 72}")
        print(f"SHRINKAGE_STRENGTH = {strength} ({label})")
        print("=" * 72)

        players, all_projections, _ = compute_draft_edge_projections(
            shrinkage_strength=strength,
            context=context,
        )
        ranked_rbs = rank_players_at_position(players, all_projections, "RB")

        print("\nRB1–40 (placeholder = no_historical_data):")
        print(f"{'Rank':>4}  {'Name':<28} {'Pts':>8}  {'Flags'}")
        print("-" * 72)
        for rank, player, proj in ranked_rbs[:40]:
            flag_str = ", ".join(projection_feature_flags(proj["features"])) or "-"
            print(f"{rank:>4}  {player['full_name']:<28} {proj['points']:>8.2f}  {flag_str}")

        slope = rb10_25_points_slope(ranked_rbs)
        print(f"\nRB10–25 avg drop per rank: {slope:.2f} pts")

        print("\nSpotlight players:")
        for name in spotlight_names:
            player = find_player_by_name(players, name)
            if not player:
                print(f"  {name}: not found in draft pool")
                continue
            proj = all_projections[player["player_id"]]
            pos = player["position"]
            if pos == "RB":
                rank = next(r for r, p, _ in ranked_rbs if p["player_id"] == player["player_id"])
                rank_label = f"RB#{rank}"
            else:
                pos_ranked = rank_players_at_position(players, all_projections, pos)
                rank = next(r for r, p, _ in pos_ranked if p["player_id"] == player["player_id"])
                rank_label = f"{pos}#{rank}"
            flags = projection_feature_flags(proj["features"])
            print(f"  {name}: {rank_label}, {proj['points']:.2f} pts [{', '.join(flags) or 'none'}]")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Draft Edge scoring pipeline")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--review",
        action="store_true",
        help="RB shrinkage calibration sweep (read-only, no edge_scores writes)",
    )
    mode.add_argument(
        "--qb-diag",
        action="store_true",
        help="Four-QB shrinkage diagnostic (read-only, no edge_scores writes)",
    )
    args = parser.parse_args()

    if args.review:
        run_shrinkage_calibration_review()
    elif args.qb_diag:
        print_qb_shrinkage_diagnostic()
    else:
        compute_and_write_draft_edge()
