"""
Vegas-derived features for the Edge scoring engine.
Reads already-ingested implied scores from the `games` table (populated during
odds ingestion in fetch_odds_game_lines.py) and derives spread + game-script.
"""


def get_game_row(client, game_id: str) -> dict:
    """Fetch a single game's relevant columns by game_id."""
    result = (
        client.table("games")
        .select(
            "game_id, home_team_id, away_team_id, game_total, "
            "implied_home_score, implied_away_score"
        )
        .eq("game_id", game_id)
        .single()
        .execute()
    )
    return result.data


def team_implied_total(game_row: dict, team_id: str) -> float | None:
    """
    Returns the de-vig'd implied point total for the given team in this game.
    Already computed and stored on the games row during odds ingestion.
    """
    if team_id == game_row["home_team_id"]:
        return game_row["implied_home_score"]
    elif team_id == game_row["away_team_id"]:
        return game_row["implied_away_score"]
    else:
        raise ValueError(f"team_id {team_id} is not part of game {game_row['game_id']}")


def team_spread(game_row: dict, team_id: str) -> float | None:
    """
    Derives the team's point spread from game_total and its implied score.
    Negative spread = favored, positive = underdog.
    Inverse of: implied_total = (game_total / 2) - (spread / 2)
             => spread = game_total - 2 * implied_total
    """
    implied = team_implied_total(game_row, team_id)
    game_total = game_row.get("game_total")
    if implied is None or game_total is None:
        return None
    return game_total - 2 * implied


def game_script(spread: float | None, cap: float = 3.0) -> float:
    """
    Normalizes point spread into "touchdowns of spread," capped at +/- `cap`.
    Positive = underdog (more expected passing volume).
    Negative = favorite (more expected rushing volume).
    """
    if spread is None:
        return 0.0
    g = spread / 7
    return max(-cap, min(cap, g))