"""
Ingest ADP (Average Draft Position) data into the `adp` table, for Draft Edge's
ADP-value-gap feature (edge_formula_nfl.md's Draft Edge section / Task 4).

Data source: Fantasy Football Calculator's public REST API
(https://fantasyfootballcalculator.com/adp -- documented at
https://help.fantasyfootballcalculator.com/article/42-adp-rest-api).

WHY THIS SOURCE (flagged per the task -- there wasn't an obvious first
choice, so this is a deliberate pick, not a default):
  - Sleeper (the source already used for `players`) does NOT expose an
    aggregated ADP endpoint -- its public API is read-only over a specific
    user's leagues/drafts/rosters. You could reconstruct an ADP by pulling
    every public league's draft picks and aggregating client-side, but
    that's a much heavier, rate-limited, multi-endpoint crawl for the same
    end result FFC already publishes directly.
  - FFC's API is free, unauthenticated, returns clean JSON (no scraping),
    updates ~daily, and -- verified directly against the live endpoint
    while building this script -- ALREADY has real 2026 preseason data
    (2,448 mock drafts from 2026-07-13 to 2026-07-20 as of this writing).
  - It supports a `format` param (standard/ppr/half-ppr/etc.) -- PPR is
    used here since this league scores a full point per reception
    (config/league_scoring_rules.py: receiving.reception = 1).
  - Team abbreviations match `teams.abbreviation` EXACTLY (verified: 0 of
    32 mismatches either direction) -- DEF entries are matched by team
    only, no name parsing needed. Skill-position names matched our
    `players.full_name` at 195/196 (99.5%) on a straight normalized
    (name, team) key -- the one miss (an accented name) is fixed here via
    unicode normalization.

ASSUMPTION FLAGGED: FFC's ADP is scoped by league size (`teams` param).
This league's actual roster count isn't tracked anywhere yet (no
completed drafts in `user_roster` to infer it from) -- defaults to a
standard 12-team league. Revisit (just change TEAMS below) once the real
league size is known.

Idempotent, same manual insert/update pattern as pipeline/seed_players.py
(there is no unique constraint on `adp` enabling a real upsert, unlike
edge_scores -- see PROJECT_LOG.md Phase 4.7). Re-running this updates
adp_value/fetched_at in place for players already matched, rather than
accumulating duplicate rows.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.supabase_client import get_supabase_client

FFC_ADP_URL = "https://fantasyfootballcalculator.com/api/v1/adp/{format}"
ADP_FORMAT = "ppr"     # matches this league's full-PPR scoring
ADP_TEAMS = 12         # see ASSUMPTION FLAGGED above
ADP_YEAR = 2026
SOURCE = "fantasyfootballcalculator"

FFC_POSITION_TO_OURS = {"PK": "K"}  # FFC calls kickers "PK"; everything else matches
PAGE_SIZE = 1000
BATCH_SIZE = 200

SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b")
NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")
WHITESPACE_RE = re.compile(r"\s+")


def _normalize_name(name: str) -> str:
    """Lowercase, strip accents/punctuation/suffixes, collapse whitespace -- for cross-source name matching."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    name = name.lower()
    name = SUFFIX_RE.sub(" ", name)
    name = NON_ALNUM_RE.sub(" ", name)
    return WHITESPACE_RE.sub(" ", name).strip()


