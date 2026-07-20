# Edge — Project Log

A running technical record of design decisions, milestones, and implementation details

for Edge, a personal fantasy sports decision-support platform (NFL + NHL). Written in

full technical detail for my own reference. When it's time to build resume bullets,

paste the relevant sections back into a chat and simplify/reframe for the target

audience (e.g. finance/quant roles — downplay specific tools like SQL/JSON, emphasize

statistical reasoning and self-directed execution instead).

---

## Phase 1 — Architecture & formula design (edge_formula_[nfl.md](http://nfl.md))

**Core architecture**: single underlying "Projected Fantasy Points" computed per player

per week, from which Edge (percentile rank, weekly), Draft Edge (season-long, ranked),

and Wire Edge (2-3 week horizon) are all derived — not three separately-tuned scores.

**Vegas-derived features** (computed once per game, reused across all positions):

- De-vig'd implied team total: `(game_total / 2) - (team_spread / 2)`
- Game-script multiplier: `team_spread / 7` (normalized to "touchdowns of spread,"
capped ±3) — positive (underdog) shifts volume toward passing/receiving roles,
negative (favorite) shifts toward rushing. This is a pregame linear proxy for the
play-calling shifts that occur as a game plays out — a deliberate simplification
vs. a full win-probability/play-by-play model (flagged as V2).

**EWMA over flat trailing-N averages** for all baseline volume features (attempts,

targets, carries) — half-life ~2-3 games, so recent form is weighted more heavily

without fully zeroing out earlier-season signal the way a hard trailing-3 cutoff would.

**Shrinkage/regression-to-mean for TD rate** (and INT rate, turnover-worthy rate):

`(event_count + k * league_mean_rate) / (attempt_count + k)` — an empirical Bayes

correction (posterior mean under a Beta-Binomial conjugate model) for the fact that

raw TD rate is extremely high-variance on small samples. Same statistical logic as

batting-average stabilization in sabermetrics. `k` (pseudo-count) is tunable per

situation — higher for low-volume/high-variance roles (goal-line backs), lower for

high-volume passers.

**Position-specific feature sets**:

- QB: EWMA attempts adjusted by game-script, matchup-adjusted YPA, shrinkage-regressed
TD/INT rates, separate rushing sub-term for mobile QBs.
- RB: deliberately split into two **independent** EWMA streams — `rush_share` and
`target_share` — rather than one blended "touches" metric, specifically so a
receiving back's role isn't diluted by declining rush share. Team-level rush/pass
volume projections scale with game-script in opposite directions
`team_rush_attempts_proj = baseline × (1 - β·game_script)`,
`team_pass_attempts_proj = baseline × (1 + β·game_script)`), and each player's
role-share multiplies against the relevant team-level projection. Red-zone share
tracked as its own separate feature (catches TD-vulture backs on low overall volume).
- WR/TE: target_share × game-script-adjusted team pass volume; aDOT tracked separately
to differentiate possession-receiver floor from deep-threat variance; red-zone
target share as its own feature.
- Kicker/DST: intentionally simple v1 (FG attempts as a function of implied team
total and red-zone-stall rate; DST driven primarily by opponent's implied total
and shrinkage-regressed turnover-worthy rate).

**Market data as a direct model input, not just a confirmation flag**: sportsbook

yardage props used directly as `market_projected_yards`; anytime-TD odds converted

