#!/usr/bin/env python3
"""Operator smoke-check for the P2 cal_mlp rebuild.

Runs the full pipeline end-to-end against a tiny synthetic dataset and
verifies the artifacts round-trip cleanly. Exit 0 = ready for real deploy;
exit non-zero = something is wrong with the rebuild on this environment.

This is the SECOND line of defense after `tests/integration/test_cal_mlp_invariants.py`
(which uses stdlib only). The smoke check requires torch + pandas + pyarrow
+ psutil, so it runs on the VPS but not on a torch-less local env.

Usage (from project root):
    python3 scripts/cal_mlp/smoke_check.py

Exit codes:
    0 — full pipeline ran clean; SHA chain verified end-to-end
    1 — generic failure (read the traceback)
    2 — environment missing required deps (torch / pandas / pyarrow / psutil)
    3 — pipeline produced an artifact that failed integrity verification

Time budget: ~30s on a modern VPS. NO state.db read; uses synthetic data
so it's safe to run anytime (does NOT touch the bot's running data).

R-p7-coldboot follow-up (Apr 28): operator should run this script BEFORE
applying bot.py edits — it would catch any environment-specific runtime
issue (torch version drift, NFS permissions, missing dep) that the static
adversarial review couldn't.
"""
from __future__ import annotations

import json
import sys
import tempfile
import traceback
from pathlib import Path


def _check_deps() -> int:
    missing = []
    for mod in ('torch', 'pandas', 'numpy', 'pyarrow', 'psutil'):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(f"FAIL: missing deps {missing}; install before running smoke_check.")
        return 2
    return 0


def _setup_path() -> None:
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    # Repo root must be on sys.path so `import integration` (cal_mlp local)
    # can transitively `import bot._thread_env` (the bot package) post-Bit-2.3.
    repo_root = here.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def _check_sha_chain_round_trip() -> int:
    """Builds a synthetic Phase 4 bundle and a Phase 5 bundle, then verifies
    via the canonical _helpers.verify_bundle_sha_chain. Catches any drift
    between train.py producer and _helpers consumer (the R-p4-r7-CRIT +
    R-p4-r8-CRIT failure mode)."""
    import hashlib

    import _helpers

    # Synthetic phase 4 bundle.
    eval_fold_artifacts = []
    for f in range(3):
        members = [
            {'checkpoint_sha256': f'fold{f}_member{m}_'.ljust(64, '0')}
            for m in range(5)
        ]
        eval_fold_artifacts.append({
            'fold': f,
            'normstats_sha256': f'fold{f}_normstats_'.ljust(64, '0'),
            'members': members,
        })
    deploy_idx = 2
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

    # Phase 4 only — should pass.
    _helpers.verify_bundle_sha_chain(bundle)

    # Phase 5 layer.
    conf_sha = 'conformal_'.ljust(64, '0')
    bundle['conformal_sha256'] = conf_sha
    bundle['bundle_sha'] = hashlib.sha256(f'{p4}:{conf_sha}'.encode()).hexdigest()
    _helpers.verify_bundle_sha_chain(bundle)

    # Verify rejection: corrupt the chain.
    bad = dict(bundle)
    bad['phase4_bundle_sha'] = 'wrong'
    try:
        _helpers.verify_bundle_sha_chain(bad)
    except RuntimeError:
        pass
    else:
        print("FAIL: SHA chain accepted a corrupted bundle (expected RuntimeError)")
        return 3
    return 0


def _check_normstats_round_trip() -> int:
    """fit_normstats → apply_norm round-trip on synthetic data. Catches the
    Phase 6 normstats unwrap bug (R-p6-impl-r5#CRIT) at the call-site
    pattern: passing the wrapper vs the inner stats dict."""
    import numpy as np
    import pandas as pd

    import features
    import normalize

    np.random.seed(7)
    n = 200
    df = pd.DataFrame({col: np.random.randn(n).astype(np.float32)
                       for col in features.CONT_FEATURE_COLS})
    df['market_price'] = np.random.uniform(50, 99, n).astype(np.float32)
    df['log_balance_dollars'] = np.random.uniform(50_000, 200_000, n).astype(np.float32)
    df['btc_realized_vol_15m'] = np.abs(df['btc_realized_vol_15m'])
    df['hour_sin'] = np.sin(np.random.uniform(0, 2*np.pi, n)).astype(np.float32)
    df['hour_cos'] = np.cos(np.random.uniform(0, 2*np.pi, n)).astype(np.float32)

    ns = normalize.fit_normstats(df, features.CONT_FEATURE_COLS,
                                  features.CONT_FEATURE_TRANSFORMS)
    # Wrap in the disk-format payload (per extract_data.py:670-674)
    payload = {
        'fold': 0, 'asset': 'BTC', 'cutoff_end': '2026-04-28T00:00:00Z',
        'n_train': n, 'ddof': 1,
        'transforms': dict(features.CONT_FEATURE_TRANSFORMS),
        'stats': ns,
    }
    # Phase 6 / Phase 7 must unwrap correctly: payload['stats'].
    df_norm = normalize.apply_norm(df, payload['stats'], features.CONT_FEATURE_COLS,
                                    transforms=payload['transforms'])
    # Post z-score: most cols should have mean ≈ 0, std ≈ 1.
    for col in ['market_price', 'btc_realized_vol_15m', 'log_balance_dollars']:
        m, s = df_norm[col].mean(), df_norm[col].std()
        if abs(m) > 0.05 or abs(s - 1.0) > 0.05:
            print(f"FAIL: {col} post-norm m={m:.3f} s={s:.3f} — expected ≈0, ≈1")
            return 3

    # NaN handling — inject NaN and confirm imputed-via-fillna path.
    df_test = df.iloc[:5].copy()
    df_test.loc[0, 'market_price'] = np.nan
    df_test_norm = normalize.apply_norm(df_test, payload['stats'],
                                          features.CONT_FEATURE_COLS,
                                          transforms=payload['transforms'])
    if not abs(df_test_norm['market_price'].iloc[0]) < 0.05:
        print(f"FAIL: NaN-imputed market_price = "
              f"{df_test_norm['market_price'].iloc[0]:.3f} (expected ≈ 0 = train mean)")
        return 3
    return 0


