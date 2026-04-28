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

### A. Startup parity-assert harness (R1#C1, C2, C7 — REVISED)

bot.py uses `from config import *` (bot.py:34) — `config.X` is NEVER bound as a module attribute. Constants like `SIZING_TIERS`, `DRAWDOWN_HALF_THRESHOLD`, `WEEKEND_EDGE_DISCOUNT` are bot-globals directly. cal_mlp imports must use ALIASES so the asserts compare meaningfully (importing without alias would shadow the bot-global).

Inside bot.py's startup sequence (after `_create_tables`, before any trading):

```python
def _calmlp_parity_assert():
    """Phase 7: cross-check vendored constants against runtime state.
    Hard-fail (SystemExit) on any mismatch — deploy is gate-blocked.
    Records pass/fail to bot_startup_log table for operator audit."""

    # Imports under aliases — never shadow bot-globals (R1#C2).
    sys.path.insert(0, str(Path(__file__).parent / 'scripts' / 'cal_mlp'))
    try:
        from features import (
            ASSET_FLOORS as _CAL_ASSET_FLOORS,
            GLOBAL_MIN_ENTRY_PRICE as _CAL_GLOBAL_MIN_ENTRY_PRICE,
            RAW_PROB_CLIP_EPS as _CAL_RAW_PROB_CLIP_EPS,
            SETTLEMENT_WHITELIST as _CAL_SETTLEMENT_WHITELIST,
            PRICE_BIN_CUTOFFS as _CAL_PRICE_BIN_CUTOFFS,
            STC_BIN_CUTOFFS as _CAL_STC_BIN_CUTOFFS,
        )
        from sizing import (
            SIZING_TIERS as _CAL_SIZING_TIERS,
            ASSET_MAX_RISK_PER_TRADE as _CAL_ASSET_MAX_RISK_PER_TRADE,
            MAX_RISK_PER_TRADE as _CAL_MAX_RISK_PER_TRADE,
            DRAWDOWN_HALF_THRESHOLD as _CAL_DRAWDOWN_HALF_THRESHOLD,
            DRAWDOWN_QUARTER_THRESHOLD as _CAL_DRAWDOWN_QUARTER_THRESHOLD,
            DRAWDOWN_HALT_THRESHOLD as _CAL_DRAWDOWN_HALT_THRESHOLD,
            STC_SIZING_SCALER_KNEE as _CAL_STC_SIZING_SCALER_KNEE,
        )
        from sim_pnl import (
            MIN_EDGE_BY_PRICE_SCHEDULE as _CAL_MIN_EDGE_BY_PRICE_SCHEDULE,
            WEEKEND_EDGE_DISCOUNT as _CAL_WEEKEND_EDGE_DISCOUNT,
            WEEKEND_EDGE_FLOOR as _CAL_WEEKEND_EDGE_FLOOR,
            OVERNIGHT_EDGE_DISCOUNT as _CAL_OVERNIGHT_EDGE_DISCOUNT,
            HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES as _CAL_HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
            STC_EXTENDED_PER_ASSET_FLOOR as _CAL_STC_EXTENDED_PER_ASSET_FLOOR,
            STC_EXTENDED_BUFFER_RESCUE as _CAL_STC_EXTENDED_BUFFER_RESCUE,
        )
    except ImportError as e:
        raise SystemExit(f"cal_mlp modules missing: {e}")

    # Bot-globals via `from config import *` and direct bot.py declarations.
    failures = []
    def _check(label, expected, actual):
        if expected != actual:
            failures.append(f"{label}: bot={expected!r} cal_mlp={actual!r}")

    # Asset floors (bot.py:219-225)
    _check("ASSET_FLOORS", {'BTC': BTC_MIN_ENTRY_PRICE, 'ETH': ETH_MIN_ENTRY_PRICE,
                            'SOL': SOL_MIN_ENTRY_PRICE, 'XRP': XRP_MIN_ENTRY_PRICE},
           _CAL_ASSET_FLOORS)
    _check("GLOBAL_MIN_ENTRY_PRICE", MIN_ENTRY_PRICE, _CAL_GLOBAL_MIN_ENTRY_PRICE)

    # Sizing (config.py:134-148, imported via `from config import *`)
    _check("SIZING_TIERS", SIZING_TIERS, _CAL_SIZING_TIERS)
    _check("ASSET_MAX_RISK_PER_TRADE", {'BTC': BTC_MAX_RISK_PER_TRADE,
                                         'ETH': ETH_MAX_RISK_PER_TRADE,
                                         'SOL': SOL_MAX_RISK_PER_TRADE,
                                         'XRP': XRP_MAX_RISK_PER_TRADE},
           _CAL_ASSET_MAX_RISK_PER_TRADE)
    _check("MAX_RISK_PER_TRADE", MAX_RISK_PER_TRADE, _CAL_MAX_RISK_PER_TRADE)
    _check("DRAWDOWN_HALF_THRESHOLD", DRAWDOWN_HALF_THRESHOLD, _CAL_DRAWDOWN_HALF_THRESHOLD)
    _check("DRAWDOWN_QUARTER_THRESHOLD", DRAWDOWN_QUARTER_THRESHOLD, _CAL_DRAWDOWN_QUARTER_THRESHOLD)
    _check("DRAWDOWN_HALT_THRESHOLD", DRAWDOWN_HALT_THRESHOLD, _CAL_DRAWDOWN_HALT_THRESHOLD)

    # Edge schedule (bot.py:1180)
    _check("MIN_EDGE_BY_PRICE",
           [tuple(x) for x in MIN_EDGE_BY_PRICE],
           [tuple(x) for x in _CAL_MIN_EDGE_BY_PRICE_SCHEDULE])

    # Discounts (bot.py:853-864 — bot-globals directly)
    _check("WEEKEND_EDGE_DISCOUNT", WEEKEND_EDGE_DISCOUNT, _CAL_WEEKEND_EDGE_DISCOUNT)
    _check("WEEKEND_EDGE_FLOOR", WEEKEND_EDGE_FLOOR, _CAL_WEEKEND_EDGE_FLOOR)
    _check("OVERNIGHT_EDGE_DISCOUNT", OVERNIGHT_EDGE_DISCOUNT, _CAL_OVERNIGHT_EDGE_DISCOUNT)

    # STC scaler (bot.py:897-898)
    _check("STC_SIZING_SCALER_KNEE", STC_SIZING_SCALER_KNEE, _CAL_STC_SIZING_SCALER_KNEE)

    # STC_EXTENDED (bot.py:238, 241-244)
    _check("STC_EXTENDED_PER_ASSET_FLOOR",
           {'BTC': STC_EXTENDED_BTC_MIN_PRICE, 'ETH': STC_EXTENDED_ETH_MIN_PRICE,
            'SOL': STC_EXTENDED_SOL_MIN_PRICE, 'XRP': STC_EXTENDED_XRP_MIN_PRICE},
           _CAL_STC_EXTENDED_PER_ASSET_FLOOR)
    _check("STC_EXTENDED_BUFFER_RESCUE", STC_EXTENDED_BUFFER_RESCUE, _CAL_STC_EXTENDED_BUFFER_RESCUE)

    # HIGH_PRICE_STC_BLOCK (bot.py:1213-1226)
    _check("HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES",
           HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
           _CAL_HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES)

    # R3#C2: write bot_startup_log row BEFORE SystemExit on failure too,
    # so operators can distinguish "skipped" (no row) from "failed" (row).
    if failures:
        self.conn.execute(
            "INSERT INTO bot_startup_log (parity_check_status, ts, pid) VALUES (?, ?, ?)",
            ('failed', datetime.utcnow().isoformat(), os.getpid()),
        )
        self.conn.commit()
        raise SystemExit("CALMLP_PARITY FAIL:\n  " + "\n  ".join(failures))

    logging.info("[CALMLP_PARITY] %d constants verified", 14)
    self.conn.execute(
        "INSERT INTO bot_startup_log (parity_check_status, ts, pid) VALUES (?, ?, ?)",
        ('passed', datetime.utcnow().isoformat(), os.getpid()),
    )
    self.conn.commit()

# Called at startup after _create_tables.
_calmlp_parity_assert()
```

