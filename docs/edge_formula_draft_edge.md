# Draft Edge — Draft Priority Score (DPS) Specification

**Status: PRODUCTION — all six positions finalized, DPS written to `edge_scores`**

| Position | DPS spec status | Production `edge_scores` write |
|---|---|---|
| QB | Finalized and committed | Live (`draft_edge`, `period='2026-draftedge'`) |
| RB | Finalized and committed | Live |
| WR | Finalized and committed | Live |
| TE | Finalized — no adjustment (Δ = 0) | Live (`DPS = ADP`) |
| K | Finalized — no adjustment (Δ = 0) | Live (`DPS = ADP`) |
| DST | Finalized — no adjustment (Δ = 0) | Live (`DPS = ADP`) |

**Implementation:** `scoring/compute_draft_edge.py` writes DPS to `edge_scores`. `scoring/draft_priority_review.py` remains the read-only review tool. Shared computation lives in `scoring/draft_priority_score.py` (imported by both paths, not duplicated); review output was verified identical after the refactor.

**Weekly Edge (separate product):** The legacy **projected-points** season model in `edge_formula_nfl.md` is no longer the Draft Edge ranking for any position. It remains authoritative for **weekly Edge** scoring only.

---

## Why this replaced projected-points ranking

The old Draft Edge ranked purely by projected 2026 fantasy points built from 2025 season stats (volume × efficiency × shrinkage, adjusted by current depth chart).

That produced indefensible rankings because a one-season stats model cannot see:

- Offseason roster moves and free-agency context
- Coaching/scheme changes
- Training-camp and beat-reporter role signals
- Multi-year track record and reputation effects already in market prices

Examples from the legacy path: Lamar Jackson ranked QB15, Matthew Stafford QB2.

**Design pivot:** Market ADP (FFC 12-team full PPR) is the baseline **price**. The model only adjusts pick position where consensus is plausibly blind — specifically where 2025 counting stats and current depth-chart role imply mispricing of **touchdown luck (xTD)** and **role/opportunity (Role Shift)** relative to peers at the same position.

Projected fantasy points remain useful for **weekly Edge** via `edge_formula_nfl.md`; they are **not** the Draft Edge rank under this spec. TE/K/DST under this spec are **pure ADP** (`Δ = 0`) — see Position specs below.

---

## Core formula

```
DPS_i = ADP_i − (lambda_pos × Delta_i)

Delta_i = 0.44 × Z_xTD_i + 0.56 × Z_Role_i
```

**Units and direction**

- `ADP_i` and `DPS_i` are in **pick-number space** (same units as ADP: lower number = draft earlier).
- **Lower DPS = draft earlier** (model says take the player sooner than ADP).
- `Delta_i` is a unitless composite of within-position z-scores.
- **Positive Delta** → model thinks the player is **undervalued** vs ADP → DPS drops below ADP.
- **Negative Delta** → model thinks **overvalued** → DPS rises above ADP.

**Z-scoring (within position, 2025-data sample only)**

For each position separately, among players **with** 2025 stats (see Scope rules):

```
Z_xTD_i   = (xTD_Delta_i   − μ_xTD_pos)   / σ_xTD_pos
Z_Role_i  = (Role_Shift_i  − μ_Role_pos)  / σ_Role_pos
```

Population mean and standard deviation use **population** stdev (`pstdev`). If σ = 0, all z-scores for that component are set to 0.

Players without 2025 data are **excluded from the μ/σ sample** and receive `Delta = 0`.

**VORP / positional scarcity — deliberately excluded**

`Delta` does **not** include VORP, replacement level, or positional scarcity curves. ADP already prices positional scarcity (RB1 vs WR1 vs QB1 in a 12-team league). Adding scarcity to `Delta` would **double-count** market structure the model is trying to second-guess.

---

## Scope rules

**ADP required**

- Only players with an **ADP row** receive a DPS.
- Source: **FFC 12-team full PPR** (`adp` table; latest `fetched_at` per `player_id`).
- Players without ADP are undrafted in a 12-team, 15-roster-spot league (180 picks) and receive **no** `draft_edge` row — correctly out of scope (~**774 of 991** draft-pool players excluded for missing ADP).

