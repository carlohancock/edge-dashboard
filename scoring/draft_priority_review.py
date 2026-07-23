"""
Draft Priority Score (DPS) review — read-only comparison vs ADP and Draft Edge.

  DPS_i = ADP_i - (lambda_pos * Delta_i)
  Delta_i = 0.44 * Z_xTD_i + 0.56 * Z_Role_i

All v1 constants below are hand-set starting values, flagged for empirical
fit against 2025→2026 outcome pairs — same status as SHRINKAGE_STRENGTH in
season_stats.py. No database writes.
"""

from __future__ import annotations

import statistics
import sys

from config.supabase_client import get_supabase_client
from scoring.compute_draft_edge import (
    PERIOD,
    SCORE_TYPE,
    fetch_players_by_names,
    load_draft_edge_context,
)
from scoring.draft_edge_features import (
    DEFAULT_QB_GAMES_ESTIMATE,
    DEFAULT_RB_RUSH_SHARE_PRIOR,
    DEFAULT_TE_TARGET_SHARE_PRIOR,
    DEFAULT_WR_TARGET_SHARE_PRIOR,
    DEFAULT_TEAM_PASS_ATTEMPTS_SEASON,
    DEFAULT_TEAM_RUSH_ATTEMPTS_SEASON,
    QB_GAMES_ESTIMATE_PRIOR,
    RB_RUSH_SHARE_PRIOR,
    TE_TARGET_SHARE_PRIOR,
    WR_TARGET_SHARE_PRIOR,
    _depth_chart_tier,
    _team_season_attempts,
)
from scoring.season_stats import (
    LEAGUE_MEAN_PASS_TD_RATE,
    LEAGUE_MEAN_RB_REC_TD_RATE,
    LEAGUE_MEAN_RUSH_TD_RATE,
    LEAGUE_MEAN_WR_TE_REC_TD_RATE,
    MIN_SEASON_GAMES,
    MIN_SEASON_SAMPLE,
    _lookup_baseline,
    aggregate_skill_season_totals,
    primary_team_id,
    season_regressed_rate,
)

# ---- v1 hand-set constants (flagged for empirical fit) ----
DELTA_XTD_WEIGHT = 0.44
DELTA_ROLE_WEIGHT = 0.56
# lambda_QB selected from a 4.0/8.0/12.0 sweep: QB ADP spacing is wide enough that
# Delta cannot meaningfully reorder the board at any reasonable lambda; 8.0 captures
# the consistent movers (Daniels, Murray, Darnold) without the top-end churn seen at
# 12.0. Flagged for empirical fit alongside the other lambdas.
LAMBDA_POS = {"QB": 8.0, "RB": 5.0, "WR": 8.0, "TE": 6.0}
CONTEXT_CHANGED_MULTIPLIER = 0.85
GAMES_NORMALIZER = 16.0

DPS_POSITIONS = ("QB", "RB", "WR", "TE")
PAGE_SIZE = 1000

ADP_SPOTLIGHT_NAMES = (
    "Ja'Marr Chase",
    "Bijan Robinson",
    "Josh Allen",
    "Lamar Jackson",
    "Puka Nacua",
    "Brock Bowers",
    "Saquon Barkley",
)
# User prompt typo alias — lookup optional
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
    """player_id -> positional_rank from stored draft_edge scores."""
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


def _z_scores(values: list[float]) -> list[float]:
    if len(values) <= 1:
        return [0.0] * len(values)
    mu = statistics.mean(values)
    sigma = statistics.pstdev(values)
    if sigma == 0:
        return [0.0] * len(values)
    return [(v - mu) / sigma for v in values]


def _context_multiplier(context_changed: bool) -> float:
    return CONTEXT_CHANGED_MULTIPLIER if context_changed else 1.0


def _low_sample(position: str, season_totals: dict) -> bool:
    gp = season_totals["games_played"]
    if gp < MIN_SEASON_GAMES:
        return True
    if position == "QB":
        return season_totals["attempts"] < MIN_SEASON_SAMPLE["pass_attempts"]
    if position == "RB":
        return (season_totals["carries"] + season_totals["targets"]) < MIN_SEASON_SAMPLE["carries"]
    if position in ("WR", "TE"):
        return season_totals["targets"] < MIN_SEASON_SAMPLE["targets"]
    return False


