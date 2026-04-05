---
status: active
updated: 2026-04-03
tags: [shadow, 15m, a1, a2, a3, a4]
---
# 15M Shadow Variants (A1-A4)

## Summary
Four alternative probability/filtering approaches for 15-minute crypto markets, all running shadow-only in `fifteenm_shadow.py`. Each variant evaluates every 15M signal independently, logs results to `fifteenm_shadow_signals`, and can never place real orders. Purpose: find a calibration or gating method that beats the live baseline before promotion.

## Variant Details

### A1: RecalibratedEGARCH
Per-asset temperature scaling + market blend + edge band filters. Reuses the live pipeline's `blended_rv` (does not re-fit EGARCH). Applies learned post-processing from historical settled evaluations.

- **Per-asset temperatures:** BTC=1.15, ETH=1.25, SOL=1.05, XRP=1.30
- **Per-asset blend weights:** BTC=0.50, ETH=0.50, SOL=0.45, XRP=0.60
- **Per-asset debias:** BTC=2pp, ETH=4pp, SOL=1pp, XRP=5pp
- **Edge blacklist:** XRP 2.8-3.8% edge zone blocked (anti-predictive)

### A2: LightGBM Binary Classifier
Per-asset model predicting settlement outcome from pipeline features (blended_rv, z_score, market_price, STC, spread, etc.). Trained on settled `evaluated_opportunities`, isotonic-calibrated, retrained daily.

- **Minimum training rows:** 200 (`LGBM_MIN_TRAINING_ROWS`)
- **Retrain interval:** 86400s (daily)
- **Status:** Killed as calibrator (price proxy only, no independent signal). Still collecting data.

### A3: EGARCH Gating
Predicts loss probability to gate trades rather than recalibrate them. Uses a pooled (cross-asset) LightGBM model, isotonic-calibrated. Fires after the live pipeline produces a candidate; if predicted loss probability exceeds threshold, the signal is blocked.

- **Minimum training rows:** 100 (`GATING_MIN_TRAINING_ROWS`)
- **Gating thresholds tested:** 10%, 20%, 30%
- **Retrain interval:** 86400s (daily)

### A4: LateWindow (55-74c)
YES-side shadow for low-price asks in the final 5 minutes before settlement. Targets the regime where outcome uncertainty has largely collapsed but the market quotes below the live pipeline's floor.

- **Price range:** 55-74c (`A4_MIN_ASK`/`A4_MAX_ASK`)
- **Max STC:** 300s (final 5 minutes)
- **Min edge:** 10% model edge
- **Execution model:** Taker-only (best_ask + 1c fee)
- **Promotion criteria:** n>=200 settled, Brier<0.15, WR>=85%, sim PnL positive for >=3/4 assets, no single-day drawdown >$15

## Architecture

### Pre-Filter Callsite
The shadow engine evaluates ALL 15M signals before the live pipeline's price filter, not just candidates that pass the per-asset floor (BTC 89c, ETH 90c, etc.). This is critical for data collection at lower prices. A `_seen` dedup set prevents double-evaluation when signals also pass the live pipeline.

### Data Collection Floor
`SHADOW_MIN_ENTRY_PRICE = 70` (lowered from 86 on Mar 8, 2026). The live bot uses per-asset floors (BTC=89, ETH=90, SOL=80, XRP=92), but shadow collects data down to 70c for broader analysis.

### Shared Sizing Parameters
- Simulated bankroll: $500 (`SHADOW_BANKROLL = 50000` cents)
- Quarter-Kelly: `MAX_KELLY_FRACTION = 0.25`
- Max risk cap: 3% per signal
- Min debiased edge: 1.5pp
- Fee: $0 (assumes maker fills)

## Cross-Thread SQLite Bug (Fixed Mar 6, 2026)
`fifteenm_shadow.py` creates its DB connection lazily in `_ensure_db()` but gets called from both the bot main thread and the supabase_sync thread. Without `check_same_thread=False`, this caused a `ProgrammingError` silently swallowed by `except Exception: logging.debug(...)` -- zero rows written for days.

**Fix (53c953b):** All `sqlite3.connect()` calls in the file now use `check_same_thread=False`, plus `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=10000`.

**Anti-pattern identified:** `except Exception: logging.debug(...)` swallowing critical DB errors. Changed to `logging.warning` with `exc_info=True`. Regression test: `TestCheckSameThread` in `tests/test_regression.py`.

## Audit Coverage
- `scripts/15m_live_audit.py` Section 10
- `scripts/15m_alpha_research.py` Section 13 (`--section shadow`)
- `scripts/audit_cron.py` approach metrics
- Dashboard: `get_dashboard_data()` returns per-asset settled stats

## Related
- [[failures/blr-calibrator.md]]
- See `kb-research/bot/ml-probability-improvements.md` for LightGBM A2 and HAR-RV evaluation research
