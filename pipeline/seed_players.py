"""Seed the Supabase players table from Sleeper's NFL player list."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.supabase_client import get_supabase_client

SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
ALLOWED_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})
SKIP_STATUSES = frozenset({None, "Inactive"})
BATCH_SIZE = 200
PAGE_SIZE = 1000


def _chunks(items: list[dict], size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _fetch_sleeper_players() -> dict[str, dict[str, Any]]:
    response = requests.get(SLEEPER_PLAYERS_URL, timeout=120)
    response.raise_for_status()
    return response.json()


def _fetch_team_abbreviation_map(client) -> dict[str, str]:
    result = (
        client.table("teams")
        .select("team_id, abbreviation")
        .eq("sport", "nfl")
        .execute()
    )
    return {row["abbreviation"]: row["team_id"] for row in (result.data or [])}


def _fetch_existing_players(client) -> dict[str, dict[str, Any]]:
    """Map sleeper_id -> existing player row for sport=nfl."""
    existing: dict[str, dict[str, Any]] = {}
    offset = 0

    while True:
        result = (
            client.table("players")
            .select(
                "player_id, full_name, team_id, position, status, "
                "depth_chart_rank, external_ids, sport"
            )
            .eq("sport", "nfl")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            break

        for row in rows:
            external_ids = row.get("external_ids") or {}
            sleeper_id = external_ids.get("sleeper_id")
            if sleeper_id is not None:
                existing[str(sleeper_id)] = row

        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return existing


def _should_include(player: dict[str, Any]) -> bool:
    if player.get("position") not in ALLOWED_POSITIONS:
        return False
    if player.get("position") == "DEF":
        return True
    if player.get("status") in SKIP_STATUSES:
        return False
    if not player.get("full_name"):
        return False
    if not player.get("team"):
        return False
    return True


def _map_player(
    sleeper_id: str,
    player: dict[str, Any],
    team_by_abbr: dict[str, str],
) -> dict[str, Any]:
    if player.get("position") == "DEF":
        team_abbr = sleeper_id.upper()
        team_id = team_by_abbr.get(team_abbr)
        return {
            "full_name": f"{team_abbr} DEF",
            "team_id": team_id,
            "position": "DEF",
            "status": player.get("status") or "Active",
            "depth_chart_rank": None,
            "external_ids": {"sleeper_id": sleeper_id},
            "sport": "nfl",
        }

    team_abbr = player.get("team")
    team_id = team_by_abbr.get(team_abbr) if team_abbr else None

    return {
        "full_name": player["full_name"],
        "team_id": team_id,
        "position": player["position"],
        "status": player["status"],
        "depth_chart_rank": player.get("depth_chart_order"),
        "external_ids": {"sleeper_id": sleeper_id},
        "sport": "nfl",
    }


def _needs_update(existing: dict[str, Any], mapped: dict[str, Any]) -> bool:
    return (
        existing.get("team_id") != mapped["team_id"]
        or existing.get("status") != mapped["status"]
        or existing.get("depth_chart_rank") != mapped["depth_chart_rank"]
    )


def seed_players() -> None:
    client = get_supabase_client()

    print("Fetching NFL players from Sleeper...")
    sleeper_players = _fetch_sleeper_players()
    print(f"Fetched {len(sleeper_players)} Sleeper player records.")

    team_by_abbr = _fetch_team_abbreviation_map(client)
    existing_by_sleeper_id = _fetch_existing_players(client)

    to_insert: list[dict[str, Any]] = []
    to_update: list[dict[str, Any]] = []
    skipped = 0
    processed = 0

    for sleeper_id, player in sleeper_players.items():
        if not _should_include(player):
            continue

        processed += 1
        mapped = _map_player(str(sleeper_id), player, team_by_abbr)
        existing = existing_by_sleeper_id.get(str(sleeper_id))

        if existing is None:
            to_insert.append(mapped)
            continue

        if not _needs_update(existing, mapped):
            skipped += 1
            continue

        # Preserve immutable fields; only refresh weekly-changing ones.
        to_update.append(
            {
                "player_id": existing["player_id"],
                "full_name": existing["full_name"],
                "position": existing["position"],
                "sport": existing["sport"],
                "external_ids": existing["external_ids"],
                "team_id": mapped["team_id"],
                "status": mapped["status"],
                "depth_chart_rank": mapped["depth_chart_rank"],
            }
        )

    inserted = 0
    for chunk in _chunks(to_insert):
        result = client.table("players").insert(chunk).execute()
        inserted += len(result.data or [])

    updated = 0
    for chunk in _chunks(to_update):
        result = (
            client.table("players")
            .upsert(chunk, on_conflict="player_id")
            .execute()
        )
        updated += len(result.data or [])

    print(
        f"NFL players seed complete: {inserted} inserted, {updated} updated, "
        f"{skipped} skipped (no changes needed), {processed} total processed."
    )


if __name__ == "__main__":
    seed_players()