**2025 stats**

- Positions in scope for DPS v1: **QB, RB, WR, TE, K, DST** (TE/K/DST are pure ADP — `Δ = 0`).
- Players **with ADP but no 2025 game stats** (rookies, never played):
  - `Delta = 0`, `DPS = ADP`
  - Flag: `no_2025_data`
  - **Excluded** from z-score sample (do not let them distort μ/σ)
  - Rationale: no model edge on players with zero NFL sample; defer fully to market.

**Flags (informational, do not zero Delta)**

- `context_changed` — primary 2025 team ≠ current `players.team_id`; Role Shift multiplied by `context_multiplier`.
- `low_sample` — 2025 volume below position thresholds (see `MIN_SEASON_GAMES`, `MIN_SEASON_SAMPLE` in `season_stats.py`).

---

## Production write

**Population:** ADP-eligible players only — **n = 217** (QB 26 / RB 53 / WR 78 / TE 23 / K 16 / DST 21). Players without ADP get no `draft_edge` row.

**Row count change:** ~**991** legacy projected-points rows → **217** DPS rows for `period='2026-draftedge'`. Stale legacy rows were explicitly deleted before upsert; delete count verified against pre-delete row count and post-delete residual check (same verification pattern as Phase 3 game seeding).

**Schema:**

- `score_type = 'draft_edge'`
- `period = '2026-draftedge'`
- `positional_rank` = DPS rank ascending within position (lower rank = draft earlier)
- `score_value` = null (rank-based, not /100)

**`factor_breakdown` contents:** `dps`, `adp`, `adp_positional_rank`, `delta`, `overall_rank`, position-appropriate z-components and raw components, `lambda_used`, and flags (`context_changed`, `low_sample`, `no_2025_data` where applicable).

**Cross-position overall board:** DPS is denominated in pick-number space for every position, so the overall rank is a straight ascending sort by `dps` — no normalization, VORP, or scarcity adjustment. Stored as `overall_rank` in `factor_breakdown`.

**Verification anchor (production run):**

- Overall top 5: Bijan (RB, DPS −2.20), Nacua (WR, 4.79), Gibbs (RB, 5.71), Chase (WR, 5.76), McCaffrey (RB, 6.47)
- WR anchor holds: Nacua WR#1, Chase WR#2, Lamb WR#3, JSN WR#6 (same as prototype at λ = 4)

**Writer:** `compute_and_write_draft_edge()` in `scoring/compute_draft_edge.py`.

---

## Position spec: QB

**`lambda_QB = 8.0`**

### xTD (touchdown luck vs expectation)

**2025 season totals** from `player_game_stats` (2025 season), aggregated via `aggregate_skill_season_totals`.

```
Expected_TDs = (pass_attempts × regressed_pass_td_rate)
             + (carries × regressed_rush_td_per_carry)

xTD_Delta = Expected_TDs − (passing_tds + rushing_tds)_actual_2025
```

**Shrinkage** — `regressed_rate` from `scoring/stats_utils.py`:

```
regressed_rate(events, attempts, league_mean_rate, k) =
  (events + k × league_mean_rate) / (attempts + k)
```

| Rate | k (`SEASON_TD_RATE_K`) | Prior | Denominator |
|---|---|---|---|
| Pass TD rate | `qb_pass_td` = **300** | `LEAGUE_MEAN_PASS_TD_RATE` = 0.045 | Pass attempts |
| Rush TD per carry | `qb_rush_td` = **50** | **Sample median** per-carry rate (`QB_RUSH_TD_PER_CARRY_PRIOR_PCTL` = 0.50) | **Carries** (not per-game) |

Positive `xTD_Delta` → scored **fewer** TDs than regression expected (unlucky) → model bumps draft priority.

### Role Shift (availability / games played)

```
Role_Shift = ((proj_games_2026 − games_played_2025) / 16.0) × context_multiplier
```

