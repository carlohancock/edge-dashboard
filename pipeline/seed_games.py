"""Seed the Supabase games table from ESPN's NFL scoreboard schedule."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.supabase_client import get_supabase_client

ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)
SEASON_YEAR = 2026
REGULAR_SEASON_WEEKS = range(1, 19)
REQUEST_DELAY_SECONDS = 0.3
BATCH_SIZE = 200
PAGE_SIZE = 1000

# TEMP: fetch + validate only; set False after reviewing per-week season output.
DRY_RUN = False

# Map source-specific abbreviations onto our teams.abbreviation values.
ESPN_ABBREVIATION_OVERRIDES: dict[str, str] = {
    "WSH": "WAS",  # ESPN Commanders vs Sleeper/our WAS
    # "JAC": "JAX",
    # "LA": "LAR",
}


def _normalize_espn_abbreviation(abbr: str | None) -> str | None:
    if not abbr:
        return abbr
    return ESPN_ABBREVIATION_OVERRIDES.get(abbr, abbr)


def _chunks(items: list[dict], size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _parse_event_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _event_in_season(date_str: str | None) -> bool:
    """True if event falls in Sept SEASON_YEAR .. Feb SEASON_YEAR+1."""
    dt = _parse_event_date(date_str)
    if dt is None:
        return False
    if dt.year == SEASON_YEAR and dt.month >= 9:
        return True
    if dt.year == SEASON_YEAR + 1 and dt.month <= 2:
        return True
    return False


def _fetch_team_abbreviation_map(client) -> dict[str, str]:
    result = (
        client.table("teams")
        .select("team_id, abbreviation")
        .eq("sport", "nfl")
        .execute()
    )
    return {row["abbreviation"]: row["team_id"] for row in (result.data or [])}


def _fetch_existing_games(client) -> set[tuple[str, str, str, str]]:
    """Set of (home_team_id, away_team_id, week_or_date, sport) for nfl games."""
    existing: set[tuple[str, str, str, str]] = set()
    offset = 0

    while True:
        result = (
            client.table("games")
            .select("home_team_id, away_team_id, week_or_date, sport")
            .eq("sport", "nfl")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            break

        for row in rows:
            existing.add(
                (
                    row["home_team_id"],
                    row["away_team_id"],
                    row["week_or_date"],
                    row["sport"],
                )
            )

        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return existing


def _fetch_nfl_game_ids(client) -> list[str]:
    game_ids: list[str] = []
    offset = 0

    while True:
        result = (
            client.table("games")
            .select("game_id")
            .eq("sport", "nfl")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            break
        game_ids.extend(row["game_id"] for row in rows)
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return game_ids


def _clear_nfl_games(client) -> int:
    """Remove stale NFL games (and linked odds) before reseeding the season."""
    game_ids = _fetch_nfl_game_ids(client)
    if not game_ids:
        return 0

    # Clear dependent odds rows first to avoid FK violations.
    for i in range(0, len(game_ids), BATCH_SIZE):
        chunk = game_ids[i : i + BATCH_SIZE]
        client.table("odds").delete().in_("game_id", chunk).execute()

    client.table("games").delete().eq("sport", "nfl").execute()
    return len(game_ids)


def _fetch_week_scoreboard(week: int) -> dict[str, Any]:
    response = requests.get(
        ESPN_SCOREBOARD_URL,
        params={
            "seasontype": 2,
            "week": week,
            "year": SEASON_YEAR,
            "dates": SEASON_YEAR,  # fallback — often more reliable than year alone
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _extract_competitors(
    event: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Return (home_abbr, away_abbr) from an ESPN event."""
    competitions = event.get("competitions") or []
    if not competitions:
        return None, None

    home_abbr = None
    away_abbr = None
    for competitor in competitions[0].get("competitors") or []:
        abbr = (competitor.get("team") or {}).get("abbreviation")
        home_away = competitor.get("homeAway")
        if home_away == "home":
            home_abbr = abbr
        elif home_away == "away":
            away_abbr = abbr

    return home_abbr, away_abbr