to implied probability (American odds formula, de-vig by normalizing against the

game's full field, scaled to expected total TDs derived from implied team total);

explicit fantasy-point props (where available) treated as a single richest input

but flagged as using generic book scoring, not the league's exact rules. Final

projected stat is a weighted blend:

`final = w_model × model_estimate + w_market × market_estimate` — `w_market` is

explicitly meant to be fit empirically via the regression calibration plan (Phase 5),

not hand-picked long-term.

**League scoring rules application**: raw stat projections run through the league's

exact scoring rules (yardage-per-point ratios, milestone bonuses, big-play bonuses,

DST tiers) as a config-driven lookup — deterministic function application, no ML here.

**Weight calibration plan** (deferred until real outcome data exists): separate

Ridge/Elastic Net regression per position group, features z-scored, target = actual

fantasy points scored under the league's exact rules. **Time-based train/test split

is mandatory** — a random k-fold would leak chronologically-future information into

training rows for this time-series-structured problem (identified as the single most

common mistake in sports projection modeling). Baseline to beat before trusting

regression weights: naive last-3-game average.

**V2/stretch goals explicitly deferred**: full win-probability-based garbage-time

model (à la PFF, needs play-by-play data + trained model), weather integration,

O-line injury tracking as its own signal, non-linear modeling (gradient

boosting/XGBoost) once data volume justifies it, coverage-scheme-specific matchup

data, distribution-based (floor/ceiling) projections instead of point estimates,

ensembling with public consensus projections.

## Phase 2 — Infrastructure setup

- Deployed the full database schema (11 tables: teams, players, games,
player_game_stats, injuries, odds, adp, edge_scores, user_roster, trades,
trade_players) — designed as a single shared schema across NFL + NHL
`sport` field distinguishes them) rather than two parallel schemas, with
sport-specific stats absorbed into flexible JSONB columns so new metrics never
require a migration.
- `factor_breakdown` JSONB column on `edge_scores` designed specifically to power
a "why is this the score" explainability feature in the eventual UI/AI-assistant
layer (EdgeGM) — an explicit design choice favoring auditability over black-box
scoring.
- Odds and stats tables designed append-only (timestamped, never overwritten) to
preserve historical movement/trend data for future multi-season analysis.
- Row-Level Security left disabled for this single-user v1 (service-role key
bypasses it anyway for pipeline writes; flagged as something to revisit if
multi-user auth is ever added).
- Set up version control (GitHub) with a Python virtual environment, `.env`-based
secret management (service-role key never exposed to frontend, only the
publishable/anon key), and connected a frontend (Lovable) with two-way GitHub sync
plus a direct Supabase connection using the publishable key.
- Learned end-to-end technical environment setup (terminal usage, git workflows,
credential scoping, OAuth/token-based auth flows) from scratch — no prior
hands-on deployment experience — including real troubleshooting (DNS/env-var
misconfiguration, git remote/auth setup, GitHub personal access token scoping).



## Phase 3 — Data ingestion pipeline

- Built idempotent seed scripts for teams (32, hardcoded), players (from Sleeper's
free public API — filtered to rostered skill positions, ~991 after filtering,external_ids JSONB stores source IDs for future re-sync), and the full seasonschedule (from ESPN's public scoreboard API, 272 games).
- Built a Vegas odds ingestion pipeline (The Odds API) that de-vigs and computes
implied home/away team scores from spread + total lines, updates the `games`table, and inserts append-only per-sportsbook rows into `odds` (spreads/totals:75 games matched, ~2,064 odds rows across 9 sportsbooks).
- Designed fetch cadence to be budget-aware against a fixed 500-request/month API
quota — daily game-line fetches are cheap (~1 request regardless of game count),while player props are priced per-market-per-event and are scoped to a 7-dayrolling window and a curated set of 4 high-value market types(anytime TD, passing/rushing/receiving yards) to stay well within budget whilemaximizing signal density.
- **Debugging log** (real root-cause diagnosis, not guesswork):
  - Player-seeding script referenced `teams.idplayers.id` instead of the actual
  schema's `team_idplayer_id` primary keys — caught via direct Postgres errormessages, fixed by aligning code to the documented schema rather than guessing.
  - Player-team mapping initially left ~2,054 of 3,045 rows with a null team
  reference; diagnosed via targeted SQL count queries and found to be legitimatestale/unrostered entries from the source API rather than a mapping bug — filterlogic tightened (exclude players with no current team) rather than patchingsymptoms.
  - Game-seeding script's "cleared N existing rows" log line was misleading — it
  reported the *count found before deletion*, not a verified delete count. Rootcause: a stale prior-season (2025) dataset silently persisted alongside newlyinserted 2026 data after a reseed, discovered via a `group by extract(year fromgame_time)` diagnostic query showing a mixed 2025/2026 dataset instead of aclean single season. Resolved via a manual, verified table wipe plus fixing thescript's reporting to reflect actual delete confirmation, not a pre-delete count.
  - Odds-matching script failed to match any games post-reseed due to the above  
  stale-data issue (not a logic bug in the matcher itself) — confirmed via atemporary structured debug block that printed the exact match query, parameters,and row counts at each step, isolating the failure to bad upstream data ratherthan flawed matching logic.

## Phase 4 — Scoring engine implementation *(complete, pending real-data validation)*

- **[Done, tested]** Config-driven league scoring rules module (`league_scoring_rules.py`) — the
  league's exact custom ruleset (non-standard rules including return-yardage
  scoring, fumble-recovery-TD bonus, granular kicker/DST tiers) encoded as a
  structured lookup table, decoupling scoring-rule changes from scoring-engine logic.
  Verified via direct dict access (spot-checked passing TD value and DST points-allowed tier).
