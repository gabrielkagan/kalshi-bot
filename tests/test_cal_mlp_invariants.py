"""Regression tests for the P2 cal_mlp rebuild invariants.

Each test corresponds to a deploy-blocker CRITICAL caught during the
Apr 28 adversarial-review session (`memory/project_p2_rebuild_apr28.md`).
Stdlib + features.py + _helpers.py + sizing.py only (torch-free) — these
run on any environment, not just the VPS.

DO NOT loosen these assertions without a corresponding spec amendment.
"""
import hashlib
import os
import sys
from pathlib import Path

import pytest

_CAL_MLP_DIR = Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp'
if str(_CAL_MLP_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_MLP_DIR))


# ---------------------------------------------------------------------------
# features.py invariants
# ---------------------------------------------------------------------------

def test_drop_predicates_locked_at_12():
    """R-p2-impl-r1#C7 + R2#C17: list grew from 10→12 post-spec; reordering
    breaks cfg_fp and invalidates all bundles."""
    import features
    assert len(features.DROP_PREDICATES_ORDER) == 12
    # Last two were added post-spec — explicit guard against accidental drop.
    names = [p[0] if isinstance(p, tuple) else p for p in features.DROP_PREDICATES_ORDER]
    assert 'null_or_invalid_side' in names
    assert 'null_seconds_to_close' in names


def test_skipped_reasons_locked_at_12():
    """R-p7-impl#C11 + R-p7-r2#H1: enum locked at 12 entries.
    R-p7-deploy-r9: post-hoc processor uses only existing reasons
    (missing_features, no_predictor, predict_runtime, env_disabled);
    the v1.5 async-pool extras (queue_full, async_predict_failed,
    row_not_found) were removed when the pool was retired."""
    import integration
    assert len(integration.SKIPPED_REASONS) == 12
    expected = {
        'no_current', 'phase_mismatch', 'sha_chain_fail', 'marker_drift',
        'load_failed', 'predict_oom', 'predict_runtime', 'env_disabled',
        'raw_prob_null', 'market_blend_w_drift', 'no_predictor', 'missing_features',
    }
    assert integration.SKIPPED_REASONS == frozenset(expected)


def test_missing_indicator_inverse_built_at_module_load():
    """R-p7-cleanroom-r6#H1: CalMLPSchemaError sibling-not-subclass means a
    runtime check inside predict() got demoted to soft skip. Must run at
    module import time; can't fire from inside predict()."""
    import integration
    import features
    assert hasattr(integration, '_MISSING_INDICATOR_SRC_TO_IND')
    inv = integration._MISSING_INDICATOR_SRC_TO_IND
    # Inverse must mirror MISSING_INDICATOR_SOURCE_MAP exactly. v1 has 0;
    # v2 reintroduces 7 with the WS-fed momentum/realized-vol features.
    assert len(inv) == len(features.MISSING_INDICATOR_SOURCE_MAP)
    fwd = features.MISSING_INDICATOR_SOURCE_MAP
    for src, ind in inv.items():
        assert fwd[ind] == src


def test_cfg_fp_deterministic():
    """compute_cfg_fp must be a pure function — calling it twice with same
    inputs yields the same hash. Otherwise bundle integrity breaks."""
    import features
    fps = [features.compute_cfg_fp(include_sub_floor=False) for _ in range(5)]
    assert len(set(fps)) == 1
    # And it MUST change on input flip (otherwise bundles can't disambiguate).
    other = features.compute_cfg_fp(include_sub_floor=True)
    assert fps[0] != other


def test_forward_keys_locked():
    """Phase 4 R3#C3: model.forward kwargs locked at exactly 8 keys."""
    import _helpers
    assert _helpers.FORWARD_KEYS == (
        'x_cont', 'x_missing',
        'price_tier', 'stc_bucket', 'vol_regime_int', 'side_int',
        'ticker_id', 'logit_raw_prob_clipped',
    )


def test_bleed_cell_locked():
    """R-p7-r2#M2: BLEED_CELL=(3,2) is the high-price × 2-5min STC cell.
    Both _helpers and conformal.py import this constant; reordering changes
    the lookup-table key and breaks every existing bundle."""
    import features
    assert features.BLEED_CELL == (3, 2)
    assert features.PRICE_BIN_CUTOFFS == [80, 90, 96]
    assert features.STC_BIN_CUTOFFS == [120, 300, 600]


# ---------------------------------------------------------------------------
# bundle_sha chain (R8 byte-aligned)
# ---------------------------------------------------------------------------

def _make_synthetic_bundle(*, n_folds=3, n_members=5, deploy_idx=2,
                            with_phase5=False):
    """Build a synthetic bundle dict whose SHAs are internally consistent."""
    import _helpers
    eval_fold_artifacts = []
    for f in range(n_folds):
        members = [
            {'checkpoint_sha256': f'fold{f}_member{m}_'.ljust(64, '0')}
            for m in range(n_members)
        ]
        eval_fold_artifacts.append({
            'fold': f,
            'normstats_sha256': f'fold{f}_normstats_'.ljust(64, '0'),
            'members': members,
        })
    deploy_fold = next(a for a in eval_fold_artifacts if a['fold'] == deploy_idx)
    ckpts = sorted(m['checkpoint_sha256'] for m in deploy_fold['members'])
    model_id = hashlib.sha256(':'.join(ckpts).encode()).hexdigest()
    nsc = hashlib.sha256()
    for fa in eval_fold_artifacts:
        nsc.update(fa['normstats_sha256'].encode())
    p4 = hashlib.sha256(f'{model_id}:{nsc.hexdigest()}:phase4'.encode()).hexdigest()
    bundle = {
        'eval_fold_artifacts': eval_fold_artifacts,
        'deploy_fold_idx': deploy_idx,
        'phase4_bundle_sha': p4,
    }
    if with_phase5:
        conf_sha = 'conformal_'.ljust(64, '0')
        bundle['conformal_sha256'] = conf_sha
        bundle['bundle_sha'] = hashlib.sha256(f'{p4}:{conf_sha}'.encode()).hexdigest()
    return bundle


def test_sha_chain_phase4_only_passes():
    """R-p4-r7-CRIT: phase-4-only bundles (no conformal_sha256) must verify
    cleanly — load_predictor gates the chain check on phase==5 but the
    helper itself must short-circuit when conformal_sha256 is absent."""
    import _helpers
    bundle = _make_synthetic_bundle(with_phase5=False)
    _helpers.verify_bundle_sha_chain(bundle)  # no exception


