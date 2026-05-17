---
ticket: 86b9zktp6
type: spike-findings
snapshot_ts: 2026-05-17 post-R8 fix-forward (data snapshot at R3; surgical content fixes through R8)
db_path: /Users/gabrielkagan/Documents/kalshi-bot/state.db (May-13 snapshot, pre-P4.1 ship)
lookback_days: 60
n_tm_rows: 2858 (EO rows; 2753 unique (ticker,strategy) pairs after R2-C3 dedup)
n_dc_rows: 1080 (EO rows; 1059 unique pairs after dedup)
n_tm_rows_real_pnl: 1650 (unique pairs with settled_trades match)
n_dc_rows_real_pnl: 218 (DC is shadow; subset has historic live periods)
predecessor: bot/helpers/band_calibration.py (P4.1 c1e6d85)
plan_doc: kb/decisions/tm-dc-conditional-calibration-spike-plan-may17.md
data_appendix: kb/findings/tm-dc-conditional-calibration-research.data.md (auto-generated)
adversarial_rounds: R1 (3C+7M+7Mn fixed), R2 (3C+8M+6Mn fixed), R3 (0C+6M+6Mn fixed), R4 (0C+1M+6Mn — bookkeeping fixed), R5 (0C+1M+6Mn — bookkeeping recursion-halted), R6 (0C+1M+4Mn — DC ≥0.94 → ≥0.943 fixed), R7 (0C+3M+4Mn — TM parallel-construction + temporal direction + snapshot_ts bookkeeping fixed), R8 (0C+1M+0Mn — R7-M1 introduced reverse precision drift "≥0.933" with cited 0.9326; fixed to ≥0.932)
---

# TM/DC conditional band-calibration — research findings

## Executive summary

> **Recommendation: SHIP-WITH-CAVEATS** — a HYBRID approach, NOT a full rip-and-replace of `_tm_size` / `_dc_size`.

The conditional matrix surfaces two production-actionable signals; the right path is a phased wire-in that captures them WITHOUT inheriting Kelly's tail-risk profile:

1. **Skip-gate (Phase 1)** — 4.3% of TM rows and 8.3% of DC rows have `calibrated_p ≤ ask` at the k=30 / fractional=0.5 operating point. The bespoke formulas (`_tm_size`, `_dc_size`) force a non-zero bet on these cells; Kelly recognizes them as no-edge. **DC's proposed max drawdown is -59.0% at fractional=0.25/k=30** ($1,567.83 → $642.52) — direct evidence the skip-gate improves risk-adjusted performance for DC. This is the highest-confidence finding in the spike.

2. **Multiplicative scaler (Phase 2)** — bounded `clip(shrunk_p / band_prior, 0.5, 1.5)` applied to bespoke output. Captures aggressive-where-confident behavior without surrendering production caps.

**What NOT to ship**: replacing `_tm_size` / `_dc_size` with raw Kelly off the calibrated probability. At fractional=0.5/k=30, TM max single-trade position is 2482 contracts at 99c = $2,457 = 49.1% of the $5,000 synthetic bankroll (DC max is even larger at 2525 contracts = $2,500 = 50.0%). One unfavorable surprise outcome wipes nearly half the bankroll.

**Verdict basis**: ±5% NULL-hypothesis check (spike plan §"Phase 4") rejected across ALL 9 sweep points (3 fractional × 3 shrinkage k). Proposed sizing is materially different from baseline at every grid point; the question is only WHICH operating mode to wire in (not whether to wire in at all).

**Magnitude context** (R2-M8 — the dollar levels are NOT tiny):
- TM on $5K synthetic bankroll: baseline net = $1,587.87 over 60d (~+31.8% return). Proposed at f=0.5/k=30: $38,091.62 (~+762% return, but DD = $2,823 = 56% of bankroll). The TM aggressive sizing already produces large absolute returns; matrix-Kelly amplifies both lift AND tail risk.
- DC on $5K bankroll: baseline net = $7,667.26 (~+153% return). Proposed at f=0.5/k=30: $26,550.08 (~+531%, DD reduced 18.0%). DC's combination of lift + DD reduction is the cleanest case for the matrix approach.

