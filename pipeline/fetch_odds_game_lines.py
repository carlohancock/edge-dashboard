"""Fetch NFL spreads/totals from The Odds API and update games + odds tables."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.supabase_client import get_supabase_client

load_dotenv()

ODDS_API_URL = (
    "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds"
)
GAME_TIME_MATCH_HOURS = 6
BATCH_SIZE = 200
PAGE_SIZE = 1000

# Odds API full team names → our teams.abbreviation
TEAM_NAME_TO_ABBR: dict[str, str] = {
    "Arizona Cardinals": "ARI",
    "Atlanta Falcons": "ATL",
    "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR",
    "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN",
    "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN",
    "Detroit Lions": "DET",
    "Green Bay Packers": "GB",
    "Houston Texans": "HOU",
    "Indianapolis Colts": "IND",
    "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV",
    "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR",
    "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN",
    "New England Patriots": "NE",
    "New Orleans Saints": "NO",
    "New York Giants": "NYG",
    "New York Jets": "NYJ",
    "Philadelphia Eagles": "PHI",
    "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA",
    "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN",
    "Washington Commanders": "WAS",
}


def _chunks(items: list[dict], size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # Handle both "...Z" and "+00:00" forms from APIs / Postgres.
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fetch_team_maps(client) -> tuple[dict[str, str], dict[str, str]]:
    """Return (abbreviation -> team_id, team_id -> abbreviation)."""
    result = (
        client.table("teams")
        .select("team_id, abbreviation")
        .eq("sport", "nfl")
        .execute()
    )
    abbr_to_id = {row["abbreviation"]: row["team_id"] for row in (result.data or [])}
    id_to_abbr = {row["team_id"]: row["abbreviation"] for row in (result.data or [])}
    return abbr_to_id, id_to_abbr


def _fetch_nfl_games(client) -> list[dict[str, Any]]:
    games: list[dict[str, Any]] = []
    offset = 0

    while True:
        result = (
            client.table("games")
            .select(
                "game_id, home_team_id, away_team_id, game_time, "
                "game_total, implied_home_score, implied_away_score"
            )
            .eq("sport", "nfl")
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


def _fetch_odds_api() -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing ODDS_API_KEY. Copy .env.example to .env and fill in your key."
        )

    response = requests.get(
        ODDS_API_URL,
        params={
            "apiKey": api_key,
            "regions": "us",
            "markets": "spreads,totals",
            "oddsFormat": "american",
        },
        timeout=60,
    )

    headers = {
        "x-requests-remaining": response.headers.get("x-requests-remaining"),
        "x-requests-used": response.headers.get("x-requests-used"),
    }

    if response.status_code == 401:
        raise RuntimeError(
            "Odds API rejected the key (401 Unauthorized). Check ODDS_API_KEY."
        )
    if response.status_code == 429:
        raise RuntimeError(
            "Odds API rate limit hit (429). Wait and retry; "
            f"remaining={headers['x-requests-remaining']}."
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Odds API error {response.status_code}: {response.text[:300]}"
        )

    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Odds API payload type: {type(data).__name__}")

    return data, headers


def _extract_consensus_lines(
    event: dict[str, Any],
) -> tuple[float | None, float | None, float | None]:
    """Return (home_spread, away_spread, game_total) averaged across books."""
    home_team = event.get("home_team")
    away_team = event.get("away_team")
    home_spreads: list[float] = []
    away_spreads: list[float] = []
    totals: list[float] = []

    for book in event.get("bookmakers") or []:
        for market in book.get("markets") or []:
            key = market.get("key")
            outcomes = market.get("outcomes") or []
            if key == "spreads":
                for outcome in outcomes:
                    name = outcome.get("name")
                    point = outcome.get("point")
                    if point is None:
                        continue
                    if name == home_team:
                        home_spreads.append(float(point))
                    elif name == away_team:
                        away_spreads.append(float(point))
            elif key == "totals":
                for outcome in outcomes:
                    if outcome.get("name") == "Over" and outcome.get("point") is not None:
                        totals.append(float(outcome["point"]))
                        break

    home_spread = mean(home_spreads) if home_spreads else None
    away_spread = mean(away_spreads) if away_spreads else None
    game_total = mean(totals) if totals else None
    return home_spread, away_spread, game_total


def _implied_scores(
    game_total: float, home_spread: float, away_spread: float
) -> tuple[float, float]:
    # team_implied_total = (game_total / 2) - (team_spread / 2)
    implied_home = (game_total / 2.0) - (home_spread / 2.0)
    implied_away = (game_total / 2.0) - (away_spread / 2.0)
    return implied_home, implied_away


def _match_game(
    home_team_id: str,
    away_team_id: str,
    commence_time: datetime,
    games: list[dict[str, Any]],
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_delta: float | None = None
    max_seconds = GAME_TIME_MATCH_HOURS * 3600

    for game in games:
        if game["home_team_id"] != home_team_id or game["away_team_id"] != away_team_id:
            continue
        game_time = _parse_iso(game.get("game_time"))
        if game_time is None:
            continue
        delta = abs((game_time - commence_time).total_seconds())
        if delta > max_seconds:
            continue
        if best_delta is None or delta < best_delta:
            best = game
            best_delta = delta

    return best


def _build_odds_rows(
    game_id: str,
    event: dict[str, Any],
    fetched_at: str,
) -> list[dict[str, Any]]:
    """One spreads row + one totals row per sportsbook (append-only history)."""
    home_team = event.get("home_team")
    rows: list[dict[str, Any]] = []

    for book in event.get("bookmakers") or []:
        sportsbook = book.get("title") or book.get("key") or "unknown"
        for market in book.get("markets") or []:
            key = market.get("key")
            outcomes = market.get("outcomes") or []
            line: float | None = None

            if key == "spreads":
                for outcome in outcomes:
                    if outcome.get("name") == home_team and outcome.get("point") is not None:
                        line = float(outcome["point"])
                        break
            elif key == "totals":
                for outcome in outcomes:
                    if outcome.get("name") == "Over" and outcome.get("point") is not None:
                        line = float(outcome["point"])
                        break
            else:
                continue

            if line is None:
                continue

            rows.append(
                {
                    "game_id": game_id,
                    "player_id": None,
                    "market_type": key,  # 'spreads' or 'totals'
                    "line": line,
                    "implied_probability": None,
                    "sportsbook": sportsbook,
                    "fetched_at": fetched_at,
                }
            )

    return rows


def fetch_odds_game_lines() -> None:
    client = get_supabase_client()
    abbr_to_id, _id_to_abbr = _fetch_team_maps(client)
    games = _fetch_nfl_games(client)

    print("Fetching NFL spreads/totals from The Odds API...")
    try:
        events, usage_headers = _fetch_odds_api()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return
    except requests.RequestException as exc:
        print(f"ERROR: Odds API request failed: {exc}")
        return

    print(f"Received {len(events)} events from Odds API.")
    remaining = usage_headers.get("x-requests-remaining")
    used = usage_headers.get("x-requests-used")
    if remaining is not None or used is not None:
        print(f"Odds API usage — used: {used}, remaining: {remaining}")

    fetched_at = datetime.now(timezone.utc).isoformat()
    updated = 0
    unmatched: list[str] = []
    odds_rows: list[dict[str, Any]] = []

    for event in events:
        home_name = event.get("home_team") or ""
        away_name = event.get("away_team") or ""
        label = f"{away_name} @ {home_name}"

        home_abbr = TEAM_NAME_TO_ABBR.get(home_name)
        away_abbr = TEAM_NAME_TO_ABBR.get(away_name)
        if not home_abbr or not away_abbr:
            missing = [
                name
                for name, abbr in ((home_name, home_abbr), (away_name, away_abbr))
                if not abbr
            ]
            unmatched.append(f"{label} (unmapped team names: {missing})")
            continue

        home_team_id = abbr_to_id.get(home_abbr)
        away_team_id = abbr_to_id.get(away_abbr)
        if not home_team_id or not away_team_id:
            unmatched.append(
                f"{label} (no teams.abbreviation for {home_abbr}/{away_abbr})"
            )
            continue

        commence_time = _parse_iso(event.get("commence_time"))
        if commence_time is None:
            unmatched.append(f"{label} (invalid commence_time)")
            continue

        matched = _match_game(home_team_id, away_team_id, commence_time, games)
        if matched is None:
            unmatched.append(
                f"{label} (no games row within {GAME_TIME_MATCH_HOURS}h of "
                f"{commence_time.isoformat()})"
            )
            continue

        home_spread, away_spread, game_total = _extract_consensus_lines(event)
        if home_spread is None or away_spread is None or game_total is None:
            unmatched.append(f"{label} (missing spreads/totals across books)")
            continue

        implied_home, implied_away = _implied_scores(
            game_total, home_spread, away_spread
        )

        client.table("games").update(
            {
                "game_total": game_total,
                "implied_home_score": implied_home,
                "implied_away_score": implied_away,
            }
        ).eq("game_id", matched["game_id"]).execute()
        updated += 1

        odds_rows.extend(
            _build_odds_rows(matched["game_id"], event, fetched_at)
        )

    inserted = 0
    for chunk in _chunks(odds_rows):
        result = client.table("odds").insert(chunk).execute()
        inserted += len(result.data or [])

    print(
        f"Odds fetch complete: {updated} games updated, "
        f"{len(unmatched)} unmatched"
        f"{f' {unmatched}' if unmatched else ''}, "
        f"{inserted} odds rows inserted."
    )
    if remaining is not None or used is not None:
        print(f"API budget — requests used: {used}, remaining: {remaining}")


if __name__ == "__main__":
    fetch_odds_game_lines()
