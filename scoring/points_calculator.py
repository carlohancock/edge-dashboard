"""
Points calculator for the Edge scoring engine.
Converts position-specific projected stats into final fantasy points,
using LEAGUE_SCORING_RULES. Pure function application — no statistics here.
"""

from config.league_scoring_rules import LEAGUE_SCORING_RULES as RULES
from scoring.stats_utils import bucket_bonus, tiered_points


def calculate_qb_points(qb_features: dict) -> float:
    r = RULES["passing"]
    yards = qb_features["proj_pass_yards"]
    points = yards / r["yards_per_point"]
    points += qb_features["proj_pass_tds"] * r["td"]
    points += qb_features["proj_pass_ints"] * r["interception"]
    points += bucket_bonus(yards, r["yardage_bonus_tiers"])

    # Rushing sub-term
    rr = RULES["rushing"]
    rush_yards = qb_features["proj_rush_yards"]
    points += rush_yards / rr["yards_per_point"]
    points += bucket_bonus(rush_yards, rr["yardage_bonus_tiers"])

    return points


def calculate_rb_points(rb_features: dict) -> float:
    rr = RULES["rushing"]
    rec = RULES["receiving"]

    rush_yards = rb_features["proj_rush_yards"]
    points = rush_yards / rr["yards_per_point"]
    points += rb_features["proj_rush_tds"] * rr["td"]
    points += bucket_bonus(rush_yards, rr["yardage_bonus_tiers"])
    # Note: big-play (40+/50+) TD bonus tiers apply to individual TD length,
    # not projected TD count — v1 limitation (documented in edge_formula_nfl.md):
    # bonuses apply only when the point estimate itself clears the threshold.
    # Not applied here since proj_rush_tds is a fractional expected count, not
    # a single scoring play with a known length.

    rec_yards = rb_features["proj_rec_yards"]
    points += rec_yards / rec["yards_per_point"]
    points += rb_features["proj_receptions"] * rec["reception"]
    points += rb_features["proj_rec_tds"] * rec["td"]
    points += bucket_bonus(rec_yards, rec["yardage_bonus_tiers"])

    return points


def calculate_wr_te_points(wr_te_features: dict) -> float:
    rec = RULES["receiving"]
    rec_yards = wr_te_features["proj_rec_yards"]

    points = rec_yards / rec["yards_per_point"]
    points += wr_te_features["proj_receptions"] * rec["reception"]
    points += wr_te_features["proj_rec_tds"] * rec["td"]
    points += bucket_bonus(rec_yards, rec["yardage_bonus_tiers"])

    return points


def calculate_kicker_points(kicker_features: dict) -> float:
    r = RULES["kicking"]

    proj_fg_made = kicker_features["proj_fg_made"]
    proj_fg_missed = kicker_features["proj_fg_attempts"] - proj_fg_made
    proj_pat_made = kicker_features["proj_pat_attempts"]  # assume PATs made at ~high rate; simplified v1

    # v1 simplification: distance mix uses season-average accuracy, not a
    # distance-bucketed make probability — so we apply the mid-tier (40-49) FG
    # value as a reasonable blended estimate, per the "distance mix: season
    # average for now" v1 flag in edge_formula_nfl.md.
    avg_fg_points = r["fg_made_tiers"][1]["points"]  # 40-49 yd tier as blended proxy
    avg_fg_miss_points = r["fg_missed_tiers"][1]["points"]

    points = proj_fg_made * avg_fg_points
    points += proj_fg_missed * avg_fg_miss_points
    points += proj_pat_made * r["pat_made"]

    return points


def calculate_dst_points(dst_features: dict) -> float:
    r = RULES["dst"]

    # v1 simplification: sack_matchup and opponent_turnover_worthy_rate feed
    # points_allowed/yards_allowed tier estimation indirectly via the implied
    # total and yards-allowed input — direct sack/turnover counts need a
    # separate projected-plays multiplier, which is a reasonable v2 refinement.
    # For v1, project points_allowed from opponent_implied_total directly
    # (rounding to nearest tier), and yards_allowed from the passed-in baseline.

    points_allowed_est = dst_features["opponent_implied_total"]
    yards_allowed_est = dst_features["opponent_yards_allowed_tier_input"]

    points = tiered_points(points_allowed_est, r["points_allowed_tiers"])
    points += tiered_points(yards_allowed_est, r["yards_allowed_tiers"])

    # Approximate takeaway/sack contribution using the shrinkage-regressed
    # turnover rate and sack_matchup as expected-value multipliers against a
    # reasonable per-game opponent-play-count assumption (65 plays, v1 default).
    assumed_opponent_plays = 65
    expected_turnovers = dst_features["opponent_turnover_worthy_rate"] * assumed_opponent_plays
    expected_sacks = dst_features["sack_matchup"] * assumed_opponent_plays

    points += expected_turnovers * r["interception"]  # treating turnovers as INT-equivalent value, v1 simplification
    points += expected_sacks * r["sack"]

    return points