Any drift collects into `failures`, then SystemExit lists them all (operator gets the full diff in one shot rather than one-at-a-time). The `bot_startup_log` row is the operator's positive-assertion that the harness ran AND passed.

### B. Sizing parity assertion (R1#C3, C4 — REVISED)

`models.PositionSizer.compute(...)` takes 3 inputs (`win_prob`, `price_cents`, `balance_cents`), derives Kelly internally, and applies drawdown via in-process state. STC scaler + per-asset cap are layered separately at call sites (bot.py:13445-13449, 12506-12508, 12680-12682). `cal_mlp.sizing.compute_size` already encodes ALL stages.

Phase 7 adds a NEW STATIC METHOD `PositionSizer.compute_for_15m_main_path(...)` (NOT an instance method that "bypasses state"). It takes the same 7 inputs as `compute_size` and re-implements the 15M MAIN-path math (NOT DC, NOT weekend, NOT overnight). Drift in scaler order = parity-assert fail.

```python
# bot.py addition: a static reimplementation of the 15M main path math.
# DO NOT use in production trading; this exists ONLY for parity-assert.
# R3#C1: helpers defined inline (not external symbols).

# Helper: per-asset cap dict (built from bot-globals BTC_MAX_RISK_PER_TRADE etc.).
ASSET_MAX_RISK_PER_TRADE_DICT = {
    'BTC': BTC_MAX_RISK_PER_TRADE,
    'ETH': ETH_MAX_RISK_PER_TRADE,
    'SOL': SOL_MAX_RISK_PER_TRADE,
    'XRP': XRP_MAX_RISK_PER_TRADE,
}

def _bot_lookup_tier(fee_adj_edge_frac: float) -> tuple[int, float]:
    """Mirrors config.py SIZING_TIERS lookup."""
    for i, (floor, risk) in enumerate(SIZING_TIERS):
        if fee_adj_edge_frac >= floor:
            return (i, risk)
    return (-1, 0.0)

def _bot_drawdown_scaler(current_balance_cents: int, hwm_cents: int) -> float:
    """Mirrors config.py drawdown ladder."""
    if hwm_cents <= 0:
        return 1.0
    ratio = current_balance_cents / hwm_cents
    if ratio < DRAWDOWN_HALT_THRESHOLD:
        return DRAWDOWN_HALT_FLOOR
    if ratio < DRAWDOWN_QUARTER_THRESHOLD:
        return 0.25
    if ratio < DRAWDOWN_HALF_THRESHOLD:
        return 0.50
    return 1.0

@staticmethod
def compute_for_15m_main_path(fee_adj_edge_frac: float, available_balance_cents: int,
                                entry_price_cents: int, current_balance_cents: int,
                                hwm_cents: int, seconds_to_close: float, asset: str) -> dict:
    tier_idx, risk_fraction = _bot_lookup_tier(fee_adj_edge_frac)
    if tier_idx < 0:
        return {'contracts': 0}
    drawdown = _bot_drawdown_scaler(current_balance_cents, hwm_cents)
    asset_cap = ASSET_MAX_RISK_PER_TRADE_DICT.get(asset, MAX_RISK_PER_TRADE)
    effective_risk = min(risk_fraction * drawdown, MAX_RISK_PER_TRADE, asset_cap)
    risk_cents = int(available_balance_cents * effective_risk)
    notional_cents = max(1, risk_cents)
    contracts = max(0, notional_cents // max(1, entry_price_cents))
    if STC_SIZING_SCALER_ENABLED and seconds_to_close > STC_SIZING_SCALER_KNEE and contracts > 0:
        contracts = max(1, int(contracts * (STC_SIZING_SCALER_KNEE / seconds_to_close)))
    return {'contracts': contracts}

def _calmlp_sizing_parity_assert():
    """R1#C4: 8 vectors covering boundary cases.
    Locked expected counts so a regression isn't masked by both sides drifting."""
    test_vectors = [
        # (fee_adj_edge_frac, balance, price, cur_bal, hwm, stc, asset, expected_contracts)
        (0.04,    100000, 95, 100000, 100000,  60,  'BTC',  None),  # tier-0 baseline
        (0.025,   100000, 90,  80000, 100000, 300, 'ETH',  None),  # drawdown=0.5 multiplier (cur/hwm=0.8 < 0.85)
        (0.012,   100000, 96,  50000, 100000, 600, 'SOL',  None),  # drawdown=0.25 (cur/hwm=0.5 < 0.75)
        (0.04,    100000, 95,  60000, 100000,  60,  'XRP',  None),  # DRAWDOWN_HALT_FLOOR engaged (0.6 < 0.65)
        (0.04,    100000, 95, 100000, 100000, 300, 'BTC',  None),  # STC=300 boundary (scaler=1.0)
        (0.04,    100000, 95, 100000, 100000, 301, 'BTC',  None),  # STC=301 boundary (scaler<1.0)
        (0.001,   100000, 95, 100000, 100000,  60,  'BTC',  0),    # edge below all tiers → 0
        (0.04,    100000, 50, 100000, 100000,  60,  'BTC',  None),  # high-tier low-price → asset_cap clamps
    ]
    from sizing import compute_size
    failures = []
    for vec in test_vectors:
        edge, bal, price, cur_bal, hwm, stc, asset, expected = vec
        cal_mlp_result = compute_size(edge, bal, price, cur_bal, hwm,
                                        seconds_to_close=stc, asset=asset)
        bot_result = PositionSizer.compute_for_15m_main_path(edge, bal, price, cur_bal, hwm, stc, asset)
        if cal_mlp_result.contract_count != bot_result['contracts']:
            failures.append(f"vec={vec}: cal_mlp={cal_mlp_result.contract_count} bot={bot_result['contracts']}")
        if expected is not None and cal_mlp_result.contract_count != expected:
            failures.append(f"vec={vec}: cal_mlp={cal_mlp_result.contract_count} expected={expected}")
    if failures:
        # R3#C2: log failure row before SystemExit.
        self.conn.execute(
            "INSERT INTO bot_startup_log (sizing_parity_status, ts, pid) VALUES (?, ?, ?)",
            ('failed', datetime.utcnow().isoformat(), os.getpid()),
        )
        self.conn.commit()
        raise SystemExit("CALMLP_SIZING_PARITY FAIL:\n  " + "\n  ".join(failures))
    logging.info("[CALMLP_PARITY] sizing parity verified across 8 vectors")
    self.conn.execute(
        "INSERT INTO bot_startup_log (sizing_parity_status, ts, pid) VALUES (?, ?, ?)",
        ('passed', datetime.utcnow().isoformat(), os.getpid()),
    )
    self.conn.commit()

_calmlp_sizing_parity_assert()
```

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
        """R1#C5: load into LOCAL variables; only atomically assign to self.*
        at the end so partial-state-on-failure doesn't leak. R1#C10: take
        LOCK_SH on the asset's models lock for the duration of read.
        R1#C6: full bundle_sha chain verification documented inline.
        R1#C12: vocab loaded BEFORE model construction."""
        with self._lock:
            if self._loaded:
                return
            try:
                project_root = Path(os.environ.get('KALSHI_PROJECT_ROOT',
                                                     str(Path(__file__).resolve().parent)))
                models_dir = project_root / 'models' / f'cal_mlp_{self.asset}'
                lock_path = models_dir / '.lock'
                # SH lock for entire load (blocks Phase 4 EX writers).
                lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_SH)
                    # CURRENT pointer
                    current_path = models_dir / 'CURRENT'
                    if not current_path.exists():
                        raise CalMLPError('no_current', f"no CURRENT for {self.asset}")
                    train_id = current_path.read_text().strip()
                    train_dir = models_dir / train_id
                    bundle_path = train_dir / f'cal_mlp_{self.asset}_{train_id}_bundle.json'
                    with open(bundle_path) as f:
                        bundle = json.load(f)
                    if bundle.get('phase') != 5:
                        raise CalMLPError('phase_mismatch',
                                            f"expected Phase 5, got {bundle.get('phase')}")
                    # Verify chain — R1#C6 inline.
                    self._verify_bundle_sha_chain(bundle, train_dir)
                    # Load conformal artifact + extract bundle + vocab + normstats FIRST.
                    deploy_idx = bundle['deploy_fold_idx']
                    extract_bundle_path = bundle['extract_bundle_path']
                    if not Path(extract_bundle_path).is_absolute():
                        extract_bundle_path = project_root / extract_bundle_path
                    ext_bundle = json.load(open(extract_bundle_path))
                    vocab_path = Path(extract_bundle_path).parent / ext_bundle['ticker_vocab_path']
                    vocab_payload = json.load(open(vocab_path))
                    _vocab = vocab_payload['vocab']
                    ns_path = Path(extract_bundle_path).parent / \
                              ext_bundle['eval_fold_artifacts'][deploy_idx]['normstats_path']
                    _normstats = json.load(open(ns_path))
                    _conformal = json.load(open(train_dir / bundle['conformal_path']))
                    # NOW build models with the known n_vocab.
                    fold = bundle['eval_fold_artifacts'][deploy_idx]
                    model_def = json.load(open(train_dir / bundle['model_definition_path']))
                    _models = []
                    for m in fold['members']:
                        # Verify marker
                        marker_path = train_dir / m['marker_path']
                        marker = json.load(open(marker_path))
                        if marker['cfg_fp'] != bundle['cfg_fp']:
                            raise CalMLPError('marker_drift', f"member {m['member']} marker mismatch")
                        from train import build_model_from_definition
                        model = build_model_from_definition(model_def, n_vocab=len(_vocab))
                        state_dict = torch.load(train_dir / m['checkpoint_path'], map_location='cpu')
                        model.load_state_dict(state_dict)
                        model.eval()
                        _models.append(model)
                    # R1#C9: assert bundle market_blend_w matches market_config live value.
                    from market_config import MARKET_CONFIGS
                    live_w = MARKET_CONFIGS['15m'].market_blend_w
                    bundle_w = bundle.get('market_blend_w')
                    if bundle_w is not None and abs(bundle_w - live_w) > 1e-9:
                        raise CalMLPError('market_blend_w_drift',
                                            f"bundle={bundle_w} live={live_w}; retrain required")
                    # All loads succeeded — atomic publish.
                    self.conformal = _conformal
                    self.models = _models
                    self.vocab = _vocab
                    self.normstats = _normstats
                    self.market_blend_w = live_w   # always use LIVE per R1#C9
                    self._loaded = True
                finally:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)
            except (CalMLPError, json.JSONDecodeError, OSError) as e:
                # On failure, no partial state visible (we accumulated locally).
                raise CalMLPError('load_failed', str(e)) from e

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
# In ScanEngine._evaluate_15m_candidate, after raw_prob is computed.
# R3#C3: every skip path sets `cal_mlp_skipped_reason` from the locked enum.
calmlp_enabled_now = (os.environ.get('CALMLP_ENABLED', '1') == '1')