def _compute_xtd_delta(
    position: str,
    season_totals: dict,
    baselines: dict,
) -> float:
    gp = season_totals["games_played"]
    if position == "QB":
        attempts = season_totals["attempts"]
        pass_tds = season_totals["passing_tds"]
        rush_tds = season_totals["rushing_tds"]
        td_rate = season_regressed_rate(pass_tds, attempts, LEAGUE_MEAN_PASS_TD_RATE, "qb_pass_td")
        rush_prior = _lookup_baseline(baselines, "QB", None, "rush_tds_per_carry_prior", 0.0)
        carries = season_totals["carries"]
        rush_td_pc = season_regressed_rate(rush_tds, carries, rush_prior, "qb_rush_td")
        expected = attempts * td_rate + carries * rush_td_pc
        actual = pass_tds + rush_tds
        return expected - actual

    if position == "RB":
        carries = season_totals["carries"]
        targets = season_totals["targets"]
        rush_tds = season_totals["rushing_tds"]
        rec_tds = season_totals["receiving_tds"]
        rush_td_rate = season_regressed_rate(rush_tds, carries, LEAGUE_MEAN_RUSH_TD_RATE, "rb_rush_td")
        rec_td_rate = season_regressed_rate(rec_tds, targets, LEAGUE_MEAN_RB_REC_TD_RATE, "rb_rec_td")
        expected = carries * rush_td_rate + targets * rec_td_rate
        actual = rush_tds + rec_tds
        return expected - actual

    if position in ("WR", "TE"):
        targets = season_totals["targets"]
        rec_tds = season_totals["receiving_tds"]
        rec_td_rate = season_regressed_rate(rec_tds, targets, LEAGUE_MEAN_WR_TE_REC_TD_RATE, "wr_te_rec_td")
        expected = targets * rec_td_rate
        return expected - rec_tds

    return 0.0


def _compute_role_shift(
    player: dict,
    season_totals: dict,
    share_team_totals: dict,
    context_changed: bool,
) -> float:
    position = player["position"]
    depth_chart_rank = player.get("depth_chart_rank")
    gp = season_totals["games_played"]
    mult = _context_multiplier(context_changed)

    if position == "QB":
        proj_games = _depth_chart_tier(depth_chart_rank, QB_GAMES_ESTIMATE_PRIOR, DEFAULT_QB_GAMES_ESTIMATE)
        return ((proj_games - gp) / GAMES_NORMALIZER) * mult

    if position == "RB":
        # v1: rush_share only (not blended touch share)
        share_team_carries = _team_season_attempts(
            share_team_totals, "carries", DEFAULT_TEAM_RUSH_ATTEMPTS_SEASON,
        )
        observed = (season_totals["carries"] / share_team_carries) if share_team_carries else 0.0
        expected = _depth_chart_tier(depth_chart_rank, RB_RUSH_SHARE_PRIOR, DEFAULT_RB_RUSH_SHARE_PRIOR)
        return (expected - observed) * mult

    if position == "WR":
        share_team_attempts = _team_season_attempts(
            share_team_totals, "attempts", DEFAULT_TEAM_PASS_ATTEMPTS_SEASON,
        )
        observed = (season_totals["targets"] / share_team_attempts) if share_team_attempts else 0.0
        expected = _depth_chart_tier(depth_chart_rank, WR_TARGET_SHARE_PRIOR, DEFAULT_WR_TARGET_SHARE_PRIOR)
        return (expected - observed) * mult

    if position == "TE":
        share_team_attempts = _team_season_attempts(
            share_team_totals, "attempts", DEFAULT_TEAM_PASS_ATTEMPTS_SEASON,
        )
        observed = (season_totals["targets"] / share_team_attempts) if share_team_attempts else 0.0
        expected = _depth_chart_tier(depth_chart_rank, TE_TARGET_SHARE_PRIOR, DEFAULT_TE_TARGET_SHARE_PRIOR)
        return (expected - observed) * mult

    return 0.0


