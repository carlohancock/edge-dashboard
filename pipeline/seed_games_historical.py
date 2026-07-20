"""Backfill a past NFL season's games into Supabase using nfl_data_py (nflverse).

Unlike seed_games.py (ESPN scoreboard, current-season only), this pulls a
completed season's schedule from nflverse so historical games/stats can be
loaded for scoring-engine testing. Change SEASON_YEAR to backfill a different
past season.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import nfl_data_py as nfl
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.supabase_client import get_supabase_client

SEASON_YEAR = 2025
BATCH_SIZE = 200
PAGE_SIZE = 1000

# nflverse plays games in the team's local Eastern-time kickoff slot;
# gameday/gametime are naive Eastern values that must be localized before
# converting to UTC for storage.
NFLVERSE_TZ = ZoneInfo("America/New_York")

# TEMP: fetch + map + print summary only, no writes. Flip to False to insert.
DRY_RUN = False

# nflverse abbreviations that don't match our teams.abbreviation values.
NFLVERSE_ABBREVIATION_OVERRIDES: dict[str, str] = {
    "LA": "LAR",  # nflverse uses "LA" for the Rams; our teams table uses "LAR"
}


def _normalize_nflverse_abbreviation(abbr: str | None) -> str | None:
    if not abbr:
        return abbr
    return NFLVERSE_ABBREVIATION_OVERRIDES.get(abbr, abbr)


def _chunks(items: list[dict], size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _build_game_time_utc(gameday: Any, gametime: Any) -> str | None:
    """Combine nflverse gameday (YYYY-MM-DD) + gametime (HH:MM, Eastern) -> UTC ISO8601."""
    if pd.isna(gameday) or pd.isna(gametime):
        return None
    try:
        naive = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    eastern = naive.replace(tzinfo=NFLVERSE_TZ)
    return eastern.astimezone(timezone.utc).isoformat()


def _fetch_team_abbreviation_map(client) -> dict[str, str]:
    result = (
        client.table("teams")
        .select("team_id, abbreviation")
        .eq("sport", "nfl")
        .execute()
    )
    return {row["abbreviation"]: row["team_id"] for row in (result.data or [])}


def _fetch_existing_season_keys(client, season: int) -> set[tuple[str, str, str]]:
    """Set of (home_team_id, away_team_id, week_or_date) already seeded for this season."""
    existing: set[tuple[str, str, str]] = set()
    offset = 0

    while True:
        result = (
            client.table("games")
            .select("home_team_id, away_team_id, week_or_date")
            .eq("sport", "nfl")
            .eq("season", season)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            break

        for row in rows:
            existing.add(
                (row["home_team_id"], row["away_team_id"], row["week_or_date"])
            )

        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return existing


def seed_historical_games() -> None:
    client = get_supabase_client()
    sport = "nfl"

    print(f"Fetching {SEASON_YEAR} schedule from nfl_data_py...")
    schedule = nfl.import_schedules([SEASON_YEAR])
    reg = schedule[schedule["game_type"] == "REG"].copy()
    print(
        f"Fetched {len(reg)} regular-season games (game_type=REG) for {SEASON_YEAR} "
        f"out of {len(schedule)} total rows (incl. playoffs)."
    )

    team_by_abbr = _fetch_team_abbreviation_map(client)
    existing_keys = _fetch_existing_season_keys(client, SEASON_YEAR)
    print(f"Found {len(existing_keys)} existing season={SEASON_YEAR} games already in `games`.")

    to_insert: list[dict[str, Any]] = []
    skipped_existing = 0
    skipped_unmapped = 0
    skipped_bad_time = 0
    unmapped_abbrs: set[str] = set()

    for row in reg.itertuples(index=False):
        home_abbr = _normalize_nflverse_abbreviation(row.home_team)
        away_abbr = _normalize_nflverse_abbreviation(row.away_team)

        missing = [a for a in (home_abbr, away_abbr) if not a or a not in team_by_abbr]
        if missing:
            skipped_unmapped += 1
            for a in missing:
                unmapped_abbrs.add(a if a else "<missing>")
            continue

        home_team_id = team_by_abbr[home_abbr]
        away_team_id = team_by_abbr[away_abbr]
        week_str = str(int(row.week))
        key = (home_team_id, away_team_id, week_str)

        if key in existing_keys:
            skipped_existing += 1
            continue

        game_time = _build_game_time_utc(row.gameday, row.gametime)
        if game_time is None:
            skipped_bad_time += 1
            print(
                f"WARNING: could not build game_time for {row.game_id} "
                f"(gameday={row.gameday!r}, gametime={row.gametime!r}); skipping."
            )
            continue

        to_insert.append(
            {
                "sport": sport,
                "season": SEASON_YEAR,
                "week_or_date": week_str,
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "game_time": game_time,
                "implied_home_score": None,
                "implied_away_score": None,
                "game_total": None,
            }
        )
        # Avoid duplicate inserts within the same run.
        existing_keys.add(key)

    inserted = 0
    if DRY_RUN:
        print(
            f"\nDRY_RUN=True: would insert {len(to_insert)} games. "
            "Set DRY_RUN=False and re-run to actually insert."
        )
    else:
        for chunk in _chunks(to_insert):
            result = client.table("games").insert(chunk).execute()
            inserted += len(result.data or [])

    unmapped_list = sorted(unmapped_abbrs)
    print("\n=== SEED SUMMARY ===")
    print(f"Season: {SEASON_YEAR}")
    print(f"Regular-season games fetched: {len(reg)}")
    print(f"Inserted: {inserted}")
    print(f"Skipped (already existed): {skipped_existing}")
    print(f"Skipped (unmapped team abbreviation): {skipped_unmapped}")
    print(f"Skipped (unparseable game_time): {skipped_bad_time}")
    if unmapped_list:
        print(f"Unmapped abbreviations encountered: {unmapped_list}")
    print("====================\n")


if __name__ == "__main__":
    seed_historical_games()