def test_sha_chain_phase5_full_passes():
    import _helpers
    bundle = _make_synthetic_bundle(with_phase5=True)
    _helpers.verify_bundle_sha_chain(bundle)  # no exception


def test_sha_chain_rejects_wrong_phase4():
    """R-p4-r7-CRIT: every real bundle was failing this check before the
    producer/consumer alignment fix."""
    import _helpers
    bundle = _make_synthetic_bundle()
    bundle['phase4_bundle_sha'] = 'wrong'
    with pytest.raises(RuntimeError, match='phase4 sha mismatch'):
        _helpers.verify_bundle_sha_chain(bundle)


def test_sha_chain_rejects_wrong_phase5():
    import _helpers
    bundle = _make_synthetic_bundle(with_phase5=True)
    bundle['bundle_sha'] = 'wrong'
    with pytest.raises(RuntimeError, match='phase5 sha mismatch'):
        _helpers.verify_bundle_sha_chain(bundle)


def test_sha_chain_uses_only_deploy_fold_members():
    """R-p4-r8-CRIT: producer was aggregating all-folds × members; consumer
    reads only deploy-fold. Multi-fold bundles failed verify on every load.
    This test pins the consumer's single-fold semantics."""
    import _helpers
    # Build a bundle whose model_id is computed from JUST the deploy-fold
    # member shas (matching the consumer expectation). Add extra non-deploy
    # member shas — these MUST NOT change the verification result.
    bundle = _make_synthetic_bundle(deploy_idx=2, n_folds=3)
    # Verify works.
    _helpers.verify_bundle_sha_chain(bundle)
    # Now mutate fold 0's member SHAs (NOT the deploy fold). Should still
    # pass — model_id is deploy-fold-only, so other folds don't affect it.
    bundle['eval_fold_artifacts'][0]['members'][0]['checkpoint_sha256'] = 'mutated'.ljust(64, 'x')
    _helpers.verify_bundle_sha_chain(bundle)


# ---------------------------------------------------------------------------
# Sizing parity vectors
# ---------------------------------------------------------------------------

def test_sizing_parity_vectors_match_integration_mirror():
    """R-p7-r3#M2 + R-p7-spec-r1#C1: cal_mlp/sizing.compute_size and
    integration.make_compute_for_15m_main_path MUST agree on every parity
    vector. Drift here is the deploy boot-blocker pattern."""
    import sizing
    # Synthetic bot_globals for the integration mirror — match cal_mlp's
    # constants so the parity_assert vectors all pass.
    bot_globals = {
        'SIZING_TIERS': sizing.SIZING_TIERS,
        'BTC_MAX_RISK_PER_TRADE': sizing.ASSET_MAX_RISK_PER_TRADE['BTC'],
        'ETH_MAX_RISK_PER_TRADE': sizing.ASSET_MAX_RISK_PER_TRADE['ETH'],
        'SOL_MAX_RISK_PER_TRADE': sizing.ASSET_MAX_RISK_PER_TRADE['SOL'],
        'XRP_MAX_RISK_PER_TRADE': sizing.ASSET_MAX_RISK_PER_TRADE['XRP'],
        'MAX_RISK_PER_TRADE': sizing.MAX_RISK_PER_TRADE,
        'DRAWDOWN_HALF_THRESHOLD': sizing.DRAWDOWN_HALF_THRESHOLD,
        'DRAWDOWN_QUARTER_THRESHOLD': sizing.DRAWDOWN_QUARTER_THRESHOLD,
        'DRAWDOWN_HALT_THRESHOLD': sizing.DRAWDOWN_HALT_THRESHOLD,
        # DRAWDOWN_HALT_FLOOR not exposed by bot.py — mirror falls back to 0.10.
        'STC_SIZING_SCALER_KNEE': sizing.STC_SIZING_SCALER_KNEE,
        'STC_SIZING_SCALER_ENABLED': sizing.STC_SIZING_SCALER_ENABLED,
    }
    import integration
    bot_compute = integration.make_compute_for_15m_main_path(bot_globals)

    # The 8 parity vectors from integration.sizing_parity_assert.
    test_vectors = [
        (0.04,    100000, 95, 100000, 100000,  60,  'BTC',  None),
        (0.025,   100000, 90,  80000, 100000, 300, 'ETH',  None),
        (0.012,   100000, 96,  50000, 100000, 600, 'SOL',  None),
        (0.04,    100000, 95,  60000, 100000,  60,  'XRP',  None),
        (0.04,    100000, 95, 100000, 100000, 300, 'BTC',  None),
        (0.04,    100000, 95, 100000, 100000, 301, 'BTC',  None),
        (0.001,   100000, 95, 100000, 100000,  60,  'BTC',  0),
        (0.04,    100000, 50, 100000, 100000,  60,  'BTC',  None),
    ]
    for vec in test_vectors:
        edge, bal, price, cur_bal, hwm, stc, asset, expected = vec
        cm = sizing.compute_size(edge, bal, price, cur_bal, hwm,
                                  seconds_to_close=stc, asset=asset)
        bot_r = bot_compute(edge, bal, price, cur_bal, hwm, stc, asset)
        assert cm.contract_count == bot_r['contracts'], (
            f"parity break on vec={vec}: cal_mlp={cm.contract_count} bot={bot_r['contracts']}"
        )
        if expected is not None:
            assert cm.contract_count == expected, (
                f"expected mismatch on vec={vec}: got={cm.contract_count} expected={expected}"
            )


def test_drawdown_halt_floor_fallback_matches_literal():
    """R-p7-spec-r1#C1: bot.py hardcodes DRAWDOWN_HALT_FLOOR=0.10 inline
    inside models.PositionSizer. integration.py uses g.get(..., 0.10) so
    parity vec 4 (XRP halt path) is reachable without a config.py edit."""
    import integration
    # Build bot_globals WITHOUT DRAWDOWN_HALT_FLOOR — the realistic case.
    import sizing
    bot_globals = {
        'SIZING_TIERS': sizing.SIZING_TIERS,
        'BTC_MAX_RISK_PER_TRADE': 0.15, 'ETH_MAX_RISK_PER_TRADE': 0.20,
        'SOL_MAX_RISK_PER_TRADE': 0.15, 'XRP_MAX_RISK_PER_TRADE': 0.15,
        'MAX_RISK_PER_TRADE': 0.25,
        'DRAWDOWN_HALF_THRESHOLD': 0.85,
        'DRAWDOWN_QUARTER_THRESHOLD': 0.75,
        'DRAWDOWN_HALT_THRESHOLD': 0.65,
        # Intentionally absent: 'DRAWDOWN_HALT_FLOOR'
        'STC_SIZING_SCALER_KNEE': 300,
        'STC_SIZING_SCALER_ENABLED': True,
    }
    bot_compute = integration.make_compute_for_15m_main_path(bot_globals)
    # Vec 4: XRP, ratio=0.6 < 0.65 → halt path → drawdown=0.10 → contracts > 0
    r = bot_compute(0.04, 100000, 95, 60000, 100000, 60, 'XRP')
    # Literal 0.10 floor: risk = 0.25 * 0.10 = 0.025; cap to 0.15 (XRP) → 0.025
    # Notional = 100000 * 0.025 = 2500; contracts = 2500 // 95 = 26
    assert r['contracts'] == 26, f"expected 26, got {r['contracts']}"


