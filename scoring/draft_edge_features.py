"""
Draft Edge feature builders -- project each player's 2026 SEASON role from
their 2025 season totals (scoring/season_stats.py) plus their CURRENT
(2026) team/depth-chart context (`players.team_id`, `players.depth_chart_rank`).

This is deliberately NOT the weekly engine re-run at a season grain. Weekly
Edge asks "given recent form, how will this player do in a specific
upcoming game against a specific opponent." Draft Edge asks "given last
year's role, and this year's depth chart, how big is this player's role
likely to be over an entire season neither of us has seen yet." No
opponent, no game-script, no market blend (no 2026 odds exist this far out
-- expected, not a gap, per edge_formula_nfl.md's Vegas-features section
being irrelevant at this horizon).

Output dicts intentionally reuse the SAME key names the weekly feature
builders use (proj_pass_yards, proj_rush_tds, proj_targets, etc.) so
scoring/points_calculator.py's existing calculate_*_points functions work
unmodified on season totals instead of per-game projections -- same
opportunity/efficiency/shrinkage math, same scoring-rule application,
re-weighted for season-long context per edge_formula_nfl.md's Draft Edge
framing. points_calculator.py is untouched; nothing here changes weekly
Edge, Wire Edge, DST, or vegas_features.py (scope boundary).

---- Role/context handling (Task 2) ----

Three cases per player, in priority order:

1. **No 2025 NFL stats at all** (rookies, or anyone who simply didn't play
   -- injury, practice squad, etc. -- the criterion is "no data", not
   "rookie" specifically): games_played_2025 == 0. These functions are
   never called for this case -- there is nothing to project a role FROM,
   and fabricating one from zero real signal is explicitly out of scope
   per the task. The orchestrator (compute_draft_edge.py) detects this
   upstream and routes to an ADP-anchored placeholder instead, flagging
   `no_historical_data: true`.

2. **Team changed since 2025** (`context_changed`): the player's 2025
   role-share stats (rush_share, target_share) were earned in a different
   offensive context and may not transfer. Rather than silently trusting
   them, or refusing to project at all, this module still produces a
   best-effort projection (their own observed rate, blended against the
   CURRENT team's depth-chart-implied role -- see `_blended_share` below)
   but the caller is told `context_changed: true` so it's visible as
   lower-confidence, per the task's explicit ask ("flag rather than
   project blindly").

3. **Normal case** (has 2025 stats, on the same team, or depth chart is
   otherwise unambiguous): 2025 season rate, adjusted toward a
   depth-chart-implied prior share using `depth_chart_rank` -- this is
   what actually captures "current role" for BOTH promoted backups (low
   2025 share, but now the depth chart's #1) and demoted starters (high
   2025 share, now buried on the chart). See `_blended_share`.
"""

from __future__ import annotations

from scoring.season_stats import (
    GAMES_IN_SEASON,
    LEAGUE_MEAN_INT_RATE,
    LEAGUE_MEAN_PASS_TD_RATE,
    LEAGUE_MEAN_RB_REC_TD_RATE,
    LEAGUE_MEAN_RUSH_TD_RATE,
    LEAGUE_MEAN_WR_TE_REC_TD_RATE,
    MIN_SEASON_GAMES,
    MIN_SEASON_SAMPLE,
    _lookup_baseline,
    season_regressed_rate,
    season_regressed_stat,
)

# ---- Depth-chart-implied role priors (Task 2's "adjusted by current
# depth_chart_rank") ----
#
# Rough, v1, hand-set typical season SHARE for a player at a given depth
# chart slot -- not derived from data yet (there's no clean historical
# depth-chart-rank-over-time table to fit this against; `players` only
# stores the CURRENT rank, overwritten on every reseed). These exist purely
# to give the blend below something sensible to pull toward when a
# player's CURRENT slot doesn't match what their 2025 observed rate would
# imply (promoted backup, demoted starter, backup stuck behind a new
# free-agent signing, etc.). Flagged for replacement by a real
# depth-chart-rank -> share regression once several draft classes of
# outcome data exist (same "revisit in Phase 5" flag as everywhere else
# hand-set in this codebase).
RB_RUSH_SHARE_PRIOR = {1: 0.58, 2: 0.24, 3: 0.10, 4: 0.05}
RB_TARGET_SHARE_PRIOR = {1: 0.12, 2: 0.08, 3: 0.05, 4: 0.02}
WR_TARGET_SHARE_PRIOR = {1: 0.22, 2: 0.16, 3: 0.11, 4: 0.06}
TE_TARGET_SHARE_PRIOR = {1: 0.15, 2: 0.06, 3: 0.03, 4: 0.02}
DEFAULT_RB_RUSH_SHARE_PRIOR = 0.02
DEFAULT_RB_TARGET_SHARE_PRIOR = 0.01
DEFAULT_WR_TARGET_SHARE_PRIOR = 0.03
DEFAULT_TE_TARGET_SHARE_PRIOR = 0.01