- `proj_games_2026` from `QB_GAMES_ESTIMATE_PRIOR` by `depth_chart_rank`: `{1: 16.0, 2: 1.5, 3: 0.5}`, default 0.2.
- `games_played_2025` = count of 2025 game rows with stats.
- Denominator 16.0 = `GAMES_NORMALIZER` (regular-season games scale).

Positive Role Shift → model expects **more** 2026 games than 2025 (e.g. backup promoted to starter).

### QB structural finding

QB ADP gaps are very wide (e.g. Josh Allen ~28, Joe Burrow ~48, Lamar Jackson ~56). A **4 / 8 / 12** `lambda_QB` sweep showed Delta cannot meaningfully reorder the QB board at any reasonable lambda — movement is minimal and elite QBs stay fixed. `lambda_QB = 8.0` retained for consistency with RB scale, not because it materially reshuffles QBs.

---

## Position spec: RB

**`lambda_RB = 8.0`**

### xTD

```
Expected_TDs = (carries × regressed_rush_td_rate)
             + (targets × regressed_rec_td_rate)

xTD_Delta = Expected_TDs − (rushing_tds + receiving_tds)_actual_2025
```

| Rate | k (`SEASON_TD_RATE_K`) | Prior |
|---|---|---|
| Rush TD rate | `rb_rush_td` = **200** | `LEAGUE_MEAN_RUSH_TD_RATE` = 0.045 |
| Receiving TD rate | `rb_rec_td` = **250** | `LEAGUE_MEAN_RB_REC_TD_RATE` = 0.055 |

Receiving-TD rate is shrunk heavily (k=250) because odd/even validation showed **no predictive signal**; see Empirical basis table.

### Role Shift (Weighted Opportunities)

Replaces v1 **rush-share-only** Role Shift. Receiving work counts via weighted opportunities.

```
W_TARGET = 2.51   # derived; see Empirical basis (runtime ~2.5107 from 2025 data)

WO_player = carries + (W_TARGET × targets)

actual_WO_share = WO_player / team_RB_backfield_WO
```

**Team RB backfield denominator (not DEF-row all-targets total)**

```
team_RB_backfield_WO = Σ WO_player  for all RBs whose primary 2025 team
                       (mode of per-game team_id in 2025) equals that team
```

Do **not** use DEF-row `team_carries + W_TARGET × team_targets` as denominator — DEF-row targets include WR/TE and understate RB share.

```
Role_Shift = (RB_WO_SHARE_PRIOR[depth_chart_rank] − actual_WO_share)
             × context_multiplier
```

**`RB_WO_SHARE_PRIOR`** (depth_chart_rank → expected WO share):

| Rank | Prior |
|---|---|
| 1 | 0.603 |
| 2 | 0.292 |
| 3 | 0.090 |
| 4+ | 0.046 |

Positive Role Shift → model expects **higher** WO share in 2026 than 2025 usage implied (under-utilized vs depth-chart prior).

### RB lambda selection

**3 / 5 / 8 / 12** sweep: movement scales smoothly; same players move in the same direction at every lambda. **8.0** leaves elite tier (Bijan / Gibbs / McCaffrey) intact while capturing mid-round moves (e.g. Hampton +3, Henry −4, Taylor −3, Barkley +2). **12.0 rejected** — reorders top 5; RB points curve is steepest at the top (a 3-spot fade in round 1 costs far more than the same move at RB25).

---

## Position spec: WR

**`lambda_WR = 4.0`**

WR is the first position under this architecture with a **three-term** `Delta` and a **pure-opportunity xTD** model with **no player TD rate**. Both departures are data-driven — WR is not a drop-in template for TE (see Not yet done).

### xTD (pure opportunity — no player TD rate)

WR **does not** use `regressed_rate` / shrunken player TD-per-target. Odd/even 2025 validation showed **zero year-over-year persistence** for WR TD-per-target (Spearman **−0.0125**, r² **= 0.0016**). Targets-per-game control, by contrast, persists strongly (r² **= 0.6975**). xTD is therefore a volume-only expectation:

```
xTD_Delta = (WR_BETA_TGT × targets_2025)
          + (WR_BETA_AY × receiving_air_yards_2025)
          − receiving_tds_2025
```

