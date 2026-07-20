"""
League Scoring Rules — Edge Project
Structured config representing the exact fantasy scoring rules for this league.
Used by the scoring engine to convert raw stat projections into fantasy points.
"""

LEAGUE_SCORING_RULES = {

    # ---- Offensive (shared across positions) ----
    "fumble": -1,
    "fumble_lost": -2,
    "fumble_recovery_td": 6,
    "return_yards_per_point": 25,       # +1 pt per 25 kickoff/punt return yards
    "return_td": 6,                      # kickoff or punt return TD
    "two_point_conversion": 2,

    # ---- Passing ----
    "passing": {
        "yards_per_point": 25,            # 1 pt per 25 yards
        "td": 4,
        "interception": -2,
        "yardage_bonus_tiers": [
            {"min": 300, "max": 399, "bonus": 1},
            {"min": 400, "max": None, "bonus": 2},   # 400+ yards
        ],
    },

    # ---- Rushing ----
    "rushing": {
        "yards_per_point": 10,            # 1 pt per 10 yards
        "td": 6,
        "td_bonus_tiers": [
            {"min": 40, "max": 49, "bonus": 2},
            {"min": 50, "max": None, "bonus": 3},     # 50+ yard rushing TD
        ],
        "yardage_bonus_tiers": [
            {"min": 100, "max": 199, "bonus": 1},
            {"min": 200, "max": None, "bonus": 2},    # 200+ yards
        ],
    },

    # ---- Receiving ----
    "receiving": {
        "yards_per_point": 10,            # 1 pt per 10 yards
        "reception": 1,                    # full PPR
        "td": 6,
        "td_bonus_tiers": [
            {"min": 40, "max": 49, "bonus": 1},
            {"min": 50, "max": None, "bonus": 2},     # 50+ yard receiving TD
        ],
        "yardage_bonus_tiers": [
            {"min": 100, "max": 199, "bonus": 1},
            {"min": 200, "max": None, "bonus": 2},    # 200+ yards
        ],
    },

    # ---- Kicking ----
    "kicking": {
        "pat_made": 1,
        "pat_missed": -1,
        "fg_made_tiers": [
            {"min": 0, "max": 39, "points": 3},
            {"min": 40, "max": 49, "points": 4},
            {"min": 50, "max": None, "points": 5},    # 50+ covers all long kicks
        ],
        "fg_missed_tiers": [
            {"min": 0, "max": 39, "points": -2},
            {"min": 40, "max": 49, "points": -1},
            # no penalty specified for 50+ misses
        ],
    },

    # ---- Defense / Special Teams ----
    "dst": {
        "sack": 1,
        "interception": 2,
        "fumble_recovery": 2,
        "safety": 2,
        "forced_fumble": 2,
        "blocked_kick": 3,
        "touchdown": 6,   # defensive, punt, or kick return TD
        "points_allowed_tiers": [
            {"min": 0, "max": 0, "points": 7},
            {"min": 1, "max": 6, "points": 5},
            {"min": 7, "max": 13, "points": 3},
            {"min": 14, "max": 20, "points": 1},
            {"min": 21, "max": 27, "points": 0},
            {"min": 28, "max": 34, "points": -1},
            {"min": 35, "max": None, "points": -4},
        ],
        "yards_allowed_tiers": [
            {"min": 0, "max": 99, "points": 4},
            {"min": 100, "max": 199, "points": 2},
            {"min": 200, "max": 399, "points": 0},
            {"min": 400, "max": 449, "points": -1},
            {"min": 450, "max": 499, "points": -2},
            {"min": 500, "max": None, "points": -3},
        ],
    },
}
