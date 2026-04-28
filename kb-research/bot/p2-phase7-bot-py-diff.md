# Phase 7: bot.py Edit Diff (operator-applied)

**Status:** documented but NOT yet applied. CLAUDE.md flags bot.py as sacred and requires explicit operator confirmation before deploy.
**Implementation module:** `scripts/cal_mlp/integration.py` (~570 lines, already committed to `rebuild/p2-cal-mlp`).
**Edits to bot.py:** 4 small insertions described below. Total: ~30 lines added to bot.py. No removals or refactors.

## Recommended workflow

1. Review this diff against current bot.py.
2. Run `/ultrareview` on the rebuild branch.
3. Apply the edits as ONE commit.
4. Verify the parity-assert log line `[CALMLP_PARITY] 14 constants verified` at startup.
5. Verify `bot_startup_log` table has a row with `parity_check_status='passed'`.
6. Initial deploy: `CALMLP_ENABLED=0` env var to keep calibrator off until shadow data accumulates.
7. After 24-48h of shadow data + spot-checks: flip `CALMLP_ENABLED=1`.

## Edit 1 — top-of-file imports (after existing imports, ~line 100)

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

The `sys.path.insert` is required because `bot.py` lives at the project root, not next to `scripts/cal_mlp/`. After import, the inserted entry stays on `sys.path` for the bot's lifetime — acceptable since cal_mlp modules don't shadow any bot.py names.

## Edit 2 — sizing parity helper (after PositionSizer class definition)

bot.py has `class PositionSizer` somewhere; add as a module-level function (NOT instance method) below the class:

```python
# Phase 7: static reimplementation of 15M main-path sizing for parity-assert.
# DO NOT use in production trading. Only used by sizing_parity_assert at startup.
compute_for_15m_main_path = make_compute_for_15m_main_path(globals())
```

This closes over bot.py's globals (SIZING_TIERS, ASSET_MAX_RISK_PER_TRADE constants, drawdown thresholds, STC scaler) so the function reads them lazily. The factory pattern keeps integration.py independent of bot.py imports.

## Edit 3 — startup invocation (immediately after `_create_tables()` call)

Find where bot.py calls `self._create_tables()` (or `_create_tables(conn)`) at startup. Add immediately after:

```python
# Phase 7: cal_mlp deploy preconditions.
_calmlp_migrate_schema(self.conn)
try:
    _calmlp_parity_assert_impl(globals(), self.conn)
    _calmlp_sizing_parity_assert_impl(self.conn, globals())
except CalMLPParityError as _calmlp_e:
    logging.error("[CALMLP_PARITY] FATAL: %s", _calmlp_e)
    raise SystemExit(2)

# Per-asset lazy predictor cache.
_calmlp_predictors = {a: CalMLPPredictor(a) for a in ('BTC', 'ETH', 'SOL', 'XRP')}
```

If the bot uses a different connection name (`conn` vs `self.conn`), adjust accordingly.

## Edit 4 — scan path integration (in `_evaluate_15m_candidate` or equivalent)

Find the bot.py site where `raw_prob` is computed via `ProbabilityEngine.compute(...)` and `final_prob` is assigned (typically followed by an `insert_evaluated_opportunity(...)` call with kwargs). Add between raw_prob computation and final_prob usage:

```python
# Phase 7: cal_mlp residual calibration.
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

`kwargs` is the dict passed to `insert_evaluated_opportunity(**kwargs)`. The hook mutates it in-place to add cal_mlp_* columns. Adjust local variable names (`raw_prob`, `ticker`, `side`, `best_ask`, `seconds_remaining`, `vol_regime`, `kwargs`) to match the existing scan-path scope.

## Test plan (post-edit)

1. **Startup logs check**:
   - `[CALMLP_MIGRATE] added columns: [...]` (only on first run after deploy)
   - `[CALMLP_PARITY] 14 constants verified`
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