def _build_player_records(context) -> tuple[list[dict], int]:
    excluded_no_adp = 0
    records: list[dict] = []

    for player in context.players:
        position = player["position"]
        if position not in DPS_POSITIONS:
            continue

        pid = player["player_id"]
        adp = context.adp_by_player.get(pid)
        if adp is None:
            excluded_no_adp += 1
            continue

        rows = context.season_games_by_player.get(pid, [])
        season_totals = aggregate_skill_season_totals(rows)
        if season_totals["games_played"] == 0:
            continue

        primary_2025_team = primary_team_id(season_totals["team_ids_seen"])
        current_team = player["team_id"]
        context_changed = bool(primary_2025_team and current_team and primary_2025_team != current_team)
        share_team_totals = context.team_totals_by_team.get(primary_2025_team, {})

        xtd_raw = _compute_xtd_delta(position, season_totals, context.baselines)
        role_raw = _compute_role_shift(player, season_totals, share_team_totals, context_changed)

        records.append({
            "player_id": pid,
            "name": player["full_name"],
            "position": position,
            "adp": adp,
            "xtd_raw": xtd_raw,
            "role_raw": role_raw,
            "context_changed": context_changed,
            "low_sample": _low_sample(position, season_totals),
        })

    return records, excluded_no_adp


def _apply_z_scores_and_dps(records: list[dict]) -> dict[str, int]:
    """Z-score xTD/Role within position; compute DPS. Returns n per position."""
    by_pos: dict[str, list[dict]] = {p: [] for p in DPS_POSITIONS}
    for rec in records:
        by_pos[rec["position"]].append(rec)

    sample_sizes: dict[str, int] = {}
    for position in DPS_POSITIONS:
        group = by_pos[position]
        sample_sizes[position] = len(group)
        if not group:
            continue

        xtd_vals = [r["xtd_raw"] for r in group]
        role_vals = [r["role_raw"] for r in group]

        xtd_sigma = statistics.pstdev(xtd_vals) if len(xtd_vals) > 1 else 0.0
        role_sigma = statistics.pstdev(role_vals) if len(role_vals) > 1 else 0.0
        if xtd_sigma == 0:
            print(f"  WARNING: {position} xTD_Delta sigma == 0 — Z_xTD set to 0 for all")
        if role_sigma == 0:
            print(f"  WARNING: {position} Role_Shift sigma == 0 — Z_Role set to 0 for all")

        z_xtd = _z_scores(xtd_vals)
        z_role = _z_scores(role_vals)
        lam = LAMBDA_POS[position]

        for rec, zx, zr in zip(group, z_xtd, z_role):
            delta = DELTA_XTD_WEIGHT * zx + DELTA_ROLE_WEIGHT * zr
            rec["z_xtd"] = zx
            rec["z_role"] = zr
            rec["delta"] = delta
            rec["dps"] = rec["adp"] - lam * delta

    return sample_sizes


def _rank_by_adp(records: list[dict]) -> None:
    by_pos: dict[str, list[dict]] = {p: [] for p in DPS_POSITIONS}
    for rec in records:
        by_pos[rec["position"]].append(rec)

    for position in DPS_POSITIONS:
        group = sorted(by_pos[position], key=lambda r: r["adp"])
        for rank, rec in enumerate(group, start=1):
            rec["adp_rank"] = rank


def _rank_by_dps(records: list[dict]) -> None:
    by_pos: dict[str, list[dict]] = {p: [] for p in DPS_POSITIONS}
    for rec in records:
        by_pos[rec["position"]].append(rec)

    for position in DPS_POSITIONS:
        group = sorted(by_pos[position], key=lambda r: (r["dps"], r["adp"]))
        for rank, rec in enumerate(group, start=1):
            rec["dps_rank"] = rank
            rec["rank_move"] = rec["adp_rank"] - rank