# QB/K don't have a shared "pool" the way RB carries / WR-TE targets do --
# a team only plays one QB and kicks with one K at a time, so "role" is
# really "how many games do they start/kick in" rather than a shared
# share. v1 games-played-equivalent estimate by depth_chart_rank.
QB_GAMES_ESTIMATE_PRIOR = {1: 16.0, 2: 1.5, 3: 0.5}
DEFAULT_QB_GAMES_ESTIMATE = 0.2
K_GAMES_ESTIMATE_PRIOR = {1: 17.0, 2: 0.5}
DEFAULT_K_GAMES_ESTIMATE = 0.1

# Team volume fallback if the current 2026 team somehow has zero 2025 DEF
# history (shouldn't happen post-Phase-3.6, but mirrors compute_edge_scores.py's
# own DEFAULT_TEAM_*_BASELINE fallback pattern, scaled to a full season).
DEFAULT_TEAM_RUSH_ATTEMPTS_SEASON = 26.0 * GAMES_IN_SEASON
DEFAULT_TEAM_PASS_ATTEMPTS_SEASON = 34.0 * GAMES_IN_SEASON


def _blended_share(
    observed_share: float | None,
    games_played_2025: int,
    prior_share: float,
    context_changed: bool,
) -> tuple[float, float]:
    """
    Empirical-Bayes-flavored blend between a player's OWN observed 2025
    season share and the depth-chart-implied prior for their CURRENT slot
    -- same shrinkage spirit as the rest of this engine (trust the
    player's own sample more as it gets bigger; lean on the positional
    prior when the sample is thin or the context changed enough that the
    2025 rate is suspect).

    Returns (blended_share, weight_on_observed) -- the weight is surfaced
    in factor_breakdown for auditability, not just the final blended number.
    """
    if observed_share is None or games_played_2025 == 0:
        return prior_share, 0.0

    sample_weight = min(games_played_2025 / 10.0, 1.0)
    if context_changed:
        # Own-team-relative share is a noisier signal once the team
        # context it was earned in no longer applies -- halve confidence
        # in it rather than discarding it outright.
        sample_weight *= 0.5

    blended = sample_weight * observed_share + (1 - sample_weight) * prior_share
    return blended, sample_weight


def _team_season_attempts(team_season_totals: dict, key: str, default: float) -> float:
    """This team's 2025 season total for `key`, normalized to a full GAMES_IN_SEASON if the source sample was short."""
    raw = team_season_totals.get(key)
    games = team_season_totals.get("games_played") or 0
    if raw is None or games == 0:
        return default
    return (raw / games) * GAMES_IN_SEASON


def _depth_chart_tier(depth_chart_rank: int | None, prior_table: dict[int, float], default: float) -> float:
    if depth_chart_rank is None:
        return default
    return prior_table.get(depth_chart_rank, default)