if raw_prob is None:
    kwargs['cal_mlp_skipped_reason'] = 'raw_prob_null'
elif not calmlp_enabled_now:
    kwargs['cal_mlp_skipped_reason'] = 'env_disabled'
else:
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
        kwargs['cal_mlp_p_mean'] = cal_prob
        kwargs['cal_mlp_p_std'] = ens_std
        kwargs['cal_mlp_final_lo'] = final_lo
        kwargs['cal_mlp_final_hi'] = final_hi
        kwargs['cal_mlp_train_id'] = calibrator.train_id
    except CalMLPError as e:
        # Soft errors: structured `code` from the enum.
        logging.warning("[CALMLP] %s asset=%s; falling back to raw_prob", e, asset)
        kwargs['cal_mlp_skipped_reason'] = e.code
    except MemoryError:
        logging.exception("[CALMLP] OOM during predict; falling back")
        kwargs['cal_mlp_skipped_reason'] = 'predict_oom'
    except RuntimeError:
        logging.exception("[CALMLP] runtime error during predict; falling back")
        kwargs['cal_mlp_skipped_reason'] = 'predict_runtime'
```

`CALMLP_ENABLED` is the env-var kill switch (`os.environ.get('CALMLP_ENABLED', '1') == '1'`). Default ON. Set to '0' to instantly disable without bot restart on next eval.

### E. Schema migration (R1#C13 — PRAGMA-based)

`evaluated_opportunities` gets 6 new columns. Migration uses PRAGMA-based detection (NOT try/except OperationalError swallow) so future typos can't silently fail:

```python
def _migrate_calmlp_columns(conn):
    """R1#C13: PRAGMA-based migration; explicit add of MISSING columns only."""
    existing = {row[1] for row in conn.execute(
        "PRAGMA table_info(evaluated_opportunities)"
    ).fetchall()}
    new_cols = [
        ('cal_mlp_p_mean',          'REAL'),
        ('cal_mlp_p_std',           'REAL'),
        ('cal_mlp_final_lo',        'REAL'),
        ('cal_mlp_final_hi',        'REAL'),
        ('cal_mlp_train_id',        'TEXT'),
        ('cal_mlp_skipped_reason',  'TEXT'),
    ]
    added = []
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE evaluated_opportunities ADD COLUMN {col} {typ}")
            added.append(col)
    if added:
        logging.info("[CALMLP_MIGRATE] added columns: %s", added)
    conn.commit()
