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

from dataclasses import dataclass
from datetime import datetime, timezone

from config.supabase_client import get_supabase_client
from scoring.season_stats import (
    MIN_SEASON_GAMES,
    aggregate_skill_season_totals,
    build_position_role_baselines,
    collect_raw_observed_stats,
    compute_qb_rush_td_per_game_prior,
    fetch_player_season_games,
    fetch_season_games_by_player,
    fetch_team_season_totals,
    primary_team_id,
)
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
    baselines[("QB", None, "rush_tds_per_game_prior")] = compute_qb_rush_td_per_game_prior(
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


def compute_and_write_draft_edge() -> None:
    client = get_supabase_client()

    players, all_projections, adp_by_player = compute_draft_edge_projections()

    print(f"Draft pool: {len(players)} players across {DRAFT_POSITIONS}.")
    real_count = sum(
        1 for proj in all_projections.values()
        if not proj["features"].get("no_historical_data")
    )
    print(f"Real (2025-history-based) projections: {real_count}. No-historical-data placeholders needed: {len(players) - real_count}.")
    by_position: dict[str, list[str]] = {}
    for pid, proj in all_projections.items():
        by_position.setdefault(proj["position"], []).append(pid)

    rows_to_upsert = []
    computed_at = datetime.now(timezone.utc).isoformat()

    for position, pids in by_position.items():
        sorted_by_points = sorted(pids, key=lambda pid: all_projections[pid]["points"], reverse=True)
        n = len(sorted_by_points)

        # Scarcity gaps use REAL projections only — ADP placeholders (no_historical_data)
        # sit out of the chain so interpolated mid-tier points don't inject artificial
        # near-zero gaps between their ranked neighbors. Placeholders get null scarcity.
        real_sorted = [
            pid for pid in sorted_by_points
            if not all_projections[pid]["features"].get("no_historical_data")
        ]
        real_points = [all_projections[pid]["points"] for pid in real_sorted]
        real_scarcity_by_pid: dict[str, float | None] = {}
        for idx, pid in enumerate(real_sorted):
            real_scarcity_by_pid[pid] = (
                real_points[idx] - real_points[idx + 1] if idx + 1 < len(real_sorted) else 0.0
            )

        # ADP positional rank -- only among players who actually have an ADP.
        pids_with_adp = [pid for pid in pids if adp_by_player.get(pid) is not None]
        pids_with_adp.sort(key=lambda pid: adp_by_player[pid])
        adp_rank_by_pid = {pid: rank + 1 for rank, pid in enumerate(pids_with_adp)}

        for rank, pid in enumerate(sorted_by_points):
            positional_rank = rank + 1
            is_placeholder = all_projections[pid]["features"].get("no_historical_data")
            scarcity = None if is_placeholder else real_scarcity_by_pid.get(pid)

            adp_positional_rank = adp_rank_by_pid.get(pid)
            adp_value_gap = (
                positional_rank - adp_positional_rank if adp_positional_rank is not None else None
            )

            proj = all_projections[pid]
            factor_breakdown = {
                **proj["features"],
                "projected_points": round(proj["points"], 2),
                "scarcity_gap_to_next_rank": round(scarcity, 2) if scarcity is not None else None,
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