def seed_games() -> None:
    client = get_supabase_client()
    sport = "nfl"

    if DRY_RUN:
        print(
            f"DRY_RUN=True — fetching ESPN weeks for {SEASON_YEAR} only "
            "(no delete/insert)."
        )
    else:
        cleared = _clear_nfl_games(client)
        print(
            f"Cleared {cleared} existing NFL games before reseeding {SEASON_YEAR}."
        )

    team_by_abbr = _fetch_team_abbreviation_map(client)
    existing_games: set[tuple[str, str, str, str]] = set()
    if not DRY_RUN:
        existing_games = _fetch_existing_games(client)

    to_insert: list[dict[str, Any]] = []
    skipped = 0
    processed = 0
    mismatch_abbreviations: set[str] = set()
    mismatch_games = 0
    wrong_season_games = 0
    wrong_season_dates: set[str] = set()

    print("\n=== PER-WEEK SEASON YEAR CHECK ===")
    for week in REGULAR_SEASON_WEEKS:
        print(
            f"Fetching ESPN scoreboard for {SEASON_YEAR} week {week}..."
        )
        payload = _fetch_week_scoreboard(week)
        events = payload.get("events") or []
        week_str = str(week)

        week_correct = 0
        week_wrong = 0
        sample_wrong_dates: list[str] = []

        for event in events:
            processed += 1
            event_date = event.get("date")

            if not _event_in_season(event_date):
                week_wrong += 1
                wrong_season_games += 1
                if event_date:
                    wrong_season_dates.add(str(event_date)[:10])
                    if len(sample_wrong_dates) < 3:
                        sample_wrong_dates.append(str(event_date))
                continue

            week_correct += 1

            home_abbr, away_abbr = _extract_competitors(event)
            home_abbr = _normalize_espn_abbreviation(home_abbr)
            away_abbr = _normalize_espn_abbreviation(away_abbr)

            unmatched = [
                abbr
                for abbr in (home_abbr, away_abbr)
                if not abbr or abbr not in team_by_abbr
            ]
            if unmatched:
                mismatch_games += 1
                for abbr in unmatched:
                    mismatch_abbreviations.add(abbr if abbr else "<missing>")
                continue

            home_team_id = team_by_abbr[home_abbr]
            away_team_id = team_by_abbr[away_abbr]
            key = (home_team_id, away_team_id, week_str, sport)

            if key in existing_games:
                skipped += 1
                continue

            to_insert.append(
                {
                    "sport": sport,
                    "week_or_date": week_str,
                    "home_team_id": home_team_id,
                    "away_team_id": away_team_id,
                    "game_time": event_date,
                    "implied_home_score": None,
                    "implied_away_score": None,
                    "game_total": None,
                }
            )
            # Avoid duplicate inserts within the same run.
            existing_games.add(key)

        wrong_note = ""
        if sample_wrong_dates:
            wrong_note = f" sample_wrong_dates={sample_wrong_dates}"
        print(
            f"Week {week}: fetched={len(events)}, "
            f"correct_season={week_correct}, wrong_season={week_wrong}"
            f"{wrong_note}"
        )

        if week < REGULAR_SEASON_WEEKS.stop - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    print("=== END PER-WEEK SEASON YEAR CHECK ===\n")

    if DRY_RUN:
        mismatch_list = sorted(mismatch_abbreviations)
        print(
            f"DRY_RUN summary ({SEASON_YEAR}): would insert {len(to_insert)} games, "
            f"{skipped} skipped, {mismatch_games} abbreviation mismatches"
            f"{f' {mismatch_list}' if mismatch_list else ''}, "
            f"{wrong_season_games} wrong-season-year mismatches"
            f"{f' dates={sorted(wrong_season_dates)}' if wrong_season_dates else ''}, "
            f"{processed} total events processed. "
            "Set DRY_RUN=False to clear + insert."
        )
        return

    inserted = 0
    for chunk in _chunks(to_insert):
        result = client.table("games").insert(chunk).execute()
        inserted += len(result.data or [])

    mismatch_list = sorted(mismatch_abbreviations)
    print(
        f"NFL games seed complete ({SEASON_YEAR}): {inserted} inserted, "
        f"{skipped} skipped, {mismatch_games} abbreviation mismatches"
        f"{f' {mismatch_list}' if mismatch_list else ''}, "
        f"{wrong_season_games} wrong-season-year mismatches"
        f"{f' dates={sorted(wrong_season_dates)}' if wrong_season_dates else ''}, "
        f"{processed} total games processed."
    )


if __name__ == "__main__":
    seed_games()