# ---------------------------------------------------------------------------
# CalMLPPredictor kill-switch contract (R-p7-coldboot#C-S2)
# ---------------------------------------------------------------------------

def test_cal_mlp_predictor_init_zero_io():
    """Kill-switch contract: CalMLPPredictor.__init__ must do NO file IO,
    NO torch.load, NO flock. With CALMLP_ENABLED=0 at boot, predictors are
    constructed but not warmed — and that construction must be free."""
    import inspect
    import integration
    src = inspect.getsource(integration.CalMLPPredictor.__init__)
    # These markers should never appear in __init__.
    forbidden = ['open(', 'torch.load', 'flock', 'fcntl.', 'sqlite3.connect',
                 'json.load', 'pd.read_', 'pq.read_']
    found = [m for m in forbidden if m in src]
    assert not found, f"CalMLPPredictor.__init__ has IO markers: {found}"


def test_warmup_catches_broad_exception():
    """R-p7-r2#C2 + R-p7-r4#MED-EXC: warmup must catch Exception (not just
    CalMLPError) and log exc_info=True so programming bugs don't masquerade
    as benign flock OSError."""
    import inspect
    import integration
    src = inspect.getsource(integration.CalMLPPredictor.warmup)
    assert 'except Exception' in src
    assert 'exc_info=True' in src


# ---------------------------------------------------------------------------
# Conformal q_level finite-sample correction
# ---------------------------------------------------------------------------

def test_conformal_q_level_finite_sample():
    """R-p7-r2#H1: split-conformal at miscoverage α achieves marginal
    coverage 1-α only with quantile level ⌈(n+1)(1-α)⌉/n. At n=20, α=0.20
    the level rises from 0.80 → 0.85 (the 17th order statistic)."""
    # Phase 5 conformal imports torch — guard so this skips on envs without it.
    pytest.importorskip("torch")
    pytest.importorskip("pandas")
    pytest.importorskip("numpy")
    pytest.importorskip("pyarrow")
    from conformal import _conformal_q_level
    # n=20, α=0.20: ceil(21 * 0.80) / 20 = ceil(16.8) / 20 = 17/20 = 0.85
    assert _conformal_q_level(20, 0.20) == pytest.approx(0.85)
    # n=100, α=0.20: ceil(101 * 0.80) / 100 = 81/100 = 0.81
    assert _conformal_q_level(100, 0.20) == pytest.approx(0.81)
    # Edge: very small n clamps to 1.0
    assert _conformal_q_level(2, 0.20) == 1.0


# ---------------------------------------------------------------------------
# CLAUDE.md WAL+busy_timeout precondition
# ---------------------------------------------------------------------------

def test_verify_wal_requires_both_pragmas(tmp_path):
    """R-p7-r3#C2: _verify_wal must reject a connection that has WAL but
    busy_timeout < 10000ms. CLAUDE.md anti-deadlock contract.
    """
    import sqlite3
    import integration

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    # journal_mode is delete by default — should raise.
    with pytest.raises(integration.CalMLPSchemaError, match="journal_mode"):
        integration._verify_wal(conn)
    # Set WAL but leave busy_timeout at 0 — should still raise.
    conn.execute("PRAGMA journal_mode=WAL")
    with pytest.raises(integration.CalMLPSchemaError, match="busy_timeout"):
        integration._verify_wal(conn)
    # Set both — should pass.
    conn.execute("PRAGMA busy_timeout=10000")
    integration._verify_wal(conn)  # no exception
    conn.close()


# ---------------------------------------------------------------------------
# annotate_evaluation_kwargs ordering + skip-reason contract
# ---------------------------------------------------------------------------

def test_annotate_env_disabled_fires_first():
    """R-p7-cleanroom#M4 regression: env check must fire BEFORE
    raw_prob_null check so kill-switch dashboards count correctly when
    raw_prob is also None."""
    import os
    import integration
    os.environ['CALMLP_ENABLED'] = '0'
    try:
        kwargs = {}
        rv = integration.annotate_evaluation_kwargs(
            kwargs, raw_prob=None, ticker='BTC-25APR2700-T117500',
            side='yes', entry_price_cents=95, row_features={}, predictor=None,
        )
        assert rv is None
        assert kwargs['cal_mlp_skipped_reason'] == 'env_disabled', \
            f"expected env_disabled, got {kwargs.get('cal_mlp_skipped_reason')}"
    finally:
        os.environ.pop('CALMLP_ENABLED', None)


def test_annotate_no_predictor_when_env_on_but_predictor_none():
    """If CALMLP_ENABLED=1 + raw_prob set + predictor=None, must surface
    'no_predictor' (not env_disabled or raw_prob_null)."""
    import os
    import integration
    os.environ['CALMLP_ENABLED'] = '1'
    try:
        kwargs = {}
        rv = integration.annotate_evaluation_kwargs(
            kwargs, raw_prob=0.97, ticker='BTC-25APR2700-T117500',
            side='yes', entry_price_cents=95, row_features={}, predictor=None,
        )
        assert rv is None
        assert kwargs['cal_mlp_skipped_reason'] == 'no_predictor'
    finally:
        os.environ.pop('CALMLP_ENABLED', None)


