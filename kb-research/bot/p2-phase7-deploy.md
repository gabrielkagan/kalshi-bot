# Phase 7: Deploy Preconditions

**Status:** Round 1 spec.
**Anchor:** `kb-research/bot/p2-phases-4-to-8-design.md`.
**Prerequisite specs:** Phases 2-6 (converged or in progress).
**Output:** bot.py amendments + `market_config.py` helpers + `state.db` schema migration + DC/cron wiring + parity assertion harness.

## Goal

Wire the Phase 5 conformal-wrapped MLP into bot.py's live trade path so the existing CalEngine output (raw_prob) is post-processed by the calibrator at decision time. Maintain the doc-drift safety contracts (single-source mirrored constants), the bundle_sha verification chain (Phase 4→5→7), and a clean rollback path (env-var kill switch).

## Pre-conditions (must hold before deploy)

1. Phase 4 bundle for each of {BTC, ETH, SOL, XRP} exists at `models/cal_mlp_<asset>/CURRENT/cal_mlp_<asset>_<train_id>_bundle.json`.
2. Phase 5 wrap exists at the same path with `phase: 5` in the bundle JSON.
3. Phase 6 validation report has `ship_recommendation: ship` for ALL 4 assets (not `manual_review`, not `block`).
4. `requirements_calmlp.txt` matches the runtime VPS environment (torch / numpy / pandas / pyarrow versions).
5. The doc-drift parity assertions PASS at the current bot.py state (constants in cal_mlp/features.py + sizing.py + sim_pnl.py match bot.py / config.py / models.py).

## bot.py amendments (in same commit as Phase 7 deploy)

### A. Startup parity-assert harness

Inside bot.py's startup sequence (around line 9683 where existing parity asserts live), add:

```python
def _calmlp_parity_assert():
    """Phase 7: cross-check vendored constants against runtime state.
    Hard-fail (SystemExit) on any mismatch — deploy is gate-blocked."""
    try:
        sys.path.insert(0, str(Path(__file__).parent / 'scripts' / 'cal_mlp'))
        from features import (
            ASSET_FLOORS, GLOBAL_MIN_ENTRY_PRICE, RAW_PROB_CLIP_EPS,
            SETTLEMENT_WHITELIST, PRICE_BIN_CUTOFFS, STC_BIN_CUTOFFS,
        )
        from sizing import (
            SIZING_TIERS, ASSET_MAX_RISK_PER_TRADE, MAX_RISK_PER_TRADE,
            DRAWDOWN_HALF_THRESHOLD, DRAWDOWN_QUARTER_THRESHOLD,
            DRAWDOWN_HALT_THRESHOLD, STC_SIZING_SCALER_KNEE,
        )
        from sim_pnl import (
            MIN_EDGE_BY_PRICE_SCHEDULE, WEEKEND_EDGE_DISCOUNT,
            WEEKEND_EDGE_FLOOR, OVERNIGHT_EDGE_DISCOUNT,
            HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
            STC_EXTENDED_PER_ASSET_FLOOR, STC_EXTENDED_BUFFER_RESCUE,
        )
    except ImportError as e:
        raise SystemExit(f"cal_mlp modules missing: {e}")

    # Asset floors (bot.py:219-225)
    assert ASSET_FLOORS == {'BTC': BTC_MIN_ENTRY_PRICE, 'ETH': ETH_MIN_ENTRY_PRICE,
                            'SOL': SOL_MIN_ENTRY_PRICE, 'XRP': XRP_MIN_ENTRY_PRICE}, \
        f"ASSET_FLOORS drift: bot.py vs cal_mlp/features.py"
    assert GLOBAL_MIN_ENTRY_PRICE == MIN_ENTRY_PRICE, "global floor drift"

    # Sizing tiers (config.py:134)
    assert SIZING_TIERS == config.SIZING_TIERS, "SIZING_TIERS drift"
    assert ASSET_MAX_RISK_PER_TRADE == {'BTC': BTC_MAX_RISK_PER_TRADE,
                                          'ETH': ETH_MAX_RISK_PER_TRADE,
                                          'SOL': SOL_MAX_RISK_PER_TRADE,
                                          'XRP': XRP_MAX_RISK_PER_TRADE}, \
        "ASSET_MAX_RISK_PER_TRADE drift"

    # Drawdown thresholds (config.py:144-146)
    assert DRAWDOWN_HALF_THRESHOLD == config.DRAWDOWN_HALF_THRESHOLD
    assert DRAWDOWN_QUARTER_THRESHOLD == config.DRAWDOWN_QUARTER_THRESHOLD
    assert DRAWDOWN_HALT_THRESHOLD == config.DRAWDOWN_HALT_THRESHOLD

    # Edge schedule (bot.py:1180)
    assert [tuple(x) for x in MIN_EDGE_BY_PRICE_SCHEDULE] == list(MIN_EDGE_BY_PRICE), \
        "MIN_EDGE_BY_PRICE drift"

    # Discounts (bot.py:853-864)
    assert WEEKEND_EDGE_DISCOUNT == config.WEEKEND_EDGE_DISCOUNT
    assert WEEKEND_EDGE_FLOOR == config.WEEKEND_EDGE_FLOOR
    assert OVERNIGHT_EDGE_DISCOUNT == config.OVERNIGHT_EDGE_DISCOUNT

    # STC scaler (bot.py:897-898)
    assert STC_SIZING_SCALER_KNEE == STC_SIZING_SCALER_KNEE_BOT  # bot.py constant

    # STC_EXTENDED floors (bot.py:241-244)
    assert STC_EXTENDED_PER_ASSET_FLOOR == {
        'BTC': STC_EXTENDED_BTC_MIN_PRICE, 'ETH': STC_EXTENDED_ETH_MIN_PRICE,
        'SOL': STC_EXTENDED_SOL_MIN_PRICE, 'XRP': STC_EXTENDED_XRP_MIN_PRICE,
    }
    assert STC_EXTENDED_BUFFER_RESCUE == STC_EXTENDED_BUFFER_RESCUE_BOT

    # HIGH_PRICE_STC_BLOCK
    assert HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES == HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES_BOT

    logging.info("[CALMLP_PARITY] all constants verified against bot.py / config.py / models.py")

# Called at startup before any trading, after _create_tables.
_calmlp_parity_assert()
```