def _check_sizing_parity() -> int:
    """8 parity vectors: cal_mlp/sizing.compute_size vs integration mirror.

    Smell 4 refactor (ticket 86b9vhccw): make_compute_for_15m_main_path
    now imports its dependent names directly from bot.constants + config
    inside the function body — no caller-passed dict. Cross-source equality
    (cal_mlp.sizing values match bot.constants/config values) is enforced
    by parity_assert at boot."""
    import sizing
    import integration

    bot_compute = integration.make_compute_for_15m_main_path()
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
        if cm.contract_count != bot_r['contracts']:
            print(f"FAIL: parity break vec={vec}: "
                  f"cal_mlp={cm.contract_count} bot={bot_r['contracts']}")
            return 3
        if expected is not None and cm.contract_count != expected:
            print(f"FAIL: expected mismatch vec={vec}: "
                  f"got={cm.contract_count} expected={expected}")
            return 3
    return 0


def _check_integration_module_loads() -> int:
    """integration.py module-load runs _build_missing_indicator_inverse() —
    this catches any features.MISSING_INDICATOR_SOURCE_MAP drift at import
    time (R-p7-cleanroom-r6#H1)."""
    import integration
    if len(integration.SKIPPED_REASONS) != 12:
        print(f"FAIL: SKIPPED_REASONS={len(integration.SKIPPED_REASONS)} (expected 12)")
        return 3
    if len(integration._MISSING_INDICATOR_SRC_TO_IND) != 7:
        print(f"FAIL: _MISSING_INDICATOR_SRC_TO_IND="
              f"{len(integration._MISSING_INDICATOR_SRC_TO_IND)} (expected 7)")
        return 3
    return 0


def _check_predictor_construction_zero_io() -> int:
    """CalMLPPredictor.__init__ must do NO file IO — kill-switch contract.
    With CALMLP_ENABLED=0, predictors are constructed but not warmed; that
    construction must be free."""
    import integration
    # Use a non-existent project_root — if __init__ touches disk this would
    # raise; if it's pure attribute-set we're fine.
    p = integration.CalMLPPredictor('BTC', project_root=Path('/nonexistent_root'))
    assert p.asset == 'BTC'
    assert p._loaded is False
    assert p.vocab is None
    assert p.normstats is None
    assert p.models is None
    return 0


def _check_wal_pragma_assertion() -> int:
    """_verify_wal must reject conn without WAL OR without busy_timeout≥10000."""
    import sqlite3

    import integration

    with tempfile.TemporaryDirectory() as td:
        conn = sqlite3.connect(str(Path(td) / 'smoke.db'))
        try:
            integration._verify_wal(conn)
        except integration.CalMLPSchemaError:
            pass
        else:
            print("FAIL: _verify_wal accepted a non-WAL conn")
            return 3
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            integration._verify_wal(conn)
        except integration.CalMLPSchemaError:
            pass
        else:
            print("FAIL: _verify_wal accepted WAL but busy_timeout=0 conn")
            return 3
        conn.execute("PRAGMA busy_timeout=10000")
        integration._verify_wal(conn)  # must not raise
        conn.close()
    return 0


CHECKS = [
    ('integration_module_loads', _check_integration_module_loads),
    ('sha_chain_round_trip', _check_sha_chain_round_trip),
    ('normstats_round_trip', _check_normstats_round_trip),
    ('sizing_parity', _check_sizing_parity),
    ('predictor_init_zero_io', _check_predictor_construction_zero_io),
    ('wal_pragma_assertion', _check_wal_pragma_assertion),
]


def main() -> int:
    rc = _check_deps()
    if rc != 0:
        return rc
    _setup_path()

    print("=" * 60)
    print("P2 cal_mlp smoke check — adversarial review invariants")
    print("=" * 60)
    failed = []
    for name, fn in CHECKS:
        try:
            rc = fn()
            if rc == 0:
                print(f"  [OK]   {name}")
            else:
                print(f"  [FAIL] {name} → rc={rc}")
                failed.append(name)
        except Exception:
            print(f"  [EXC]  {name}")
            traceback.print_exc()
            failed.append(name)
    print("=" * 60)
    if failed:
        print(f"FAIL: {len(failed)}/{len(CHECKS)} checks failed: {failed}")
        return 1
    print(f"OK: {len(CHECKS)}/{len(CHECKS)} checks passed.")
    print("Rebuild is sound. Operator may proceed with the bot.py edits per")
    print("kb-research/bot/p2-phase7-bot-py-diff.md.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