def test_annotate_raw_prob_null_when_env_on_predictor_present():
    """env=1, predictor present, raw_prob=None → 'raw_prob_null'."""
    import os
    import integration
    os.environ['CALMLP_ENABLED'] = '1'
    try:
        # Stub predictor (won't actually be called because raw_prob_null
        # short-circuits before predictor.predict).
        class StubPredictor:
            asset = 'BTC'
        kwargs = {}
        rv = integration.annotate_evaluation_kwargs(
            kwargs, raw_prob=None, ticker='BTC-25APR2700-T117500',
            side='yes', entry_price_cents=95, row_features={},
            predictor=StubPredictor(),
        )
        assert rv is None
        assert kwargs['cal_mlp_skipped_reason'] == 'raw_prob_null'
    finally:
        os.environ.pop('CALMLP_ENABLED', None)


def test_calmlp_error_code_round_trips_to_skipped_reason():
    """R-p7-r2#C11: when predict() raises CalMLPError(code), annotate
    stamps cal_mlp_skipped_reason=code IFF code is in SKIPPED_REASONS;
    otherwise stamps 'load_failed'."""
    import os
    import integration
    os.environ['CALMLP_ENABLED'] = '1'
    try:
        # Stub predictor that raises CalMLPError with each known code.
        for code in integration.SKIPPED_REASONS:
            class RaisingPredictor:
                asset = 'BTC'
                def predict(self, **kw):
                    raise integration.CalMLPError(code, f'test {code}')
            kwargs = {}
            integration.annotate_evaluation_kwargs(
                kwargs, raw_prob=0.97, ticker='BTC-25APR2700-T117500',
                side='yes', entry_price_cents=95, row_features={},
                predictor=RaisingPredictor(),
            )
            assert kwargs.get('cal_mlp_skipped_reason') == code, \
                f"code={code!r} → got {kwargs.get('cal_mlp_skipped_reason')!r}"

        # Unknown code falls back to 'load_failed'.
        class WeirdPredictor:
            asset = 'BTC'
            def predict(self, **kw):
                raise integration.CalMLPError('not_in_enum', 'test')
        kwargs = {}
        integration.annotate_evaluation_kwargs(
            kwargs, raw_prob=0.97, ticker='BTC-25APR2700-T117500',
            side='yes', entry_price_cents=95, row_features={},
            predictor=WeirdPredictor(),
        )
        assert kwargs['cal_mlp_skipped_reason'] == 'load_failed'
    finally:
        os.environ.pop('CALMLP_ENABLED', None)


# ---------------------------------------------------------------------------
# features.py contract checks
# ---------------------------------------------------------------------------

def test_missing_indicator_source_map_keys_subset_of_indicator_cols():
    """forward map's keys must be ⊆ MISSING_INDICATOR_COLS — otherwise the
    inverse lookup at integration._predict_inner sets a non-existent col."""
    import features
    fwd_keys = set(features.MISSING_INDICATOR_SOURCE_MAP.keys())
    indicator_cols = set(features.MISSING_INDICATOR_COLS)
    assert fwd_keys.issubset(indicator_cols), \
        f"orphan keys: {fwd_keys - indicator_cols}"


def test_missing_indicator_source_values_subset_of_cont_cols():
    """forward map's values must be ⊆ CONT_FEATURE_COLS — otherwise the
    NaN→indicator flip targets a non-existent source col."""
    import features
    src_cols = set(features.MISSING_INDICATOR_SOURCE_MAP.values())
    cont_cols = set(features.CONT_FEATURE_COLS)
    assert src_cols.issubset(cont_cols), \
        f"orphan source cols: {src_cols - cont_cols}"


def test_cont_feature_transforms_keys_subset_of_cont_cols():
    """CONT_FEATURE_TRANSFORMS only covers the columns it transforms;
    every key MUST be in CONT_FEATURE_COLS."""
    import features
    transform_keys = set(features.CONT_FEATURE_TRANSFORMS.keys())
    cont_cols = set(features.CONT_FEATURE_COLS)
    assert transform_keys.issubset(cont_cols), \
        f"orphan transform keys: {transform_keys - cont_cols}"
    valid_transforms = {
        'logit', 'log_cents_to_dollars', 'log1p', 'log1p_signed',
        'identity', 'identity_no_zscore',
    }
    for col, name in features.CONT_FEATURE_TRANSFORMS.items():
        assert name in valid_transforms, \
            f"col {col!r} has invalid transform {name!r}"


def test_skipped_reasons_is_immutable_frozenset():
    """SKIPPED_REASONS is frozenset to prevent accidental runtime mutation
    (e.g., a future caller doing `SKIPPED_REASONS.add('new_code')` would
    silently break the audit contract)."""
    import integration
    assert isinstance(integration.SKIPPED_REASONS, frozenset)


# ---------------------------------------------------------------------------
# _SHA_CHAIN_CACHE keying invariants (R-p7-r4#MED-CACHE)
# ---------------------------------------------------------------------------

def test_sha_chain_cache_includes_project_root():
    """R-p7-r4#MED-CACHE: cache key must include project_root so test
    fixtures with the same train_id under a fake root can't poison the
    real-prod cache entry."""
    import inspect
    import integration
    src = inspect.getsource(integration.CalMLPPredictor._verify_bundle_sha_chain)
    # The cache_key must combine train_id + asset + project_root.
    assert 'project_root' in src or 'self.project_root' in src, \
        "cache key missing project_root component"
    assert 'train_id' in src
    assert 'asset' in src or 'self.asset' in src


# ---------------------------------------------------------------------------
# bundle_sha producer/consumer alignment (R-p4-r7-CRIT, R-p4-r8-CRIT)
# ---------------------------------------------------------------------------

def test_phase4_bundle_sha_excludes_phase5_when_conformal_absent():
    """The helper must short-circuit at phase4 when conformal_sha256 is
    absent — phase-4-only bundles don't have a phase-5 chain to verify."""
    import _helpers
    bundle = _make_synthetic_bundle(with_phase5=False)
    # Should NOT crash on missing 'bundle_sha'/'conformal_sha256'.
    _helpers.verify_bundle_sha_chain(bundle)
    # And missing 'bundle_sha' shouldn't matter at this point.
    bundle.pop('bundle_sha', None)
    _helpers.verify_bundle_sha_chain(bundle)


def test_integration_all_matches_bot_py_diff_edit_1():
    """The 9 names imported by bot-py-diff Edit 1 MUST be exported via
    `integration.__all__` AND resolvable as module attributes. If a future
    refactor renames or hides any of these, the operator's `from integration
    import ...` line in bot.py would NameError on the next deploy."""
    import integration
    edit_1_imports = {
        'CalMLPError', 'CalMLPParityError', 'CalMLPSchemaError',
        'migrate_schema', 'parity_assert', 'sizing_parity_assert',
        'make_compute_for_15m_main_path', 'CalMLPPredictor',
        'annotate_evaluation_kwargs',
    }
    all_set = set(integration.__all__)
    missing = edit_1_imports - all_set
    assert not missing, f"Edit 1 names missing from __all__: {missing}"
    for name in edit_1_imports:
        assert hasattr(integration, name), f"{name} not on module"