Any drift triggers immediate SystemExit. This catches accidental edits to one side without the other.

### B. Sizing parity assertion (R-p6-1#C10 SHIP PRECONDITION)

Verify that `sizing.compute_size(...)` produces the same output as bot.py's inline `PositionSizer.compute(...)` on a fixed test vector:

```python
def _calmlp_sizing_parity_assert():
    test_vectors = [
        # (fee_adj_edge_frac, available_balance, entry_price, current_balance, hwm, stc, asset)
        (0.04,  100000, 95, 100000, 100000, 60,  'BTC'),
        (0.025, 100000, 90, 80000,  100000, 300, 'ETH'),
        (0.012, 100000, 96, 50000,  100000, 600, 'SOL'),
        (0.0025, 100000, 88, 100000, 100000, 120, 'XRP'),
    ]
    from sizing import compute_size
    for vec in test_vectors:
        edge, bal, price, cur_bal, hwm, stc, asset = vec
        cal_mlp_result = compute_size(
            edge, bal, price, cur_bal, hwm, seconds_to_close=stc, asset=asset
        )
        bot_result = position_sizer.compute_for_test_vector(vec)
        assert cal_mlp_result.contract_count == bot_result['contracts'], \
            f"sizing parity break on {vec}: cal_mlp={cal_mlp_result.contract_count} vs bot={bot_result['contracts']}"
    logging.info("[CALMLP_PARITY] sizing parity verified")

_calmlp_sizing_parity_assert()
```

bot.py's `PositionSizer.compute_for_test_vector` is a NEW method added in the same commit; it bypasses the in-process state and computes from the inputs alone (deterministic).

### C. Calibrator load + cache (per-asset)

Add a `CalMLPPredictor` class wrapping the EnsemblePredictor + conformal artifact + ticker_vocab + normstats for fast inference:

```python
class CalMLPPredictor:
    def __init__(self, asset: str):
        self.asset = asset
        self._lock = threading.Lock()
        self._loaded = False
        # Load lazily on first use to avoid blocking startup if a single asset's
        # bundle is missing.

    def _load(self):
        with self._lock:
            if self._loaded:
                return
            # Read CURRENT pointer
            current_path = Path('models') / f'cal_mlp_{self.asset}' / 'CURRENT'
            if not current_path.exists():
                raise CalMLPError(f"no CURRENT for {self.asset}")
            train_id = current_path.read_text().strip()
            train_dir = current_path.parent / train_id
            with open(train_dir / f'cal_mlp_{self.asset}_{train_id}_bundle.json') as f:
                bundle = json.load(f)
            assert bundle['phase'] == 5, f"expected Phase 5 bundle, got {bundle['phase']}"
            # Verify chain
            self._verify_bundle_sha_chain(bundle, train_dir)
            # Load conformal artifact
            self.conformal = json.load(open(train_dir / bundle['conformal_path']))
            # Load deploy fold (K-1) members
            deploy_idx = bundle['deploy_fold_idx']
            fold = bundle['eval_fold_artifacts'][deploy_idx]
            members = fold['members']
            self.models = []
            for m in members:
                model = build_model_from_definition(
                    json.load(open(train_dir / bundle['model_definition_path'])),
                    n_vocab=...
                )
                state_dict = torch.load(train_dir / m['checkpoint_path'])
                # Verify marker
                marker = json.load(open(train_dir / m['marker_path']))
                assert marker['cfg_fp'] == bundle['cfg_fp'], "marker drift"
                model.load_state_dict(state_dict)
                model.eval()
                self.models.append(model)
            # Load normstats from extract bundle (resolved against project_root)
            extract_bundle_path = Path(bundle['extract_bundle_path'])
            if not extract_bundle_path.is_absolute():
                extract_bundle_path = Path(__file__).parent / extract_bundle_path
            ext_bundle = json.load(open(extract_bundle_path))
            ns_path = extract_bundle_path.parent / ext_bundle['eval_fold_artifacts'][deploy_idx]['normstats_path']
            self.normstats = json.load(open(ns_path))
            # Load vocab
            vocab_path = extract_bundle_path.parent / ext_bundle['ticker_vocab_path']
            self.vocab = json.load(open(vocab_path))['vocab']
            self.market_blend_w = bundle['market_blend_w']
            self._loaded = True

    def predict(self, features: dict, raw_prob: float, ticker: str, side: str,
                 entry_price_cents: int) -> tuple[float, float, float, float]:
        """Returns (calibrated_prob, ensemble_std, final_lo, final_hi).
        calibrated_prob is the 80%-coverage interval midpoint (= p_blend)."""
        if not self._loaded:
            self._load()
        # ... build batch ... apply normstats ... ensemble predict ... apply conformal
        return (calibrated_prob, ensemble_std, final_lo, final_hi)
```

### D. CalEngine integration point

The bot already has `ProbabilityEngine.compute(...) → raw_prob`. Phase 7 adds a layer:

```python
# In ScanEngine._evaluate_15m_candidate, after raw_prob is computed:
if raw_prob is not None and CALMLP_ENABLED:
    try:
        calibrator = _calmlp_predictors[asset]
        cal_prob, ens_std, final_lo, final_hi = calibrator.predict(
            features=row_features,
            raw_prob=raw_prob,
            ticker=ticker,
            side=side,
            entry_price_cents=best_ask,
        )
        # Replace calibrated_prob downstream
        final_prob = cal_prob
        # Annotate the eval_opportunity row for audit
        kwargs['cal_mlp_p_mean'] = cal_prob
        kwargs['cal_mlp_p_std'] = ens_std
        kwargs['cal_mlp_final_lo'] = final_lo
        kwargs['cal_mlp_final_hi'] = final_hi
    except CalMLPError as e:
        logging.warning("[CALMLP] %s asset=%s; falling back to raw_prob", e, asset)
        # Skip calibration; existing raw_prob path runs unchanged.
```

`CALMLP_ENABLED` is the env-var kill switch (`os.environ.get('CALMLP_ENABLED', '1') == '1'`). Default ON. Set to '0' to instantly disable without bot restart on next eval.

### E. Schema migration

`evaluated_opportunities` ALTER TABLE adds:

```sql
ALTER TABLE evaluated_opportunities ADD COLUMN cal_mlp_p_mean REAL;
ALTER TABLE evaluated_opportunities ADD COLUMN cal_mlp_p_std REAL;
ALTER TABLE evaluated_opportunities ADD COLUMN cal_mlp_final_lo REAL;
ALTER TABLE evaluated_opportunities ADD COLUMN cal_mlp_final_hi REAL;
ALTER TABLE evaluated_opportunities ADD COLUMN cal_mlp_train_id TEXT;
ALTER TABLE evaluated_opportunities ADD COLUMN cal_mlp_skipped_reason TEXT;
```

Same migration pattern as existing schema migrations (idempotent ALTERs in `_create_tables`).

### F. market_config.py helpers (single source for blend weights)

