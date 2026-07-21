# Edge Projection Engine — NFL (v1)

## Architecture (supersedes earlier draft)

Primary output per player per week: **Projected Fantasy Points** (a real number, computed under your exact league scoring rules). Edge, Draft Edge, and Wire Edge are all derived from this same projection at different time horizons — not separately-tuned percentile scores.

```
Projected Points → Edge (percentile rank, this week)
                 → Draft Edge (season-long projection, ranked)
                 → Wire Edge (short-horizon projection, next 2-3 weeks)
```

---

## Vegas-derived features (computed once per game, used everywhere)

**Implied team total** (de-vig'd from spread + total):
```
team_implied_total = (game_total / 2) - (team_spread / 2)
```
(spread negative = favored → higher implied total; positive = underdog → lower implied total)

**Game-script multiplier** — this is the piece that captures your instinct about garbage time / role shifts:
```
game_script = team_spread / 7    # normalized to "touchdowns of spread", capped at ±3
```
Positive game_script (underdog) → pass-attempt volume up, pass-catching-role volume up, early-down rush volume down.
Negative game_script (favorite) → rush-attempt volume up (clock-killing), pass volume down slightly.

This reuses data already flowing in through The Odds API for the matchup factor — same inputs, second job.

---

## Feature set and functional form, by position

All "baseline volume" features use an **exponentially-weighted moving average (EWMA)**, not a flat trailing-N average — recent games matter more, but it doesn't fully ignore earlier-season signal like a strict trailing-3 cutoff would. Half-life of ~2–3 games is a reasonable starting point (tunable).

### QB
- `baseline_attempts` = EWMA(pass attempts)
- `adj_attempts` = `baseline_attempts × (1 + β_script × game_script)`
- `ypa_matchup` = EWMA(yards/attempt) adjusted by opponent's pass defense efficiency allowed
- `td_rate` = **shrinkage-regressed** TD rate (see below) — this matters a lot for QB since TD rate is high-variance on small samples
- `int_rate` = shrinkage-regressed INT rate
- Rushing sub-term for mobile QBs: `EWMA(rush attempts) × ypc_matchup`, added separately

**Shrinkage/regression-to-mean for TD rate** (this is the real statistical fix for touchdown noise):
```
regressed_td_rate = (player_td_count + k × league_mean_rate) / (player_attempts + k)
```
`k` is a tunable pseudo-count (effectively "how many league-average attempts worth of skepticism to apply") — same logic as batting-average stabilization in sabermetrics. Higher `k` for positions/situations with fewer attempts (e.g., goal-line-only backs), lower `k` for high-volume passers.

### RB — split into two independent usage streams (this is the fix for your receiving-back scenario)
- `rush_share` = EWMA(carry share of team rush attempts)
- `target_share` = EWMA(target share of team pass attempts) — tracked completely separately, not blended into one "touches" number
- `team_rush_attempts_proj` = team's baseline rush volume × `(1 - β_script × game_script)` (favorites run more)
- `team_pass_attempts_proj` = team's baseline pass volume × `(1 + β_script × game_script)` (underdogs/trailing teams pass more)
- `proj_carries` = `rush_share × team_rush_attempts_proj`
- `proj_targets` = `target_share × team_pass_attempts_proj`
- Efficiency: `ypc_matchup`, `ypt_matchup`/catch rate, matchup-adjusted against opponent run/pass defense
- **Red-zone share tracked as its own feature**, separate from overall rush/target share — this catches the "closer" back who vultures TDs on low overall volume
- TD rate: shrinkage-regressed, split rushing-TD-rate and receiving-TD-rate

### WR / TE
- `target_share` = EWMA(share of team targets)
- `proj_targets` = `target_share × team_pass_attempts_proj` (same game-script-adjusted team volume as RB targets)
- `aDOT` (average depth of target) tracked as its own feature — differentiates possession-receiver floor from deep-threat ceiling/variance, matters for how tight vs. wide a player's outcome range is
- `catch_rate`, `yards_per_target` — matchup-adjusted
- `red_zone_target_share` — own feature, same logic as RB red-zone share
- TD rate: shrinkage-regressed

### Kicker (v1: intentionally simple, inherently the noisiest position)
- `proj_fg_attempts` ≈ f(team implied total, team red-zone-trip rate that stalls short of TD)
- `proj_pat` ≈ team's projected TDs
- Distance mix: season-average for now (see V2 flag below)

### DST
- `opponent_implied_total` — the single strongest input here (already flagged this earlier — the market's team-total estimate is doing most of the work for points-allowed tier and takeaway upside)
- `opponent_turnover_worthy_rate` — shrinkage-regressed EWMA of opponent giveaways, since turnovers are extremely noisy game-to-game
- `sack_matchup` = team's sack rate generated vs. opponent's sack rate allowed
- Yardage-allowed tier: derived from opponent's baseline offensive efficiency, not raw season yardage (avoids garbage-time yardage inflation skewing this)

---

## Player prop lines as direct model inputs (upgrade from "confirmation flag" to blended input)

Where available, sportsbook player props are a stronger signal than a simple agree/disagree check — they should be **blended into the projection itself**, not just used to flag disagreement.

**Yardage props (passing/rushing/receiving O/U lines)**: the posted line is effectively the market's expected value for that stat — use it directly as `market_projected_yards` for that category.

**Anytime TD odds → implied probability**:
```
implied_prob = 100 / (odds + 100)          # positive American odds
implied_prob = |odds| / (|odds| + 100)     # negative American odds
```
Raw implied probabilities across a game's field will sum to more than the true expected TD count (bookmaker vig) — de-vig by normalizing each player's implied probability against the sum of all implied probabilities for that game, scaled to the game's expected total TDs (itself derivable from `team_implied_total`). v1 can skip full de-vig and use raw implied probability as a feature directly; de-vig normalization is a cheap, worthwhile refinement once the pipeline exists.

**Explicit "player fantasy points" props** (where a book offers one directly): this is the single richest input available, since it already bakes in volume, efficiency, and game-script from the market's perspective — but flag that these use the book's own generic scoring format, not your league's exact rules, so treat it as a strong prior, not a literal final answer.

**Blending, not just flagging**: final projected stat becomes a weighted blend of your own model estimate and the market-implied estimate:
```
final_projected_stat = w_model × model_estimate + w_market × market_implied_estimate
```
`w_market` is exactly the kind of coefficient the regression calibration plan should fit empirically (does adding the market feature reduce out-of-sample error, and by how much) — don't guess this weight by hand for long.

**Missing data handling**: props don't exist for every player (deeper bench/waiver-caliber players especially) — when unavailable, fall back to the pure model estimate rather than treating it as zero, same principle as the earlier missing-market-data case.

---

## League scoring rules application

All raw stat projections above (yards, TDs, INTs, receptions, etc.) get run through your exact scoring rules — the yardage-per-point ratios, milestone bonuses (100/200-yard games), big-play bonuses (40+/50+ yard TDs), and DST tiers — via the `league_scoring_rules` config table. This is a straightforward function once the stat projections exist: apply your point values, sum them. Milestone/big-play bonuses in v1 are applied when the point estimate itself clears the threshold (documented limitation, see V2).

---

## Weight calibration plan (this is where your regression experience comes in)

**Target variable**: actual fantasy points scored in week N under your league's exact scoring rules.

**Model**: separate **Ridge or Elastic Net regression** per position group (QB/RB/WR/TE modeled independently — a QB's scoring drivers are structurally different from a WR's). Start linear/regularized rather than tree-based — keeps coefficients directly interpretable as "weights," which is both more debuggable for you and avoids overfitting on what will initially be a modest sample size.

**Features**: everything defined above, standardized (z-scored) before regression — regression needs the actual variance structure, not percentile ranks (percentile rank is a *display* transform applied to final output, not something to regress on).

**Critical validation rule: time-based train/test split, never random k-fold.** Since these are time-series-structured observations (weeks within a season), a random split would let the model train on information that's chronologically "from the future" relative to some test rows — this is the single most common mistake in sports projection modeling. Train on earlier weeks/seasons, validate on held-out later weeks.

**Sample size**: pool across all players within a position group across multiple seasons — you won't have enough games for any individual player to fit anything meaningful, but pooling positions gives real sample size fast.

**Baseline to beat**: before trusting the regression's weights, benchmark against a naive baseline (e.g., "just use last-3-game average") — if your model can't beat that out-of-sample, something's wrong with the features or the shrinkage constants, not the concept.

---

## Chicken-and-egg: what v1 ships with before we have data

We can't run the regression until Phase 3 gives us historical stats + historical odds in Supabase. So:
1. **v1 ships with reasonable manually-set starting weights** (informed by general fantasy analytics findings — e.g., target share is consistently the strongest single predictor of WR fantasy output; usage/opportunity metrics generally out-predict efficiency metrics)
2. **Once several weeks of real data flow through the pipeline, refit via the regression plan above** — this is an explicit calibration phase, not something deferred indefinitely

---

## V2 / stretch goals (flagged, not built now — revisit once v1 is fully working)

- **Full win-probability-based garbage-time model** (à la PFF's approach: gradient-boosted model over time/down/distance/field-position/score-differential) instead of the spread-proxy game-script adjustment — more accurate, especially for blowouts that weren't expected pre-game, but needs play-by-play data and a trained model
- **Weather integration** for outdoor games (wind/precipitation affecting passing efficiency and kicking specifically)
- **Offensive line injury tracking** as its own signal (affects both run-blocking efficiency and sack rate allowed, distinct from skill-position injuries)
- **Non-linear modeling (gradient boosting/XGBoost)** once you have enough data to justify it over linear regression — could capture interaction effects (e.g., game-script effect might not be purely additive with volume)
- **Coverage-scheme-specific matchup data** (man vs. zone efficiency splits) instead of blanket "opponent allows X to position"
- **Kicker distance-mix refinement** using actual drive-level red-zone-stall data instead of season averages
- **Distribution-based projections instead of point estimates** — simulate a full outcome range per player (floor/ceiling) rather than a single number, and use model/market disagreement width as a proxy for projection uncertainty. This would make Trade Edge and lineup decisions risk-aware, not just expected-value-aware — genuinely the highest-value v2 upgrade if you want to go further than the big public apps
- **Ensembling with public consensus projections** as an additional input/sanity check once your own model has a track record to compare against
