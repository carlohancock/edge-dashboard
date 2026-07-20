"""Backfill 2025 player_game_stats from nflverse weekly stats.

WRITES TO THE DATABASE. Idempotent: for any (player_id, game_id) pair that
already exists in player_game_stats, the stats JSONB is left untouched --
only the top-level team_id/opponent_team_id columns are (re-)set via an
UPDATE, resolved the same way as for new inserts.

Two separate paths, matching how nflverse itself splits the data:

1. Skill players (QB/RB/WR/TE/K): nflverse's per-PLAYER weekly stats file.
   nflverse player id -> our player_id via the sleeper_id crosswalk
   (pipeline/nflverse_crosswalk.py). game_id resolved by (team, week,
   season=2025) against `games`.

2. Team defenses (DEF): nflverse's per-TEAM weekly stats file (the same
   stat columns, aggregated to the team level — this bundles the team's
   whole box score, offense and defense, since nflverse doesn't ship a
   separate "defense-only" weekly file). Matched directly by team_id ->
   the DEF player row for that team (players.position == 'DEF'). Same
   game_id resolution as above.

IMPORTANT — data source note: nfl_data_py 0.3.3 (latest on PyPI as of
2026-07) still points at nflverse's OLD "player_stats" release tag, which
stops at the 2024 season (nflverse migrated to "stats_player"/"stats_team"
release tags in mid-2025). This script fetches those new URLs directly via
pandas instead of going through nfl_data_py's import_weekly_data(), which
would 404 for 2025. It's still nflverse's official data, just the current
file locations.

Sizes checked before running (see comments below) — both files are small
(well under 30MB in memory combined), so this is not a memory/scale
concern.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("SSL_CERT_DIR", os.path.dirname(certifi.where()))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.supabase_client import get_supabase_client  # noqa: E402
from pipeline.nflverse_crosswalk import build_sleeper_to_gsis_lookup  # noqa: E402

SEASON_YEAR = 2025
BATCH_SIZE = 200
PAGE_SIZE = 1000

# nflverse's new (2025+) release tags. player_stats_week/stats_team_week are
# both small (~19k rows / ~27MB, and ~570 rows / <1MB respectively as of this
# writing) -- verified before writing this script, no memory concern.
PLAYER_WEEK_STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{season}.parquet"
)
TEAM_WEEK_STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_team/stats_team_week_{season}.parquet"
)

SKILL_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K"})

# Same nflverse quirk as pipeline/seed_games_historical.py: nflverse uses "LA"
# for the Rams where our teams.abbreviation uses "LAR".
NFLVERSE_ABBREVIATION_OVERRIDES: dict[str, str] = {"LA": "LAR"}


def _normalize_nflverse_abbreviation(abbr: str | None) -> str | None:
    if not abbr:
        return abbr
    return NFLVERSE_ABBREVIATION_OVERRIDES.get(abbr, abbr)

# Identity/metadata columns present in nflverse's weekly files that we do NOT
# want mixed into the stats JSONB -- they're redundant with player_id/game_id
# (already relational columns) or are display-only (headshot_url etc.).
SKILL_METADATA_COLUMNS = frozenset(
    {
        "player_id",
        "player_name",
        "player_display_name",
        "headshot_url",
        "position",
        "position_group",
        "season",
        "week",
        "season_type",
        "game_id",
        "team",
        "opponent_team",
    }
)
TEAM_METADATA_COLUMNS = frozenset(
    {"season", "week", "season_type", "game_id", "team", "opponent_team"}
)


def _row_to_json_safe(row: pd.Series, exclude: frozenset[str]) -> dict[str, Any]:
    """Convert a pandas row to plain Python types the Supabase client can send as JSON."""
    result: dict[str, Any] = {}
    for col, val in row.items():
        if col in exclude:
            continue
        if pd.isna(val):
            result[col] = None
        elif isinstance(val, np.integer):
            result[col] = int(val)
        elif isinstance(val, np.floating):
            result[col] = float(val)
        elif isinstance(val, np.bool_):
            result[col] = bool(val)
        else:
            result[col] = val
    return result


def _chunks(items: list[dict], size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _fetch_all_players(client) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = (
            client.table("players")
            .select("player_id, full_name, position, team_id, external_ids")
            .eq("sport", "nfl")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            break
        players.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return players


def _fetch_team_abbreviation_map(client) -> dict[str, str]:
    result = (
        client.table("teams").select("team_id, abbreviation").eq("sport", "nfl").execute()
    )
    return {row["abbreviation"]: row["team_id"] for row in (result.data or [])}


def _fetch_season_games(client, season: int) -> list[dict[str, Any]]:
    games: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = (
            client.table("games")
            .select("game_id, home_team_id, away_team_id, week_or_date")
            .eq("sport", "nfl")
            .eq("season", season)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            break
        games.extend(rows)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return games


def _build_game_by_team_week(games: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """(team_id, week_or_date) -> game_id, for either home or away side."""
    lookup: dict[tuple[str, str], str] = {}
    for g in games:
        lookup[(g["home_team_id"], g["week_or_date"])] = g["game_id"]
        lookup[(g["away_team_id"], g["week_or_date"])] = g["game_id"]
    return lookup


def _fetch_existing_stat_keys(client, game_ids: list[str]) -> set[tuple[str, str]]:
    """Existing (player_id, game_id) pairs already in player_game_stats for these games."""
    existing: set[tuple[str, str]] = set()
    for i in range(0, len(game_ids), BATCH_SIZE):
        chunk_ids = game_ids[i : i + BATCH_SIZE]
        offset = 0
        while True:
            result = (
                client.table("player_game_stats")
                .select("player_id, game_id")
                .in_("game_id", chunk_ids)
                .range(offset, offset + PAGE_SIZE - 1)
                .execute()
            )
            rows = result.data or []
            if not rows:
                break
            for row in rows:
                existing.add((row["player_id"], row["game_id"]))
            if len(rows) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
    return existing


def _build_skill_rows(
    client,
    team_by_abbr: dict[str, str],
    game_by_team_week: dict[tuple[str, str], str],
    existing_keys: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    players = _fetch_all_players(client)
    skill_players = [p for p in players if p.get("position") in SKILL_POSITIONS]

    sleeper_to_gsis = build_sleeper_to_gsis_lookup()

    # gsis_id -> our player record, for skill players with a usable crosswalk match.
    gsis_to_player: dict[str, dict[str, Any]] = {}
    for p in skill_players:
        sleeper_id = (p.get("external_ids") or {}).get("sleeper_id")
        if sleeper_id is None:
            continue
        entry = sleeper_to_gsis.get(str(sleeper_id))
        if entry is None or not entry["gsis_id"]:
            continue
        gsis_id = entry["gsis_id"]
        if gsis_id in gsis_to_player:
            print(
                f"WARNING: gsis_id {gsis_id} maps to multiple players "
                f"({gsis_to_player[gsis_id]['full_name']!r} and {p['full_name']!r}); "
                "keeping the first."
            )
            continue
        gsis_to_player[gsis_id] = p

    print(
        f"Skill players in `players`: {len(skill_players)}; "
        f"{len(gsis_to_player)} have a usable nflverse gsis_id via the crosswalk."
    )

    url = PLAYER_WEEK_STATS_URL.format(season=SEASON_YEAR)
    print(f"Fetching {url} ...")
    weekly = pd.read_parquet(url)
    print(f"Fetched {len(weekly)} rows, {len(weekly.columns)} columns.")

    reg = weekly[
        (weekly["season_type"] == "REG") & (weekly["position"].isin(SKILL_POSITIONS))
    ].copy()
    print(f"{len(reg)} REG-season rows for skill positions {sorted(SKILL_POSITIONS)}.")

    to_insert: list[dict[str, Any]] = []
    to_update: list[dict[str, Any]] = []
    stats_keys: set[str] = set()
    unmapped_gsis = 0
    unmapped_team = 0
    unmapped_opponent_team = 0
    no_game_found = 0
    skipped_existing = 0
    zero_stat_weeks: set[tuple[str, int]] = set()  # (team_abbr, week) with no matched player

    for row in reg.itertuples(index=False):
        row_dict = row._asdict()
        gsis_id = row_dict.get("player_id")
        player = gsis_to_player.get(gsis_id)
        if player is None:
            unmapped_gsis += 1
            continue

        team_abbr = _normalize_nflverse_abbreviation(row_dict.get("team"))
        team_id = team_by_abbr.get(team_abbr)
        if team_id is None:
            unmapped_team += 1
            continue

        opponent_abbr = _normalize_nflverse_abbreviation(row_dict.get("opponent_team"))
        opponent_team_id = team_by_abbr.get(opponent_abbr)
        if opponent_team_id is None:
            unmapped_opponent_team += 1

        week_str = str(int(row_dict["week"]))
        game_id = game_by_team_week.get((team_id, week_str))
        if game_id is None:
            no_game_found += 1
            zero_stat_weeks.add((team_abbr, int(row_dict["week"])))
            continue

        key = (player["player_id"], game_id)
        if key in existing_keys:
            skipped_existing += 1
            to_update.append({
                "player_id": player["player_id"],
                "game_id": game_id,
                "team_id": team_id,
                "opponent_team_id": opponent_team_id,
            })
            continue

        stats = _row_to_json_safe(pd.Series(row_dict), SKILL_METADATA_COLUMNS)
        stats_keys.update(stats.keys())

        to_insert.append({
            "player_id": player["player_id"],
            "game_id": game_id,
            "team_id": team_id,
            "opponent_team_id": opponent_team_id,
            "stats": stats,
        })
        existing_keys.add(key)

    summary = {
        "reg_rows_seen": len(reg),
        "to_insert": len(to_insert),
        "unmapped_gsis": unmapped_gsis,
        "unmapped_team": unmapped_team,
        "unmapped_opponent_team": unmapped_opponent_team,
        "no_game_found": no_game_found,
        "skipped_existing": skipped_existing,
        "distinct_players_covered": len({r["player_id"] for r in to_insert}),
        "stats_keys": sorted(stats_keys),
        "zero_game_matches": sorted(zero_stat_weeks),
    }
    return to_insert, to_update, summary


def _build_def_rows(
    client,
    team_by_abbr: dict[str, str],
    game_by_team_week: dict[tuple[str, str], str],
    existing_keys: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    result = (
        client.table("players")
        .select("player_id, full_name, team_id")
        .eq("sport", "nfl")
        .eq("position", "DEF")
        .execute()
    )
    def_player_by_team_id = {row["team_id"]: row for row in (result.data or [])}
    print(f"DEF players in `players`: {len(def_player_by_team_id)}.")

    url = TEAM_WEEK_STATS_URL.format(season=SEASON_YEAR)
    print(f"Fetching {url} ...")
    weekly = pd.read_parquet(url)
    print(f"Fetched {len(weekly)} rows, {len(weekly.columns)} columns.")

    reg = weekly[weekly["season_type"] == "REG"].copy()
    print(f"{len(reg)} REG-season team-week rows.")

    to_insert: list[dict[str, Any]] = []
    to_update: list[dict[str, Any]] = []
    stats_keys: set[str] = set()
    unmapped_team = 0
    unmapped_opponent_team = 0
    no_def_player = 0
    no_game_found = 0
    skipped_existing = 0
    zero_stat_weeks: set[tuple[str, int]] = set()

    for row in reg.itertuples(index=False):
        row_dict = row._asdict()
        team_abbr = _normalize_nflverse_abbreviation(row_dict.get("team"))
        team_id = team_by_abbr.get(team_abbr)
        if team_id is None:
            unmapped_team += 1
            continue

        def_player = def_player_by_team_id.get(team_id)
        if def_player is None:
            no_def_player += 1
            continue

        opponent_abbr = _normalize_nflverse_abbreviation(row_dict.get("opponent_team"))
        opponent_team_id = team_by_abbr.get(opponent_abbr)
        if opponent_team_id is None:
            unmapped_opponent_team += 1

        week_str = str(int(row_dict["week"]))
        game_id = game_by_team_week.get((team_id, week_str))
        if game_id is None:
            no_game_found += 1
            zero_stat_weeks.add((team_abbr, int(row_dict["week"])))
            continue

        key = (def_player["player_id"], game_id)
        if key in existing_keys:
            skipped_existing += 1
            to_update.append({
                "player_id": def_player["player_id"],
                "game_id": game_id,
                "team_id": team_id,
                "opponent_team_id": opponent_team_id,
            })
            continue

        stats = _row_to_json_safe(pd.Series(row_dict), TEAM_METADATA_COLUMNS)
        stats_keys.update(stats.keys())

        to_insert.append({
            "player_id": def_player["player_id"],
            "game_id": game_id,
            "team_id": team_id,
            "opponent_team_id": opponent_team_id,
            "stats": stats,
        })
        existing_keys.add(key)

    summary = {
        "reg_rows_seen": len(reg),
        "to_insert": len(to_insert),
        "unmapped_team": unmapped_team,
        "unmapped_opponent_team": unmapped_opponent_team,
        "no_def_player": no_def_player,
        "no_game_found": no_game_found,
        "skipped_existing": skipped_existing,
        "distinct_teams_covered": len({r["player_id"] for r in to_insert}),
        "stats_keys": sorted(stats_keys),
        "zero_game_matches": sorted(zero_stat_weeks),
    }
    return to_insert, to_update, summary


def backfill_player_game_stats() -> None:
    client = get_supabase_client()

    team_by_abbr = _fetch_team_abbreviation_map(client)
    games = _fetch_season_games(client, SEASON_YEAR)
    print(f"Loaded {len(games)} season={SEASON_YEAR} games from `games`.")
    if not games:
        print(
            f"No season={SEASON_YEAR} games found. Run pipeline/seed_games_historical.py "
            "(Step 1) first. Aborting."
        )
        return
    game_by_team_week = _build_game_by_team_week(games)
    game_ids = [g["game_id"] for g in games]

    existing_keys = _fetch_existing_stat_keys(client, game_ids)
    print(
        f"Found {len(existing_keys)} existing player_game_stats rows already "
        f"linked to season={SEASON_YEAR} games."
    )

    print("\n--- SKILL PLAYERS (QB/RB/WR/TE/K) ---")
    skill_rows, skill_updates, skill_summary = _build_skill_rows(
        client, team_by_abbr, game_by_team_week, existing_keys
    )
    skill_inserted = 0
    for chunk in _chunks(skill_rows):
        result = client.table("player_game_stats").insert(chunk).execute()
        skill_inserted += len(result.data or [])

    print("\n--- TEAM DEFENSE (DEF) ---")
    def_rows, def_updates, def_summary = _build_def_rows(
        client, team_by_abbr, game_by_team_week, existing_keys
    )
    def_inserted = 0
    for chunk in _chunks(def_rows):
        result = client.table("player_game_stats").insert(chunk).execute()
        def_inserted += len(result.data or [])

    print("\n--- BACKFILLING team_id/opponent_team_id ON EXISTING ROWS ---")
    all_updates = skill_updates + def_updates
    rows_updated = 0
    for i, update in enumerate(all_updates, start=1):
        client.table("player_game_stats").update({
            "team_id": update["team_id"],
            "opponent_team_id": update["opponent_team_id"],
        }).eq("player_id", update["player_id"]).eq("game_id", update["game_id"]).execute()
        rows_updated += 1
        if i % BATCH_SIZE == 0 or i == len(all_updates):
            print(f"  ...{i}/{len(all_updates)} existing rows updated")

    print("\n=== CRITICAL OUTPUT: distinct JSONB keys actually written ===")
    print(f"SKILL ({len(skill_summary['stats_keys'])} keys):")
    for k in skill_summary["stats_keys"]:
        print(f"  {k}")
    print(f"\nDEF ({len(def_summary['stats_keys'])} keys):")
    for k in def_summary["stats_keys"]:
        print(f"  {k}")

    print("\n=== COVERAGE SUMMARY ===")
    print("SKILL:")
    print(f"  REG rows seen (nflverse):          {skill_summary['reg_rows_seen']}")
    print(f"  Rows inserted:                      {skill_inserted}")
    print(f"  Rows skipped (already existed):     {skill_summary['skipped_existing']}")
    print(f"  Rows skipped (no crosswalk match):  {skill_summary['unmapped_gsis']}")
    print(f"  Rows skipped (unmapped team abbr):  {skill_summary['unmapped_team']}")
    print(f"  Rows skipped (no matching game):    {skill_summary['no_game_found']}")
    print(f"  Distinct players covered:           {skill_summary['distinct_players_covered']}")
    if skill_summary["zero_game_matches"]:
        print(f"  Team/weeks with no matching game:   {skill_summary['zero_game_matches']}")

    print("\nDEF:")
    print(f"  REG rows seen (nflverse):          {def_summary['reg_rows_seen']}")
    print(f"  Rows inserted:                      {def_inserted}")
    print(f"  Rows skipped (already existed):     {def_summary['skipped_existing']}")
    print(f"  Rows skipped (no DEF player row):   {def_summary['no_def_player']}")
    print(f"  Rows skipped (unmapped team abbr):  {def_summary['unmapped_team']}")
    print(f"  Rows skipped (no matching game):    {def_summary['no_game_found']}")
    print(f"  Distinct teams covered:             {def_summary['distinct_teams_covered']}")
    if def_summary["zero_game_matches"]:
        print(f"  Team/weeks with no matching game:   {def_summary['zero_game_matches']}")

    print("\nTEAM_ID / OPPONENT_TEAM_ID BACKFILL:")
    print(f"  Existing rows updated:                       {rows_updated}")
    print(
        f"  Unmapped opponent_team abbr (SKILL, team_id still set): "
        f"{skill_summary['unmapped_opponent_team']}"
    )
    print(
        f"  Unmapped opponent_team abbr (DEF, team_id still set):   "
        f"{def_summary['unmapped_opponent_team']}"
    )

    all_covered_game_ids = {r["game_id"] for r in skill_rows} | {r["game_id"] for r in def_rows}
    for pair in existing_keys:
        all_covered_game_ids.add(pair[1])
    games_with_zero_stats = [g["game_id"] for g in games if g["game_id"] not in all_covered_game_ids]
    print(f"\nGames (season={SEASON_YEAR}) with zero player_game_stats rows: {len(games_with_zero_stats)}")
    if games_with_zero_stats:
        print(f"  game_ids: {games_with_zero_stats}")

    print("\nDone.")


if __name__ == "__main__":
    backfill_player_game_stats()
