"""
Draft Edge orchestration layer.

Season-long positional rankings for the upcoming draft, built from the SAME
opportunity/efficiency/shrinkage engine as weekly Edge (season_stats.py +
draft_edge_features.py reuse scoring/stats_utils.py's regressed_rate, and
points_calculator.py's existing calculate_*_points functions), but
re-weighted for season-long context per edge_formula_nfl.md: no per-week
matchup factor (there's no specific week to matchup against), scarcity and
ADP value gap instead.

Deliberately separate from compute_edge_scores.py -- this is NOT a trailing
EWMA over recent games (there are no 2026 games yet to trail). It projects
each player's 2026 role from their 2025 SEASON totals, adjusted by their
CURRENT depth-chart context. Does not import or call `_get_upcoming_game`
/ `_get_game_for_period` -- there is no game being targeted here.

Scope (see PROJECT_LOG.md Phase 5): QB/RB/WR/TE/K only. DST intentionally
excluded -- out of scope per this session's task boundaries (dst_features.py/
points_calculator.py's DST logic is untouched). vegas_features.py, weekly
Edge, and Wire Edge are also untouched; no 2026 odds data exists this far
before the season, so there's no market-blend feature to add here yet
(expected, not a gap -- see edge_formula_nfl.md's Vegas-features section,
which only ever applied at the per-game level anyway).

Writes to `edge_scores` with score_type='draft_edge'. Per the schema's
Draft Edge column comment, this is RANK-based, not a /100 score:
`score_value` is left null and `positional_rank` is populated. Reuses the
same UNIQUE(player_id, score_type, period) upsert path added in Phase 4.7
-- no schema change needed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from config.supabase_client import get_supabase_client
from scoring.season_stats import aggregate_skill_season_totals, fetch_player_season_games, fetch_team_season_totals, primary_team_id
from scoring.draft_edge_features import (
    build_draft_kicker_features,
    build_draft_qb_features,
    build_draft_rb_features,
    build_draft_wr_te_features,
)
from scoring.points_calculator import (
    calculate_kicker_points,
    calculate_qb_points,
    calculate_rb_points,
    calculate_wr_te_points,
)

SEASON = 2026            # the season being projected/drafted for
BASE_SEASON = 2025       # the season the projection is BUILT FROM
DRAFT_POSITIONS = ("QB", "RB", "WR", "TE", "K")
PERIOD = f"{SEASON}-draftedge"

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


def _team_season_totals_by_team(client, def_player_by_team: dict[str, str]) -> dict[str, dict]:
    """2025 season rush/pass attempt totals for every team, keyed by team_id (used as the shared RB/WR/TE volume baseline)."""
    totals: dict[str, dict] = {}
    for team_id, def_player_id in def_player_by_team.items():
        totals[team_id] = fetch_team_season_totals(client, def_player_id, BASE_SEASON)
    return totals


def _empty_team_totals() -> dict:
    return {"carries": None, "attempts": None, "games_played": 0}


def compute_player_draft_projection(
    client,
    player: dict,
    team_totals_by_team: dict[str, dict],
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

    rows = fetch_player_season_games(client, player["player_id"], BASE_SEASON)
    season_totals = aggregate_skill_season_totals(rows)
    if season_totals["games_played"] == 0:
        return None

    primary_2025_team_id = primary_team_id(season_totals["team_ids_seen"])
    context_changed = bool(primary_2025_team_id and current_team_id and primary_2025_team_id != current_team_id)

    if position == "QB":
        features = build_draft_qb_features(season_totals, depth_chart_rank, context_changed)
        points = calculate_qb_points(features)

    elif position == "RB":
        current_team_totals = team_totals_by_team.get(current_team_id, _empty_team_totals())
        share_team_totals = team_totals_by_team.get(primary_2025_team_id, _empty_team_totals())
        features = build_draft_rb_features(
            season_totals, current_team_totals, share_team_totals, depth_chart_rank, context_changed,
        )
        points = calculate_rb_points(features)

    elif position in ("WR", "TE"):
        current_team_totals = team_totals_by_team.get(current_team_id, _empty_team_totals())
        share_team_totals = team_totals_by_team.get(primary_2025_team_id, _empty_team_totals())
        features = build_draft_wr_te_features(
            season_totals, current_team_totals, share_team_totals, depth_chart_rank, context_changed, position,
        )
        points = calculate_wr_te_points(features)

    elif position == "K":
        features = build_draft_kicker_features(season_totals, depth_chart_rank, context_changed)
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


def compute_and_write_draft_edge() -> None:
    client = get_supabase_client()

    players = _fetch_draft_pool(client)
    def_player_by_team = _get_def_player_by_team(client)
    team_totals_by_team = _team_season_totals_by_team(client, def_player_by_team)
    adp_by_player = _fetch_adp_by_player(client)

    print(f"Draft pool: {len(players)} players across {DRAFT_POSITIONS}.")

    # Pass 1: real projections for anyone with 2025 history; collect the rest for the placeholder pass.
    real_by_pid: dict[str, dict] = {}
    placeholder_pids: list[str] = []
    for player in players:
        proj = compute_player_draft_projection(client, player, team_totals_by_team)
        if proj is None:
            placeholder_pids.append(player["player_id"])
        else:
            real_by_pid[player["player_id"]] = {**proj, "position": player["position"]}

    print(f"Real (2025-history-based) projections: {len(real_by_pid)}. No-historical-data placeholders needed: {len(placeholder_pids)}.")

    # Pass 2: ADP-anchored placeholders (Task 2's rookie/no-data handling + Task 3's ADP use).
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

    # Pass 3: per-position rank, scarcity (Task 4a), ADP value gap (Task 4b).
    by_position: dict[str, list[str]] = {}
    for pid, proj in all_projections.items():
        by_position.setdefault(proj["position"], []).append(pid)

    rows_to_upsert = []
    computed_at = datetime.now(timezone.utc).isoformat()

    for position, pids in by_position.items():
        sorted_by_points = sorted(pids, key=lambda pid: all_projections[pid]["points"], reverse=True)
        n = len(sorted_by_points)
        points_list = [all_projections[pid]["points"] for pid in sorted_by_points]

        # ADP positional rank -- only among players who actually have an ADP.
        pids_with_adp = [pid for pid in pids if adp_by_player.get(pid) is not None]
        pids_with_adp.sort(key=lambda pid: adp_by_player[pid])
        adp_rank_by_pid = {pid: rank + 1 for rank, pid in enumerate(pids_with_adp)}

        for rank, pid in enumerate(sorted_by_points):
            positional_rank = rank + 1
            # Scarcity: point gap to the next-ranked player at this position (Task 4a: "simple
            # version is fine for v1" -- deliberately not modeling anything beyond this one gap).
            scarcity = points_list[rank] - points_list[rank + 1] if rank + 1 < n else 0.0

            adp_positional_rank = adp_rank_by_pid.get(pid)
            adp_value_gap = (
                positional_rank - adp_positional_rank if adp_positional_rank is not None else None
            )

            proj = all_projections[pid]
            factor_breakdown = {
                **proj["features"],
                "projected_points": round(proj["points"], 2),
                "scarcity_gap_to_next_rank": round(scarcity, 2),
                "adp_value": adp_by_player.get(pid),
                "adp_positional_rank": adp_positional_rank,
                # your_projected_positional_rank - adp_positional_rank (edge_formula_nfl.md /
                # Task 4b). Positive: the market ranks this player BETTER than the model does
                # (potential fade/overvalued-by-ADP). Negative: the model likes this player MORE
                # than the market does (potential value/sleeper). Null: no ADP data for this player.
                "adp_value_gap": adp_value_gap,
            }

            rows_to_upsert.append({
                "player_id": pid,
                "score_type": "draft_edge",
                "period": PERIOD,
                "score_value": None,
                "positional_rank": positional_rank,
                "factor_breakdown": factor_breakdown,
                "computed_at": computed_at,
            })

    if rows_to_upsert:
        client.table("edge_scores").upsert(
            rows_to_upsert, on_conflict="player_id,score_type,period"
        ).execute()

    print(f"Computed and wrote {len(rows_to_upsert)} draft_edge rows for period={PERIOD}.")


if __name__ == "__main__":
    compute_and_write_draft_edge()