Positive `xTD_Delta` → scored **fewer** receiving TDs than the opportunity model expected (unlucky) → model bumps draft priority.

The legacy WR/TE shrunk rec-TD path (`SEASON_TD_RATE_K["wr_te_rec_td"]`) remains in `season_stats.py` for other engines; the WR DPS path does not use it.

### Role Shift (shrunk per-game WOPR vs depth-rank median)

Replaces v1 **season target-share** Role Shift (`WR_TARGET_SHARE_PRIOR`). Role is measured as weighted opportunity share **per game**, restricted to games the player appeared in, then shrunk toward a depth-rank prior.

**Per-game WOPR** (exact per-game team restriction — not a season-total ratio):

For each 2025 game row where the player has stats, using that game's DEF-row team targets and air yards:

```
WOPR_game = 1.5 × (player_targets / team_targets_that_game)
          + 0.7 × (player_air_yards / team_air_yards_that_game)

WOPR_pg = mean(WOPR_game) over appeared-in games
```

**Shrinkage toward rank median:**

```
shrunk_WOPR_pg = (GP × WOPR_pg + WR_WOPR_SHRINK_K × WR_WOPR_PG_MEDIAN[rank])
               / (GP + WR_WOPR_SHRINK_K)

Role_Shift = (WR_WOPR_PG_MEDIAN[rank] − shrunk_WOPR_pg) × context_multiplier
```

**`WR_WOPR_PG_MEDIAN`** (depth_chart_rank → median per-game WOPR, 2025, ≥ 4 GP):

| Rank | Median |
|---|---|
| 1 | 0.6856 |
| 2 | 0.5017 |
| 3 | 0.3306 |
| 4 | 0.2078 |
| 5+ / null | 0.1387 (`WR_WOPR_PG_DEFAULT`) |

Positive Role Shift → model expects **higher** per-game WOPR in 2026 than 2025 usage implied (under-utilized vs depth-chart prior).

### Availability (games missed)

Per-game WOPR conflates **role quality** with **games played** — a WR who missed games looks artificially low on WOPR_pg even if role per active game was strong. Availability is split out as a third raw component:

```
Availability = (WR_PROJ_GAMES − games_played_2025) / GAMES_NORMALIZER
```

- `WR_PROJ_GAMES = 17.0` — flat across depth ranks (WR2/WR3 play full seasons; no rank-tier prior like QB).
- `GAMES_NORMALIZER = 16.0` — shared with QB/RB.

Positive Availability → model expects **more** 2026 games than 2025 (missed time in 2025).

### Three-term Delta (unlike QB/RB)

QB/RB use two components at **0.44 / 0.56**. WR uses three z-scored components at **0.20 / 0.50 / 0.30**:

```
Delta_WR = 0.20 × Z_xTD + 0.50 × Z_Role + 0.30 × Z_Avail
```

**Why three terms:** per-game WOPR alone mixed role signal with availability; splitting Availability avoids penalizing high-per-game WOPR players who simply missed games. A weight sweep (Sets A–F) at `lambda_WR = 8.0` selected **Set C** (xTD-down, role-led). The three raw components are **near-orthogonal** in the 2025 z-sample (pairwise |r| **< 0.03**, n = 66), so the split is stable rather than double-counting.

Z-scoring rules unchanged: population μ/σ within WR, 2025-data sample only; σ = 0 → z = 0.

### WR lambda selection

**2 / 3 / 4 / 5 / 6 / 8** sweep at Set C weights: WR ADP is denser than QB — same `Delta` moves more rank spots at higher lambda. **4.0** keeps the elite tier intact (only JSN / Lamb λ-sensitive); **8.0** over-promotes Lamb and distorts JSN. At λ = 4: mean |Move| ≈ 0.92, max |Move| = 5, n_move ≥ 5 = 1.

Prototype board at λ = 4 (Set C): Nacua DPS#1, Chase #2, Lamb #3 (+3), JSN #6 (−3) — confirmed unchanged in production write.

---

## Position spec: TE

