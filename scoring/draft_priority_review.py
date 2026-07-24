"""
Draft Priority Score (DPS) review — read-only comparison vs ADP and Draft Edge.

Computation lives in scoring/draft_priority_score.py (shared with production write).
This module is the read-only board printer only — no database writes.
"""

from __future__ import annotations

import sys

from config.supabase_client import get_supabase_client
from scoring.compute_draft_edge import (
    PERIOD,
    SCORE_TYPE,
    _get_def_player_by_team,
    fetch_players_by_names,
    load_draft_edge_context,
)
from scoring.draft_priority_score import (
    DPS_POSITIONS,
    PAGE_SIZE,
    W_TARGET_COMPARISON,
    _apply_z_scores_and_dps,
    _build_player_records,
    _build_team_game_stats,
    _build_team_targets_by_team,
    _derive_w_target,
    _rank_by_adp,
    _rank_by_dps,
    _recompute_rb_role_shifts,
    DELTA_ROLE_WEIGHT,
    DELTA_XTD_WEIGHT,
    LAMBDA_POS,
    LAMBDA_WR,
)


ADP_SPOTLIGHT_NAMES = (
    "Ja'Marr Chase",
    "Bijan Robinson",
    "Josh Allen",
    "Lamar Jackson",
    "Puka Nacua",
    "Brock Bowers",
    "Saquon Barkley",
)
ADP_SPOTLIGHT_ALIASES = {"Brock Bowders": "Brock Bowers"}