def build_draft_qb_features(
    season_totals: dict,
    depth_chart_rank: int | None,
    context_changed: bool,
    baselines: dict | None = None,
    shrinkage_strength: float | None = None,
) -> dict:
    """
    QB has no shared "pool" to split -- role is "will this player be the
    guy taking snaps," approximated as an expected games-played-equivalent
    from depth_chart_rank, applied against the player's OWN observed 2025
    per-game rates (own efficiency/volume-when-playing is portable in a
    way team-relative RB/WR share isn't).
    """
    games_played = season_totals["games_played"]
    attempts = season_totals["attempts"]
    pass_yards = season_totals["passing_yards"]
    pass_tds = season_totals["passing_tds"]
    pass_ints = season_totals["passing_interceptions"]
    rush_attempts = season_totals["carries"]
    rush_yards = season_totals["rushing_yards"]
    rush_tds = season_totals["rushing_tds"]

    proj_games = _depth_chart_tier(depth_chart_rank, QB_GAMES_ESTIMATE_PRIOR, DEFAULT_QB_GAMES_ESTIMATE)

    attempts_per_game_raw = attempts / games_played
    rush_attempts_per_game_raw = rush_attempts / games_played
    ypa_raw = (pass_yards / attempts) if attempts > 0 else 0.0
    ypc_raw = (rush_yards / rush_attempts) if rush_attempts > 0 else 0.0

    if baselines:
        attempts_per_game = season_regressed_stat(
            attempts_per_game_raw,
            _lookup_baseline(baselines, "QB", depth_chart_rank, "attempts_per_game", attempts_per_game_raw),
            games_played,
            shrinkage_strength,
        )
        rush_attempts_per_game = season_regressed_stat(
            rush_attempts_per_game_raw,
            _lookup_baseline(baselines, "QB", depth_chart_rank, "rush_attempts_per_game", rush_attempts_per_game_raw),
            games_played,
            shrinkage_strength,
        )
        ypa = season_regressed_stat(
            ypa_raw,
            _lookup_baseline(baselines, "QB", depth_chart_rank, "ypa", ypa_raw),
            games_played,
            shrinkage_strength,
        )
        ypc = season_regressed_stat(
            ypc_raw,
            _lookup_baseline(baselines, "QB", depth_chart_rank, "ypc", ypc_raw),
            games_played,
            shrinkage_strength,
        )
    else:
        attempts_per_game = attempts_per_game_raw
        rush_attempts_per_game = rush_attempts_per_game_raw
        ypa = ypa_raw
        ypc = ypc_raw

    proj_pass_attempts = attempts_per_game * proj_games
    proj_rush_attempts = rush_attempts_per_game * proj_games

    td_rate = season_regressed_rate(pass_tds, attempts, LEAGUE_MEAN_PASS_TD_RATE, "qb_pass_td")
    int_rate = season_regressed_rate(pass_ints, attempts, LEAGUE_MEAN_INT_RATE, "qb_int")
    rush_td_per_game_prior = (
        _lookup_baseline(baselines, "QB", None, "rush_tds_per_game_prior", 0.0)
        if baselines
        else 0.0
    )
    rush_tds_per_game = season_regressed_rate(
        rush_tds, games_played, rush_td_per_game_prior, "qb_rush_td_per_game",
    )

    proj_pass_yards = proj_pass_attempts * ypa
    proj_pass_tds = proj_pass_attempts * td_rate
    proj_pass_ints = proj_pass_attempts * int_rate
    proj_rush_yards = proj_rush_attempts * ypc
    proj_rush_tds = rush_tds_per_game * proj_games

    low_sample = games_played < MIN_SEASON_GAMES or attempts < MIN_SEASON_SAMPLE["pass_attempts"]

    return {
        "proj_pass_attempts": proj_pass_attempts,
        "proj_pass_yards": proj_pass_yards,
        "proj_pass_tds": proj_pass_tds,
        "proj_pass_ints": proj_pass_ints,
        "proj_rush_attempts": proj_rush_attempts,
        "proj_rush_yards": proj_rush_yards,
        "proj_rush_tds": proj_rush_tds,
        "season_2025_games_played": games_played,
        "season_2025_attempts": attempts,
        "season_2025_ypa": ypa,
        "regressed_td_rate": td_rate,
        "regressed_int_rate": int_rate,
        "regressed_rush_tds_per_game": rush_tds_per_game,
        "qb_rush_td_per_game_prior": rush_td_per_game_prior,
        "proj_games_2026": proj_games,
        "depth_chart_rank": depth_chart_rank,
        "context_changed": context_changed,
        "low_sample": low_sample,
        "no_historical_data": False,
    }


