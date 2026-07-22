"""
Draft Edge shrinkage calibration review — read-only, no edge_scores writes.

Runs RB1–40 at three SHRINKAGE_STRENGTH values for user review before committing.
"""

from __future__ import annotations

from config.supabase_client import get_supabase_client
from scoring.compute_draft_edge import compute_draft_edge_projections, load_draft_edge_context

STRENGTHS = {
    "low": 3.0,
    "moderate": 8.0,
    "high": 18.0,
}

SPOTLIGHT_NAMES = ("Christian McCaffrey", "Kyler Murray", "Jeremiyah Love", "Jadarian Price")


def _rank_rbs(players: list[dict], all_projections: dict[str, dict]) -> list[tuple[int, dict, dict]]:
    players_by_id = {p["player_id"]: p for p in players}
    rb_pids = [p["player_id"] for p in players if p["position"] == "RB"]
    sorted_pids = sorted(rb_pids, key=lambda pid: all_projections[pid]["points"], reverse=True)
    return [
        (rank + 1, players_by_id[pid], all_projections[pid])
        for rank, pid in enumerate(sorted_pids)
    ]


def _find_by_name(players: list[dict], name: str) -> dict | None:
    for p in players:
        if p["full_name"] == name:
            return p
    return None


def _rb10_25_slope(ranked: list[tuple[int, dict, dict]]) -> float:
    """Average point drop per rank slot across RB10–RB25 (tiebreaker metric)."""
    pts = [proj["points"] for rank, _, proj in ranked if 10 <= rank <= 25]
    if len(pts) < 2:
        return 0.0
    total_drop = pts[0] - pts[-1]
    return total_drop / (len(pts) - 1)


def run_review() -> None:
    client = get_supabase_client()
    print("Loading draft pool + 2025 season data (one-time fetch)...")
    context = load_draft_edge_context(client)
    print(f"Loaded {len(context.players)} players.\n")

    for label, strength in STRENGTHS.items():
        print(f"\n{'=' * 72}")
        print(f"SHRINKAGE_STRENGTH = {strength} ({label})")
        print("=" * 72)

        players, all_projections, _ = compute_draft_edge_projections(
            shrinkage_strength=strength,
            context=context,
        )
        ranked = _rank_rbs(players, all_projections)

        print(f"\nRB1–40 (placeholder = no_historical_data):")
        print(f"{'Rank':>4}  {'Name':<28} {'Pts':>8}  {'Flags'}")
        print("-" * 72)
        for rank, player, proj in ranked[:40]:
            flags = []
            feats = proj["features"]
            if feats.get("no_historical_data"):
                flags.append("PLACEHOLDER")
            if feats.get("low_sample"):
                flags.append("low_sample")
            if feats.get("context_changed"):
                flags.append("context_changed")
            flag_str = ", ".join(flags) if flags else "-"
            print(f"{rank:>4}  {player['full_name']:<28} {proj['points']:>8.2f}  {flag_str}")

        slope = _rb10_25_slope(ranked)
        print(f"\nRB10–25 avg drop per rank: {slope:.2f} pts")

        print("\nSpotlight players:")
        for name in SPOTLIGHT_NAMES:
            player = _find_by_name(players, name)
            if not player:
                print(f"  {name}: not found in draft pool")
                continue
            proj = all_projections[player["player_id"]]
            pos = player["position"]
            if pos == "RB":
                rank = next(r for r, p, _ in ranked if p["player_id"] == player["player_id"])
                rank_label = f"RB#{rank}"
            else:
                pos_pids = [p["player_id"] for p in players if p["position"] == pos]
                pos_sorted = sorted(pos_pids, key=lambda pid: all_projections[pid]["points"], reverse=True)
                rank = pos_sorted.index(player["player_id"]) + 1
                rank_label = f"{pos}#{rank}"
            flags = []
            feats = proj["features"]
            if feats.get("no_historical_data"):
                flags.append("PLACEHOLDER")
            if feats.get("low_sample"):
                flags.append("low_sample")
            if feats.get("context_changed"):
                flags.append("context_changed")
            print(f"  {name}: {rank_label}, {proj['points']:.2f} pts [{', '.join(flags) or 'none'}]")


if __name__ == "__main__":
    run_review()