# ---------------------------------------------------------------------------
# bot.py Edit 4 deploy-blocker regressions (R-p7-deploy-r2)
# These tests exist because the original bee1ebb commit would have crashed
# the scan loop on first 15M window. Adversarial review caught the issues
# pre-deploy. Each test pins one of the ship-blockers in place.
# ---------------------------------------------------------------------------

def _read_bot_py():
    """Cached read of bot.py for the AST guards below."""
    p = Path(__file__).resolve().parents[1] / 'bot.py'
    if not p.exists():
        pytest.skip('bot.py not present (running on rebuild branch without bot.py edits)')
    return p.read_text()


def test_edit4_hook_does_not_pass_unbound_side():
    """R-p7-deploy-r2#C1 regression: Edit 4's annotate call must NOT pass
    `side=side` — `side` is unbound in the 15M scan scope and Python
    evaluates kwargs at call time, NameError-ing before the function enters
    and the predictor=None graceful-skip can fire. Hardcode 'yes'.
    R-p7-deploy-r8: the hook is now `_calmlp_annotate_async` (async enqueue);
    same contract applies.
    """
    src = _read_bot_py()
    if '_calmlp_annotate_async' not in src and '_calmlp_annotate_kwargs' not in src:
        pytest.skip('Edit 4 not yet applied to bot.py (rebuild-only branch)')
    import re
    # Match either the legacy sync call or the new async enqueue.
    m = re.search(
        r'_calmlp_annotate(?:_async|_kwargs)\s*\((.*?)\)',
        src, re.DOTALL,
    )
    assert m, 'expected exactly one _calmlp_annotate_async/kwargs call'
    call_args = m.group(1)
    # Must pass side="yes" (the literal — anything else risks NameError or
    # silent miscalibration for a side that the model wasn't trained on).
    assert 'side="yes"' in call_args or "side='yes'" in call_args, (
        f"Edit 4 must hardcode side='yes' (15M main path is YES-only entry); "
        f"call_args={call_args!r}"
    )
    # And the bare side=side foot-gun MUST NOT be there.
    assert 'side=side' not in call_args, (
        "Edit 4 passed `side=side` (unbound local in 15M scan scope). "
        "This NameError'd on first run; commit bee1ebb shipped the bug, R2 fixed it."
    )


def test_edit4_hook_gated_by_pt_15m():
    """R-p7-deploy-r2#H1 regression: the cal_mlp hook must be wrapped in
    `if _pt in (None, "15m"):` so calibration only runs for 15M windows.
    Without this gate, hourly windows that share BTC/ETH/XRP assets would
    get silently miscalibrated (model trained on 15M data) and SPX/weather
    would pollute the skipped-reason histogram with no_predictor rows."""
    src = _read_bot_py()
    if '_calmlp_annotate_async' not in src and '_calmlp_annotate_kwargs' not in src:
        pytest.skip('Edit 4 not yet applied to bot.py (rebuild-only branch)')
    import re
    # The gate must appear BEFORE the hook call, in close proximity.
    # Match the pattern: `if _pt in (None, "15m"):` ... `_calmlp_annotate_kwargs`
    pat = re.compile(
        r'if\s+_pt\s+in\s*\(\s*None\s*,\s*[\'"]15m[\'"]\s*\)\s*:'
        r'(?:[\s\S]{0,8000})_calmlp_annotate(?:_async|_kwargs)',
    )
    assert pat.search(src), (
        "Edit 4 hook must be wrapped in `if _pt in (None, \"15m\"):` so "
        "calibration only fires for 15M product_type. Hourly/SPX/weather "
        "windows must skip the hook entirely."
    )


def test_edit4_shadow_queue_strips_cal_mlp_prefix():
    """R-p7-deploy-r2#MED1 regression: shadow-queue snapshots taken AFTER
    Edit 4's _shadow_diag mutation must filter out cal_mlp_* keys so
    shadow-strategy DB rows don't get tagged with main-path calibrator
    audit data that those shadows didn't actually go through."""
    src = _read_bot_py()
    if '_calmlp_annotate_async' not in src and '_calmlp_annotate_kwargs' not in src:
        pytest.skip('Edit 4 not yet applied to bot.py')
    # The post-hook snapshot pattern uses a dict comprehension with
    # `not k.startswith('cal_mlp_')`. Pin that the bare `_shadow_diag.copy()`
    # is not used in the post-hook path (where cal_mlp_* keys exist).
    # Heuristic: count `_shadow_diag.copy()` (pre-hook) vs the comprehension
    # filter (post-hook). Pre-hook path has 4 copies, post-hook has 0;
    # post-hook should have 2 filter-comprehensions.
    n_copy = src.count('_shadow_diag.copy()')
    n_filter = src.count(
        "if not k.startswith('cal_mlp_')"
    ) + src.count(
        'if not k.startswith("cal_mlp_")'
    )
    # 4 pre-hook _shadow_diag.copy() sites are unchanged (rejection paths).
    # 2 post-hook sites must use the filter comprehension.
    assert n_copy >= 4, f"expected ≥4 _shadow_diag.copy() pre-hook sites, got {n_copy}"
    assert n_filter >= 2, (
        f"expected ≥2 cal_mlp_* prefix-filter comprehensions for post-hook "
        f"snapshots, got {n_filter}. Without the filter, shadow-strategy rows "
        f"get tagged with main-path cal_mlp_* audit data."
    )


def test_shadow_diag_assertion_includes_cal_mlp_keys():
    """R-p7-deploy-r2#MED2 + R-p7-deploy-r8: the startup assertion that pins
    _shadow_diag keys against insert function signatures must include the
    7 cal_mlp_* keys (6 audit + 1 request_id) for
    insert_evaluated_opportunity. Otherwise a future refactor that drops a
    cal_mlp_* param silently breaks the **_shadow_diag splat."""
    src = _read_bot_py()
    if '_calmlp_annotate_async' not in src and '_calmlp_annotate_kwargs' not in src:
        pytest.skip('Edit 4 not yet applied to bot.py')
    expected_calmlp_keys = [
        'cal_mlp_p_mean', 'cal_mlp_p_std', 'cal_mlp_final_lo',
        'cal_mlp_final_hi', 'cal_mlp_train_id', 'cal_mlp_skipped_reason',
        'cal_mlp_request_id',
    ]
    for k in expected_calmlp_keys:
        assert f'"{k}"' in src or f"'{k}'" in src, (
            f"_shadow_diag startup assertion missing key {k!r}. "
            f"Without it, dropping the param from insert_evaluated_opportunity "
            f"would silently break the **_shadow_diag splat at runtime."
        )