def build_draft_rb_features(
    season_totals: dict,
    current_team_season_totals: dict,
    share_denominator_team_totals: dict,
    depth_chart_rank: int | None,
    context_changed: bool,
    baselines: dict | None = None,
    shrinkage_strength: float | None = None,
) -> dict:
    """
    Two different teams' 2025 season totals go in here, deliberately kept
    separate:
      - `share_denominator_team_totals`: the team this player actually
        EARNED their 2025 rate/share against (their primary 2025 team,
        resolved via season_stats.primary_team_id upstream) -- for a
        traded player that's an approximation across whichever team they
        spent the most 2025 games with, not a perfectly game-by-game-
        attributed share. That imprecision is exactly what `context_changed`
        already flags as lower-confidence, so it's not a silent gap.
      - `current_team_season_totals`: the player's CURRENT (2026) team --
        used to project 2026 volume, since that's whose offense they'll
        actually be part of. For a non-traded player these are the same
        team; for a traded player they aren't, which is exactly the case
        this split exists to handle correctly instead of conflating the two.
    """
    games_played = season_totals["games_played"]
    carries = season_totals["carries"]
    rush_yards = season_totals["rushing_yards"]
    rush_tds = season_totals["rushing_tds"]
    targets = season_totals["targets"]
    receptions = season_totals["receptions"]
    rec_yards = season_totals["receiving_yards"]
    rec_tds = season_totals["receiving_tds"]

    share_team_carries_season = _team_season_attempts(share_denominator_team_totals, "carries", DEFAULT_TEAM_RUSH_ATTEMPTS_SEASON)
    share_team_attempts_season = _team_season_attempts(share_denominator_team_totals, "attempts", DEFAULT_TEAM_PASS_ATTEMPTS_SEASON)
    proj_team_carries_season = _team_season_attempts(current_team_season_totals, "carries", DEFAULT_TEAM_RUSH_ATTEMPTS_SEASON)
    proj_team_attempts_season = _team_season_attempts(current_team_season_totals, "attempts", DEFAULT_TEAM_PASS_ATTEMPTS_SEASON)

    observed_rush_share = (carries / share_team_carries_season) if share_team_carries_season else None
    observed_target_share = (targets / share_team_attempts_season) if share_team_attempts_season else None

    rush_share_prior = _depth_chart_tier(depth_chart_rank, RB_RUSH_SHARE_PRIOR, DEFAULT_RB_RUSH_SHARE_PRIOR)
    target_share_prior = _depth_chart_tier(depth_chart_rank, RB_TARGET_SHARE_PRIOR, DEFAULT_RB_TARGET_SHARE_PRIOR)

    rush_share, rush_share_weight = _blended_share(observed_rush_share, games_played, rush_share_prior, context_changed)
    target_share, target_share_weight = _blended_share(observed_target_share, games_played, target_share_prior, context_changed)

    # 2026 team-level volume projected from the CURRENT (2026) team's 2025
    # pace -- there's no odds/game-script signal to adjust it with yet
    # (no 2026 market data exists this far out; expected per scope notes),
    # so "last year's team pace persists" is the v1 assumption.
    proj_carries_raw = rush_share * proj_team_carries_season
    proj_targets_raw = target_share * proj_team_attempts_season

    ypc_raw = (rush_yards / carries) if carries > 0 else 0.0
    catch_rate_raw = (receptions / targets) if targets > 0 else 0.0
    ypt_raw = (rec_yards / targets) if targets > 0 else 0.0

    if baselines:
        proj_carries = season_regressed_stat(
            proj_carries_raw,
            _lookup_baseline(baselines, "RB", depth_chart_rank, "carries", proj_carries_raw),
            games_played,
            shrinkage_strength,
        )
        proj_targets = season_regressed_stat(
            proj_targets_raw,
            _lookup_baseline(baselines, "RB", depth_chart_rank, "targets", proj_targets_raw),
            games_played,
            shrinkage_strength,
        )
        ypc = season_regressed_stat(
            ypc_raw,
            _lookup_baseline(baselines, "RB", depth_chart_rank, "ypc", ypc_raw),
            games_played,
            shrinkage_strength,
        )
        catch_rate = season_regressed_stat(
            catch_rate_raw,
            _lookup_baseline(baselines, "RB", depth_chart_rank, "catch_rate", catch_rate_raw),
            games_played,
            shrinkage_strength,
        )
        ypt = season_regressed_stat(
            ypt_raw,
            _lookup_baseline(baselines, "RB", depth_chart_rank, "ypt", ypt_raw),
            games_played,
            shrinkage_strength,
        )
    else:
        proj_carries = proj_carries_raw
        proj_targets = proj_targets_raw
        ypc = ypc_raw
        catch_rate = catch_rate_raw
        ypt = ypt_raw

    rush_td_rate = season_regressed_rate(rush_tds, carries, LEAGUE_MEAN_RUSH_TD_RATE, "rb_rush_td")
    rec_td_rate = season_regressed_rate(rec_tds, targets, LEAGUE_MEAN_RB_REC_TD_RATE, "rb_rec_td")

    proj_rush_yards = proj_carries * ypc
    proj_rush_tds = proj_carries * rush_td_rate
    proj_receptions = proj_targets * catch_rate
    proj_rec_yards = proj_targets * ypt
    proj_rec_tds = proj_targets * rec_td_rate

    low_sample = games_played < MIN_SEASON_GAMES or (carries + targets) < MIN_SEASON_SAMPLE["carries"]

    return {
        "proj_carries": proj_carries,
        "proj_rush_yards": proj_rush_yards,
        "proj_rush_tds": proj_rush_tds,
        "proj_targets": proj_targets,
        "proj_receptions": proj_receptions,
        "proj_rec_yards": proj_rec_yards,
        "proj_rec_tds": proj_rec_tds,
        "season_2025_games_played": games_played,
        "season_2025_carries": carries,
        "season_2025_targets": targets,
        "observed_rush_share_2025": observed_rush_share,
        "observed_target_share_2025": observed_target_share,
        "rush_share_prior": rush_share_prior,
        "target_share_prior": target_share_prior,
        "blended_rush_share": rush_share,
        "blended_target_share": target_share,
        "rush_share_weight_on_observed": rush_share_weight,
        "target_share_weight_on_observed": target_share_weight,
        "regressed_rush_td_rate": rush_td_rate,
        "regressed_rec_td_rate": rec_td_rate,
        "depth_chart_rank": depth_chart_rank,
        "context_changed": context_changed,
        "low_sample": low_sample,
        "no_historical_data": False,
    }