def _fetch_all_adp_rows(client) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        result = (
            client.table("adp")
            .select("player_id, adp_value, fetched_at")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def _fetch_draft_edge_ranks(client) -> dict[str, int]:
    ranks: dict[str, int] = {}
    offset = 0
    while True:
        result = (
            client.table("edge_scores")
            .select("player_id, positional_rank")
            .eq("score_type", SCORE_TYPE)
            .eq("period", PERIOD)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = result.data or []
        for row in batch:
            if row.get("positional_rank") is not None:
                ranks[row["player_id"]] = row["positional_rank"]
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return ranks


def _board_header(position: str) -> str:
    if position == "WR":
        return (
            f"{'DPS#':>4} | {'Player':<26} | {'ADP#':>4} | {'DE#':>4} | {'ADP':>6} | "
            f"{'DPS':>7} | {'Delta':>6} | {'Z_xTD':>6} | {'Z_Role':>6} | {'Z_Avail':>7} | "
            f"{'Move':>5} | Flags"
        )
    return (
        f"{'DPS#':>4} | {'Player':<26} | {'ADP#':>4} | {'DE#':>4} | {'ADP':>6} | "
        f"{'DPS':>7} | {'Delta':>6} | {'Z_xTD':>6} | {'Z_Role':>6} | {'Move':>5} | Flags"
    )


def _format_board_row(rec: dict, position: str, draft_edge_ranks: dict[str, int]) -> str:
    de_rank = draft_edge_ranks.get(rec["player_id"])
    de_str = str(de_rank) if de_rank is not None else "-"
    # Review boards historically omit no_adjustment from flags display for TE.
    flags = []
    if rec.get("no_2025_data"):
        flags.append("no_2025_data")
    if rec.get("context_changed"):
        flags.append("context_changed")
    if rec.get("low_sample"):
        flags.append("low_sample")
    flags_s = ",".join(flags) if flags else "-"
    if position == "WR":
        return (
            f"{rec['dps_rank']:>4} | {rec['name']:<26} | {rec['adp_rank']:>4} | {de_str:>4} | "
            f"{rec['adp']:>6.1f} | {rec['dps']:>7.2f} | {rec['delta']:>6.3f} | "
            f"{rec['z_xtd']:>6.2f} | {rec['z_role']:>6.2f} | {rec['z_avail']:>7.2f} | "
            f"{rec['rank_move']:>+5} | {flags_s}"
        )
    return (
        f"{rec['dps_rank']:>4} | {rec['name']:<26} | {rec['adp_rank']:>4} | {de_str:>4} | "
        f"{rec['adp']:>6.1f} | {rec['dps']:>7.2f} | {rec['delta']:>6.3f} | "
        f"{rec['z_xtd']:>6.2f} | {rec['z_role']:>6.2f} | {rec['rank_move']:>+5} | "
        f"{flags_s}"
    )


def _print_board_table(
    records: list[dict],
    position: str,
    draft_edge_ranks: dict[str, int],
    *,
    title: str,
    top_n: int = 30,
) -> None:
    group = [r for r in records if r["position"] == position]
    group.sort(key=lambda r: r["dps_rank"])

    header = _board_header(position)
    print(f"\n{'=' * len(header)}")
    print(title)
    print(header)
    print("-" * len(header))

    for rec in group[:top_n]:
        print(_format_board_row(rec, position, draft_edge_ranks))


def _print_no_2025_data_summary(records: list[dict], no_data_counts: dict[str, int]) -> None:
    print("\nno_2025_data players added (Delta=0, DPS=ADP, excluded from z-score sample):")
    for position in DPS_POSITIONS:
        print(f"  {position}: {no_data_counts.get(position, 0)}")

    no_data = [r for r in records if r.get("no_2025_data")]
    top5 = sorted(no_data, key=lambda r: r["adp"])[:5]
    print("\nTop 5 no_2025_data players by ADP (where they land on DPS board):")
    if not top5:
        print("  (none)")
        return
    for rec in top5:
        print(
            f"  {rec['name']:<26} ADP={rec['adp']:>6.1f}  ADP#{rec['adp_rank']:>3}  "
            f"DPS#{rec['dps_rank']:>3}  move={rec['rank_move']:>+3}"
        )


def _print_position_board(
    records: list[dict],
    position: str,
    draft_edge_ranks: dict[str, int],
    sample_n: int,
    *,
    top_n: int = 30,
) -> None:
    group = [r for r in records if r["position"] == position]
    group.sort(key=lambda r: r["dps_rank"])

    header = _board_header(position)
    print(f"\n{'=' * len(header)}")
    print(f"{position} — top {top_n} by DPS (lower DPS = draft earlier)")
    print(header)
    print("-" * len(header))

    for rec in group[:top_n]:
        print(_format_board_row(rec, position, draft_edge_ranks))

    _print_movers(records, position, n=5)
    print(f"\n{position} z-score sample: n={sample_n}")


def _print_movers(records: list[dict], position: str, *, n: int = 5) -> None:
    group = [r for r in records if r["position"] == position]

    risers = sorted(group, key=lambda r: r["rank_move"], reverse=True)[:n]
    fallers = sorted(group, key=lambda r: r["rank_move"])[:n]

    print(f"\n{position} — top {n} RISERS vs ADP (rank_move = ADP# - DPS#)")
    for rec in risers:
        flags = []
        if rec.get("no_2025_data"):
            flags.append("no_2025_data")
        if rec.get("context_changed"):
            flags.append("context_changed")
        if rec.get("low_sample"):
            flags.append("low_sample")
        flags_s = ",".join(flags) if flags else "-"
        print(
            f"  {rec['name']:<26} ADP#{rec['adp_rank']:>3} -> DPS#{rec['dps_rank']:>3} "
            f"move={rec['rank_move']:>+3}  DPS={rec['dps']:.2f}  flags={flags_s}"
        )

    print(f"\n{position} — top {n} FALLERS vs ADP")
    for rec in fallers:
        flags = []
        if rec.get("no_2025_data"):
            flags.append("no_2025_data")
        if rec.get("context_changed"):
            flags.append("context_changed")
        if rec.get("low_sample"):
            flags.append("low_sample")
        flags_s = ",".join(flags) if flags else "-"
        print(
            f"  {rec['name']:<26} ADP#{rec['adp_rank']:>3} -> DPS#{rec['dps_rank']:>3} "
            f"move={rec['rank_move']:>+3}  DPS={rec['dps']:.2f}  flags={flags_s}"
        )


def main() -> None:
    positions_filter: tuple[str, ...] | None = None
    if len(sys.argv) > 1:
        arg = sys.argv[1].upper()
        if arg in DPS_POSITIONS:
            positions_filter = (arg,)

    print("Draft Priority Score review — read-only, no database writes.\n")

    client = get_supabase_client()
    context = load_draft_edge_context(client)
    def_player_by_team = _get_def_player_by_team(client)
    team_targets_by_team = _build_team_targets_by_team(
        context.season_games_by_player, def_player_by_team,
    )
    team_game_stats = _build_team_game_stats(
        context.season_games_by_player, def_player_by_team,
    )

    ppc, ppt, w_target_derived, w_target_rb_n = _derive_w_target(context)
    print("RB W_TARGET derivation (>= 50 carries in 2025, full PPR scoring rules)")
    print("-" * 60)
    print(f"  RBs in sample:     {w_target_rb_n}")
    print(f"  points/carry:      {ppc:.4f}")
    print(f"  points/target:     {ppt:.4f}")
    print(f"  W_TARGET (derived): {w_target_derived:.4f}")

    records, excluded_no_adp, no_data_counts, missing_inputs = _build_player_records(
        context,
        w_target=w_target_derived,
        team_targets_by_team=team_targets_by_team,
        team_game_stats=team_game_stats,
        positions=DPS_POSITIONS,
    )
    print(f"\nExcluded (draft pool, no ADP): {excluded_no_adp}")
    if missing_inputs:
        print(f"Missing inputs substituted: n={len(missing_inputs)}")
        for line in missing_inputs[:20]:
            print(f"  {line}")

    draft_edge_ranks = _fetch_draft_edge_ranks(client)

    sample_sizes = _apply_z_scores_and_dps(records, positions=DPS_POSITIONS)
    _rank_by_adp(records, positions=DPS_POSITIONS)
    _rank_by_dps(records, positions=DPS_POSITIONS)

    positions = positions_filter or DPS_POSITIONS

    if "RB" in positions:
        _print_board_table(
            records,
            "RB",
            draft_edge_ranks,
            title=f"RB — top 30 by DPS (W_TARGET={w_target_derived:.4f}, lower DPS = draft earlier)",
        )
        _print_no_2025_data_summary(records, no_data_counts)

        _recompute_rb_role_shifts(
            records, w_target=W_TARGET_COMPARISON, team_targets_by_team=team_targets_by_team,
        )
        _apply_z_scores_and_dps(records, positions=DPS_POSITIONS)
        _rank_by_dps(records, positions=DPS_POSITIONS)
        _print_board_table(
            records,
            "RB",
            draft_edge_ranks,
            title=f"RB — top 30 by DPS (W_TARGET={W_TARGET_COMPARISON} comparison, lower DPS = draft earlier)",
        )
        print(f"\nRB z-score sample (2025-data only): n={sample_sizes.get('RB', 0)}")

    for position in positions:
        if position == "RB":
            continue
        _print_position_board(
            records, position, draft_edge_ranks, sample_sizes.get(position, 0),
        )

    if "RB" not in positions:
        _print_no_2025_data_summary(records, no_data_counts)


if __name__ == "__main__":
    main()