```
Δ_TE  = 0
DPS_TE = ADP_TE
```

TE receives **no model adjustment** off market ADP. This is a deliberate, evidence-backed conclusion — not an unfinished position. `lambda_TE` is **not applied**.

### Sample

- **n = 23** TEs with an ADP row (vs **78** WR).
- Draft pool **216** TEs; **97** with 2025 stats and ≥ 4 GP.

### `depth_chart_rank` is unusable for TE

All **32** teams show exactly one rank-1 and one rank-2 TE — mechanical reseed assignment, not football. Every TE in the top 25 by Draft Edge (legacy projected-points) rank reads `dc_rank = 1`, including Isaiah Likely (36 targets) and Cade Otton. Rank-3 and rank-4 per-game WOPR medians are inverted (**0.0812** vs **0.0925**). Role Shift in the `rank_median − actual` form has **no valid baseline**.

### Unmeasurable mechanisms

The plausible TE-specific TD drivers — **red-zone role** and **snap alignment** (inline vs slot/wide) — are absent from the stats JSONB. Only `targets` and `target_share` are available among target-related keys; **no red-zone-like keys** exist across **3,362** scanned game rows (TE+WR). Flagged for revisit if a data source providing these is added.

### Pooled WR+TE opportunity fit failed

Odd/even 2025 split, **n = 112** (WR **79** / TE **33**), ≥ 20 targets both halves. Directions disagree materially:

| Direction | Best `(β_tgt, β_ay)` |
|---|---|
| odd → even | **(0.054, 0.0000)** |
| even → odd | **(0.038, 0.0016)** |

Direction A wants the air-yards term at zero — i.e. a different model. At the selected midpoint **(0.046, 0.0008)**, TEs are systematically under-predicted on the odd holdout: TE mean residual **−0.62** vs WR **+0.22**, ratio to pooled stdev **0.55**. Pooling averaged over a position effect rather than absorbing it.

### TE-only shrunken rate lost to baselines

TE TD-per-target showed **apparent** persistence (odd/even r² **= 0.1184**, Spearman **0.346**, n **= 33**) against WR's r² **= 0.0039** in the same run — but a shrunken player rate failed the out-of-sample gate:

| Direction | Best k (RMSE) | Shrunken RMSE | vs opportunity (WR stored / pooled) | vs flat TE mean |
|---|---|---|---|---|
| odd → even | k = 260 | 1.5964 | **loses** (1.5229 / 1.5239) | beats (1.6034) |
| even → odd | k = 400 | 1.5331 | beats (1.7776 / 1.6513) | **loses** (1.5323) |

Dir B improves monotonically to the grid endpoint (k = 400) — i.e. asks for the **league mean with no player rate**. Midpoint k = 330 fails in both directions. Best-RMSE and best-Spearman k disagree in both directions (**260 vs 0**; **400 vs 70**). **Not sharply identified; loses to the baseline.**

### The apparent persistence was one player

Leave-one-out on the worst residual (**Trey McBride**, both directions) moves the fitted k from **260 → 50** (Dir A) and **400 → 80** (Dir B) — a **4–5×** swing from removing one of 33. The r² = 0.1184 reflected high-volume TEs scoring more TDs, not stable per-target conversion skill.

### Availability alone was rejected

A one-term `Δ = Z_Avail` would rank TEs purely by 2025 games missed — an injury list, not a draft board — and inherits the durability-penalty asymmetry with no offsetting signal.

---

## Position spec: K

```
Δ_K  = 0
DPS_K = ADP_K
```

Kicker receives **no model adjustment** off market ADP. `lambda_K` is **not applied**.

- Kicker output is a function of **team offensive quality** and **drive-stall luck**, not kicker skill — the position ADP already prices well.
- Phase 4.9's leakage-safe backtest found the weekly kicker model **failed to beat a naive last-3-game average** (MAE **4.45** vs **4.44**) on opportunity/efficiency features alone. Different score type, same underlying signal problem.
- The one differentiating lever identified in `edge_formula_nfl.md` — **implied team total scaling** — requires Vegas data that does not exist for 2026 this far before the season.
- ADP-eligible K pool is thin.
- **No gate was run:** the mechanism is absent rather than untested. Flagged for revisit once 2026 odds exist, though a season-long draft ranking is a poor fit for a signal that is inherently week-to-week.