```python
# In market_config.py:
@dataclass
class MarketConfig:
    # ... existing fields ...
    market_blend_w: float = 0.0   # 0.0 = use MLP output as-is; 1.0 = ignore MLP, use breakeven prior

MARKET_CONFIGS = {
    '15m': MarketConfig(..., market_blend_w=0.0),
    'hourly': MarketConfig(..., market_blend_w=0.0),
    ...
}
```

Phase 5/6 read from this single source. Phase 6 already-rebuilt validate.py imports `MARKET_CONFIGS['15m'].market_blend_w` (line 419-422).

## Deploy sequence (locked operator runbook)

1. On dev machine: `make calmlp-train ASSET={BTC,ETH,SOL,XRP}` (parallel; runs Phase 4 + Phase 5 for each asset).
2. `make calmlp-validate` runs Phase 6 for all assets; check `ship_recommendation: ship` in each report.
3. If any asset is `manual_review` or `block`: STOP. Investigate, do not deploy.
4. Commit: bot.py amendments + cal_mlp/* + Phase 5 bundles + schema migration in ONE commit (per CLAUDE.md doc-drift rule).
5. `git push origin main`. Watch the VPS pull.
6. Verify on VPS:
   - `[CALMLP_PARITY] all constants verified` log line at startup.
   - `[CALMLP_PARITY] sizing parity verified` log line.
   - First trade with `cal_mlp_p_mean IS NOT NULL` row in `evaluated_opportunities`.
7. Monitor for 24h: `cal_mlp_skipped_reason` should be NULL for >99% of evals (skipped only when raw_prob=None or model load fails).
8. If issues: `ssh vps; export CALMLP_ENABLED=0`. Restart bot. Calibrator disables; raw_prob path resumes. No code revert needed.

## Rollback path

- **Soft rollback (env var):** set `CALMLP_ENABLED=0` on VPS. Calibrator skipped; raw_prob path resumes. No DB changes.
- **Hard rollback (revert):** `git revert <calmlp-deploy-commit>` + push. Schema columns remain (idempotent ALTER, no DROP). Bundles in `models/` can be left in place for future re-enable.

## Failure modes (locked exit codes)

bot.py at startup with cal_mlp issues:

```python
class CalMLPError(RuntimeError): pass        # logged + raw_prob fallback at runtime
class CalMLPParityError(RuntimeError): pass  # SystemExit at startup; deploy gate
class CalMLPSchemaError(RuntimeError): pass  # SystemExit at startup
```

Soft errors (CalMLPError) at runtime fall back to raw_prob. Hard errors (Parity / Schema) at startup are hard-fail.

## What this phase does NOT do

- No retraining. Models are the ones from Phase 4/5.
- No live A/B. The deploy is "ship for all 4 assets at once". A/B is done OFFLINE in Phase 6 via `--challenger-bundle-sha`.
- No promotion of `MARKET_BLEND_W` from current value (0.0 default). That's a separate amendment after the calibrator is shown to be net-positive on real shadow data.

## Open questions for adversarial review

1. **First-trade UX:** the calibrator loads lazily on first eval. The first call adds ~2s latency. Should we pre-warm at startup (load all 4 assets even if only 1 is being scanned)?
2. **Skip on bundle drift mid-session:** if the bundle file changes under us (operator copies a new train_id without restarting), `_calmlp_predictors[asset]` is stale. Should we mtime-check on each predict?
3. **Concurrent thread safety:** `CalMLPPredictor._load` uses a Lock but `predict()` doesn't. With multiple scan threads, are torch model.eval() forward passes thread-safe? Generally yes for inference, but verify.
4. **CALMLP_ENABLED env-var read:** spec calls `os.environ.get` once per evaluation. Hot path; cache the value? Tradeoff: cache loses the live-toggle property of the env var.
5. **Memory budget at deploy:** 4 assets × ensemble + normstats + vocab ≈ ~50MB. Negligible vs bot's existing footprint.
6. **DB schema migration order:** ALTERs in `_create_tables` run on every startup. New columns cost nothing on already-migrated rows. Verify this doesn't churn `evaluated_opportunities` indexes.
7. **Concurrent training and live trade:** Phase 4 takes models_lock EX while training. The live bot's CalMLPPredictor reads from `models/cal_mlp_<asset>/CURRENT/...`. If a Phase 4 train fires while the bot is reading, the bot blocks (LOCK_SH wait). Is that acceptable, or should the bot run lock-free with mtime-based reload?