def test_insert_evaluated_opportunity_signature_has_cal_mlp_params():
    """Lock the insert_evaluated_opportunity surgery: signature MUST accept
    the 7 cal_mlp_* params (6 audit + 1 request_id). Without these, the
    **_shadow_diag splat at the Edit 4 hook downstream raises TypeError
    ('unexpected keyword argument') on every scan tick that has a
    calibrator result. R-p7-deploy-r8 adds cal_mlp_request_id (uuid for
    the async UPDATE)."""
    src = _read_bot_py()
    if '_calmlp_annotate_async' not in src and '_calmlp_annotate_kwargs' not in src:
        pytest.skip('Edit 4 not yet applied to bot.py')
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == 'insert_evaluated_opportunity'):
            param_names = {a.arg for a in node.args.args}
            param_names.update(a.arg for a in node.args.kwonlyargs)
            for required in [
                'cal_mlp_p_mean', 'cal_mlp_p_std', 'cal_mlp_final_lo',
                'cal_mlp_final_hi', 'cal_mlp_train_id', 'cal_mlp_skipped_reason',
                'cal_mlp_request_id',
            ]:
                assert required in param_names, (
                    f"insert_evaluated_opportunity signature missing {required!r}. "
                    f"The **_shadow_diag splat at Edit 4 downstream would TypeError."
                )
            return
    pytest.fail('insert_evaluated_opportunity not found in bot.py AST')


# ---------------------------------------------------------------------------
# _predict_inner safety net (R-p7-deploy-r4#C1)
# ---------------------------------------------------------------------------
# These tests pin the fail-loud behavior that surfaces incomplete row_features
# instead of silently mean-imputing toward the training prior.

def test_predict_inner_safety_net_logic():
    """R-p7-deploy-r4#C1: _predict_inner must raise CalMLPError('missing_features')
    for any CONT_FEATURE_COL that is missing AND has no *_missing companion AND
    is not identity_no_zscore. AST-style guard so a future refactor can't
    remove the safety net without failing this test."""
    import inspect
    import integration
    src = inspect.getsource(integration.CalMLPPredictor._predict_inner)
    # The safety check must include the no-companion + not-identity branch.
    assert "_IDENTITY_NO_Z" in src or "identity_no_zscore" in src, (
        "_predict_inner missing the identity_no_zscore exemption — "
        "hour_sin/hour_cos would falsely raise missing_features."
    )
    assert "missing_features" in src
    # The branch must check `ind is None` (no missing-indicator companion).
    assert "ind is None" in src or "ind is not None" in src, (
        "Safety net must distinguish cols WITH vs WITHOUT a *_missing companion."
    )


def test_predict_inner_uses_module_level_identity_no_zscore():
    """R-p7-deploy-r5#L2: _IDENTITY_NO_Z is now hoisted to module scope
    (avoids per-call import overhead in the hot path). The safety net
    references the module-level constant; verify."""
    import integration
    assert hasattr(integration, '_IDENTITY_NO_Z')
    # hour_sin and hour_cos are the canonical identity_no_zscore cols.
    assert 'hour_sin' in integration._IDENTITY_NO_Z
    assert 'hour_cos' in integration._IDENTITY_NO_Z
    # _predict_inner must reference _IDENTITY_NO_Z (the safety net branch).
    import inspect
    src = inspect.getsource(integration.CalMLPPredictor._predict_inner)
    assert '_IDENTITY_NO_Z' in src, (
        "_predict_inner must reference module-level _IDENTITY_NO_Z to "
        "exempt analytical columns from missing_features raise."
    )


def test_post_hoc_processor_populates_full_cont_feature_cols():
    """R-p7-deploy-r4#C1 + R-p7-deploy-r9: the row_features dict built by the
    post-hoc processor's _process_row must include every CONT_FEATURE_COL
    that isn't auto-seeded by predict() (market_price, side_int, ticker_id,
    logit_raw_prob_clipped) and isn't identity_no_zscore (hour_sin/cos).
    Otherwise the safety net raises missing_features and calibration skips.
    R-p7-deploy-r9 moved feature reconstruction from bot.py Edit 4 (synchronous
    on scan thread) to post_hoc_processor.py (daemon-thread polling)."""
    import inspect
    from post_hoc_processor import CalMLPPostHocProcessor
    src = inspect.getsource(CalMLPPostHocProcessor._process_row)
    # Derive the actual v1 schema from features.py at test time, so this
    # test self-updates when v2/v3 expand the feature set.
    import features
    cont_cols = list(features.CONT_FEATURE_COLS)
    transforms = features.CONT_FEATURE_TRANSFORMS
    AUTO_SEEDED = {'market_price'}
    required = [
        c for c in cont_cols
        if c not in AUTO_SEEDED
        and transforms.get(c) != 'identity_no_zscore'
    ]
    missing = [k for k in required if f"'{k}'" not in src and f'"{k}"' not in src]
    assert not missing, (
        f"post_hoc_processor._process_row missing required CONT_FEATURE_COLS keys "
        f"(would skip via safety net): {missing}. CONT_FEATURE_COLS={cont_cols}"
    )


def test_normstats_concat_uses_per_file_sha_strings():
    """R-p4-r7-CRIT: producer/consumer alignment regression. The hash
    input is the per-file SHA hex strings from eval_fold_artifacts —
    NOT the canonical-JSON bytes of the normstats dicts. Mutating any
    fold's normstats_sha256 hex MUST change phase4_bundle_sha."""
    import hashlib
    import _helpers
    bundle = _make_synthetic_bundle()
    # Mutate fold 1's normstats_sha (a non-deploy fold) — phase4_bundle_sha
    # changes because it includes ALL folds' normstats_shas in the concat.
    bundle['eval_fold_artifacts'][1]['normstats_sha256'] = 'mutated'.ljust(64, 'x')
    with pytest.raises(RuntimeError, match='phase4 sha mismatch'):
        _helpers.verify_bundle_sha_chain(bundle)