---

## Position spec: DST

```
Δ_DST  = 0
DPS_DST = ADP_DST
```

DST receives **no model adjustment** off market ADP. `lambda_DST` is **not applied**.

- DST fantasy value is overwhelmingly **matchup-driven** week to week; the season-long component is defensive quality, which the market prices efficiently.
- One plausible untested hypothesis: **turnover-rate regression** (a defense with an abnormal 2025 fumble-recovery share should regress, and ADP may not adjust) — the same logic as xTD applied to takeaways.
- **Not tested** because n = **32** teams, thinner in effective terms than TE's n = 23, and the TE result demonstrated that a sample this size can have its fitted constant swung **4–5×** by a single outlier (McBride leave-one-out: k 260 → 50 and 400 → 80).
- **Decision:** anchor fully to ADP. Flagged for revisit if multi-season DEF data accumulates enough to make the turnover-regression hypothesis testable.

---

## Shared definitions

**Primary 2025 team**

Mode of `team_id` across 2025 `player_game_stats` rows (per-game team, not `players.team_id`).

**`context_changed`**

`True` if primary 2025 team ≠ current `players.team_id` (offseason trade / signing).

**`context_multiplier`**

```
context_multiplier = 0.85   if context_changed
                   = 1.0    otherwise
```

Hand-set, never empirically tested — flagged for future fit.

**Depth chart**

`players.depth_chart_rank` — **current 2026** reseed value. No persisted 2025 depth-chart snapshot exists.

**Team volume (legacy / other features)**

