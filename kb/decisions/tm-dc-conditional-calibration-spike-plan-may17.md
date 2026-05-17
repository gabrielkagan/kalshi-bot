---
ticket: 86b9zktp6
type: spike-plan
risk_tier: safe
status: in_progress
session_date: 2026-05-17
worktree: /Users/gabrielkagan/Documents/kalshi-bot-tm-dc-spike
branch: 86b9zktp6-tm-dc-spike
base: origin/main @ 8ffc551
predecessor: kb/decisions/money-printer-roadmap-may17.md (P4.1, shipped c1e6d85)
---

# Spike plan: TM/DC conditional band-calibration research

## Discipline gates (stated atop per CLAUDE.md "Extraction-bit discipline" + `feedback_discipline_on_safe_tier_spikes`)

1. **RCA** every CRITICAL / MAJOR finding before patching the spike.
2. **TDD-first** — failing contract test scaffolded via `/test-writer` BEFORE any data-pulling code. Test pins matrix shape + shrinkage formula + sample-size guards.
3. **Adversarial review** to 2 consecutive zero-CRITICAL/MAJOR rounds (smallness should clear in 1-2 rounds, NOT justify skipping). Budget 5-7 rounds if the findings doc carries heavy numbers per `feedback_adv_review_doc_spike_drift_may17`.
4. **Drift-sweeper** before HARD GATE (sister-doc + audit-script anchors — `bot/CLAUDE.md` "Band-calibrated sizing (P4.1)" section is the most adjacent surface).
5. **Mac-side only** per `feedback_vps_compute_isolation` — 2 vCPU / 2 GB / 0-swap VPS cannot host sustained research compute.
6. **Parallel-session discipline** per `feedback_parallel_sessions` — re-read shared files before edit, fetch+verify before commit, never blind-push, narrow surgical edits on `CLAUDE.md` / `MEMORY.md` / `agent_docs/` / `kb/`.

## Hypothesis

P4.1 (`bot/helpers/band_calibration.py`, shipped `c1e6d85`) replaces miscalibrated `final_prob` with an empirical realized-rate substitution at 10 15M Kelly sites. TM and DC bypass `_sizer.compute()` entirely — they use bespoke formulas that encode signal strength CATEGORICALLY:

- **TM**: `_tm_size(price, stc, balance_cents, buf_pct)` — margin-based, `risk_fraction=0.0`, 50ct thin-buffer cap when `buf_pct < TM_THIN_BUFFER_PCT (0.20)`. Bypasses the Kelly ladder (`tier_idx=-1`).
- **DC**: `_dc_size(strategy, price, balance, asset)` with fixed `risk_fraction` per tier (T1 / T1B / T2 / T2_Z25 / T2_Z2). Bypasses Kelly ladder.

**Spike question**: Would a TM/DC-CONDITIONAL empirical matrix (same shape as P4.1, conditioned on TM/DC eligibility + sub-signal) produce meaningfully different sizing than the bespoke formulas? If yes, does sim PnL replay show lift?

**Spike scope**: read-only, offline, Mac-side. ZERO production code changes. Spike output is a findings doc + research script. Production wire-in (if recommended) is a SEPARATE followup ticket with its own full discipline pass.

## Research questions (spike resolves; do NOT pre-answer)

1. Within TM-eligible signals at each `(asset, price_band)`, do realized rates differ meaningfully across `buf_pct` buckets? (If no dispersion → current TM coarseness is fine, no lift available.)
2. Within DC-eligible signals at each `(asset, price_band, tier)`, do realized rates differ from current tier `risk_fraction` implied by the bespoke formula? (If tiers already align with empirical rates → no lift.)
3. Replayed on 30-60d of historical TM/DC firings, does Kelly-sized-via-conditional-prob beat `_tm_size` / `_dc_size` on (net PnL, max drawdown, Sharpe-ish ratio, contract dispersion)? Net PnL = `SUM(pnl_cents - COALESCE(fee_cents, 0))`.
4. What's the per-cell sample size? Are most cells thin enough that the shrinkage prior dominates? (Thin cells → matrix ≈ strategy-aggregate; may not differ from current formula.)
5. At the high price bands TM fires in (96-99c), how explosive is Kelly to calibration noise? Recommend fractional-Kelly multiplier.

## Locked design parameters

To keep the spike scoped and reproducible, these are LOCKED — don't drift mid-spike:

| Parameter | Value | Rationale |
|---|---|---|
| YES-side only | true | Mirrors P4.1 scope (`bot/helpers/band_calibration.py` docstring §Scope) |
| Lookback hybrid | 30d (bands 70-93c) / 60d (bands 94-100c) | Mirrors P4.1 — regime-sensitivity at lower bands, thin-cell stability at higher |
| Shrinkage k sweep | {30, 50, 100} | 30 = P4.1 baseline; higher k dampens explosive Kelly at high bands |
| Fractional Kelly sweep | {0.25, 0.5, 1.0} | Standard dampener at high-price bands where Kelly is explosive |
| Per-cell N floor | N ≥ 10 | Below floor, fall back to (asset, band) aggregate without sub-signal |
| Asset universe | {BTC, ETH, SOL, XRP, HYPE, DOGE} | Live 15M assets (HYPE/DOGE T4 promoted 2026-05-14) |
| TM sub-signal | `buf_pct ∈ [0.0, 0.1, 0.3, 0.6, 1.0, ∞]` | Absolute buckets primary; quantile buckets reported as comparison |
| DC sub-signal | Existing tier labels {T1, T1B, T2, T2_Z25, T2_Z2} | Tiers are production discrimination; spike preserves them |
| Cohort partitioning | `COHORT_PARTITION_STAGES` UNION | Per `bot/CLAUDE.md` — `filter_stage='candidate'` alone under-counts post cell-blocks |
| Sim sizing semantics | Kelly-sized counterfactual | Per CLAUDE.md "Sim PnL and counterfactuals use actual Kelly sizing. Never flat 1-contract." |

## Open design choices (spike resolves; emit recommendation in findings doc)

- Rip-and-replace bespoke formulas vs multiplicative scaler on top of them
- Drawdown sensitivity inheritance — TM/DC are currently drawdown-agnostic (`drawdown_scaler=1.0`); calibrated Kelly would normally inherit `drawdown_scaler` from `_sizer.compute()`
- Cap preservation — does the current TM thin-buffer 50ct cap survive in a calibrated-Kelly world?
- Floors — minimum-contract floor to avoid 0-contract sizing on small-bankroll/high-price cells

## Data sources

| Table | Use |
|---|---|
| `state.db.evaluated_opportunities` | TM/DC eligibility population — for TM: `(strategy = 'terminal_momentum' OR strategy LIKE 'terminal\_momentum\_%' ESCAPE '\')` covering both bare + suffixed forms (R1-C1 fix); for DC: `strategy IN ('decided_t1', 'decided_t1b', 'decided_t2', 'decided_t2_z25', 'decided_t2_z2')` (R2-M3 fix — production writes `decided_t*` not `decided_contract_*`). Cohort UNION on `filter_stage` IN `COHORT_PARTITION_STAGES`. |
| `state.db.fifteenm_shadow_signals` | `market_result` linkage for realized YES/NO |
| `state.db.settled_trades` | **TM is LIVE** (`TERMINAL_MOMENTUM_ENABLED=1`, ~1633 yes / 19 no in 60d). **DC is LIVE for 4 of 5 tiers**: T1/T1B/T2/T2_Z25 default `DECIDED_T*_ENABLED=1`; only T2_Z2 is shadow (`DECIDED_T2_Z2_ENABLED=0`, due to -$333 PnL in 47 historic trades). Total ~218 DC settled rows joined to EO. The `DECIDED_CONTRACT_SHADOW=1` constant default gates only shadow-LOGGING paths in scanner; live trading is per-tier. Spike misframed DC as "shadow only" through R1-R8; corrected post-R8 by user catch. Net PnL via `pnl_cents - COALESCE(fee_cents, 0)` per row, then divided by `count` for per-contract net (real path), or synthesized via Kalshi fee schedule for rows without settled_trades match. |
| `PRAGMA table_info(<table>)` + `SELECT DISTINCT` | Schema verification before query per CLAUDE.md |

## Methodology

### Phase 2 (TDD-RED via /test-writer)

Failing contract test at `tests/contracts/test_tm_dc_calibration_research.py`:

- Pins the matrix data structure shape: `{(asset, band, sub_signal_bucket): (n, raw_p, shrunk_p)}`
- Pins the shrinkage formula: `shrunk = (n * raw_p + k * prior) / (n + k)` (mirrors P4.1 `_shrink`)
- Pins per-cell N floor behavior: cells with `n < 10` fall back to `(asset, band)` aggregate
- Pins fractional Kelly sweep coverage: research outputs cover k ∈ {30,50,100} × frac ∈ {0.25,0.5,1.0}
- Pins Mac-side / read-only assertion: `PRAGMA query_only=1` + WAL + busy_timeout=10000
- Pins net-PnL accounting: `SUM(pnl_cents - COALESCE(fee_cents, 0))` (NOT `SUM(pnl_cents)`)

