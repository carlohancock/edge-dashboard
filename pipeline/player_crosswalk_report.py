"""DRY RUN: report how many `players` rows have a matching nflverse player id.

Builds a lookup from players.external_ids->>'sleeper_id' to nflverse's
gsis_id (the id used by nfl_data_py's weekly stats tables) via nflverse's
community ID crosswalk (dynastyprocess/data, exposed as nfl.import_ids()).

This script writes NOTHING to the database. It only prints a coverage
report so you can review who matched and who didn't before Step 3 backfills
actual stats.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.supabase_client import get_supabase_client  # noqa: E402
from pipeline.nflverse_crosswalk import build_sleeper_to_gsis_lookup  # noqa: E402

PAGE_SIZE = 1000

# nflverse's crosswalk only covers individual athletes; team defenses have
# no gsis_id here. Step 3 matches DEF stats by team abbreviation instead,
# so DEF misses are expected and reported separately, not as real gaps.
EXPECTED_NON_MATCH_POSITIONS = frozenset({"DEF"})

# A "contributor" miss is one worth actually worrying about: a starter/
# primary-backup depth chart slot, or a player currently marked active.
CONTRIBUTOR_DEPTH_CHART_RANKS = frozenset({1, 2, 3})
CONTRIBUTOR_STATUS = "active"


def _fetch_all_players(client) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []
    offset = 0

    while True:
        result = (
            client.table("players")
            .select(
                "player_id, full_name, position, team_id, depth_chart_rank, "
                "status, external_ids"
            )
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
    """team_id -> abbreviation, so miss reports can show a readable team code."""
    result = (
        client.table("teams")
        .select("team_id, abbreviation")
        .eq("sport", "nfl")
        .execute()
    )
    return {row["team_id"]: row["abbreviation"] for row in (result.data or [])}


def _is_contributor(player: dict[str, Any]) -> bool:
    depth_chart_rank = player.get("depth_chart_rank")
    status = (player.get("status") or "").strip().lower()
    return depth_chart_rank in CONTRIBUTOR_DEPTH_CHART_RANKS or status == CONTRIBUTOR_STATUS


def build_crosswalk_report() -> None:
    client = get_supabase_client()

    print("Fetching players from Supabase...")
    players = _fetch_all_players(client)
    team_by_id = _fetch_team_abbreviation_map(client)
    print(f"Fetched {len(players)} players (sport=nfl).")

    print("Fetching nflverse ID crosswalk (nfl_data_py.import_ids())...")
    sleeper_to_gsis = build_sleeper_to_gsis_lookup()
    print(f"Crosswalk has {len(sleeper_to_gsis)} unique sleeper_id entries.\n")

    matched: list[dict[str, Any]] = []
    no_sleeper_id: list[dict[str, Any]] = []
    sleeper_not_in_crosswalk: list[dict[str, Any]] = []
    found_but_no_gsis: list[dict[str, Any]] = []

    expected_non_match_count = 0

    for player in players:
        full_name = player.get("full_name")
        position = player.get("position")
        external_ids = player.get("external_ids") or {}
        sleeper_id = external_ids.get("sleeper_id")

        if sleeper_id is None:
            no_sleeper_id.append(player)
            continue

        sleeper_id = str(sleeper_id)
        entry = sleeper_to_gsis.get(sleeper_id)

        if entry is None:
            if position in EXPECTED_NON_MATCH_POSITIONS:
                expected_non_match_count += 1
            else:
                sleeper_not_in_crosswalk.append(player)
            continue

        if not entry["gsis_id"]:
            if position in EXPECTED_NON_MATCH_POSITIONS:
                expected_non_match_count += 1
            else:
                found_but_no_gsis.append(player)
            continue

        matched.append(player)

    total = len(players)
    real_misses = no_sleeper_id + sleeper_not_in_crosswalk + found_but_no_gsis

    print("=== CROSSWALK COVERAGE REPORT (dry run — no DB writes) ===")
    print(f"Total players (sport=nfl):           {total}")
    print(f"Matched to a usable gsis_id:          {len(matched)} "
          f"({len(matched) / total:.1%})")
    print(f"Expected non-matches (DEF, team-level):{expected_non_match_count}")
    print(f"Real misses (excluding DEF):          {len(real_misses)}")
    print(f"  - no sleeper_id stored at all:        {len(no_sleeper_id)}")
    print(f"  - sleeper_id not found in crosswalk:  {len(sleeper_not_in_crosswalk)}")
    print(f"  - found in crosswalk, no gsis_id:     {len(found_but_no_gsis)}")
    print("============================================================\n")

    def _print_miss_group(title: str, group: list[dict[str, Any]]) -> None:
        print(f"--- {title} ({len(group)}) ---")
        for p in sorted(group, key=lambda p: (p.get("position") or "", p.get("full_name") or "")):
            print(f"  {p.get('full_name'):30s} {p.get('position')}")
        print()

    _print_miss_group("MISS: no sleeper_id stored", no_sleeper_id)
    _print_miss_group("MISS: sleeper_id not in nflverse crosswalk", sleeper_not_in_crosswalk)
    _print_miss_group("MISS: in crosswalk but no gsis_id", found_but_no_gsis)

    contributor_misses = [p for p in real_misses if _is_contributor(p)]
    top3_misses = [
        p for p in real_misses if p.get("depth_chart_rank") in CONTRIBUTOR_DEPTH_CHART_RANKS
    ]
    print(
        f"=== CONTRIBUTOR-ONLY MISSES (depth_chart_rank in "
        f"{sorted(CONTRIBUTOR_DEPTH_CHART_RANKS)} OR status='{CONTRIBUTOR_STATUS}') "
        f"({len(contributor_misses)} of {len(real_misses)} real misses) ==="
    )
    print(
        f"NOTE: {sum(1 for p in real_misses if (p.get('status') or '').lower() == CONTRIBUTOR_STATUS)} "
        f"of {len(real_misses)} misses are status='active' (that field just means "
        f"'on a roster', not 'starter' — 999/1023 players overall are Active, so it "
        f"barely narrows anything down). The more informative cut is depth_chart_rank "
        f"in {sorted(CONTRIBUTOR_DEPTH_CHART_RANKS)} alone: only "
        f"{len(top3_misses)} misses qualify. That shorter list is printed below.\n"
    )
    if not contributor_misses:
        print("  None — every real miss is a deep-bench/inactive player.\n")
    else:
        header = f"  {'full_name':30s} {'position':4s} {'team':5s} {'dc_rank':8s} {'status':10s}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for p in sorted(
            contributor_misses,
            key=lambda p: (p.get("position") or "", p.get("full_name") or ""),
        ):
            team_abbr = team_by_id.get(p.get("team_id"), "?")
            dc_rank = p.get("depth_chart_rank")
            dc_rank_str = str(dc_rank) if dc_rank is not None else "-"
            status = p.get("status") or "-"
            print(
                f"  {p.get('full_name') or '?':30s} {p.get('position') or '?':4s} "
                f"{team_abbr:5s} {dc_rank_str:8s} {status:10s}"
            )
        print()

    print(
        f"=== TOP-3 DEPTH CHART MISSES ONLY (depth_chart_rank in "
        f"{sorted(CONTRIBUTOR_DEPTH_CHART_RANKS)}, ignoring status) "
        f"({len(top3_misses)} of {len(real_misses)} real misses) ==="
    )
    if not top3_misses:
        print("  None — no starter/primary-backup-level player is missing a match.\n")
    else:
        header = f"  {'full_name':30s} {'position':4s} {'team':5s} {'dc_rank':8s} {'status':10s}"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for p in sorted(
            top3_misses,
            key=lambda p: (p.get("depth_chart_rank") or 0, p.get("position") or ""),
        ):
            team_abbr = team_by_id.get(p.get("team_id"), "?")
            dc_rank_str = str(p.get("depth_chart_rank"))
            status = p.get("status") or "-"
            print(
                f"  {p.get('full_name') or '?':30s} {p.get('position') or '?':4s} "
                f"{team_abbr:5s} {dc_rank_str:8s} {status:10s}"
            )
        print()

    print("This is a dry run — nothing was written to the database.")


if __name__ == "__main__":
    build_crosswalk_report()