DEF-row season totals (`player_game_stats` for each team's DEF player) provide team carries and pass attempts for other engines. RB WO share denominator uses **RB-only backfield sum** as above.

---

## Empirical basis for every constant

| Constant | Value | How chosen | Status |
|---|---|---|---|
| `DELTA_XTD_WEIGHT` | 0.44 | Hand-set starting split between xTD and Role | **HAND-SET** |
| `DELTA_ROLE_WEIGHT` | 0.56 | Hand-set (complement of 0.44) | **HAND-SET** |
| `lambda_QB` | 8.0 | 4/8/12 sweep; minimal QB reordering at any value | **HAND-SET** |
| `lambda_RB` | 8.0 | 3/5/8/12 sweep; stable signal, elite tier intact at 8 | **HAND-SET** |
| `lambda_WR` | 4.0 | 2/3/4/5/6/8 sweep (Set C weights); elite tier intact; JSN/Lamb λ-sensitive | **HAND-SET** |
| `WR_BETA_TGT` | 0.034 | Opportunity xTD target coefficient | **FITTED — not sharply identified** (odd 0.030 / even 0.038) |
| `WR_BETA_AY` | 0.0017 | Opportunity xTD air-yards coefficient | **FITTED — not sharply identified** (odd 0.0018 / even 0.0016) |
| `WR_WOPR_SHRINK_K` | 2.5 | WOPR_pg shrinkage toward rank median | **FITTED — not sharply identified** (odd k=1.5 / even k=3.0, flat curve) |
| `WR_WOPR_PG_MEDIAN` | 0.6856 / 0.5017 / 0.3306 / 0.2078 | 2025 median per-game WOPR by depth rank (≥ 4 GP) | **FITTED** |
| `WR_WOPR_PG_DEFAULT` | 0.1387 | Rank 5+ / null WOPR prior | **FITTED** |
| `WR_PROJ_GAMES` | 17.0 | Flat projected games for Availability | **HAND-SET** |
| `DELTA_WR_XTD_WEIGHT` | 0.20 | WR three-way Δ split (Set C sweep) | **HAND-SET** |
| `DELTA_WR_ROLE_WEIGHT` | 0.50 | WR three-way Δ split (Set C sweep) | **HAND-SET** |
| `DELTA_WR_AVAIL_WEIGHT` | 0.30 | WR three-way Δ split (Set C sweep) | **HAND-SET** |
| `Δ_TE` / `DPS_TE` | `0` / `ADP_TE` | Task 2 head-to-head: shrunken rate loses gate both directions (Dir A 1.5964 vs opp 1.5229/1.5239; Dir B 1.5331 vs flat 1.5323); pooled opp unidentified; Role Shift baseline unusable | **VALIDATED — no usable signal** |
| `lambda_TE` | not applied (`Δ = 0`) | Placeholder 6.0 never used; TE is pure ADP | **N/A** |
| TE shrunken k (rejected) | — | Odd/even n=33; best-RMSE k 260 vs 400 disagree; Dir A loses to opportunity (1.5964 vs 1.5229/1.5239); Dir B loses to flat TE mean (1.5331 vs 1.5323); LOO on McBride swings k 4–5× | **REJECTED — no usable signal** |
| TE pooled `(β_tgt, β_ay)` (rejected) | — | n=112 (WR 79 / TE 33); directions 0.054/0.0000 vs 0.038/0.0016; TE odd-holdout mean residual −0.62 vs WR +0.22 | **REJECTED — not identified / masks position effect** |
| `Δ_K` / `DPS_K` | `0` / `ADP_K` | Mechanism absent (Vegas implied-total lever); weekly K failed naive in Phase 4.9 (MAE 4.45 vs 4.44); no DPS gate run | **DECISION — not tested (mechanism absent)** |
| `lambda_K` | not applied (`Δ = 0`) | K is pure ADP | **N/A** |
| `Δ_DST` / `DPS_DST` | `0` / `ADP_DST` | Matchup-driven season-long; turnover-regression hypothesis untested (n=32 too thin; TE LOO showed 4–5× k swing at similar n) | **DECISION — not tested (sample insufficient)** |
| `lambda_DST` | not applied (`Δ = 0`) | DST is pure ADP | **N/A** |
| `context_multiplier` | 0.85 | Hand-set discount on role shift after team change | **HAND-SET** |
| `GAMES_NORMALIZER` | 16.0 | NFL regular-season games scale | **HAND-SET** |
| `SEASON_TD_RATE_K["qb_pass_td"]` | 300 | Odd/even 2025 split (n=33); RMSE min in 300–400 band; chronological splits disagree | **VALIDATED — not sharply identified** |
| `SEASON_TD_RATE_K["qb_rush_td"]` | 50 | Odd/even split (n=37); per-carry beats per-game; k=50–100 plateau ~0.7% RMSE spread | **FITTED — not sharply identified** |
| QB rush-TD prior | Median (p50) | p25 prior underperformed on odd/even split | **FITTED** |
| `SEASON_TD_RATE_K["rb_rush_td"]` | 200 | Odd/even split (n=64, ≥20 carries both halves); plateau k=150–400, all beat baseline | **VALIDATED — not sharply identified** |
| `SEASON_TD_RATE_K["rb_rec_td"]` | 250 | Odd/even (n=52); RMSE improves toward k=250 but **still loses** to prior×opportunities baseline; Spearman 0.31–0.41 | **UNIDENTIFIED** — k raised to near-zero player influence |
| `W_TARGET` | 2.51 | Derived from 2025 RBs (n=63, ≥50 carries), full-PPR league rules: ppc 0.6547, ppt 1.6438 | **FITTED (derived)** |
| `RB_WO_SHARE_PRIOR` | 0.603 / 0.292 / 0.09 / 0.046 | 2025 median WO share by depth rank (n=110, ≥4 games, RB-only denominator) | **FITTED** |
| `LEAGUE_MEAN_*_TD_RATE` | see `season_stats.py` | League constants for shrinkage priors | **HAND-SET** |
| `QB_GAMES_ESTIMATE_PRIOR` | 16 / 1.5 / 0.5 | Depth-chart games prior | **HAND-SET** |

---

## Known limitations

1. **Backfield composition is inferred from `depth_chart_rank`, not measured** — vacated or arriving touches are invisible unless the depth chart moved. Multi-team touch tracking deferred.

2. **WO share priors use CURRENT (2026) `depth_chart_rank` against 2025 usage** — promoted/demoted players contaminate rank buckets; likely biases rank-1 median slightly low.

3. **Within-rank share dispersion is wide** — e.g. RB rank 1: p10 = 0.32, p90 = 0.77; a single median prior is coarse.

4. **Z-scoring normalizes away uniform shifts** — any change that moves all players' raw xTD or Role Shift by the same amount does **not** change `Delta` or DPS. Only **relative** reordering within the position moves the board. Example: fixing a 17% prior-sum bias on RB WO priors produced only one z-score sign flip across 50 RBs.

5. **No 2026 market / Vegas features** — no odds or game-script data exists this far before the season; not in DPS v1 (K revisit flagged once 2026 odds exist).

6. **QB Role Shift uses flat 16 games for QB1** — ignores 2025 injury/availability history except via `games_played_2025`.

7. **WR Availability rewards missed games unconditionally** — no distinction between fluke and chronic injury; no injury-history data. Any 2025 games missed increase Availability equally.

8. **WR xTD cannot see offseason target competition** — a WR1's TD-luck rebound is projected without regard to a new arrival in the receiving corps (e.g. vacated or competing targets invisible to the opportunity model).

9. **WR `proj_games` is flat 17 across depth ranks** — Availability is a pure games-missed count, not a depth-chart games expectation like QB.

10. **TE is anchored fully to market ADP** — any TE mispricing the market carries through unchanged (`Δ_TE = 0`). Revisit if red-zone or snap-alignment data becomes available.

11. **DPS can go below 1.0 for elite players** — e.g. Bijan at −2.20 means the model wants them earlier than the first pick, which is unrepresentable in a real draft. A display-layer floor at 1.0 is appropriate; the stored value is left unclamped.

12. **The overall board is RB-weighted at the top** — 7 of the first 15 overall are RBs. Partly genuine full-PPR RB scarcity, partly that RB carries both the largest λ among adjusted positions and the most active Δ.

13. **Three of six positions (TE, K, DST) pass ADP through unchanged** — model contribution is concentrated in QB, RB, and WR.

14. **FFC ADP is scoped to 12 teams** (`ADP_TEAMS` in `seed_adp.py`), which matches this league. Re-seed required if league size changes.

---

## Not yet done

- [ ] Empirical fit of QB/RB `Delta` weights (0.44/0.56) and `context_multiplier`
- [ ] 2025→2026 outcome validation for lambdas and priors
- [ ] Persisted 2025 depth-chart snapshot (or inferred role from usage alone)
- [ ] TE revisit if red-zone / snap-alignment data becomes available
- [ ] K revisit once 2026 odds exist (implied-team-total lever)
- [ ] DST revisit if multi-season DEF data accumulates enough to test turnover-regression hypothesis

---

## Data dependencies (rebuild checklist)

| Input | Source |
|---|---|
| ADP | `adp` table, FFC 12-team PPR, latest per player |
| 2025 stats | `player_game_stats` joined to `games.season = 2025` |
| Current team / depth chart | `players.team_id`, `players.depth_chart_rank` |
| Team DEF-row totals | DEF `player_game_stats` rows (team box score) |
| League scoring (W_TARGET derivation) | `config/league_scoring_rules.py` + `points_calculator.calculate_rb_points` |
| Shrinkage k table | `season_stats.SEASON_TD_RATE_K` |
| Shared DPS computation | `scoring/draft_priority_score.py` |
| Production writer | `scoring/compute_draft_edge.py` → `edge_scores` |
| Read-only review | `scoring/draft_priority_review.py` |

---

## Related documents

- **Weekly Edge / shared projection math:** `edge_formula_nfl.md` (authoritative for weekly Edge only — not Draft Edge)
- **Calibration log:** `PROJECT_LOG.md` Phase 5.1 (QB + RB + WR + TE; K/DST Δ = 0 in this spec)
- **Production Draft Edge writer:** `scoring/compute_draft_edge.py` (DPS → `edge_scores`)
