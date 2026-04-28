# Phase 6: Validation Harness

**Status:** Round 1 spec doc rewrite (impl already converged at commit `22755d7` then patched at `da2d70a`).
**Anchor:** `kb-research/bot/p2-phases-4-to-8-design.md`.
**Implementation:**
- `scripts/cal_mlp/validate.py` — orchestrator + ship-blocker decision tree
- `scripts/cal_mlp/sim_pnl.py` — counterfactual PnL replay
- `scripts/cal_mlp/sizing.py` — Kelly + drawdown + STC scaler + per-asset cap
- `scripts/cal_mlp/stats.py` — cluster_bootstrap_ci, day_bootstrap_ci, escalate_n_if_close

The implementation went through 4 rounds of adversarial review on Phase 6 spec PLUS 4 rounds on impl (40+30+19+3+1 critiques converged at R5 spec, then R1=42 → R2=3 → R3=3 → R4=0 on impl). This doc captures the converged contracts.

## Goal

Take a Phase 5 bundle (M=5 ensemble + Mondrian conformal artifact) and produce a single ship/manual_review/block recommendation per asset, plus a counterfactual sim PnL audit and an A/B option for challenger comparison.

## Inputs (CLI)

```
validate.py --asset {BTC,ETH,SOL,XRP}
            --bundle-sha <hex>                    # Phase 5 bundle's `bundle_sha`
            [--challenger-bundle-sha <hex>]
            [--alpha 0.20]                        # default; matches Phase 5 fit
            [--bootstrap-n 2000]
            [--seed 0]
            [--models-dir models]
            [--data-dir data/cal_mlp]
            [--reports-dir reports]
            [--db state.db]
            [--device cpu|cuda]
            [--allow-shipblocker-fail]
            [--override-market-blend-w <float>]
            [--allow-alpha-mismatch]              # for A/B with different α
            [--quiet|--verbose]
```

Paths are anchored via `Path(__file__).resolve().parents[2]` (project_root) when relative — matches Phase 2 + Phase 4 contract.

## Outputs