```

Also add `bot_startup_log` table for parity-check audit:

```sql
CREATE TABLE IF NOT EXISTS bot_startup_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    parity_check_status TEXT,
    sizing_parity_status TEXT,
    pid INTEGER
);
```

### F. market_config.py helpers (R1#C14 — current production value is 0.40, not 0.0)

`market_blend_w` is ALREADY a field on `MarketConfig` per market_config.py:45 (current 15M production = 0.40). Phase 7 adds NO new field; it adds an enforcement: Phase 5 bundle's `market_blend_w` MUST equal `MARKET_CONFIGS['15m'].market_blend_w` at load time. Otherwise → `CalMLPError('market_blend_w_drift')` and skip calibration.

Operator policy: changing `market_blend_w` in market_config.py REQUIRES Phase 5 retrain. Without retrain, the loader refuses (R1#C9 lock).

Phase 6 already-rebuilt validate.py imports `MARKET_CONFIGS['15m'].market_blend_w` (line 419-422); no change needed.

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

## Failure modes (locked exit codes + skipped_reason enum)

bot.py at startup with cal_mlp issues:

```python
class CalMLPError(RuntimeError):
    """Soft error — logged + raw_prob fallback at runtime."""
    def __init__(self, code: str, detail: str = ''):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail

class CalMLPParityError(RuntimeError): pass  # SystemExit at startup; deploy gate
class CalMLPSchemaError(RuntimeError): pass  # SystemExit at startup
```

**R1#C11 — `cal_mlp_skipped_reason` enum (locked):**

```python
SKIPPED_REASONS = (
    'no_current',          # CURRENT pointer absent for this asset
    'phase_mismatch',      # bundle phase != 5
    'sha_chain_fail',      # bundle_sha chain verification failed
    'marker_drift',        # member marker cfg_fp != bundle cfg_fp
    'load_failed',         # generic load error (file IO, JSON, torch.load)
    'predict_oom',         # MemoryError during predict
    'predict_runtime',     # other RuntimeError during predict
    'env_disabled',        # CALMLP_ENABLED=0
    'raw_prob_null',       # ProbabilityEngine returned None
    'market_blend_w_drift', # bundle vs market_config divergence
)
```

In §D's `except CalMLPError`, write `kwargs['cal_mlp_skipped_reason'] = e.code` to the row. Operators monitor `cal_mlp_skipped_reason IS NULL` for >99%; non-null counts grouped by code surface specific failure modes.

## `_verify_bundle_sha_chain` (R1#C6)

```python
def _verify_bundle_sha_chain(self, bundle: dict, train_dir: Path) -> None:
    """Recompute phase4_bundle_sha and phase5_bundle_sha; assert match.
    Cached per-process (no re-verify within a single bot startup)."""
    if (bundle.get('train_id'), self.asset) in _SHA_CHAIN_CACHE:
        return
    deploy_idx = bundle['deploy_fold_idx']
    fold = bundle['eval_fold_artifacts'][deploy_idx]
    # Recompute model_identity_sha256 = sha over canonical-ordered checkpoint shas.
    ckpt_shas = sorted(m['checkpoint_sha256'] for m in fold['members'])
    model_id_sha = hashlib.sha256(':'.join(ckpt_shas).encode()).hexdigest()
    # Recompute normstats_concat_sha256.
    ns_concat = hashlib.sha256()
    for fold_art in bundle['eval_fold_artifacts']:
        ns_concat.update(fold_art['normstats_sha256'].encode())
    ns_sha = ns_concat.hexdigest()
    # Reconstruct phase4_bundle_sha.
    expected_p4 = hashlib.sha256(f"{model_id_sha}:{ns_sha}:phase4".encode()).hexdigest()
    if expected_p4 != bundle.get('phase4_bundle_sha'):
        raise CalMLPError('sha_chain_fail',
                            f"phase4_bundle_sha mismatch: expected={expected_p4} bundle={bundle.get('phase4_bundle_sha')}")
    # Reconstruct phase5_bundle_sha.
    expected_p5 = hashlib.sha256(
        f"{expected_p4}:{bundle['conformal_sha256']}".encode()
    ).hexdigest()
    if expected_p5 != bundle.get('bundle_sha'):
        raise CalMLPError('sha_chain_fail',
                            f"phase5_bundle_sha mismatch: expected={expected_p5} bundle={bundle.get('bundle_sha')}")
    _SHA_CHAIN_CACHE[(bundle['train_id'], self.asset)] = True
```

Per-process cache via module-level `_SHA_CHAIN_CACHE: dict = {}`. First load per (train_id, asset) hashes; subsequent loads in same bot process skip the recompute.

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
