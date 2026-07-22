"""
QB shrinkage diagnostic — read-only, no edge_scores writes.

Side-by-side comparison of 2025 observed season totals vs stored Draft Edge
projections (factor_breakdown) for four spotlight QBs, plus projected/observed
ratios for pass attempts, rush yards, and rush TDs.
"""

from __future__ import annotations

from config.supabase_client import get_supabase_client
from scoring.points_calculator import calculate_qb_points
from scoring.season_stats import aggregate_skill_season_totals, fetch_player_season_games

BASE_SEASON = 2025
DRAFT_EDGE_PERIOD = "2026-draftedge"
DRAFT_EDGE_SCORE_TYPE = "draft_edge"

SPOTLIGHT_QBS = (
    "Lamar Jackson",
    "Josh Allen",
    "Matthew Stafford",
    "Jared Goff",
)

SHRINKAGE_KEYS = (
    "season_2025_games_played",
    "season_2025_attempts",
    "season_2025_ypa",
    "regressed_td_rate",
    "regressed_int_rate",
    "regressed_rush_tds_per_game",
    "qb_rush_td_per_game_prior",
    "proj_games_2026",
    "depth_chart_rank",
    "context_changed",
    "low_sample",
    "no_historical_data",
)


def _find_by_name(players: list[dict], name: str) -> dict | None:
    for player in players:
        if player["full_name"] == name:
            return player
    return None


def _ratio(numerator: float | None, denominator: float) -> str:
    if numerator is None:
        return "N/A"
    if denominator == 0:
        return "inf" if numerator else "N/A"
    return f"{numerator / denominator:.3f}"


def _fetch_players_by_name(client, names: tuple[str, ...]) -> dict[str, dict]:
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


def _fetch_draft_edge_breakdowns(client, player_ids: list[str]) -> dict[str, dict]:
    result = (
        client.table("edge_scores")
        .select("player_id, factor_breakdown, positional_rank, computed_at")
        .eq("score_type", DRAFT_EDGE_SCORE_TYPE)
        .eq("period", DRAFT_EDGE_PERIOD)
        .in_("player_id", player_ids)
        .execute()
    )
    by_player = {row["player_id"]: row for row in (result.data or [])}
    missing = [pid for pid in player_ids if pid not in by_player]
    if missing:
        raise RuntimeError(
            f"No draft_edge row for period={DRAFT_EDGE_PERIOD}: {', '.join(missing)}"
        )
    return by_player


def _observed_qb_points(season_totals: dict) -> float:
    return calculate_qb_points(
        {
            "proj_pass_yards": season_totals["passing_yards"],
            "proj_pass_tds": season_totals["passing_tds"],
            "proj_pass_ints": season_totals["passing_interceptions"],
            "proj_rush_yards": season_totals["rushing_yards"],
            "proj_rush_tds": season_totals["rushing_tds"],
        }
    )


def run_diagnostic() -> None:
    client = get_supabase_client()
    players_by_name = _fetch_players_by_name(client, SPOTLIGHT_QBS)
    player_ids = [players_by_name[name]["player_id"] for name in SPOTLIGHT_QBS]
    edge_rows = _fetch_draft_edge_breakdowns(client, player_ids)

    print(f"QB shrinkage diagnostic — {BASE_SEASON} observed vs Draft Edge ({DRAFT_EDGE_PERIOD})")
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

    for name in SPOTLIGHT_QBS:
        player = players_by_name[name]
        player_id = player["player_id"]
        rows = fetch_player_season_games(client, player_id, BASE_SEASON)
        totals = aggregate_skill_season_totals(rows)
        fb = edge_rows[player_id]["factor_breakdown"]

        obs_att = totals["attempts"]
        obs_rush_yards = totals["rushing_yards"]
        obs_rush_tds = totals["rushing_tds"]
        obs_fp = _observed_qb_points(totals)

        proj_att = fb.get("proj_pass_attempts")
        proj_rush_yards = fb.get("proj_rush_yards")
        proj_rush_tds = fb.get("proj_rush_tds")
        proj_fp = fb.get("projected_points")

        print(
            f"{name:<18} "
            f"{obs_att:>8.0f} {proj_att:>8.1f} {_ratio(proj_att, obs_att):>9} "
            f"{obs_rush_yards:>9.0f} {proj_rush_yards:>10.1f} {_ratio(proj_rush_yards, obs_rush_yards):>11} "
            f"{obs_rush_tds:>10.0f} "
            f"{proj_rush_tds if proj_rush_tds is not None else 'N/A':>11} "
            f"{_ratio(proj_rush_tds, obs_rush_tds):>12} "
            f"{obs_fp:>8.1f} {proj_fp if proj_fp is not None else 'N/A':>8} "
            f"{totals['games_played']:>4.0f}"
        )

    print("\n2025 observed season totals (player_game_stats, Phase 4.8 team_id backfill)")
    obs_header = (
        f"{'Name':<18} {'Pass Att':>8} {'Pass Yds':>9} {'Pass TD':>7} "
        f"{'Rush Att':>8} {'Rush Yds':>8} {'Rush TD':>7} {'GP':>4}"
    )
    print(obs_header)
    print("-" * len(obs_header))
    for name in SPOTLIGHT_QBS:
        player_id = players_by_name[name]["player_id"]
        rows = fetch_player_season_games(client, player_id, BASE_SEASON)
        totals = aggregate_skill_season_totals(rows)
        print(
            f"{name:<18} "
            f"{totals['attempts']:>8.0f} {totals['passing_yards']:>9.0f} {totals['passing_tds']:>7.0f} "
            f"{totals['carries']:>8.0f} {totals['rushing_yards']:>8.0f} {totals['rushing_tds']:>7.0f} "
            f"{totals['games_played']:>4.0f}"
        )

    print(f"\nDraft Edge factor_breakdown ({DRAFT_EDGE_PERIOD}) — projections + shrinkage intermediates")
    proj_header = (
        f"{'Name':<18} {'Proj Att':>8} {'Proj Rush Att':>13} {'Proj Rush Yds':>13} "
        f"{'Proj Rush TDs':>13} {'Proj Pass Yds':>13} {'Proj Pass TDs':>13} "
        f"{'Pos Rank':>8}"
    )
    print(proj_header)
    print("-" * len(proj_header))
    for name in SPOTLIGHT_QBS:
        player_id = players_by_name[name]["player_id"]
        row = edge_rows[player_id]
        fb = row["factor_breakdown"]
        proj_rush_tds = fb.get("proj_rush_tds")
        print(
            f"{name:<18} "
            f"{fb.get('proj_pass_attempts', 0):>8.1f} "
            f"{fb.get('proj_rush_attempts', 0):>13.1f} "
            f"{fb.get('proj_rush_yards', 0):>13.1f} "
            f"{proj_rush_tds if proj_rush_tds is not None else 'N/A':>13} "
            f"{fb.get('proj_pass_yards', 0):>13.1f} "
            f"{fb.get('proj_pass_tds', 0):>13.2f} "
            f"{row.get('positional_rank', 'N/A'):>8}"
        )

    print("\nShrinkage / audit fields stored in factor_breakdown")
    for name in SPOTLIGHT_QBS:
        player_id = players_by_name[name]["player_id"]
        fb = edge_rows[player_id]["factor_breakdown"]
        print(f"\n  {name}:")
        for key in SHRINKAGE_KEYS:
            print(f"    {key}: {fb.get(key)}")
        print(f"    projected_points: {fb.get('projected_points')}")
        print(f"    computed_at: {edge_rows[player_id].get('computed_at')}")


if __name__ == "__main__":
    run_diagnostic()
