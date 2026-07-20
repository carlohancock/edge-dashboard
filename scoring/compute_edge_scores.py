"""
Orchestration layer for the Edge scoring engine.
Pulls a player's recent stat history + upcoming game context from Supabase,
routes to the correct position's feature builder + points calculator,
computes percentile-rank Edge scores within each position group, and writes
results to edge_scores with factor_breakdown populated.

NOTE: player_game_stats is currently empty (season hasn't started / no
backfill yet). The JSONB stat key names assumed below (pass_attempts,
rush_yards, etc.) are placeholders based on common naming conventions and
WILL need verification/adjustment once real data is loaded via the Phase 3.6
backfill. This file is untested end-to-end until that data exists.
"""

from datetime import datetime, timezone, timedelta

from config.supabase_client import get_supabase_client
from scoring.vegas_features import get_game_row, team_implied_total, team_spread
from scoring.qb_features import build_qb_features
from scoring.rb_features import build_rb_features
from scoring.wr_te_features import build_wr_te_features
from scoring.kicker_features import build_kicker_features
from scoring.dst_features import build_dst_features
from scoring.points_calculator import (
    calculate_qb_points, calculate_rb_points, calculate_wr_te_points,
    calculate_kicker_points, calculate_dst_points,
)

NUM_HISTORY_GAMES = 5
LEAGUE_AVG_IMPLIED_TOTAL = 22.0


def _get_upcoming_game(client, team_id: str) -> dict | None:
    """Nearest future game for a team, either as home or away."""
    now = datetime.now(timezone.utc).isoformat()
    result = (
        client.table("games")
        .select("*")
        .eq("sport", "nfl")
        .gte("game_time", now)
        .or_(f"home_team_id.eq.{team_id},away_team_id.eq.{team_id}")
        .order("game_time")
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def _get_stat_history(client, player_id: str, keys: list[str], n: int = NUM_HISTORY_GAMES) -> dict:
    """
    Fetch the player's last n games' stats (JSONB), oldest-first, and extract
    the requested keys into parallel lists. Missing keys default to 0.
    """
    result = (
        client.table("player_game_stats")
        .select("stats, game_id, games!inner(game_time)")
        .eq("player_id", player_id)
        .order("games(game_time)", desc=True)
        .limit(n)
        .execute()
    )
    rows = list(reversed(result.data or []))  # oldest-first for EWMA

    history = {key: [] for key in keys}
    for row in rows:
        stats = row.get("stats") or {}
        for key in keys:
            history[key].append(stats.get(key, 0))
    return history


def compute_player_projection(client, player: dict) -> dict | None:
    """
    Routes a player to the correct feature builder + points calculator based
    on position. Returns {"points": float, "features": dict} or None if the
    player has no upcoming game or insufficient data.
    """
    position = player["position"]
    team_id = player["team_id"]
    if not team_id:
        return None

    game = _get_upcoming_game(client, team_id)
    if not game:
        return None

    is_home = game["home_team_id"] == team_id
    opponent_id = game["away_team_id"] if is_home else game["home_team_id"]
    spread = team_spread(game, team_id)
    if spread is None:
        return None

    if position == "QB":
        hist = _get_stat_history(client, player["player_id"], [
            "pass_attempts", "pass_yards", "pass_td", "pass_int",
            "rush_attempts", "rush_yards",
        ])
        features = build_qb_features(
            hist["pass_attempts"], hist["pass_yards"], hist["pass_td"], hist["pass_int"],
            hist["rush_attempts"], hist["rush_yards"], spread,
        )
        points = calculate_qb_points(features)

    elif position == "RB":
        hist = _get_stat_history(client, player["player_id"], [
            "rush_share", "target_share", "rush_yards", "rush_attempts",
            "rec_yards", "receptions", "targets", "rush_td", "rec_td",
        ])
        # Team-level baselines: v1 simplification — use league-average team
        # volume constants until a dedicated team-level stats aggregator exists.
        features = build_rb_features(
            hist["rush_share"], hist["target_share"], 26.0, 34.0,
            hist["rush_yards"], hist["rush_attempts"], hist["rec_yards"],
            hist["receptions"], hist["targets"], hist["rush_td"], hist["rec_td"],
            spread,
        )
        points = calculate_rb_points(features)

    elif position in ("WR", "TE"):
        hist = _get_stat_history(client, player["player_id"], [
            "target_share", "targets", "receptions", "rec_yards", "rec_td", "adot",
        ])
        features = build_wr_te_features(
            hist["target_share"], 34.0, hist["targets"], hist["receptions"],
            hist["rec_yards"], hist["rec_td"], hist["adot"], spread,
        )
        points = calculate_wr_te_points(features)

    elif position == "K":
        hist = _get_stat_history(client, player["player_id"], [
            "fg_attempts", "fg_made", "pat_attempts",
        ])
        implied = team_implied_total(game, team_id)
        features = build_kicker_features(
            hist["fg_attempts"], hist["fg_made"], hist["pat_attempts"],
            implied, LEAGUE_AVG_IMPLIED_TOTAL,
        )
        points = calculate_kicker_points(features)

    elif position == "DEF":
        opp_implied = team_implied_total(game, opponent_id)
        hist = _get_stat_history(client, player["player_id"], [
            "sack_rate", "opponent_sack_rate_allowed", "opponent_turnovers", "opponent_plays",
        ])
        features = build_dst_features(
            opp_implied, hist["sack_rate"], hist["opponent_sack_rate_allowed"],
            hist["opponent_turnovers"], hist["opponent_plays"], 340.0,
        )
        points = calculate_dst_points(features)

    else:
        return None

    return {"points": points, "features": features, "game_id": game["game_id"]}


def compute_and_write_edge_scores(period: str = "week1") -> None:
    client = get_supabase_client()

    players_result = client.table("players").select("*").eq("sport", "nfl").execute()
    players = players_result.data or []

    # Compute raw points for every player first (needed for percentile ranking)
    projections = {}  # player_id -> {points, features, game_id, position}
    for player in players:
        proj = compute_player_projection(client, player)
        if proj:
            proj["position"] = player["position"]
            projections[player["player_id"]] = proj

    # Percentile rank within each position group
    by_position: dict[str, list[str]] = {}
    for pid, proj in projections.items():
        by_position.setdefault(proj["position"], []).append(pid)

    rows_to_upsert = []
    for position, pids in by_position.items():
        sorted_pids = sorted(pids, key=lambda pid: projections[pid]["points"])
        n = len(sorted_pids)
        for rank, pid in enumerate(sorted_pids):
            percentile = (rank / (n - 1) * 100) if n > 1 else 100.0
            proj = projections[pid]
            rows_to_upsert.append({
                "player_id": pid,
                "score_type": "edge",
                "period": period,
                "score_value": round(percentile, 2),
                "positional_rank": n - rank,  # highest points = rank 1
                "factor_breakdown": proj["features"],
            })

    if rows_to_upsert:
        client.table("edge_scores").upsert(
            rows_to_upsert, on_conflict="player_id,score_type,period"
        ).execute()

    print(f"Computed and wrote {len(rows_to_upsert)} edge_scores rows for period={period}.")


if __name__ == "__main__":
    compute_and_write_edge_scores()