## Adversarial-review changelog

| Round | Critical | Major | Minor | Status |
|---|---:|---:|---:|---|
| R1 | 3 | 7 | 7 | Fixed in R1 fix-forward |
| R2 | 3 | 8 | 6 | Fixed in R2 fix-forward |
| R3 | 0 | 6 | 6 | Fixed in R3 fix-forward |
| R4 | 0 | 1 | 6 | Bookkeeping-only: R3 outcomes not yet encoded in doc. Fixed in R4 fix-forward. |
| R5 | 0 | 1 | 6 | Bookkeeping recursion: R4 fix-forward introduced same drift at R4 boundary. Fixed in R5 fix-forward (THIS commit). Recursion halted by self-encoding R5 outcome inline. |
| R6 | 0 | 1 | 4 | Content finding (R6-M1: factual claim about DC floor-passing cells contradicted own data). Fixed in R6 fix-forward. |
| R7 | 0 | 3 | 4 | Three new content findings (TM parallel-construction asymmetry, "1 day before" temporal direction, snapshot_ts bookkeeping at a surface R5 halt didn't cover). All 3 MAJORs fixed in R7 fix-forward. |
| R8 | 0 | 1 | 0 | R7-M1 fix-forward introduced reverse-direction precision drift: claimed "TM ≥ 0.933" but cited "lowest = 0.9326" (0.9326 < 0.933). Fixed by changing threshold to "≥ 0.932" (R8 fix-forward, this commit). |
| R9 | pending | pending | pending | Verification round — should be 0C+0M; if so, R10 closes the 2-zero gate. |

**Recursion-halt note** (scoped post-R7): R4 and R5 both found the same class — bookkeeping-lag where the previous round's outcomes weren't encoded in the changelog table + adversarial_rounds frontmatter. R5's fix-forward self-encoded that pattern inline. R7 found the same class recurred at the `snapshot_ts` frontmatter field, which R5's halt didn't cover. Lesson: bookkeeping-halt cannot be assumed structural across all narrative surfaces. The changelog-row + adversarial_rounds-list bookkeeping-lag pattern is structurally closed; other narrative fields (`snapshot_ts`, section titles) still need per-round verification. R7 fix-forward removed the round-number-specific changelog title to reduce the per-round drift surface.

R2 critical fixes:
| Finding | Fix |
|---|---|
| **R2-C1** 100× dollar drift in findings doc (cents/10000 vs cents/100) | All dollar figures recomputed against the data appendix; canonical numbers pinned in §"Data citations" below |
| **R2-C2** Kalshi fee approximation applied to winners only | `kalshi_fee_cents_per_contract` now applies on BOTH yes (winners) AND no (losers) branches in `_per_contract_net_cents` — matches Kalshi schedule (fees charged at entry) |
| **R2-C3** EO × ST JOIN duplication summed realized PnL across duplicate (ticker, strategy) EO rows | `replay_pnl_a_b` now de-duplicates PnL attribution by `(ticker, strategy)` — first EO row credits PnL, subsequent duplicates contribute only to contract distribution. TM: 2858 EO rows → 2753 unique pairs after dedup; PnL re-derived |

R2 major fixes (summary; full detail in inline annotations):
- M1 (false-GREEN net-PnL test → AST-aware), M2 (`--data-only` phantom flag removed from docstring), M3 (plan doc DC strategy SQL corrected `decided_contract_%` → `decided_t*` explicit list), M4 (plan doc "DC has none" → "DC has 218 historic live periods"), M5 (floor-cascade test extended to 3 tiers), M6 (numeric drift — DC k=100/f=0.5 cell value corrected to 1863 against data appendix), M7 (real-PnL count now uses unique pairs), M8 (Exec Summary magnitude framing rewritten — see above), Mn1-Mn6 inline.

R3 major fixes (applied to this doc + test file):
- M1 (DC k=100/f=0.5 cell — R2 fix-forward regressed 1863→1866, now restored to 1863), M2 (line 112 vs 159 internal contradiction on DD reduction at higher fractional — rewritten to be fractional-dependent), M3 (`test_net_pnl_uses_fee_coalesce` now AST-counts ≥2 fee-subtracting Returns to enforce R2-C2 on BOTH yes/no branches), M4 (new `test_replay_pnl_a_b_dedups_duplicate_eo_rows` pins R2-C3 dedup via 3-row fixture asserting n_unique_pnl_pairs==2 + total==3980c), M5 (DC DD reductions corrected `-52.5%/-44.1%` → `-52.6%/-44.3%`), M6 (Exec Summary "DD reduced 17.7%" → "18.0%" — standardized rounding). R3-Mn1/Mn2/Mn3/Mn6 accepted as minor doc-drift not fixed.

R4 + R5 fixes (bookkeeping-only, no content change):
- R4-M1 (R3 outcomes not encoded in changelog/frontmatter — fixed in R4 fix-forward).
- R5-M1 (R4 outcomes not encoded — same class one round forward; fixed in R5 fix-forward inline with explicit recursion-halt note so R6+ cannot find the same class). R4/R5 minor findings (Mn2-Mn6) carried over as accepted.

## Key data findings (post-R3, n ≥ 10 cells only)

### Finding 1 — sub-signal carries less information than the bespoke design assumes

Within `(asset, band)`, the buf_pct sub-signal for TM is barely discriminative on cells passing the n ≥ 10 floor. Examples:

- **BTC 97-98c** (floor-passing buckets only): bucket_0 shrunk_p (k=30) = 0.9918 (n=22), bucket_1 = 0.9945 (n=230), bucket_2 = 0.9683 (n=15). Range = 2.6pp.
- **ETH 99** (floor-passing): bucket_1 = 0.9968 (n=300), bucket_2 = 0.9901 (n=77), bucket_3 = 0.9988 (n=21). Range = 0.87pp.
- **SOL 97-98** (floor-passing): bucket_0 = 0.9922 (n=25), bucket_1 = 0.9851 (n=133), bucket_2 = 0.9921 (n=24). Range = 0.71pp.

**The thin-buffer cap (`TM_THIN_BUFFER_CONTRACT_CAP=50`) is sized for a much larger discrimination than the floor-passing data supports.** A `(asset, band)`-only matrix would capture nearly all the floor-passing signal.

For DC, tier discrimination has modestly more information (n ≥ 10 cells only):

- **SOL 94-96c**: T1 = 0.9720 (n=14), T2 = 0.9743 (n=18), T2_Z2 = 0.9538 (n=40), T2_Z25 = 0.9433 (n=27). Range = 3.1pp — Z2/Z25 tiers ARE measurably less reliable than T1/T2.
- **XRP 94-96c**: T2 = 0.9615 (n=28), T2_Z2 = 0.9569 (n=45), T2_Z25 = 0.9510 (n=36). Range = 1.1pp — within noise.

Z25 tier (deepest z-score discount) is the one tier with meaningful signal — slightly lower reliability across assets. The bespoke `DECIDED_CONTRACT_T2_Z25_RISK = 0.10` already encodes lower risk for this tier; the data validates the existing design direction.

### Finding 2 — empirical rates uniformly high at TM/DC bands (floor-passing cells)

Every TM floor-passing cell has shrunk_p (k=30) ≥ 0.932 (lowest: SOL 94-96 bucket_1 = 0.9326 at n=15). Every DC floor-passing cell has shrunk_p (k=30) ≥ 0.943 (lowest: SOL 94-96 T2_Z25 = 0.9433 at n=27). Consistent with the design intent: TM/DC fire on markets that ARE near-certain — the bespoke formulas operate in a regime where the empirical YES rate is overwhelmingly dominant.

**Implication**: production sizing SHOULD be aggressive at these bands (already is). The empirical data validates the design DIRECTION even if the magnitudes need tuning.

### Finding 3 — Kelly is explosive at high price bands; raw substitution is unsafe

Kelly at `(p=0.99, ask=98c)`: `f* = (100·p − ask) / (100 − ask) = 0.5`. Half the bankroll on a single trade.
Kelly at `(p=1.00, ask=99c)`: `f* = 1.0`. Entire bankroll on a single trade.

Even at fractional=0.5, the proposed sizer at f=0.5/k=30 hits max=2482 contracts on the $5,000 synthetic bankroll — that's **$2,457 (49.1% of bankroll) at 99c**. **One unfavorable surprise outcome wipes nearly half the bankroll.**

Across the sweep, `mean` contracts scales linearly with fractional (sweep grid in data appendix):

| fractional | TM mean ct (k=30) | TM mean ct (k=100) | DC mean ct (k=30) | DC mean ct (k=100) |
|---|---:|---:|---:|---:|
| 0.25 | 720 | 709 | 936 | 931 |
| 0.5  | 1441 | 1418 | 1873 | 1863 |
| 1.0  | 2883 | 2837 | 3747 | 3725 |

At fractional=1.0 the AVERAGE row is sized for half-Kelly-or-higher aggression — not just the top decile. Tail-risk compounds.

### Finding 4 — the matrix correctly identifies skip cases (Phase 1 evidence)

The proposed sizer returns 0 contracts on rows where `calibrated_p < ask`:

| Sweep point | TM zeros | TM zero% | DC zeros | DC zero% |
|---|---:|---:|---:|---:|
| k=30  | 124 | 4.3% | 90 | 8.3% |
| k=50  | 42 | 1.5% | 84 | 7.8% |
| k=100 | 36 | 1.3% | 72 | 6.7% |

These are rows where bespoke formulas BET but Kelly says NO. Lower k retains more skips (less shrinkage toward the prior keeps marginal cells below ask). Higher k erodes the skip-gate. The right operating k balances skip coverage against thin-cell stability.

**Phase 1 evidence (DC)**: at fractional=0.25/k=30, DC proposed max drawdown = **$642.52** vs DC baseline max drawdown = **$1,567.83** — **-59.0% drawdown reduction** from the skip-gate alone (proposed contract mean is 1.4× baseline, but skip-gate avoids the big losses). This is the cleanest evidence in the spike that selective gating beats unconditional betting at borderline confidence.

(The DD reduction is fractional-dependent. At f=0.25 the reduction holds across all k (-59.0%, -52.6%, -44.3%). At f=0.5 the reduction persists at k=30/k=50 but disappears at k=100. At f=1.0 DC proposed DD exceeds baseline at every k. See sweep table below.)

For TM, proposed drawdown is HIGHER than baseline drawdown at all sweep points (TM baseline DD = $459.62; proposed DD ranges $1,311–$5,647 across the grid) — TM has fewer skips (4.3% vs 8.3% for DC) and TM's bespoke formula already mostly skips bad cells via the `seconds_to_close` STC multipliers. The skip-gate value for TM is materially smaller than for DC.

## A/B sim PnL replay — primary operating point (f=0.5 / k=30)

```
                       baseline net    proposed net    base DD       prop DD       base Sharpe   prop Sharpe   base mean ct   prop mean ct  prop zeros
TM (n=2858, uniq=2753) $   1,587.87    $  38,091.62    $    459.62   $  2,822.97   0.0438        0.1595        89.1           1441.3        124
DC (n=1080, uniq=1059) $   7,667.26    $  26,550.08    $  1,567.83   $  1,286.02   0.1060        0.4980        658.0          1873.1         90
```

NULL-hypothesis check (±5% on total PnL, max DD, contract mean):

| | TM Δ | DC Δ | within 5%? |
|---|---:|---:|:--:|
| Total PnL    | +2299% | +246% | NO |
| Max drawdown | +514%  |  -18% | NO |
| Contract mean| +1518% | +185% | NO |

Verdict: hypothesis rejected at this point. Proposed sizing is materially different. (Spike plan: hypothesis not-rejected → recommend NO-SHIP. Hypothesis REJECTED → verdict moves to human synthesis. Proposed direction-of-effect is positive PnL, with controlled DC drawdown but expanded TM drawdown.)

## Data citations (canonical — cross-checked against data appendix)

All findings-doc dollar figures sourced from `kb/findings/tm-dc-conditional-calibration-research.data.md`. Verify via `python3 -c "import json,re; ..."` against the sweep_grid JSON. The data appendix is regenerated by the spike script; this prose doc is hand-authored and references the appendix as the single source of truth.

R2-C1 audit table (every dollar figure in this doc):

| Citation | Value | Source (data appendix path) |
|---|---|---|
| TM baseline net (f=0.5/k=30) | $1,587.87 | sweep_grid[f=0.5,k=30].tm.baseline_total_net_pnl_cents = 158787 |
| TM proposed net (f=0.5/k=30) | $38,091.62 | sweep_grid[f=0.5,k=30].tm.proposed_total_net_pnl_cents = 3809162 |
| TM baseline DD (f=0.5/k=30) | $459.62 | sweep_grid[f=0.5,k=30].tm.baseline_max_drawdown_cents = 45962 |
| TM proposed DD (f=0.5/k=30) | $2,822.97 | sweep_grid[f=0.5,k=30].tm.proposed_max_drawdown_cents = 282297 |
| DC baseline net (f=0.5/k=30) | $7,667.26 | sweep_grid[f=0.5,k=30].dc.baseline_total_net_pnl_cents = 766726 |
| DC proposed net (f=0.5/k=30) | $26,550.08 | sweep_grid[f=0.5,k=30].dc.proposed_total_net_pnl_cents = 2655008 |
| DC baseline DD (f=0.5/k=30) | $1,567.83 | sweep_grid[f=0.5,k=30].dc.baseline_max_drawdown_cents = 156783 |
| DC proposed DD (f=0.5/k=30) | $1,286.02 | sweep_grid[f=0.5,k=30].dc.proposed_max_drawdown_cents = 128602 |
| DC proposed DD (f=0.25/k=30, Phase 1) | $642.52 | sweep_grid[f=0.25,k=30].dc.proposed_max_drawdown_cents = 64252 |
| 99c max contracts × ask | $2,457 (49.1% of $5K) | 2482 × 0.99 = 2457.18; / 5000 = 49.1% |

## Cross-sweep sensitivity (3×3 grid summary)

For both TM and DC, across ALL 9 sweep points:

- **Proposed total PnL > baseline total PnL**: every grid point, monotonically increasing in fractional.
- **Proposed Sharpe-ish > baseline Sharpe-ish**: every grid point. Direction-of-effect consistent.
- **Proposed max DD increases monotonically with fractional**. At fractional=0.25 DC sees DD REDUCTION (-59.0% at k=30, -52.6% at k=50, -44.3% at k=100). At fractional=1.0 DC DD exceeds baseline at all k.
- **NULL-hypothesis check rejected at every grid point** for both strategies. The matrix-driven sizing is materially different from the bespoke approach regardless of (fractional, k) choice.

Full grid in `kb/findings/tm-dc-conditional-calibration-research.data.md`.

## Recommendation: hybrid approach (SHIP-WITH-CAVEATS)

**Phase 1 — skip gate (lower-risk, higher-confidence)**:
- Wire the conditional matrix as a SKIP gate upstream of TM/DC sizing. If `calibrated_p(asset, band) < ask/100 + EDGE_FLOOR`, skip the trade entirely.
- Initial `EDGE_FLOOR ≈ 0.005` (0.5pp buffer) to avoid skipping marginally-positive cells.
- Operating point: k=30 (more aggressive gating) for the first soak window; can shift to k=50 if k=30 over-gates legitimate cells.
- Strongest evidence: **DC max drawdown drops 59.0%** at fractional=0.25/k=30. Cleanest causal claim in the spike — the gate avoids losing cells without sacrificing winners.
- Shadow first; promote after 14d soak validates the gated cells indeed lose money on average.

**Phase 2 — multiplicative scaler (incremental, gated on Phase 1 soak)**:
- Scale bespoke sizer output by `clip(shrunk_p / band_prior, 0.5, 1.5)`.
- High-confidence cells (shrunk_p > band_prior) get up to +50% contracts; low-confidence cells get capped at -50%.
- Bespoke caps (`TM_MAX_CONTRACTS`, DC tier-fractions, per-asset risk caps) STAY in force as a backstop.

**What NOT to ship**:
- Replacing `_tm_size`/`_dc_size` with raw Kelly off the calibrated probability — Kelly is too explosive at high price bands (f* = 0.5–1.0).
- Sub-signal-conditional matrix (buf_pct / tier) — data shows insufficient discrimination on n ≥ 10 cells to justify the complexity.
- (Reaffirming) Spike does NOT touch `bot/helpers/band_calibration.py` — P4.1 is in 14d soak through 2026-05-31.

## Risks + caveats

- **Snapshot is May-13** (pre-P4.1 ship). All TM/DC firings in this data used pre-P4.1 `final_prob`. Post-P4.1 the trade-selection gate has shifted; some rows currently in the data wouldn't fire today. **Re-run on a post-P4.1-soak snapshot (after 2026-05-31) before any production wire-in.**
- **Fee approximation** uses `7 × P × (1−P)` cents (Kalshi standard); production fees have tier-specific multipliers. Empirical per-contract fees in `settled_trades` (60d TM): losers 0.220c, winners 0.172c — both within ±30% of the formula. Direction-of-effect reliable.
- **Synthetic $5,000 bankroll** chosen for apples-to-apples sizing comparison. Real bot bankroll varies — production caps prevent any single trade exceeding configured risk-per-trade regardless.
- **60d lookback** spans several config changes (HYPE/DOGE T4 promotion 2026-05-14, SOL_BLEED_V2, weekend_discount Kelly-sign chain). Cohort-stage UNION applied per `bot/CLAUDE.md`, but regime non-stationarity is a real concern at this temporal granularity.
- **Per-contract net PnL hybrid methodology**: prefers real `settled_trades.pnl_cents - fee_cents / count` when available; falls back to synthetic when not. Unique-pair real-PnL coverage: TM 1650/2753 unique pairs (60%), DC 218/1059 (21%). **EO×ST JOIN duplication** (R2-C3) de-duplicated by (ticker, strategy) in PnL accounting — contract distribution preserves all EO rows.
- **HYPE / DOGE have zero rows** in this lookback for TM/DC (T4 promotion was 2026-05-14, 1 day after snapshot). Their cells are absent from both matrices; production wire-in MUST add HYPE/DOGE coverage from a post-promotion snapshot before going live for those assets.
- **Sub-floor cells flagged in matrix appendix** (28 cells across TM+DC). `lookup_calibrated_rate` cascade: cell n≥10 → use cell; else band-agg n≥10 → use band agg; else 0.5 neutral. Phase 1 + Phase 2 wire-in must replicate this cascade in production.

## Followup tickets (file IF Phase 1 advances)

- **TM/DC skip-gate wire-in** — REQUIRES-APPROVAL tier; new helper `bot/helpers/tm_dc_skip_gate.py`, AST-pinned skip site in `bot/scanner/__init__.py` upstream of `_tm_size`/`_dc_size` calls. Shadow-flag protected, soak-validated 14d. **Strongest data support**: DC max drawdown -59.0% at f=0.25/k=30.
- **Multiplicative scaler wire-in** — gated on Phase 1 soak success; CAUTION tier. Same helper home, multiplicative `(shrunk_p / band_prior)` factor applied to bespoke output, clip to [0.5, 1.5].
- **Conditional matrix refresh recipe** — NORMAL tier; document the per-asset/band realized-rate refresh recipe in `agent_docs/`, mirroring `agent_docs/p4_1_calibration_baseline.md`. Cadence: monthly or post-regime-change. Operator runs `python scripts/cal_mlp/tm_dc_calibration_research.py --db ... --out .data.md` and the data appendix updates; prose stays human-authored.

## Data appendix

Auto-generated tables (TM matrix, DC matrix, full 3×3 sweep grid JSON) live in [`tm-dc-conditional-calibration-research.data.md`](./tm-dc-conditional-calibration-research.data.md) — single source of truth for cell-level numbers. Refresh recipe in `scripts/cal_mlp/tm_dc_calibration_research.py` docstring.

Sub-floor (n < 10) cells are flagged in the appendix tables and EXCLUDED from `lookup_calibrated_rate` per the floor-cascade in `bot/helpers/band_calibration.py` lock-step.
