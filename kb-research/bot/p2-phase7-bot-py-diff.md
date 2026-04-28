# Phase 7: bot.py Edit Diff (operator-applied)

**Status:** documented but NOT yet applied. CLAUDE.md flags bot.py as sacred and requires explicit operator confirmation before deploy.
**Implementation module:** `scripts/cal_mlp/integration.py` (~570 lines, already committed to `rebuild/p2-cal-mlp`).
**Edits to bot.py:** 4 small insertions described below. Total: ~30 lines added to bot.py. No removals or refactors.

## Recommended workflow

1. Review this diff against current bot.py.
2. Run `/ultrareview` on the rebuild branch.
3. Apply the edits as ONE commit.
4. Verify the parity-assert log line `[CALMLP_PARITY] N constants verified` (N is the dynamic _check() count — currently 17 with the R-p7-r3#M2 STC_SIZING_SCALER_ENABLED addition; will rise as more parity vectors land) at startup.
5. Verify `bot_startup_log` table has a row with `parity_check_status='passed'`.
6. Initial deploy: `CALMLP_ENABLED=0` env var to keep calibrator off until shadow data accumulates.
7. After 24-48h of shadow data + spot-checks: flip `CALMLP_ENABLED=1`.

## Edit 1 — top-of-file imports (after existing imports, ~line 100)

**R-p7-deploy-r1#C1 prerequisites:** bot.py already imports `os` and `sys`; verify it does NOT import `numpy` or `pathlib.Path`. Edit 1 (sys.path) and Edit 4 (np.digitize) require both. Verify with `grep -nE '^(import numpy|from pathlib)' bot.py`. If empty, add to bot.py's import block FIRST as a separate sub-step:

```python
# Required by Phase 7 cal_mlp integration.
import numpy as np
from pathlib import Path
```

(Re-adding `os`/`sys` would be harmless but is redundant — bot.py:4-5 already imports both.)

Then add the cal_mlp imports:

```python
# Phase 7: cal_mlp integration (single import surface).
sys.path.insert(0, str(Path(__file__).parent / 'scripts' / 'cal_mlp'))
from integration import (
    CalMLPError, CalMLPParityError, CalMLPSchemaError,
    migrate_schema as _calmlp_migrate_schema,
    parity_assert as _calmlp_parity_assert_impl,
    sizing_parity_assert as _calmlp_sizing_parity_assert_impl,
    make_compute_for_15m_main_path,
    CalMLPPredictor,
    annotate_evaluation_kwargs as _calmlp_annotate_kwargs,
)
```

The `sys.path.insert` is required because `bot.py` lives at the project root, not next to `scripts/cal_mlp/`. After import, the inserted entry stays on `sys.path` for the bot's lifetime — acceptable since cal_mlp modules don't shadow any bot.py names. Note that `integration.py:50` does an idempotent `sys.path.insert` of the same dir at module load, so the bot.py-side insert is technically redundant — keep it for explicitness.

## Edit 2 — sizing parity helper (after PositionSizer class definition)

bot.py has `class PositionSizer` somewhere; add as a module-level function (NOT instance method) below the class:

```python
# Phase 7: static reimplementation of 15M main-path sizing for parity-assert.
# DO NOT use in production trading. Only used by sizing_parity_assert at startup.
compute_for_15m_main_path = make_compute_for_15m_main_path(globals())
```

This closes over bot.py's globals (SIZING_TIERS, ASSET_MAX_RISK_PER_TRADE constants, drawdown thresholds, STC scaler) so the function reads them lazily. The factory pattern keeps integration.py independent of bot.py imports.

## Edit 3 — startup invocation (immediately after `_create_tables()` call)

**R-p7-deploy-r1#C2 SCOPING:** `self._create_tables()` is called inside `StateManager.__init__`. If you paste verbatim, `_calmlp_predictors` becomes a LOCAL variable inside __init__ and Edit 4's scan path (different class) will hit `NameError`. The fix: split Edit 3 into two parts.

**R-p7-deploy-r1#H3 PRECONDITION:** Edit 3 MUST run on a connection that has already executed `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout >= 10000`. Currently true at `bot.py:2547-2548`. If you move Edit 3 to a different startup spot (earlier than the PRAGMA setup), `_verify_wal` will raise `CalMLPSchemaError` and the bot will refuse to start.

**Edit 3a** (inside `StateManager.__init__`, immediately after `self._create_tables()`).

**R-p7-cleanroom#M3:** wrap `_calmlp_migrate_schema` inside the try/except too — `_verify_wal` can raise `CalMLPSchemaError` if the conn isn't WAL+busy_timeout-configured, and an unwrapped exception leaves the operator with a raw stack trace and no `bot_startup_log` row.

```python
# Phase 7: cal_mlp deploy preconditions.
try:
    _calmlp_migrate_schema(self.conn)
    _calmlp_parity_assert_impl(globals(), self.conn)
    _calmlp_sizing_parity_assert_impl(globals(), self.conn)
except (CalMLPParityError, CalMLPSchemaError) as _calmlp_e:
    logging.error("[CALMLP_PARITY] FATAL: %s", _calmlp_e)
    raise SystemExit(2)
```

**Edit 3b** — the predictor cache MUST live at module scope so the scan-path code (different class/function) can read it. Place this immediately AFTER the `StateManager` class definition (or anywhere at module top-level after Edit 1 is in scope).

**R-p7-cleanroom#H2:** gate construction + warmup on the kill-switch. With `CALMLP_ENABLED=0` the cache stays empty and `annotate_evaluation_kwargs` returns `'no_predictor'` (or `'env_disabled'`) — no torch loads, no file IO during boot, true no-op. The annotate_evaluation_kwargs hook ALSO checks the env per-call so flipping to `=1` mid-process still works (predictors lazy-load on first scan tick).

```python
# Phase 7: per-asset predictor cache (module-level — accessed from scan path).
# Kill-switch leak fix (R-p7-cleanroom#H2): empty dict when CALMLP_ENABLED=0
# so no model files are touched during boot.
import os as _calmlp_os
if _calmlp_os.environ.get('CALMLP_ENABLED', '1').strip().lower() in ('1', 'true', 'yes'):
    _calmlp_predictors = {a: CalMLPPredictor(a) for a in ('BTC', 'ETH', 'SOL', 'XRP')}
    for _calmlp_p in _calmlp_predictors.values():
        _calmlp_p.warmup()
else:
    _calmlp_predictors = {}
    logging.info("[CALMLP] CALMLP_ENABLED!=truthy at boot — predictor cache empty")
```

**Note on bleed-cell semantics (R-p7-r2#M2):** `np.digitize(96, [80,90,96], right=True)` returns 2 (boundary value falls in lower bin). Therefore the calibrator's BLEED_CELL=(3,2) corresponds to entries 97¢-99¢, NOT 96¢-99¢. Phase 2 extraction uses identical semantics, so the calibrator is internally consistent — but the "≥96¢ × 300-600s" label in `kb-research/bot/finding_96c_sol_xrp_bleed_apr26.md` is loose. Operator-track item: decide whether to retroactively rebin (e.g. cutoffs `[80,90,95]` or `[80,90,96]` with `right=False`) or update the docs. Until then, 96¢ entries get the tier-2 quantile, not the bleed quantile.

If the bot uses a different connection name (`conn` vs `self.conn`), adjust accordingly.

## Edit 4 — scan path integration (in `_evaluate_15m_candidate` or equivalent)

**R-p7-deploy-r1#H2 + R-p7-deploy-r2#C2 ANCHOR:** bot.py has multiple `ProbabilityEngine.compute(...)` call sites AND multiple `final_prob = prob_with_market["calibrated_prob"]` assignments (at last grep: bot.py:11744 in 15M scan, AND bot.py:15317 in `_process_price_shadow`). Single-line anchors are NOT unique. Use the TWO-line pair below — only the 15M scan immediately follows `final_prob` with `z_score = prob_with_market["z_score"]`:

```
final_prob = prob_with_market["calibrated_prob"]
z_score = prob_with_market["z_score"]
```

Verify with `grep -B0 -A1 'final_prob = prob_with_market\["calibrated_prob"\]' bot.py` — only the 15M site should show the `z_score` follow-up. Land Edit 4 immediately after `z_score = prob_with_market["z_score"]`. Do NOT land at `_process_price_shadow` (bot.py:15317) — that path has no calibrator wiring.

**R-p7-deploy-r1#H1 LOCAL VARS:** bot.py's scan scope uses `vol_est["regime"]` (NOT a bare `vol_regime` local). Add the binding line below first, OR inline it.

```python
# Phase 7: cal_mlp residual calibration.
# R-p7-deploy-r1#H1: bind vol_regime from vol_est["regime"]; bot.py never
# declares a bare `vol_regime` local in this scope.
vol_regime = vol_est["regime"]  # 'normal' | 'elevated'
_calmlp_predictor = _calmlp_predictors.get(asset)
_calmlp_row_features = {
    'price_tier': int(np.digitize(best_ask, [80, 90, 96], right=True)),
    'stc_bucket': int(np.digitize(seconds_remaining, [120, 300, 600], right=True)),
    'vol_regime_int': 1 if vol_regime == 'elevated' else 0,
    'vol_regime': vol_regime,  # source string for logging
    # Continuous features come from the existing eval_opp kwargs; the
    # predictor's apply_norm fills missing CONT_FEATURE_COLS with mean.
}
_calmlp_new_prob = _calmlp_annotate_kwargs(
    kwargs, raw_prob=raw_prob, ticker=ticker, side=side,
    entry_price_cents=best_ask, row_features=_calmlp_row_features,
    predictor=_calmlp_predictor,
)
if _calmlp_new_prob is not None:
    final_prob = _calmlp_new_prob   # use calibrated; otherwise raw_prob path runs
```

`kwargs` is the dict passed to `insert_evaluated_opportunity(**kwargs)`. The hook mutates it in-place to add cal_mlp_* columns. Adjust local variable names (`raw_prob`, `ticker`, `side`, `best_ask`, `seconds_remaining`, `vol_est`, `kwargs`) to match the existing scan-path scope.

## Test plan (post-edit)

1. **Startup logs check**:
   - `[CALMLP_MIGRATE] added columns: [...]` (only on first run after deploy)
   - `[CALMLP_PARITY] N constants verified` (N is the dynamic _check() count — currently 17 with the R-p7-r3#M2 STC_SIZING_SCALER_ENABLED addition; will rise as more parity vectors land)
   - `[CALMLP_PARITY] sizing parity verified across 8 vectors`
2. **`bot_startup_log` table**:
   ```sql
   SELECT ts, parity_check_status, sizing_parity_status FROM bot_startup_log
     ORDER BY id DESC LIMIT 1;
   ```
   Expect: both `'passed'`, ts within 60s of bot start.
3. **`evaluated_opportunities` schema**:
   ```sql
   PRAGMA table_info(evaluated_opportunities);
   ```
   Expect: 6 new `cal_mlp_*` columns present.
4. **Calibrator-off path** (default with no Phase 5 bundles deployed):
   - Wait 5 min for first 15M scan.
   - Check: `SELECT cal_mlp_skipped_reason, COUNT(*) FROM evaluated_opportunities WHERE evaluation_time >= datetime('now', '-5 minutes') GROUP BY cal_mlp_skipped_reason;`
   - Expect: `'no_predictor'` (no bundle yet) or `'no_current'` for >99% of rows.
5. **After Phase 4/5 bundles land** (separate cron run):
   - Check: `SELECT cal_mlp_skipped_reason, COUNT(*) FROM evaluated_opportunities WHERE evaluation_time >= datetime('now', '-1 hour') GROUP BY cal_mlp_skipped_reason;`
   - Expect: NULL for >99% (calibration succeeded), with `cal_mlp_p_mean IS NOT NULL`.
6. **Kill switch**:
   - `export CALMLP_ENABLED=0; <restart bot>` (or wait 60s — env is read per evaluation).
   - Check: `cal_mlp_skipped_reason='env_disabled'` for next batch of evals.

## Rollback paths

- **Soft (env)**: `CALMLP_ENABLED=0`. Calibrator skipped; raw_prob path resumes. No DB or code change.
- **Hard (revert commit)**: `git revert <Phase-7-bot.py-edit-commit>` + push to main. Schema columns remain (idempotent ALTER, no DROP). Bundles in `models/cal_mlp_*/` remain on disk for future re-enable.

## Cross-references

- Implementation module: `scripts/cal_mlp/integration.py`
- Phase 7 spec: `kb-research/bot/p2-phase7-deploy.md`
- Anchor: `kb-research/bot/p2-phases-4-to-8-design.md`