def _flags_str(rec: dict) -> str:
    flags = []
    if rec["context_changed"]:
        flags.append("context_changed")
    if rec["low_sample"]:
        flags.append("low_sample")
    return ",".join(flags) if flags else "-"


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

    header = (
        f"{'DPS#':>4} | {'Player':<26} | {'ADP#':>4} | {'DE#':>4} | {'ADP':>6} | "
        f"{'DPS':>7} | {'Delta':>6} | {'Z_xTD':>6} | {'Z_Role':>6} | {'Move':>5} | Flags"
    )
    print(f"\n{'=' * len(header)}")
    print(f"{position} — top {top_n} by DPS (lower DPS = draft earlier)")
    print(header)
    print("-" * len(header))

    for rec in group[:top_n]:
        de_rank = draft_edge_ranks.get(rec["player_id"])
        de_str = str(de_rank) if de_rank is not None else "-"
        print(
            f"{rec['dps_rank']:>4} | {rec['name']:<26} | {rec['adp_rank']:>4} | {de_str:>4} | "
            f"{rec['adp']:>6.1f} | {rec['dps']:>7.2f} | {rec['delta']:>6.3f} | "
            f"{rec['z_xtd']:>6.2f} | {rec['z_role']:>6.2f} | {rec['rank_move']:>+5} | "
            f"{_flags_str(rec)}"
        )

    _print_movers(records, position, n=5)
    print(f"\n{position} z-score sample: n={sample_n}")


def _print_movers(records: list[dict], position: str, *, n: int = 5) -> None:
    group = [r for r in records if r["position"] == position]

    risers = sorted(group, key=lambda r: r["rank_move"], reverse=True)[:n]
    fallers = sorted(group, key=lambda r: r["rank_move"])[:n]

    print(f"\n{position} — top {n} RISERS vs ADP (rank_move = ADP# - DPS#)")
    for rec in risers:
        print(
            f"  {rec['name']:<26} ADP#{rec['adp_rank']:>3} -> DPS#{rec['dps_rank']:>3} "
            f"move={rec['rank_move']:>+3}  DPS={rec['dps']:.2f}  flags={_flags_str(rec)}"
        )

    print(f"\n{position} — top {n} FALLERS vs ADP")
    for rec in fallers:
        print(
            f"  {rec['name']:<26} ADP#{rec['adp_rank']:>3} -> DPS#{rec['dps_rank']:>3} "
            f"move={rec['rank_move']:>+3}  DPS={rec['dps']:.2f}  flags={_flags_str(rec)}"
        )


def _print_adp_freshness(client, adp_rows: list[dict], context) -> None:
    fetched = [r["fetched_at"] for r in adp_rows if r.get("fetched_at")]
    print("ADP FRESHNESS CHECK")
    print("-" * 40)
    print(f"  adp row count (all sources/history): {len(adp_rows)}")
    print(f"  adp players (latest per player_id):  {len(context.adp_by_player)}")
    if fetched:
        print(f"  fetched_at min: {min(fetched)}")
        print(f"  fetched_at max: {max(fetched)}")
    else:
        print("  fetched_at: (none)")

    # Spotlight sanity names
    lookup_names = list(ADP_SPOTLIGHT_NAMES)
    optional = [ADP_SPOTLIGHT_ALIASES["Brock Bowders"]]
    try:
        by_name = fetch_players_by_names(client, lookup_names)
    except RuntimeError as exc:
        print(f"  Spotlight lookup error: {exc}")
        by_name = {}

    print("\n  Spotlight ADP values (latest per player):")
    for name in ADP_SPOTLIGHT_NAMES:
        player = by_name.get(name)
        if not player:
            print(f"    {name:<22} NOT FOUND")
            continue
        adp = context.adp_by_player.get(player["player_id"])
        adp_str = f"{adp:.1f}" if adp is not None else "no ADP row"
        print(f"    {name:<22} adp_value={adp_str}")

    # Also note Brock Bowders alias if user meant Bowers
    print("    (prompt alias 'Brock Bowders' -> Brock Bowers)")


def main() -> None:
    positions_filter: tuple[str, ...] | None = None
    if len(sys.argv) > 1:
        arg = sys.argv[1].upper()
        if arg in DPS_POSITIONS:
            positions_filter = (arg,)

    print("Draft Priority Score review — read-only, no database writes.\n")

    client = get_supabase_client()
    context = load_draft_edge_context(client)

    records, excluded_no_adp = _build_player_records(context)
    print(f"Excluded (draft pool, no ADP): {excluded_no_adp}")

    draft_edge_ranks = _fetch_draft_edge_ranks(client)

    sample_sizes = _apply_z_scores_and_dps(records)
    _rank_by_adp(records)
    _rank_by_dps(records)

    positions = positions_filter or DPS_POSITIONS
    for position in positions:
        _print_position_board(
            records, position, draft_edge_ranks, sample_sizes.get(position, 0),
        )


if __name__ == "__main__":
    main()