- **[Done, tested]** Statistical utility module (`stats_utils.py`): EWMA
  (parameterized by half-life, not a fixed window), shrinkage regression
  (`regressed_rate`, generalized beyond just TD rate to any count/attempt-based
  rate stat), and tiered bonus/point lookup helpers (`bucket_bonus`,
  `tiered_points`) shared across all position-specific scoring logic. Verified
  numerically: EWMA correctly overweights recent values vs. flat average;
  shrinkage regression confirmed by hand against the closed-form posterior-mean
  calculation `(event_count + k*mean) / (attempt_count + k)`; tier lookups
  confirmed against known config values.
- **[Done, tested]** Vegas-derived features module (`vegas_features.py`):
  derives team implied total and spread from already-ingested de-vig'd game
  data, computes game-script multiplier. Verified against a real seeded game
  row — correct sign/magnitude (underdog gives positive game-script).
- **[Done, tested]** Position-specific feature builders (`qb_features.py`,
  `rb_features.py`, `wr_te_features.py`, `kicker_features.py`,
  `dst_features.py`) — full implementation of the position-specific formulas
  from edge_formula_nfl.md, including the RB/WR-TE independent rush-share vs.
  target-share design (validated against the receiving-back/game-script
  scenario this project was originally motivated by) and shrinkage-regressed
  TD/turnover rates per position. All verified by hand against synthetic,
  internally-consistent test inputs — confirmed EWMA recency-weighting,
  game-script volume shifts, and shrinkage pull-toward-mean all behave
  correctly and in the expected direction. One real debugging lesson logged
  here: an early RB test used mutually-inconsistent synthetic inputs (a
  rush_share history that didn't match the corresponding rush_attempts
  history), which produced a misleadingly low projection — caught by manual
  inspection, not an automated check, and corrected by regenerating internally
  consistent test data. Worth remembering once real data flows in: rush/target
  share and raw attempt counts must be derived from the same underlying source
  to stay consistent.
- **[Done, tested]** Points calculator (`points_calculator.py`): converts each
  position's projected-stats dict into final fantasy points via
  LEAGUE_SCORING_RULES, using the bucket_bonus/tiered_points helpers. Two
  documented v1 simplifications: (1) RB/QB rushing big-play TD bonuses
  (40+/50+ yard) aren't applied to fractional projected TD counts, since bonus
  tiers are scoring-play-length-based, not expected-count-based — flagged in
  edge_formula_nfl.md as a known limitation; (2) kicker FG value uses a
  blended 40-49-yard tier as a proxy in lieu of full distance-mix modeling
  (also an explicit v1 flag in the formula doc), and DST sack/turnover point
  contributions are approximated via an assumed opponent-play-count constant
  rather than a full projected-plays model. All five position calculators
  tested end-to-end (feature builder to points calculator) with synthetic
  data, producing realistic point ranges (QB ~18, RB ~17, WR ~15, K ~12,
  DST ~9).
- **[Written, untested]** Orchestration layer (`compute_edge_scores.py`):
  pulls each player's recent stat history plus upcoming game context from the
  database, routes to the correct position's feature builder and points
  calculator, computes percentile-rank Edge scores within each position
  group, and writes results to edge_scores with factor_breakdown populated.
  Cannot be tested end-to-end until player_game_stats is populated (Phase 3.6
  backfill) — the JSONB stat key names assumed here (e.g. pass_attempts,
  rush_yards, target_share) are placeholders based on common naming
  conventions and will need verification/adjustment against whatever schema
  the real backfilled data actually uses.

**Phase 4 status**: fully written (4.1-4.6). Steps 4.1-4.5 tested and verified
by hand against synthetic data. 4.6 is written and logically sound but
untestable until Phase 3.6 (backfill of 2025 season player stats) is done —
that backfill, plus fixing whatever field-name mismatches it reveals in
compute_edge_scores.py, is the next work session.



## For later: resume bullet synthesis

When ready to draft resume bullets, paste the relevant phase(s) above back into a

chat along with target audience (e.g., quant/finance roles — avoid naming specific

languages/tools like SQL, Python, JSON as the headline; foreground the statistical

methods, self-directed scope, and forecasting/decision-framework angle instead) and

we'll simplify/reframe from this source material.