def test_torch_threads_constrained_at_import():
    """R-p7-deploy-r7 regression: importing cal_mlp.integration must constrain
    torch to 1 intra-op AND 1 inter-op thread. Without this, scan loop
    balloons to ~1s/predict under contention (vs 15ms with threads=1).
    Production incident 2026-04-29: env=1 produced 0 candidate rows in 5 min;
    root cause was torch defaulting to num_cpus threads. Round-2 review
    flagged that asserting only intra-op missed the interop vector."""
    import integration  # noqa: F401 — triggers _constrain_torch_threads_at_import
    try:
        import torch
    except ImportError:
        pytest.skip("torch not installed")
    assert torch.get_num_threads() == 1, (
        f"torch.get_num_threads() = {torch.get_num_threads()} != 1; "
        "_constrain_torch_threads_at_import() did not configure intra-op."
    )
    # Module-level observed values reflect what the helper saw post-call.
    assert integration._TORCH_THREADS_INTRA == 1
    # Inter-op race CAN be lost if pytest collects another torch-using test
    # first. We don't assert RACE=False here (that's the subprocess test);
    # instead assert the helper ATTEMPTED interop=1, observed via the post-
    # call snapshot. If race lost, ATTRS still capture default==2 — that's a
    # sign of regression in the import-order contract.
    assert integration._TORCH_THREADS_INTEROP is not None, (
        "_constrain_torch_threads_at_import() did not run; integration "
        "import order is broken."
    )


def test_omp_mkl_env_set():
    """R-p7-deploy-r7: OMP/MKL/OpenBLAS thread caps must be in os.environ.
    This is a TAUTOLOGY check — the value-add is the subprocess test below
    that asserts they were set BEFORE numpy import. Keeping this for
    fast-fail when the setdefault block is deleted entirely."""
    import integration  # noqa: F401
    for var in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS'):
        assert os.environ.get(var) == '1', (
            f"{var} = {os.environ.get(var)!r}; expected '1'."
        )


# Numerical libs whose C extensions cache OpenBLAS/MKL thread count at load
# time. Any of these importing before _thread_env defeats the contention fix.
_NUMERICAL_LIBS = {
    'numpy', 'scipy', 'sklearn', 'pandas', 'torch',
    # also catch dotted forms like `scipy.stats`, `numpy.random`
}


def _first_lineno_of_numerical_or_thread_env(bot_py_text: str):
    """Walk the AST and return (thread_env_line, numerical_line). Both
    `ast.Import` (`import numpy`) and `ast.ImportFrom` (`from scipy.stats
    import t`) are checked. Returns the FIRST line of either, so a
    regression in either form is caught."""
    import ast
    tree = ast.parse(bot_py_text)
    thread_env_line = None
    numerical_line = None

    def _is_numerical(name: str) -> bool:
        # match 'numpy', 'scipy', 'scipy.stats', 'numpy.random', etc.
        if name is None:
            return False
        head = name.split('.')[0]
        return head in _NUMERICAL_LIBS

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == '_thread_env' and thread_env_line is None:
                    thread_env_line = node.lineno
                if _is_numerical(alias.name) and numerical_line is None:
                    numerical_line = node.lineno
        elif isinstance(node, ast.ImportFrom):
            # `from scipy.stats import t` → node.module = 'scipy.stats'
            if _is_numerical(node.module) and numerical_line is None:
                numerical_line = node.lineno
    return thread_env_line, numerical_line


def test_thread_env_imported_before_numerical_libs_in_bot_py():
    """R-p7-deploy-r7-r2#H1 + r3#H1: OMP/MKL setdefaults are NO-OP if any
    numerical lib (numpy/scipy/sklearn/pandas/torch) imports first — those
    C extensions cache OpenBLAS/MKL thread count at C-extension load. AST
    walk covers BOTH `import X` and `from X import ...` forms. Production
    incident 2026-04-29: scan loop 7.75s, 0 candidates in 5 min when this
    contract broke."""
    bot_py = Path(__file__).resolve().parents[1] / 'bot.py'
    thread_env_line, numerical_line = _first_lineno_of_numerical_or_thread_env(
        bot_py.read_text()
    )
    assert thread_env_line is not None, (
        "bot.py must `import _thread_env` (sets OMP_NUM_THREADS=1 etc.) "
        "before any numerical lib (numpy/scipy/sklearn/pandas/torch). "
        "Line not found."
    )
    assert numerical_line is not None, (
        "bot.py is expected to import a numerical lib. If this changed, "
        "the contract is moot — but verify other consumers still need it."
    )
    assert thread_env_line < numerical_line, (
        f"_thread_env imported at line {thread_env_line}, but numerical "
        f"lib at line {numerical_line}. Numerical libs must come AFTER "
        "_thread_env so OMP_NUM_THREADS=1 is read by OpenBLAS at C-ext "
        "load. Production-incident regression."
    )


@pytest.fixture(autouse=True)
def _reset_async_pool_state():
    """R-p7-deploy-r9: tests that touch the post-hoc processor leave a
    daemon thread + open sqlite conn behind. This fixture stops the
    processor between tests so module-level state doesn't leak."""
    yield
    try:
        import integration
    except ImportError:
        return
    proc = getattr(integration, '_POSTHOC_PROCESSOR', None)
    if proc is not None:
        try:
            proc.stop(timeout_sec=5.0)
        except Exception:
            pass
        integration._POSTHOC_PROCESSOR = None


def test_annotate_async_returns_none_no_calibrated_prob():
    """R-p7-deploy-r8 + Round-1#10: the async enqueue MUST return None.
    The sync version returned a calibrated final_prob that overrode raw;
    if a future regression returns a value, bot.py would mis-trade because
    Edit 4 no longer captures or uses the return value (v1 is shadow-only).
    Pin: signature has no `-> Optional[float]` return annotation OR returns
    None unconditionally."""
    import integration
    import inspect
    sig = inspect.signature(integration.annotate_evaluation_async_enqueue)
    # Either return annotation is None / Optional[None] / no annotation,
    # OR the function body has no `return` with an expression.
    src = inspect.getsource(integration.annotate_evaluation_async_enqueue)
    # Walk AST: every Return node must have value=None or no value.
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            assert node.value is None or (
                isinstance(node.value, ast.Constant) and node.value.value is None
            ), (
                "annotate_evaluation_async_enqueue returned a non-None value at "
                f"line {node.lineno}: v1 is shadow-only by spec; bot.py does "
                "not capture or use the return value, so a future regression "
                "would silently change trading behavior."
            )