def build_draft_wr_te_features(
    season_totals: dict,
    current_team_season_totals: dict,
    share_denominator_team_totals: dict,
    depth_chart_rank: int | None,
    context_changed: bool,
    position: str,
    baselines: dict | None = None,
    shrinkage_strength: float | None = None,
) -> dict:
    """See build_draft_rb_features's docstring for why two team-totals dicts are needed."""
    games_played = season_totals["games_played"]
    targets = season_totals["targets"]
    receptions = season_totals["receptions"]
    rec_yards = season_totals["receiving_yards"]
    rec_tds = season_totals["receiving_tds"]
    air_yards = season_totals["receiving_air_yards"]

    share_team_attempts_season = _team_season_attempts(share_denominator_team_totals, "attempts", DEFAULT_TEAM_PASS_ATTEMPTS_SEASON)
    proj_team_attempts_season = _team_season_attempts(current_team_season_totals, "attempts", DEFAULT_TEAM_PASS_ATTEMPTS_SEASON)
    observed_target_share = (targets / share_team_attempts_season) if share_team_attempts_season else None

    prior_table = WR_TARGET_SHARE_PRIOR if position == "WR" else TE_TARGET_SHARE_PRIOR
    default_prior = DEFAULT_WR_TARGET_SHARE_PRIOR if position == "WR" else DEFAULT_TE_TARGET_SHARE_PRIOR
    target_share_prior = _depth_chart_tier(depth_chart_rank, prior_table, default_prior)

    target_share, target_share_weight = _blended_share(observed_target_share, games_played, target_share_prior, context_changed)
    proj_targets_raw = target_share * proj_team_attempts_season

    catch_rate_raw = (receptions / targets) if targets > 0 else 0.0
    ypt_raw = (rec_yards / targets) if targets > 0 else 0.0
    adot = (air_yards / targets) if targets > 0 else 0.0

    if baselines:
        proj_targets = season_regressed_stat(
            proj_targets_raw,
            _lookup_baseline(baselines, position, depth_chart_rank, "targets", proj_targets_raw),
            games_played,
            shrinkage_strength,
        )
        catch_rate = season_regressed_stat(
            catch_rate_raw,
            _lookup_baseline(baselines, position, depth_chart_rank, "catch_rate", catch_rate_raw),
            games_played,
            shrinkage_strength,
        )
        ypt = season_regressed_stat(
            ypt_raw,
            _lookup_baseline(baselines, position, depth_chart_rank, "ypt", ypt_raw),
            games_played,
            shrinkage_strength,
        )
    else:
        proj_targets = proj_targets_raw
        catch_rate = catch_rate_raw
        ypt = ypt_raw

    rec_td_rate = season_regressed_rate(rec_tds, targets, LEAGUE_MEAN_WR_TE_REC_TD_RATE, "wr_te_rec_td")

    proj_receptions = proj_targets * catch_rate
    proj_rec_yards = proj_targets * ypt
    proj_rec_tds = proj_targets * rec_td_rate

    low_sample = games_played < MIN_SEASON_GAMES or targets < MIN_SEASON_SAMPLE["targets"]

    return {
        "proj_targets": proj_targets,
        "proj_receptions": proj_receptions,
        "proj_rec_yards": proj_rec_yards,
        "proj_rec_tds": proj_rec_tds,
        "adot": adot,
        "season_2025_games_played": games_played,
        "season_2025_targets": targets,
        "observed_target_share_2025": observed_target_share,
        "target_share_prior": target_share_prior,
        "blended_target_share": target_share,
        "target_share_weight_on_observed": target_share_weight,
        "regressed_rec_td_rate": rec_td_rate,
        "depth_chart_rank": depth_chart_rank,
        "context_changed": context_changed,
        "low_sample": low_sample,
        "no_historical_data": False,
    }


