"""Seed the Supabase teams table with all 32 NFL franchises."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.supabase_client import get_supabase_client

NFL_TEAMS: list[dict] = [
    # AFC East
    {"name": "Buffalo Bills", "abbreviation": "BUF", "conference": "AFC", "division": "East", "bye_week": None},
    {"name": "Miami Dolphins", "abbreviation": "MIA", "conference": "AFC", "division": "East", "bye_week": None},
    {"name": "New England Patriots", "abbreviation": "NE", "conference": "AFC", "division": "East", "bye_week": None},
    {"name": "New York Jets", "abbreviation": "NYJ", "conference": "AFC", "division": "East", "bye_week": None},
    # AFC North
    {"name": "Baltimore Ravens", "abbreviation": "BAL", "conference": "AFC", "division": "North", "bye_week": None},
    {"name": "Cincinnati Bengals", "abbreviation": "CIN", "conference": "AFC", "division": "North", "bye_week": None},
    {"name": "Cleveland Browns", "abbreviation": "CLE", "conference": "AFC", "division": "North", "bye_week": None},
    {"name": "Pittsburgh Steelers", "abbreviation": "PIT", "conference": "AFC", "division": "North", "bye_week": None},
    # AFC South
    {"name": "Houston Texans", "abbreviation": "HOU", "conference": "AFC", "division": "South", "bye_week": None},
    {"name": "Indianapolis Colts", "abbreviation": "IND", "conference": "AFC", "division": "South", "bye_week": None},
    {"name": "Jacksonville Jaguars", "abbreviation": "JAX", "conference": "AFC", "division": "South", "bye_week": None},
    {"name": "Tennessee Titans", "abbreviation": "TEN", "conference": "AFC", "division": "South", "bye_week": None},
    # AFC West
    {"name": "Denver Broncos", "abbreviation": "DEN", "conference": "AFC", "division": "West", "bye_week": None},
    {"name": "Kansas City Chiefs", "abbreviation": "KC", "conference": "AFC", "division": "West", "bye_week": None},
    {"name": "Las Vegas Raiders", "abbreviation": "LV", "conference": "AFC", "division": "West", "bye_week": None},
    {"name": "Los Angeles Chargers", "abbreviation": "LAC", "conference": "AFC", "division": "West", "bye_week": None},
    # NFC East
    {"name": "Dallas Cowboys", "abbreviation": "DAL", "conference": "NFC", "division": "East", "bye_week": None},
    {"name": "New York Giants", "abbreviation": "NYG", "conference": "NFC", "division": "East", "bye_week": None},
    {"name": "Philadelphia Eagles", "abbreviation": "PHI", "conference": "NFC", "division": "East", "bye_week": None},
    {"name": "Washington Commanders", "abbreviation": "WAS", "conference": "NFC", "division": "East", "bye_week": None},
    # NFC North
    {"name": "Chicago Bears", "abbreviation": "CHI", "conference": "NFC", "division": "North", "bye_week": None},
    {"name": "Detroit Lions", "abbreviation": "DET", "conference": "NFC", "division": "North", "bye_week": None},
    {"name": "Green Bay Packers", "abbreviation": "GB", "conference": "NFC", "division": "North", "bye_week": None},
    {"name": "Minnesota Vikings", "abbreviation": "MIN", "conference": "NFC", "division": "North", "bye_week": None},
    # NFC South
    {"name": "Atlanta Falcons", "abbreviation": "ATL", "conference": "NFC", "division": "South", "bye_week": None},
    {"name": "Carolina Panthers", "abbreviation": "CAR", "conference": "NFC", "division": "South", "bye_week": None},
    {"name": "New Orleans Saints", "abbreviation": "NO", "conference": "NFC", "division": "South", "bye_week": None},
    {"name": "Tampa Bay Buccaneers", "abbreviation": "TB", "conference": "NFC", "division": "South", "bye_week": None},
    # NFC West
    {"name": "Arizona Cardinals", "abbreviation": "ARI", "conference": "NFC", "division": "West", "bye_week": None},
    {"name": "Los Angeles Rams", "abbreviation": "LAR", "conference": "NFC", "division": "West", "bye_week": None},
    {"name": "San Francisco 49ers", "abbreviation": "SF", "conference": "NFC", "division": "West", "bye_week": None},
    {"name": "Seattle Seahawks", "abbreviation": "SEA", "conference": "NFC", "division": "West", "bye_week": None},
]


def seed_teams() -> None:
    client = get_supabase_client()
    sport = "nfl"

    existing = (
        client.table("teams")
        .select("abbreviation")
        .eq("sport", sport)
        .execute()
    )
    existing_abbreviations = {row["abbreviation"] for row in (existing.data or [])}

    to_insert = []
    skipped = 0
    for team in NFL_TEAMS:
        if team["abbreviation"] in existing_abbreviations:
            skipped += 1
            continue
        to_insert.append({**team, "sport": sport})

    inserted = 0
    if to_insert:
        result = client.table("teams").insert(to_insert).execute()
        inserted = len(result.data or [])

    print(
        f"NFL teams seed complete: {inserted} inserted, {skipped} skipped "
        f"({len(NFL_TEAMS)} total)."
    )


if __name__ == "__main__":
    seed_teams()