def test_post_hoc_processor_updates_row_via_request_id(tmp_path):
    """R-p7-deploy-r9: integration test that the post-hoc processor polls
    evaluated_opportunities, finds the row by cal_mlp_request_id, runs
    predict(), and UPDATEs the row's cal_mlp_* columns. Replaces the v1.5
    worker test (the worker pool was retired)."""
    try:
        import torch  # noqa: F401  — integration imports torch via predictor
    except ImportError:
        pytest.skip("torch not installed")
    import integration
    import sqlite3 as _sql
    import time as _time
    from datetime import datetime, timezone
    db_path = str(tmp_path / "state.db")
    conn = _sql.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    # Schema includes the columns the post-hoc processor SELECTs from.
    conn.execute("""
        CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT, asset TEXT, side TEXT, product_type TEXT,
            evaluation_time TEXT,
            market_price INTEGER, seconds_to_close REAL,
            spot_distance_to_strike_sigma REAL, prob_breakeven_gap REAL,
            vol_regime TEXT, raw_prob REAL,
            cal_mlp_request_id TEXT,
            cal_mlp_p_mean REAL, cal_mlp_p_std REAL,
            cal_mlp_final_lo REAL, cal_mlp_final_hi REAL,
            cal_mlp_train_id TEXT, cal_mlp_skipped_reason TEXT
        )
    """)
    request_id = "test-request-id-posthoc"
    eval_time = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    conn.execute("""
        INSERT INTO evaluated_opportunities
            (ticker, asset, side, product_type, evaluation_time,
             market_price, seconds_to_close, spot_distance_to_strike_sigma,
             prob_breakeven_gap, vol_regime, raw_prob, cal_mlp_request_id)
        VALUES (?, 'BTC', 'yes', '15m', ?, 96, 240, 0.3, 0.05,
                'normal', 0.9, ?)
    """, ('KXBTC15M-TEST', eval_time, request_id))
    conn.commit()

    class StubPredictor:
        train_id = "test-train-id-posthoc"
        asset = "BTC"
        def predict(self, raw_prob, ticker, side, entry_price_cents, row_features):
            return (0.85, 0.05, 0.75, 0.95)

    proc = integration.start_post_hoc_processor(
        db_path=db_path, predictors={'BTC': StubPredictor()},
        poll_interval_sec=0.1, batch_size=10,
    )
    deadline = _time.monotonic() + 5.0
    pmean = None
    while _time.monotonic() < deadline:
        row = conn.execute(
            "SELECT cal_mlp_p_mean FROM evaluated_opportunities WHERE cal_mlp_request_id=?",
            (request_id,),
        ).fetchone()
        if row and row[0] is not None:
            pmean = row[0]
            break
        _time.sleep(0.1)
    integration.stop_post_hoc_processor(timeout_sec=2.0)
    assert pmean is not None, "post-hoc processor did not UPDATE the row within 5s"
    final = conn.execute(
        "SELECT cal_mlp_p_mean, cal_mlp_p_std, cal_mlp_final_lo, "
        "cal_mlp_final_hi, cal_mlp_train_id, cal_mlp_skipped_reason "
        "FROM evaluated_opportunities WHERE cal_mlp_request_id=?",
        (request_id,),
    ).fetchone()
    assert final == (0.85, 0.05, 0.75, 0.95, "test-train-id-posthoc", None), (
        f"post-hoc processor did not UPDATE all fields correctly: {final}"
    )
    conn.close()


def test_thread_env_is_zero_deps_no_numerical_imports():
    """R-p7-deploy-r7-r3#H2: subprocess test that PROVES the contract by
    showing _thread_env doesn't itself pull in numpy/scipy/torch. If a
    future change adds `import numpy` to _thread_env.py, the OMP=1
    setdefault becomes a no-op (numpy already loaded with default OpenBLAS
    threads). This test fails on that regression."""
    import os as _os
    import subprocess
    import textwrap
    import sys as _sys
    env = {k: v for k, v in _os.environ.items()
           if k not in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS',
                        'OPENBLAS_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
                        'VECLIB_MAXIMUM_THREADS')}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['PYTHONNOUSERSITE'] = '1'  # don't pull in user-site numpy
    env.pop('PYTHONSTARTUP', None)
    repo_root = str(Path(__file__).resolve().parents[1])
    script = textwrap.dedent(f"""
        import sys, os
        sys.path.insert(0, {repo_root!r} + '/scripts/cal_mlp')
        # Sanity: env should NOT have OMP set yet.
        assert 'OMP_NUM_THREADS' not in os.environ
        # Sanity: numerical libs should NOT have been imported yet by
        # Python's site init or our sys.path insert.
        for _lib in ('numpy', 'scipy', 'sklearn', 'pandas', 'torch'):
            assert _lib not in sys.modules, (
                f"unexpected: {{_lib}} loaded by Python init/site; this "
                f"test cannot validate _thread_env zero-deps contract."
            )
        # The actual contract:
        import _thread_env  # noqa: F401
        # 1) setdefault fired
        assert os.environ['OMP_NUM_THREADS'] == '1'
        assert os.environ['MKL_NUM_THREADS'] == '1'
        assert os.environ['OPENBLAS_NUM_THREADS'] == '1'
        # 2) _thread_env is ZERO-DEPS — it must not pull in any numerical
        # lib. If it did, OMP=1 would be a no-op since the lib's BLAS
        # backend would have read its env at the prior import.
        for _lib in ('numpy', 'scipy', 'sklearn', 'pandas', 'torch'):
            assert _lib not in sys.modules, (
                f"_thread_env.py pulled in {{_lib}} — the OMP setdefault "
                f"is now a no-op for that lib's BLAS backend. Either remove "
                f"the import from _thread_env, or do the setdefault even "
                f"earlier (e.g., in a sitecustomize.py)."
            )
        print("OK")
    """)
    # `-S` disables `import site` — hard-blocks sitecustomize.py / .pth
    # injectors that some HPC/conda envs use to preload numpy at startup.
    # Without -S the pre-assertion ("numpy not in sys.modules before
    # _thread_env import") could fire on environments where Python loaded
    # numpy via a system-site customization, even though our contract
    # would still be intact.
    result = subprocess.run(
        [_sys.executable, '-S', '-c', script],
        env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        f"subprocess failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert 'OK' in result.stdout, f"unexpected stdout: {result.stdout!r}"
