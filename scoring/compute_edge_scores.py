"""
Orchestration layer for the Edge scoring engine.
Pulls a player's recent stat history + upcoming game context from Supabase,
routes to the correct position's feature builder + points calculator,
computes percentile-rank Edge scores within each position group, and writes
results to edge_scores with factor_breakdown populated.

Stat key names below were reconciled against the REAL nflverse keys written
by pipeline/backfill_player_game_stats_2025.py (Step 3) -- verified directly
against `player_game_stats.stats` in the database, not guessed. Two stats
that don't exist as raw nflverse columns (`rush_share`, `adot`) are derived
here instead:
  - rush_share = this player's carries ÷ their team's total carries that
    game. Team totals are read from the TEAM's own DEF row for the same
    game_id, since a DEF row is that team's full box score (see gotcha #4
    in PROJECT_LOG.md / dst_features.py's docstring).
  - adot = receiving_air_yards ÷ targets, per game (no team lookup needed).
The same "DEF row = team box score" trick also replaces the old hardcoded
26.0/34.0 team-volume placeholders with real EWMA'd team baselines.
"""

from datetime import datetime, timezone, timedelta

from config.supabase_client import get_supabase_client
from scoring.stats_utils import ewma
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

# Fallbacks used only when a team has no DEF-row history yet (e.g. week 1).
DEFAULT_TEAM_RUSH_ATTEMPTS_BASELINE = 26.0
DEFAULT_TEAM_PASS_ATTEMPTS_BASELINE = 34.0


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