def build_draft_kicker_features(
    season_totals: dict,
    depth_chart_rank: int | None,
    context_changed: bool,
    baselines: dict | None = None,
    shrinkage_strength: float | None = None,
) -> dict:
    games_played = season_totals["games_played"]
    fg_att = season_totals["fg_att"]
    fg_made = season_totals["fg_made"]
    pat_att = season_totals["pat_att"]

    proj_games = _depth_chart_tier(depth_chart_rank, K_GAMES_ESTIMATE_PRIOR, DEFAULT_K_GAMES_ESTIMATE)

    fg_att_per_game_raw = fg_att / games_played
    pat_att_per_game_raw = pat_att / games_played
    fg_accuracy_raw = (fg_made / fg_att) if fg_att > 0 else 0.80

    if baselines:
        fg_att_per_game = season_regressed_stat(
            fg_att_per_game_raw,
            _lookup_baseline(baselines, "K", depth_chart_rank, "fg_att_per_game", fg_att_per_game_raw),
            games_played,
            shrinkage_strength,
        )
        pat_att_per_game = season_regressed_stat(
            pat_att_per_game_raw,
            _lookup_baseline(baselines, "K", depth_chart_rank, "pat_att_per_game", pat_att_per_game_raw),
            games_played,
            shrinkage_strength,
        )
        fg_accuracy = season_regressed_stat(
            fg_accuracy_raw,
            _lookup_baseline(baselines, "K", depth_chart_rank, "fg_accuracy", fg_accuracy_raw),
            games_played,
            shrinkage_strength,
        )
    else:
        fg_att_per_game = fg_att_per_game_raw
        pat_att_per_game = pat_att_per_game_raw
        fg_accuracy = fg_accuracy_raw

    proj_fg_attempts = fg_att_per_game * proj_games
    proj_fg_made = proj_fg_attempts * fg_accuracy
    proj_pat_attempts = pat_att_per_game * proj_games

    low_sample = games_played < MIN_SEASON_GAMES

    return {
        "proj_fg_attempts": proj_fg_attempts,
        "proj_fg_made": proj_fg_made,
        "proj_pat_attempts": proj_pat_attempts,
        "fg_accuracy": fg_accuracy,
        "season_2025_games_played": games_played,
        "proj_games_2026": proj_games,
        "depth_chart_rank": depth_chart_rank,
        "context_changed": context_changed,
        "low_sample": low_sample,
        "no_historical_data": False,
    }