### Phase 3 (research script)

`scripts/cal_mlp/tm_dc_calibration_research.py` — re-runnable, idempotent, Mac-side. Signature:

```
python scripts/cal_mlp/tm_dc_calibration_research.py --db /path/to/state.db --strategy {tm,dc,both} --out kb/findings/tm-dc-conditional-calibration-research.md
```

Steps:
1. Pull TM/DC-eligible signals from `evaluated_opportunities` (cohort-UNION on `filter_stage`).
2. Join `fifteenm_shadow_signals.market_result` on `ticker` + window.
3. Bucket by `(asset, band, sub_signal)` per locked parameters.
4. Compute raw realized rate + shrunk rate per `(k, sub_signal)` combination.
5. Replay sim PnL: for each historical TM/DC firing, compute (a) current `_tm_size`/`_dc_size` contract count, (b) proposed Kelly-via-conditional-prob contract count across the fractional-Kelly sweep.
6. Emit findings-doc tables + plots-data (CSV; no live plotting in spike phase to keep deterministic).

### Phase 4 (sim PnL A/B)

Hold the trade-selection gate constant — every signal that fires today still fires in the replay. ONLY the contract count differs. Report per scenario:

- Net PnL (gross − fees)
- Max drawdown across the 30/60d window
- Sharpe-ish: `net_pnl / std(daily_pnl)`
- Contract dispersion: median, p90, max contracts per signal
- Win rate
- Avg loss / avg win

Pin a **NULL-HYPOTHESIS baseline**: if proposed sizing == current sizing within ±5% across all metrics, recommend NO-SHIP — the matrix is too thin or the formulas are already aligned with empirics.

### Phase 5 (findings doc)

`kb/findings/tm-dc-conditional-calibration-research.md` — sections:

1. Executive summary (SHIP / NO-SHIP / SHIP-WITH-CAVEATS) — top of doc
2. Per-cell realized rates (TM matrix + DC matrix tables)
3. Sim PnL A/B comparison table (scenarios × metrics grid)
4. Kelly explosivity analysis at 96-99c
5. Risk callouts
6. Recommendation + reasoning
7. Followup ticket scopes IF SHIP

## Out of scope (becomes followup tickets if spike SHIPs)

- Touching `_tm_size` / `_dc_size` / `_strategy_size` in `scripts/cal_mlp/sim_pnl.py` or live TM sizing
- Touching `bot/helpers/band_calibration.py` (P4.1 in 14d soak ending 2026-05-31)
- NO-side, hourly, V2 paths
- Extending the unconditional P4.1 matrix

## Followup tickets (file ONLY if spike recommends SHIP)

- TM production wire-in proposal — REQUIRES-APPROVAL tier (live code change at live Kelly site)
- DC skip-gate wire-in proposal — REQUIRES-APPROVAL tier (DC is LIVE for 4/5 tiers — real money impact, not shadow-only as originally stated; spike misframing corrected post-R8)

## Branch + worktree hygiene

- Worktree: `/Users/gabrielkagan/Documents/kalshi-bot-tm-dc-spike`
- Branch: `86b9zktp6-tm-dc-spike` tracking `origin/main @ 8ffc551`
- Parallel sessions active (per `feedback_parallel_sessions` + `MEMORY.md` cross-session awareness 14:00 UTC). This spike adds ONLY NEW files (no edits to shared docs until findings ship). Sister-doc updates (`bot/CLAUDE.md` "Band-calibrated sizing (P4.1)" section, `MEMORY.md` project entry) are explicit narrow edits deferred to closeout phase 7.

## Predecessors + anchors

- P4.1 ship: commit `c1e6d85` — `bot/helpers/band_calibration.py` (242 LOC) + `tests/contracts/test_p4_1_band_calibrated_sizing.py` (661 LOC)
- P4.1 docstring + scope: `bot/helpers/band_calibration.py:1-33`
- `bot/CLAUDE.md` "Band-calibrated sizing (P4.1)" section
- TM/DC bespoke sizing: `scripts/cal_mlp/sim_pnl.py:_strategy_size` (lines 450-540), `_tm_size`, `_dc_size`
- Cohort partition: `bot.helpers.cohort_attribution.COHORT_PARTITION_STAGES`
- Money Printer Roadmap: `kb/decisions/money-printer-roadmap-may17.md`
- Weekend-discount Kelly-sign chain (cross-surface sim/live parity precedent): `kb/decisions/session-resume-may17-from-weekend-discount-kelly-sign-followup-chain-shipped.md`