def _get_game_for_period(client, team_id: str, season: int, week: int) -> dict | None:
    """Specific game for a team in a given season/week — used for historical/backtest scoring."""
    result = (
        client.table("games")
        .select("*")
        .eq("sport", "nfl")
        .eq("season", season)
        .eq("week_or_date", str(week))
        .or_(f"home_team_id.eq.{team_id},away_team_id.eq.{team_id}")
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def _get_stat_history(
    client, player_id: str, keys: list[str], n: int = NUM_HISTORY_GAMES
) -> tuple[dict, list[str]]:
    """
    Fetch the player's last n games' stats (JSONB), oldest-first, and extract
    the requested keys into parallel lists. Missing/null keys default to 0.
    Also returns the aligned list of game_ids (oldest-first), so callers can
    look up the SAME games' team-level context elsewhere (see
    _get_stats_by_game_id below).
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

    game_ids = [row["game_id"] for row in rows]
    history = {key: [] for key in keys}
    for row in rows:
        stats = row.get("stats") or {}
        for key in keys:
            # `.get(key) or 0` (not `.get(key, 0)`) because several real
            # nflverse keys are present but explicitly null (e.g. fg_pct with
            # no attempts) -- both "missing" and "present-but-null" should
            # default to 0.
            history[key].append(stats.get(key) or 0)
    return history, game_ids


def _get_stats_by_game_id(
    client, player_id: str | None, game_ids: list[str], key: str
) -> dict[str, float]:
    """A single stat key for player_id, keyed by game_id, for a specific set of games."""
    if not player_id or not game_ids:
        return {}
    result = (
        client.table("player_game_stats")
        .select("game_id, stats")
        .eq("player_id", player_id)
        .in_("game_id", game_ids)
        .execute()
    )
    return {row["game_id"]: (row.get("stats") or {}).get(key) or 0 for row in (result.data or [])}


def _get_def_player_by_team(client) -> dict[str, str]:
    """team_id -> DEF player_id, fetched once and reused across all players."""
    result = (
        client.table("players")
        .select("player_id, team_id")
        .eq("sport", "nfl")
        .eq("position", "DEF")
        .execute()
    )
    return {row["team_id"]: row["player_id"] for row in (result.data or []) if row.get("team_id")}


def _get_team_offense_baseline(
    client, def_player_id: str | None, n: int = NUM_HISTORY_GAMES, half_life: float = 2.5
) -> dict[str, float]:
    """
    EWMA'd team-level rush/pass attempt volume, read from the team's own DEF
    row history (which is that team's own full box score -- carries/attempts
    there are the TEAM's totals, not any individual defender's).
    """
    if not def_player_id:
        return {
            "carries": DEFAULT_TEAM_RUSH_ATTEMPTS_BASELINE,
            "attempts": DEFAULT_TEAM_PASS_ATTEMPTS_BASELINE,
        }
    hist, _ = _get_stat_history(client, def_player_id, ["carries", "attempts"], n)
    carries = ewma(hist["carries"], half_life) if hist["carries"] else DEFAULT_TEAM_RUSH_ATTEMPTS_BASELINE
    attempts = ewma(hist["attempts"], half_life) if hist["attempts"] else DEFAULT_TEAM_PASS_ATTEMPTS_BASELINE
    return {"carries": carries, "attempts": attempts}


def compute_player_projection(
    client,
    player: dict,
    def_player_by_team: dict[str, str],
    season: int | None = None,
    week: int | None = None,
) -> dict | None:
    """
    Routes a player to the correct feature builder + points calculator based
    on position. Returns {"points": float, "features": dict} or None if the
    player has no target game or insufficient data.

    By default (season and week both None) this projects the player's next
    real upcoming game (live use). Pass both season and week to instead
    target a specific historical game (backtest/historical use) — the
    `period` string elsewhere is a display label only and never controls
    which game gets selected.
    """
    position = player["position"]
    team_id = player["team_id"]
    if not team_id:
        return None

    if season is not None and week is not None:
        game = _get_game_for_period(client, team_id, season, week)
    else:
        game = _get_upcoming_game(client, team_id)
    if not game:
        return None

    is_home = game["home_team_id"] == team_id
    opponent_id = game["away_team_id"] if is_home else game["home_team_id"]
    spread = team_spread(game, team_id)
    if spread is None:
        return None

    if position == "QB":
        hist, _ = _get_stat_history(client, player["player_id"], [
            "attempts", "passing_yards", "passing_tds", "passing_interceptions",
            "carries", "rushing_yards",
        ])
        features = build_qb_features(
            hist["attempts"], hist["passing_yards"], hist["passing_tds"], hist["passing_interceptions"],
            hist["carries"], hist["rushing_yards"], spread,
        )
        points = calculate_qb_points(features)

    elif position == "RB":
        hist, game_ids = _get_stat_history(client, player["player_id"], [
            "carries", "rushing_yards", "receiving_yards", "receptions", "targets",
            "rushing_tds", "receiving_tds", "target_share",
        ])

        def_player_id = def_player_by_team.get(team_id)
        team_carries_by_game = _get_stats_by_game_id(client, def_player_id, game_ids, "carries")
        rush_share_history = [
            (hist["carries"][i] / team_carries_by_game[gid]) if team_carries_by_game.get(gid) else 0.0
            for i, gid in enumerate(game_ids)
        ]
        team_baseline = _get_team_offense_baseline(client, def_player_id)

        features = build_rb_features(
            rush_share_history, hist["target_share"],
            team_baseline["carries"], team_baseline["attempts"],
            hist["rushing_yards"], hist["carries"], hist["receiving_yards"],
            hist["receptions"], hist["targets"], hist["rushing_tds"], hist["receiving_tds"],
            spread,
        )
        points = calculate_rb_points(features)

    elif position in ("WR", "TE"):
        hist, _ = _get_stat_history(client, player["player_id"], [
            "target_share", "targets", "receptions", "receiving_yards",
            "receiving_tds", "receiving_air_yards",
        ])
        adot_history = [
            (hist["receiving_air_yards"][i] / hist["targets"][i]) if hist["targets"][i] else 0.0
            for i in range(len(hist["targets"]))
        ]

        def_player_id = def_player_by_team.get(team_id)
        team_baseline = _get_team_offense_baseline(client, def_player_id)

        features = build_wr_te_features(
            hist["target_share"], team_baseline["attempts"], hist["targets"], hist["receptions"],
            hist["receiving_yards"], hist["receiving_tds"], adot_history, spread,
        )
        points = calculate_wr_te_points(features)

    elif position == "K":
        hist, _ = _get_stat_history(client, player["player_id"], ["fg_att", "fg_made", "pat_att"])
        implied = team_implied_total(game, team_id)
        features = build_kicker_features(
            hist["fg_att"], hist["fg_made"], hist["pat_att"],
            implied, LEAGUE_AVG_IMPLIED_TOTAL,
        )
        points = calculate_kicker_points(features)

    elif position == "DEF":
        opp_implied = team_implied_total(game, opponent_id)

        own_hist, _ = _get_stat_history(client, player["player_id"], [
            "def_sacks", "def_interceptions", "fumble_recovery_opp",
            "def_fumbles_forced", "def_tds", "special_teams_tds", "def_safeties",
        ])
        own_takeaways_history = [
            own_hist["def_interceptions"][i] + own_hist["fumble_recovery_opp"][i]
            for i in range(len(own_hist["def_interceptions"]))
        ]
        own_def_st_tds_history = [
            own_hist["def_tds"][i] + own_hist["special_teams_tds"][i]
            for i in range(len(own_hist["def_tds"]))
        ]

        opponent_def_player_id = def_player_by_team.get(opponent_id)
        if opponent_def_player_id:
            opp_hist, _ = _get_stat_history(client, opponent_def_player_id, [
                "sacks_suffered", "passing_interceptions", "fumbles_lost_total",
                "passing_yards", "rushing_yards",
            ])
            opp_sacks_suffered_history = opp_hist["sacks_suffered"]
            opp_giveaways_history = [
                opp_hist["passing_interceptions"][i] + opp_hist["fumbles_lost_total"][i]
                for i in range(len(opp_hist["passing_interceptions"]))
            ]
            opp_yards_history = [
                opp_hist["passing_yards"][i] + opp_hist["rushing_yards"][i]
                for i in range(len(opp_hist["passing_yards"]))
            ]
        else:
            opp_sacks_suffered_history, opp_giveaways_history, opp_yards_history = [], [], []

        features = build_dst_features(
            opp_implied,
            own_hist["def_sacks"], own_takeaways_history, own_hist["def_fumbles_forced"],
            own_def_st_tds_history, own_hist["def_safeties"],
            opp_sacks_suffered_history, opp_giveaways_history, opp_yards_history,
        )
        points = calculate_dst_points(features)

    else:
        return None

    return {"points": points, "features": features, "game_id": game["game_id"]}


def compute_and_write_edge_scores(
    period: str = "week1",
    season: int | None = None,
    week: int | None = None,
) -> None:
    """
    `period` is a display label only (written to edge_scores.period) — it does
    NOT control which games get selected. Pass `season`/`week` together to
    target a specific historical week (backtest/historical use); leave both
    None to project each player's next real upcoming game (live use, default).
    """
    client = get_supabase_client()

    players_result = client.table("players").select("*").eq("sport", "nfl").execute()
    players = players_result.data or []

    def_player_by_team = _get_def_player_by_team(client)

    # Compute raw points for every player first (needed for percentile ranking)
    projections = {}  # player_id -> {points, features, game_id, position}
    for player in players:
        proj = compute_player_projection(client, player, def_player_by_team, season=season, week=week)
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
