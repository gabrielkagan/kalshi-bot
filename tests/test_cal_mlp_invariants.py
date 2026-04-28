"""Regression tests for the P2 cal_mlp rebuild invariants.

Each test corresponds to a deploy-blocker CRITICAL caught during the
Apr 28 adversarial-review session (`memory/project_p2_rebuild_apr28.md`).
Stdlib + features.py + _helpers.py + sizing.py only (torch-free) — these
run on any environment, not just the VPS.

DO NOT loosen these assertions without a corresponding spec amendment.
"""
import hashlib
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
    """R-p7-impl#C11 + R-p7-r2#H1: enum locked at 12 entries."""
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
    assert hasattr(integration, '_MISSING_INDICATOR_SRC_TO_IND')
    inv = integration._MISSING_INDICATOR_SRC_TO_IND
    # Must be a frozen 7-entry dict (matches MISSING_INDICATOR_COLS count).
    assert len(inv) == 7
    # Inverse must round-trip: indicator → source → indicator.
    import features
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
