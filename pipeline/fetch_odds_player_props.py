"""Fetch NFL player props from The Odds API into the odds table."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.supabase_client import get_supabase_client

load_dotenv()

# Set False to spend quota and write odds rows.
DRY_RUN = True

EVENTS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events"
EVENT_ODDS_URL = (
    "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events/{event_id}/odds"
)
PROP_MARKETS = (
    "player_anytime_td",
    "player_pass_yds",
    "player_rush_yds",
    "player_reception_yds",
)
REGIONS = "us"
GAME_WINDOW_DAYS = 7
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

_OUTCOME_LABELS = frozenset({"over", "under", "yes", "no"})


def _chunks(items: list[dict], size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _parse_iso(value: str | None) -> datetime | None:
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


def _api_key() -> str:
    key = os.getenv("ODDS_API_KEY")
    if not key:
        raise RuntimeError(
            "Missing ODDS_API_KEY. Copy .env.example to .env and fill in your key."
        )
    return key


def _raise_odds_api_error(response: requests.Response) -> None:
    remaining = response.headers.get("x-requests-remaining")
    if response.status_code == 401:
        raise RuntimeError(
            "Odds API rejected the key (401 Unauthorized). Check ODDS_API_KEY."
        )
    if response.status_code == 429:
        raise RuntimeError(
            f"Odds API rate limit hit (429). remaining={remaining}."
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Odds API error {response.status_code}: {response.text[:300]}"
        )


def _american_to_implied_prob(price: float) -> float:
    """Raw implied probability from American odds (edge_formula_nfl.md)."""
    if price >= 0:
        return 100.0 / (price + 100.0)
    abs_price = abs(price)
    return abs_price / (abs_price + 100.0)


def _normalize_player_name(name: str) -> str:
    n = name.lower().strip()
    for suffix in (" jr.", " jr", " sr.", " sr", " ii", " iii", " iv", " v"):
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
            break
    n = n.replace(".", "").replace("'", "").replace(",", "")
    return " ".join(n.split())


def _fetch_team_maps(client) -> dict[str, str]:
    result = (
        client.table("teams")
        .select("team_id, abbreviation")
        .eq("sport", "nfl")
        .execute()
    )
    return {row["abbreviation"]: row["team_id"] for row in (result.data or [])}


def _fetch_upcoming_games(client, now: datetime) -> list[dict[str, Any]]:
    """NFL games with game_time in [now, now+7d]."""
    window_end = now + timedelta(days=GAME_WINDOW_DAYS)
    result = (
        client.table("games")
        .select("game_id, home_team_id, away_team_id, game_time")
        .eq("sport", "nfl")
        .gte("game_time", now.isoformat())
        .lte("game_time", window_end.isoformat())
        .order("game_time")
        .execute()
    )
    return result.data or []


def _fetch_players(client) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = (
            client.table("players")
            .select("player_id, full_name, status")
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


def _build_player_index(
    players: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """normalized full_name -> player_id; first+last -> [player_ids]."""
    by_full: dict[str, str] = {}
    by_first_last: dict[str, list[str]] = {}

    for row in players:
        full = row.get("full_name") or ""
        norm = _normalize_player_name(full)
        if not norm:
            continue
        # Prefer Active when duplicate normalized names collide.
        existing = by_full.get(norm)
        if existing is None or row.get("status") == "Active":
            by_full[norm] = row["player_id"]

        parts = norm.split()
        if len(parts) >= 2:
            key = f"{parts[0]} {parts[-1]}"
            by_first_last.setdefault(key, [])
            if row["player_id"] not in by_first_last[key]:
                by_first_last[key].append(row["player_id"])

    return by_full, by_first_last


def _resolve_player_id(
    prop_name: str,
    by_full: dict[str, str],
    by_first_last: dict[str, list[str]],
) -> str | None:
    norm = _normalize_player_name(prop_name)
    if not norm:
        return None
    if norm in by_full:
        return by_full[norm]

    parts = norm.split()
    if len(parts) >= 2:
        key = f"{parts[0]} {parts[-1]}"
        candidates = by_first_last.get(key) or []
        if len(candidates) == 1:
            return candidates[0]
        if key in by_full:
            return by_full[key]
    return None


def _fetch_events() -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    """List upcoming NFL events (free — does not count against quota)."""
    response = requests.get(
        EVENTS_URL,
        params={"apiKey": _api_key()},
        timeout=60,
    )
    _raise_odds_api_error(response)
    headers = {
        "x-requests-remaining": response.headers.get("x-requests-remaining"),
        "x-requests-used": response.headers.get("x-requests-used"),
        "x-requests-last": response.headers.get("x-requests-last"),
    }
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected events payload type: {type(data).__name__}")
    return data, headers


def _fetch_event_odds(
    event_id: str,
) -> tuple[dict[str, Any], dict[str, str | None]]:
    response = requests.get(
        EVENT_ODDS_URL.format(event_id=event_id),
        params={
            "apiKey": _api_key(),
            "regions": REGIONS,
            "markets": ",".join(PROP_MARKETS),
            "oddsFormat": "american",
        },
        timeout=90,
    )
    _raise_odds_api_error(response)
    headers = {
        "x-requests-remaining": response.headers.get("x-requests-remaining"),
        "x-requests-used": response.headers.get("x-requests-used"),
        "x-requests-last": response.headers.get("x-requests-last"),
    }
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected event odds payload type: {type(data).__name__}")
    return data, headers


def _match_game_to_event(
    game: dict[str, Any],
    events: list[dict[str, Any]],
    abbr_to_id: dict[str, str],
) -> dict[str, Any] | None:
    game_time = _parse_iso(game.get("game_time"))
    if game_time is None:
        return None

    best: dict[str, Any] | None = None
    best_delta: float | None = None
    max_seconds = GAME_TIME_MATCH_HOURS * 3600

    for event in events:
        home_abbr = TEAM_NAME_TO_ABBR.get(event.get("home_team") or "")
        away_abbr = TEAM_NAME_TO_ABBR.get(event.get("away_team") or "")
        if not home_abbr or not away_abbr:
            continue
        home_id = abbr_to_id.get(home_abbr)
        away_id = abbr_to_id.get(away_abbr)
        if home_id != game["home_team_id"] or away_id != game["away_team_id"]:
            continue

        commence = _parse_iso(event.get("commence_time"))
        if commence is None:
            continue
        delta = abs((commence - game_time).total_seconds())
        if delta > max_seconds:
            continue
        if best_delta is None or delta < best_delta:
            best = event
            best_delta = delta

    return best


def _player_name_from_outcome(outcome: dict[str, Any]) -> str | None:
    description = (outcome.get("description") or "").strip()
    if description:
        return description
    name = (outcome.get("name") or "").strip()
    if name and name.lower() not in _OUTCOME_LABELS:
        return name
    return None


def _should_keep_outcome(market_key: str, outcome: dict[str, Any]) -> bool:
    """Keep Yes (anytime TD) or Over (yardage) sides as the primary line."""
    name = (outcome.get("name") or "").strip().lower()
    if market_key == "player_anytime_td":
        return name in {"yes", ""} or name not in _OUTCOME_LABELS
    # Yardage O/U — store the Over line.
    return name == "over"


def _build_prop_rows(
    *,
    game_id: str,
    event_odds: dict[str, Any],
    by_full: dict[str, str],
    by_first_last: dict[str, list[str]],
    fetched_at: str,
    unmatched_names: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for book in event_odds.get("bookmakers") or []:
        sportsbook = book.get("title") or book.get("key") or "unknown"
        for market in book.get("markets") or []:
            market_key = market.get("key")
            if market_key not in PROP_MARKETS:
                continue

            for outcome in market.get("outcomes") or []:
                if not _should_keep_outcome(market_key, outcome):
                    continue

                player_name = _player_name_from_outcome(outcome)
                if not player_name:
                    continue

                player_id = _resolve_player_id(player_name, by_full, by_first_last)
                if player_id is None:
                    unmatched_names.add(player_name)
                    continue

                price = outcome.get("price")
                point = outcome.get("point")

                if market_key == "player_anytime_td":
                    if price is None:
                        continue
                    line = float(price)
                    implied = _american_to_implied_prob(float(price))
                else:
                    if point is None:
                        continue
                    line = float(point)
                    implied = None

                rows.append(
                    {
                        "game_id": game_id,
                        "player_id": player_id,
                        "market_type": market_key,
                        "line": line,
                        "implied_probability": implied,
                        "sportsbook": sportsbook,
                        "fetched_at": fetched_at,
                    }
                )

    return rows


def fetch_odds_player_props() -> None:
    client = get_supabase_client()
    now = datetime.now(timezone.utc)

    print(
        f"{'DRY_RUN' if DRY_RUN else 'LIVE'}: fetching player props for NFL games "
        f"in the next {GAME_WINDOW_DAYS} days..."
    )

    games = _fetch_upcoming_games(client, now)
    print(f"Found {len(games)} NFL games between now and +{GAME_WINDOW_DAYS}d.")

    if not games:
        print("Nothing to do — no games in the window.")
        return

    abbr_to_id = _fetch_team_maps(client)

    try:
        events, events_headers = _fetch_events()
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return
    except requests.RequestException as exc:
        print(f"ERROR: Odds API events request failed: {exc}")
        return

    print(
        f"Fetched {len(events)} Odds API events "
        f"(events endpoint is free; remaining="
        f"{events_headers.get('x-requests-remaining')})."
    )

    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    unmatched_games: list[str] = []
    for game in games:
        event = _match_game_to_event(game, events, abbr_to_id)
        if event is None:
            unmatched_games.append(
                f"game_id={game['game_id']} game_time={game.get('game_time')}"
            )
            continue
        matched.append((game, event))

    print(f"Matched {len(matched)} / {len(games)} games to Odds API event IDs.")
    if unmatched_games:
        print(f"Unmatched games ({len(unmatched_games)}): {unmatched_games}")

    # Cost: event-odds credits ≈ (# markets returned) × (# regions), up to 4×1 per event.
    max_credits = len(matched) * len(PROP_MARKETS) * 1
    print(
        f"Would spend up to {max_credits} credits "
        f"({len(matched)} events × {len(PROP_MARKETS)} markets × 1 region). "
        f"Plus 0 for /events."
    )

    if DRY_RUN:
        print(
            "DRY_RUN=True — skipping event odds fetches and inserts. "
            "Set DRY_RUN=False in the script to run for real."
        )
        return

    players = _fetch_players(client)
    by_full, by_first_last = _build_player_index(players)
    fetched_at = datetime.now(timezone.utc).isoformat()

    odds_rows: list[dict[str, Any]] = []
    unmatched_names: set[str] = set()
    events_processed = 0
    credits_this_run = 0
    last_remaining: str | None = events_headers.get("x-requests-remaining")
    last_used: str | None = events_headers.get("x-requests-used")

    for game, event in matched:
        event_id = event.get("id")
        if not event_id:
            continue
        try:
            event_odds, headers = _fetch_event_odds(str(event_id))
        except RuntimeError as exc:
            print(f"ERROR fetching odds for event {event_id}: {exc}")
            continue
        except requests.RequestException as exc:
            print(f"ERROR fetching odds for event {event_id}: {exc}")
            continue

        last_cost = headers.get("x-requests-last")
        if last_cost is not None:
            try:
                credits_this_run += int(last_cost)
            except ValueError:
                pass
        last_remaining = headers.get("x-requests-remaining") or last_remaining
        last_used = headers.get("x-requests-used") or last_used

        events_processed += 1
        odds_rows.extend(
            _build_prop_rows(
                game_id=game["game_id"],
                event_odds=event_odds,
                by_full=by_full,
                by_first_last=by_first_last,
                fetched_at=fetched_at,
                unmatched_names=unmatched_names,
            )
        )

    inserted = 0
    for chunk in _chunks(odds_rows):
        result = client.table("odds").insert(chunk).execute()
        inserted += len(result.data or [])

    mismatches = sorted(unmatched_names)
    print(
        f"Player props fetch complete: {events_processed} events processed, "
        f"{inserted} props inserted, {len(mismatches)} player-name mismatches"
        f"{f' {mismatches}' if mismatches else ''}."
    )
    print(
        f"API budget this run — credits spent≈{credits_this_run}, "
        f"used={last_used}, remaining={last_remaining} (of ~500/month)."
    )


if __name__ == "__main__":
    fetch_odds_player_props()