- `data/cal_mlp/<asset>/validation_audit_v<art_tag>_<conformal_sha[:8]>.json` — diagnostic counters + ship_recommendation
- `reports/p2_validation_<asset>_<art_tag>_<conformal_sha[:8]>.md` — 7-section human-readable report (R1#C4)
- Atomic-bundle write: report tmp first, then audit JSON tmp + `os.replace`, then report `os.replace`. Bundle = audit+report; failure to write report unlinks audit (never publishes one without the other).

## Lock domain

`SHARED` lock on `models/cal_mlp_<asset>/.lock` (per Phase 4 R3 fix). Multiple Phase 6 readers can hold concurrently; blocked by Phase 4/5 EXCLUSIVE writers.

Lock file is opened with `os.open(... O_RDWR | O_CREAT)` — readers do NOT truncate it (writer's content preserved across reader opens).

## Per-band Brier ship-blockers

Bands (by `method_output` = raw_prob): `[<0.85, 0.85-0.92, 0.92-0.96, 0.96+]` — same partition as Phase 2 buckets at `[80, 90, 96]`.

Per-band paired bootstrap on Brier delta = `mlp - prod` (signed; more-negative = MLP improves more):

```python
def stat(d):
    err_prod = ((d['method_output'] - d['outcome']) ** 2).mean()
    err_mlp  = ((d['p_pred']        - d['outcome']) ** 2).mean()
    return err_mlp - err_prod
```

**Cluster-bootstrap by ticker** (Phase 2 R2-OPS#C7 acknowledged this is effectively per-row bootstrap when `pct_tickers_with_only_one_row > 0.5`). N=2000 default; tiered escalation to 10000/50000 via `escalate_n_if_close` based on margin = `|ci_hi - 0.005|`.

**Ship-blockers (per-band):**

- `#1 brier_band[<band>]`: `n >= 150` AND `ci_hi > 0.005` (degradation upper bound positive at α=0.05).
- `#2 A50 inequality`: `d_[0.96+] ≤ d_[0.85, 0.92)` on point estimates. The bleed band must improve at least as much as the body band.
- `#3` — **RETIRED** during R3 spec convergence (slot intentionally vacant; do not renumber). Original draft had a per-band MC-uncertainty check; subsumed by ensemble-std handling in Phase 5.

**Soft-flag:** `n < 150` AND `point > 0.005` — degradation below power floor; manual_review.
**Soft-flag:** `n_[0.96+] < 20` — A50 unverifiable (insufficient bleed-band data).

## Per-cell coverage ship-blockers

Cells: 3D Mondrian `(price_tier, stc_bucket, vol_regime)` per Phase 5 R1#C1. Up to 32 cells.

For each cell with `n >= 20`:
- `coverage_wilson_lo < (1 - α) - tol`: ship-blocker `#4 cov[<cell>]`.
- `frac_below_lo` Wilson lo > `α/2 + 0.05`: ship-blocker `#5 lower-miscov`.
- `frac_above_hi` Wilson lo > `α/2 + 0.05`: ship-blocker `#5 upper-miscov`.
- `clip_wilson_hi > 0.05`: ship-blocker `#6 clip rate`.

Cells with `0 < n < 20` and coverage < target: soft-flag `per_cell_low_n` (no abort).

`tol` = `COVERAGE_TOL_NORMAL=0.05` for normal cells, `COVERAGE_TOL_SMALL_N=0.10` for bleed cells or `20 <= n < 40`.

`#7 dispatch_miss`: any test row whose conformal lookup returned None.

## Sim PnL ship-blockers

`sim_pnl.run_sim_pnl(...)` does dual replay (`block_off` and `block_on` for HIGH_PRICE_STC_BLOCK_ENABLED).

Locked gate replay path (mirrors bot.py):
1. Per-asset entry-floor filter (BTC=88, ETH=90, SOL=86, XRP=92)
2. STC_EXTENDED 300-600s zone per-asset floors with STC_EXTENDED_BUFFER_RESCUE=0.25 bypass
3. Regular gate (fee-adjusted edge ≥ MIN_EDGE_BY_PRICE)
4. Weekend/overnight discount fallback (regular-gate-first; fee-adjusted edge ≥ discounted threshold)
5. HIGH_PRICE_STC_BLOCK 4-strategy bleeder list (96¢ × {SOL,XRP} × 2-5min × side='yes')
6. Sizing: fee-adjusted edge in fractions → SIZING_TIERS lookup → drawdown × STC scaler × per-asset MAX_RISK_PER_TRADE cap

**Ship-blockers:**
- `#8`: `total_pessimistic_30d <= 0` (block_off path)
- `#9`: `abs(modeled - pessim) / abs(pessim) > 0.5` (DEAD by construction in current spec — modeled == pessimistic; soft_flag emitted)
- `#10`: `weighted_avg_risk_drop > 0.15` (tier migration too aggressive)

**Soft-flags:**
- `unsettled_drop_rate > 0.05` (catastrophic backfill failure soft)
- `unsettled_drop_rate > 0.20` → ship-blocker (catastrophic — abort)
- `worst_7d_drawdown_ratio > 1.5`
- `hwm_init_source = 'forward_only_from_now'`
- A27 deprecation candidate: `block_marginal < 100` (negative or sub-$100 marginal PnL)
- `psutil_missing` (RSS check disabled)
- `challenger_replay_error` (A/B failed)
- `bootstrap_inconclusive` (tiered escalation hit max_n=50000)

## ship_recommendation decision tree

LOCKED order of precedence:

1. `bootstrap_inconclusive` → `manual_review`
2. `--override-market-blend-w` provided → `manual_review` (off-path config)
3. `blockers and not allow_shipblocker_fail` → `block`
4. `blockers and allow_shipblocker_fail` → `manual_review` (operator override)
5. `soft_flags` → `manual_review`
6. else → `ship`

## A/B comparison

`--challenger-bundle-sha` triggers second predictor load + dual replay. Day-bootstrap CI on per-day delta (`delta_pessimistic_30d`) computed via `stats.day_bootstrap_ci` at α=0.05.

Compatibility gates (raise SystemExit on mismatch unless `--allow-alpha-mismatch`):
- `cfg_fp` must match (same feature schema)
- `alpha` must match (same conformal coverage target)

Challenger uses its OWN normstats and ticker_to_id (per R2-impl-2#C12).

## Constants mirrored from bot.py / config.py / models.py

This section is the doc-drift contract. Phase 7 startup parity-asserts each constant.

| Constant | Source | Mirror |
|---|---|---|
| `SIZING_TIERS` | `config.py:134` (8 fraction tuples) | `cal_mlp/sizing.py` |
| `ASSET_MAX_RISK_PER_TRADE` | `bot.py:226-229` | `cal_mlp/sizing.py` |
| `DRAWDOWN_HALF/QUARTER/HALT_THRESHOLD` | `config.py:144-148` | `cal_mlp/sizing.py` |
| `MAX_RISK_PER_TRADE` | `config.py:148` | `cal_mlp/sizing.py` |
| `STC_SIZING_SCALER_KNEE` | `bot.py:897` | `cal_mlp/sizing.py` |
| `MIN_EDGE_BY_PRICE` | `bot.py:1180` (6-tier fractions) | `cal_mlp/sim_pnl.py` |
| `WEEKEND_EDGE_DISCOUNT/FLOOR` | `bot.py:853-864` | `cal_mlp/sim_pnl.py` (R1#C2: no `WEEKEND_HOURS` constant — gating uses `is_weekend` flag, not hour bounds) |
| `OVERNIGHT_*` | `bot.py:861-864 + 12606` | `cal_mlp/sim_pnl.py` |
| `HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES` | `bot.py:1213-1226` (4 strategies) | `cal_mlp/sim_pnl.py` |
| `STC_EXTENDED_PER_ASSET_FLOOR` | `bot.py:241-244` | `cal_mlp/sim_pnl.py` |
| `STC_EXTENDED_BUFFER_RESCUE` | `bot.py:238` | `cal_mlp/sim_pnl.py` |
| `ASSET_FLOORS` (per-asset min entry) | `bot.py:219-225` | `cal_mlp/features.py` (canonical); `sim_pnl.py:232 PER_ASSET_MIN_ENTRY_PRICE` is a pre-rebuild duplicate that should be replaced with an import from features.py — R1#C1 cleanup TODO |
| `RAW_PROB_CLIP_EPS` | this project (1e-6) | `cal_mlp/features.py` |
| `SETTLEMENT_WHITELIST` | `bot.py:4351` | `cal_mlp/features.py` |
| Strategy taker-only set | bot.py STRATEGY_CLAMP_POLICY 1583-1623 | `cal_mlp/sim_pnl.py` (only MAKER_PATIENT is maker) |

Adding/removing a constant on either side without the other = ship-block at Phase 7 parity-assert.

## Tiered N escalation (R-p6-1#C9)

Bootstrap N starts at 2000. If margin = `|ci_hi - 0.005|` is close to threshold:
- `margin < 0.01`: escalate to N >= 10000
- `margin < 0.003`: escalate to N >= 50000
- `margin < 0.001 AND current_n >= 50000`: abstain → `bootstrap_inconclusive`

Uses `stats.escalate_n_if_close(margin, current_n) → (target_n, abstain)`.

## Audit JSON schema (locked)

```json
{
  "phase": 6,
  "schema_version": 1,
  "asset": "...",
  "bundle_sha": "...",
  "conformal_sha": "...",
  "challenger_bundle_sha": null,
  "alpha": 0.20,
  "bootstrap_n_initial": 2000, "bootstrap_n_final": 10000,
  "bootstrap_inconclusive": false, "bootstrap_seed": 0,
  "market_blend_w_used": 0.0,
  "market_blend_w_source": "market_config.py",
  "current_market_blend_w": 0.0,
  "market_blend_w_drift": false,
  "brier_per_band": {<band>: {n, point, ci_lo, ci_hi, mc_se, ship_blocker_active, ship_blocker_fires}},
  "coverage_per_cell": [{cell_kind, price_tier, stc_bucket, vol_regime, n, n_covered, n_below_lo,
                          n_above_hi, n_clipped, n_dispatch_miss, coverage, coverage_wilson_lo,
                          coverage_wilson_hi, clip_rate, clip_wilson_hi, frac_below_lo, frac_above_hi}],
  "coverage_summary": {n_total_test_rows, n_eval_cells, n_total_dispatch_miss},
  "sim_pnl": {
    "block_off": {total_pessimistic_30d, total_modeled_30d, per_asset/band/strategy_pnl_30d,
                   tier_migration, worst_7d_drawdown_cents, worst_7d_drawdown_prod_cents,
                   daily_pnl, daily_pnl_count},
    "block_on": {...},
    "tier_migration": {tiers, risk_fractions, counts, pre_weighted_avg_risk, post_weighted_avg_risk, drop_pct},
    "weighted_avg_risk_drop": ...,
    "worst_7d_drawdown_ratio": ...,
    "hwm_init_cents": ..., "hwm_init_source": "balance_walked",
    "unsettled_drop_rate": ..., "n_candidate_universe": ...,
    "n_total_pre_filter": ..., "n_unsettled_in_window": ...,
    "excluded_null_market_price": ...,
    "block_deprecation_confound_note": "...",
    "known_limitations": [...],
    "challenger": {...}, "ab_summary": {...}, "challenger_error": "..."
  },
  "blockers_fired": [...],
  "soft_flags": [...],
  "shipblocker_overrides": [...],
  "allow_shipblocker_fail": false,
  "allow_alpha_mismatch": false,
  "override_market_blend_w": null,
  "ship_recommendation": "ship|manual_review|block",
  "peak_rss_mb": 412.5,
  "torch_version": "...",
  "pandas_version": "...",
  "generated_at": "..."
}
```

## Report MD layout (LOCKED 7-section)

1. Per-band Brier delta table
2. Per-cell coverage table
3. Sim PnL summary
4. Drawdown
5. HARD blockers fired
6. Soft flags
7. Provenance (bootstrap_n, market_blend_w, peak_rss_mb, generated_at)

## Known limitations (deferred from impl R4)

1. `pnl_modeled == pnl_pessimistic` — per-cell fill-rate model deferred; ship-blocker #9 dead by construction.
2. `worst_7d_drawdown_prod == worst_7d_drawdown_mlp` — production-path replay deferred; ratio always 1.0.
3. HWM uses all-time monotonic peak; bot.py uses 7-day rolling (R-p6-impl-4#C5 deferred).
4. HWM init via `balance_walked` may fall back to `forward_only_from_now` if no pre-window balance signal.

These are documented in `out['known_limitations']` of every audit JSON.

## Cross-references

- Phase 2 (data extraction): `kb-research/bot/p2-phase2-data-extraction.md`
- Phase 3 (architecture): `kb-research/bot/p2-phase3-mlp-architecture.md`
- Phase 4 (training): `kb-research/bot/p2-phase4-training.md`
- Phase 5 (conformal): `kb-research/bot/p2-phase5-conformal.md`
- Phase 7 (deploy): `kb-research/bot/p2-phase7-deploy.md`
- Design overview: `kb-research/bot/p2-phases-4-to-8-design.md`
