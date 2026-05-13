"""Phase 2 P2.1.a-1 — TDD baseline equivalence test for v1 cal_mlp bundles.

Pins the v1 (2026-04-28 CURRENT) predictor's exact (cal_prob, ens_std,
final_lo, final_hi) output for each of BTC/ETH/SOL/XRP on a fixed 6-row
stratified synthetic corpus. See `cal_mlp_v1_baseline/CORPUS.md` for the
design and the regen protocol.

Why this exists (per ClickUp `86b9wuhhr` C0 discipline):
> "TDD-first: write equivalence test against existing v1 bundle output
> BEFORE retraining"

When the v1.1 retrain ships, this test will continue to pass against the
v1 bundles (they stay on disk under their old train_id directories) — it
locks v1 forever, so v1.1 validation can compare against a stable v1 baseline.

Local-only by design: cal_mlp bundles are gitignored (see `.gitignore`
`/models/` entry) so they don't ship to CI; the test self-skips when the
CURRENT pointer is absent. Re-sync from VPS with:
    rsync -av botuser@<vps>:~/kalshi-bot-repo/models/ models/

The captured snapshots live in `tests/integration/cal_mlp_v1_baseline/
cal_mlp_v1_baseline_<asset>.json`.

REGEN: see `cal_mlp_v1_baseline/CORPUS.md` § REGEN PROTOCOL. Never run
the capture script with `--regen` autonomously — regen is a
human-with-diff-review operation.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_DIR = _REPO_ROOT / 'tests' / 'integration' / 'cal_mlp_v1_baseline'
_CAL_MLP_DIR = _REPO_ROOT / 'scripts' / 'cal_mlp'

# Expected v1 CURRENT train_ids per asset (see kb/findings/
# cal-mlp-v1-baseline-date-discrepancy-may13.md — ALL are 2026-04-28, the
# 2026-05-03 candidate dirs are unpromoted).
_EXPECTED_V1_TRAIN_IDS = {
    'BTC': '2026-04-28T11:50:29.671752Z-8acc233e',
    'ETH': '2026-04-28T11:50:48.975743Z-a0000cc1',
    'SOL': '2026-04-28T11:50:38.948474Z-2901fa5f',
    'XRP': '2026-04-28T11:50:39.500729Z-b8161b7b',
}

# v1 feature-recipe fingerprint. Any change to features.compute_cfg_fp inputs
# would shift this; the test enforces v1's fingerprint is byte-stable.
_V1_CFG_FP = '178d14020bd21beb'

_OUTPUT_KEYS = ('cal_prob', 'ens_std', 'final_lo', 'final_hi')


def _v1_bundle_dir(asset: str) -> Path:
    """Pinned absolute path to the v1 bundle for `asset`, regardless of what
    `models/cal_mlp_<asset>/CURRENT` points at. After v1.1 ships and rotates
    CURRENT, this path remains valid because the old v1 train_id directory
    stays on disk."""
    return (_REPO_ROOT / 'models' / f'cal_mlp_{asset}'
            / _EXPECTED_V1_TRAIN_IDS[asset])


def _bundle_available(asset: str) -> bool:
    """v1 bundle on disk is the load-bearing artifact. CURRENT pointer is NOT
    checked here — once v1.1 ships, CURRENT rotates but v1 dir remains."""
    bundle_dir = _v1_bundle_dir(asset)
    train_id = _EXPECTED_V1_TRAIN_IDS[asset]
    phase5 = bundle_dir / f'cal_mlp_{asset}_{train_id}_phase5_bundle.json'
    return phase5.exists()


def _load_baseline(asset: str) -> dict:
    path = _BASELINE_DIR / f'cal_mlp_v1_baseline_{asset}.json'
    if not path.exists():
        pytest.skip(f'baseline snapshot not found: {path.name}')
    return json.loads(path.read_text())


@pytest.fixture(scope='module')
def _predictor_factory():
    """Lazy import + path setup so the test self-skips cleanly when bundles
    are missing (avoids importing torch / bot._thread_env on CI where this
    test won't run anyway)."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    if str(_CAL_MLP_DIR) not in sys.path:
        sys.path.insert(0, str(_CAL_MLP_DIR))
    import integration  # noqa: E402
    return integration.CalMLPPredictor


@pytest.fixture
def _pin_v1_bundle_dir(monkeypatch):
    """Pin each asset's predictor to the v1 train_id via the per-asset
    `CALMLP_BUNDLE_DIR_<ASSET>` env override. Without this, the test reads
    `models/cal_mlp_<asset>/CURRENT` — which rotates to the v1.1 train_id
    the moment v1.1 ships, breaking the test's TDD-anchor purpose.

    `_resolve_bundle_dir` (scripts/cal_mlp/integration.py:659) reads these
    env vars on every `_load()`, so monkeypatching them in the fixture is
    sufficient — no module reload needed."""
    def _pin(asset: str) -> None:
        bundle_dir = _v1_bundle_dir(asset)
        monkeypatch.setenv(f'CALMLP_BUNDLE_DIR_{asset}', str(bundle_dir))
    return _pin


@pytest.mark.parametrize('asset', ['BTC', 'ETH', 'SOL', 'XRP'])
def test_v1_baseline_predict_reproduces_pinned_outputs(
    asset, _predictor_factory, _pin_v1_bundle_dir,
):
    """For each asset, re-run the v1 predictor against the 6-row corpus
    and assert each row's (cal_prob, ens_std, final_lo, final_hi) matches
    the pinned snapshot to floating-point precision.

    Uses `CALMLP_BUNDLE_DIR_<ASSET>` to pin to the v1 train_id directly —
    NOT via CURRENT — so the test continues to validate v1's serving
    behavior after v1.1 rotates CURRENT.

    A failure here means ONE of:
    - torch / numpy / scipy minor version drift on the local machine
    - `scripts/cal_mlp/features.py` feature transform changed (cfg_fp drift)
    - `scripts/cal_mlp/integration.py` predict path changed
    - `scripts/cal_mlp/conformal.py` conformal arithmetic changed
    - The v1 bundle on disk was overwritten

    DO NOT fix by regenerating the snapshot — investigate the divergence
    first (see CORPUS.md § REGEN PROTOCOL)."""
    if not _bundle_available(asset):
        pytest.skip(
            f'v1 bundle dir missing: '
            f'models/cal_mlp_{asset}/{_EXPECTED_V1_TRAIN_IDS[asset]}/. '
            f'Re-sync from VPS via `rsync -av '
            f'botuser@<vps>:~/kalshi-bot-repo/models/ models/` '
            f'(local-only by `.gitignore` /models/ rule).'
        )

    _pin_v1_bundle_dir(asset)
    baseline = _load_baseline(asset)

    assert baseline['asset'] == asset, (
        f'snapshot file misnamed: stored asset={baseline["asset"]!r}, expected {asset!r}'
    )
    assert baseline['train_id'] == _EXPECTED_V1_TRAIN_IDS[asset], (
        f'baseline snapshot pinned to train_id={baseline["train_id"]} but '
        f'expected v1 CURRENT={_EXPECTED_V1_TRAIN_IDS[asset]}. Either the '
        f'snapshot was regenerated against the wrong bundle, or v1 CURRENT '
        f'has rotated (which should ship with a KB decision doc).'
    )
    assert baseline['cfg_fp'] == _V1_CFG_FP, (
        f'baseline cfg_fp={baseline["cfg_fp"]} != v1 fingerprint {_V1_CFG_FP}; '
        f'a different feature recipe was captured.'
    )

    predictor = _predictor_factory(asset, project_root=_REPO_ROOT)
    predictor.warmup()

    assert predictor.train_id == _EXPECTED_V1_TRAIN_IDS[asset], (
        f'{asset}: predictor loaded train_id={predictor.train_id} '
        f'but v1 CURRENT is {_EXPECTED_V1_TRAIN_IDS[asset]}. '
        f'`models/cal_mlp_{asset}/CURRENT` may have been rotated.'
    )

    for row in baseline['rows']:
        inputs = row['inputs']
        cal_prob, ens_std, final_lo, final_hi = predictor.predict(
            raw_prob=inputs['raw_prob'],
            ticker=inputs['ticker'],
            side=inputs['side'],
            entry_price_cents=inputs['price_cents'],
            row_features=inputs['row_features'],
        )
        live = {
            'cal_prob': cal_prob,
            'ens_std': ens_std,
            'final_lo': final_lo,
            'final_hi': final_hi,
        }
        expected = row['outputs']
        for k in _OUTPUT_KEYS:
            # exact equality — same torch+numpy+pyarrow versions, same bundle
            # on disk, same RNG path → bit-identical. Float drift would indicate
            # a real change.
            assert live[k] == expected[k], (
                f'{asset} row_id={row["row_id"]}: {k} drifted. '
                f'live={live[k]!r}, pinned={expected[k]!r}. '
                f'Investigate the divergence per CORPUS.md REGEN PROTOCOL — '
                f'do NOT regenerate the snapshot autonomously.'
            )


def test_baseline_snapshots_all_pin_v1_2026_04_28():
    """Snapshot-schema invariant: every captured baseline pins v1 CURRENT
    (2026-04-28), NOT the unpromoted 2026-05-03 candidates that also live
    on disk. See kb/findings/cal-mlp-v1-baseline-date-discrepancy-may13.md
    for context (C0 ticket text incorrectly says 2026-05-03)."""
    for asset, expected_train_id in _EXPECTED_V1_TRAIN_IDS.items():
        path = _BASELINE_DIR / f'cal_mlp_v1_baseline_{asset}.json'
        if not path.exists():
            pytest.skip(f'no baseline for {asset}')
        snap = json.loads(path.read_text())
        assert snap['train_id'] == expected_train_id, (
            f'{asset}: snapshot pinned train_id={snap["train_id"]}, '
            f'expected v1 CURRENT {expected_train_id} (2026-04-28). '
            f'The 2026-05-03 candidate bundles are NOT what serves traffic.'
        )
        assert snap['cfg_fp'] == _V1_CFG_FP
        assert len(snap['rows']) == 6, (
            f'{asset}: expected 6-row corpus, got {len(snap["rows"])}. '
            f'Adding/removing rows is a corpus-design change — update CORPUS.md '
            f'and regen with explicit operator approval.'
        )


def test_baseline_snapshots_corpus_rows_are_strata_complete():
    """The corpus design (CORPUS.md) stratifies by price-tier × STC-tier.
    A snapshot that lost stratification coverage would silently weaken the
    test. Pin the 6 (price, stc) tuples."""
    expected_strata = [
        (60, 720),
        (75, 360),
        (85, 600),
        (92, 180),
        (97, 120),
        (99, 60),
    ]
    for asset in _EXPECTED_V1_TRAIN_IDS:
        path = _BASELINE_DIR / f'cal_mlp_v1_baseline_{asset}.json'
        if not path.exists():
            pytest.skip(f'no baseline for {asset}')
        snap = json.loads(path.read_text())
        strata = [
            (r['inputs']['price_cents'], r['inputs']['row_features']['seconds_to_close'])
            for r in snap['rows']
        ]
        assert strata == expected_strata, (
            f'{asset}: corpus strata drifted. live={strata!r}, '
            f'expected={expected_strata!r}'
        )