def _chunks(items: list[dict], size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _fetch_ffc_adp() -> dict[str, Any]:
    url = FFC_ADP_URL.format(format=ADP_FORMAT)
    response = requests.get(url, params={"teams": ADP_TEAMS, "year": ADP_YEAR}, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("status") != "Success":
        raise RuntimeError(f"FFC ADP API returned non-success status: {payload.get('status')!r}")
    return payload


def _fetch_all_players(client) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []
    offset = 0
    while True:
        result = (
            client.table("players")
            .select("player_id, full_name, position, team_id")
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
    result = client.table("teams").select("team_id, abbreviation").eq("sport", "nfl").execute()
    return {row["team_id"]: row["abbreviation"] for row in (result.data or [])}


def _fetch_existing_adp(client) -> dict[str, str]:
    """player_id -> adp row `id`, for rows already sourced from FFC (so re-runs update, not duplicate)."""
    existing: dict[str, str] = {}
    offset = 0
    while True:
        result = (
            client.table("adp")
            .select("id, player_id, source")
            .eq("source", SOURCE)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            break
        for row in rows:
            existing[row["player_id"]] = row["id"]
        if len(rows) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return existing


def _build_player_lookup(
    players: list[dict[str, Any]], abbr_by_team_id: dict[str, str]
) -> tuple[dict[tuple[str, str], dict], dict[str, str], dict[str, list[dict]]]:
    """
    (normalized_name, team_abbr) -> player row, for skill positions.
    team_abbr -> DEF player_id, for team defenses.
    normalized_name -> [rows], for the name-only fallback match.
    """
    by_name_team: dict[tuple[str, str], dict] = {}
    def_by_team_abbr: dict[str, str] = {}
    by_name_only: dict[str, list[dict]] = {}

    for p in players:
        abbr = abbr_by_team_id.get(p.get("team_id"))
        if p["position"] == "DEF":
            if abbr:
                def_by_team_abbr[abbr] = p["player_id"]
            continue
        norm = _normalize_name(p["full_name"])
        if abbr:
            by_name_team[(norm, abbr)] = p
        by_name_only.setdefault(norm, []).append(p)

    return by_name_team, def_by_team_abbr, by_name_only


def seed_adp() -> None:
    client = get_supabase_client()

    print(f"Fetching FFC ADP: format={ADP_FORMAT}, teams={ADP_TEAMS}, year={ADP_YEAR} ...")
    payload = _fetch_ffc_adp()
    meta = payload["meta"]
    ffc_players = payload["players"]
    print(
        f"Fetched {len(ffc_players)} ADP rows "
        f"({meta['total_drafts']} drafts, {meta['start_date']} to {meta['end_date']})."
    )

    players = _fetch_all_players(client)
    abbr_by_team_id = _fetch_team_abbreviation_map(client)
    by_name_team, def_by_team_abbr, by_name_only = _build_player_lookup(players, abbr_by_team_id)
    existing_adp = _fetch_existing_adp(client)
    print(f"Loaded {len(players)} players, {len(existing_adp)} existing {SOURCE!r} adp rows.")

    to_insert: list[dict[str, Any]] = []
    to_update: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    matched_by_position: dict[str, int] = {}
    fallback_used = 0

    for fp in ffc_players:
        ffc_position = FFC_POSITION_TO_OURS.get(fp["position"], fp["position"])
        player_id: str | None = None

        if ffc_position == "DEF":
            player_id = def_by_team_abbr.get(fp["team"])
        else:
            norm = _normalize_name(fp["name"])
            match = by_name_team.get((norm, fp["team"]))
            if match is None:
                candidates = by_name_only.get(norm) or []
                if len(candidates) == 1:
                    match = candidates[0]
                    fallback_used += 1
            if match is not None:
                player_id = match["player_id"]

        if player_id is None:
            unmatched.append(fp)
            continue

        matched_by_position[ffc_position] = matched_by_position.get(ffc_position, 0) + 1
        row = {"player_id": player_id, "adp_value": fp["adp"], "source": SOURCE}

        existing_id = existing_adp.get(player_id)
        if existing_id:
            to_update.append({**row, "id": existing_id})
        else:
            to_insert.append(row)

    inserted = 0
    for chunk in _chunks(to_insert):
        result = client.table("adp").insert(chunk).execute()
        inserted += len(result.data or [])

    updated = 0
    for row in to_update:
        client.table("adp").update(
            {"adp_value": row["adp_value"], "source": row["source"]}
        ).eq("id", row["id"]).execute()
        updated += 1

    print("\n=== ADP SEED SUMMARY ===")
    print(f"Matched: {sum(matched_by_position.values())} / {len(ffc_players)} FFC rows.")
    print(f"  by position: {matched_by_position}")
    print(f"  matched via name-only fallback (team mismatch): {fallback_used}")
    print(f"Inserted: {inserted}. Updated: {updated}.")
    print(f"Unmatched: {len(unmatched)}")
    if unmatched:
        for u in unmatched:
            print(f"  {u['name']!r} ({u['position']}, {u['team']}) adp={u['adp']}")


if __name__ == "__main__":
    seed_adp()
