"""Phase 1b — ETH v2 rollout (one-shot operator handoff).

Plan: kb/decisions/calmlp-phase-1b-eth-v2-rollout-may06.md
Phase 1a (resolver): tests/test_calmlp_bundle_dir_per_asset.py

Pins:
  - chosen v2 ETH train_id (so a future rename / accidental re-train surfaces)
  - v1 ETH train_id (so v2 ≠ v1 sanity holds)
  - per-asset v1 train_ids for BTC/SOL/XRP (so Step 4 acceptance is exact strings)
  - cfg_fp values (so a re-train under a different feature config surfaces)
  - market_blend_w (so a config drift between training and live serving surfaces)
  - n_vocab (so a re-train with a different ticker universe surfaces)
  - the verifier script's exit-code contract under the green path AND a
    negative path

Bundle-on-disk tests are gated by a sentinel: if the bundle directory is
absent (clean checkout, CI without LFS, post-supersession after a v3
rollout), the affected tests skip with a clear message rather than fail.
This keeps the harness re-runnable on any host without prerequisites.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERIFIER_PATH = PROJECT_ROOT / "scripts" / "cal_mlp" / "phase1b_verify_eth_v2.py"

# Load the verifier as a module so we can pin its constants and call its
# helpers from pytest. importlib avoids polluting sys.modules with the
# script as a top-level module across the broader test suite.
_spec = importlib.util.spec_from_file_location("_phase1b_verifier", VERIFIER_PATH)
_verifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_verifier)


@pytest.fixture(autouse=True)
def _isolate_calmlp_env(monkeypatch):
    """Ensure no Phase 1a env vars leak across tests in this file."""
    for var in (
        "CALMLP_BUNDLE_DIR",
        "CALMLP_BUNDLE_DIR_BTC",
        "CALMLP_BUNDLE_DIR_ETH",
        "CALMLP_BUNDLE_DIR_SOL",
        "CALMLP_BUNDLE_DIR_XRP",
        "KALSHI_PROJECT_ROOT",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Constant pins — surface a regression if a future Phase 1b accidentally
# overwrites these without an intentional re-pin.
# ---------------------------------------------------------------------------

def test_v2_eth_train_id_pinned():
    """The chosen v2 ETH train_id must remain stable. If you're rolling
    out a different v2/v3 bundle, write a new verifier (and a new pin)."""
    assert _verifier.V2_ETH_TRAIN_ID == "2026-05-03T16:28:21.622306Z-f08fbc00"


def test_v1_eth_train_id_pinned():
    """v1 baseline. Used by `check_v2_differs_from_v1` and
    `check_current_pointer_is_v1` to assert the rollout is shipping a
    meaningfully different model AND that rollback would fall back to v1."""
    assert _verifier.V1_ETH_TRAIN_ID == "2026-04-28T11:50:48.975743Z-a0000cc1"


def test_prod_v1_train_ids_pinned():
    """Step 4 post-restart SQL compares each asset's loaded train_id
    against an exact pin. Each asset's v1 was trained separately with a
    different commit-sha tail; "starts-with" matching would silently
    accept a wrong bundle, so we pin the exact strings."""
    assert _verifier.PROD_V1_TRAIN_IDS == {
        "BTC": "2026-04-28T11:50:29.671752Z-8acc233e",
        "ETH": "2026-04-28T11:50:48.975743Z-a0000cc1",
        "SOL": "2026-04-28T11:50:38.948474Z-2901fa5f",
        "XRP": "2026-04-28T11:50:39.500729Z-b8161b7b",
    }


def test_prod_v1_eth_matches_eth_pin():
    """Cross-check: the ETH entry in PROD_V1_TRAIN_IDS must equal the
    standalone V1_ETH_TRAIN_ID pin. If they drift, the runbook + verifier
    disagree on what 'v1 ETH' is."""
    assert _verifier.PROD_V1_TRAIN_IDS["ETH"] == _verifier.V1_ETH_TRAIN_ID


def test_cfg_fps_pinned_and_distinct():
    """Phase 0 evaluated v2 (1969b…) against v1 (178d1…). Either drifting
    means the rollout is no longer the bundle Phase 0 cleared."""
    assert _verifier.V2_ETH_EXPECTED_CFG_FP == "1969b12c6c0c39bf"
    assert _verifier.V1_ETH_EXPECTED_CFG_FP == "178d14020bd21beb"
    assert (
        _verifier.V2_ETH_EXPECTED_CFG_FP != _verifier.V1_ETH_EXPECTED_CFG_FP
    ), "v2 and v1 cfg_fp must differ; otherwise we're 'rolling out' the same model"


def test_market_blend_w_matches_15m_market_config():
    """Bundle-vs-live blend-w drift would raise CalMLPError('market_blend_w_drift')
    in production. Pin the bundle's expected value to 15m's MARKET_CONFIGS
    entry so this test fails BEFORE deploy if either side drifts."""
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
        _added = True
    else:
        _added = False
    try:
        from market_config import MARKET_CONFIGS
        live_w = float(MARKET_CONFIGS["15m"].market_blend_w)
    finally:
        if _added:
            try:
                sys.path.remove(str(PROJECT_ROOT))
            except ValueError:
                pass
    assert (
        abs(live_w - _verifier.EXPECTED_MARKET_BLEND_W) < 1e-9
    ), f"15m MARKET_CONFIGS.market_blend_w={live_w} != verifier pin={_verifier.EXPECTED_MARKET_BLEND_W}"


def test_phase_5_pinned():
    """Phase-4-only ablation bundles must NOT deploy (per integration._load
    R-p7-r2#H3). Verifier pin guards that."""
    assert _verifier.EXPECTED_PHASE == 5


def test_n_vocab_pinned():
    """A re-train with a different ticker universe (e.g., new asset added
    to extract_data) would change n_vocab. Pin so the verifier surfaces
    this before deploy."""
    assert _verifier.EXPECTED_N_VOCAB == 3140


def test_full_model_def_fields_pinned():
    """All 16 model_definition fields are pinned (not just n_vocab) so
    when canonical-SHA mismatches in check_file_shas, bundle_metadata's
    field-drift report shows WHICH fields changed."""
    expected_keys = {
        "cfg_fp", "delta_logit_clamp", "dropout", "emb_dim", "hidden_1",
        "hidden_2", "input_continuous_dim", "model_kind", "n_cont",
        "n_missing_indicator_cols", "n_price_tiers", "n_sides",
        "n_stc_buckets", "n_vocab", "n_vol_regimes", "raw_prob_clip_eps",
    }
    assert set(_verifier.EXPECTED_MODEL_DEF_FIELDS.keys()) == expected_keys
    # Cross-check: n_vocab in the dict matches the standalone constant.
    assert _verifier.EXPECTED_MODEL_DEF_FIELDS["n_vocab"] == _verifier.EXPECTED_N_VOCAB
    assert _verifier.EXPECTED_MODEL_DEF_FIELDS["cfg_fp"] == _verifier.V2_ETH_EXPECTED_CFG_FP


def test_required_extract_fold_sha_keys():
    """Extract bundle MUST have normstats + parquet per fold. predictions
    lives in the model bundle, not the extract bundle — so it's NOT in
    this list (this was a bug in the verifier's first draft)."""
    assert _verifier.REQUIRED_EXTRACT_FOLD_SHA_KEYS == (
        "normstats_sha256", "parquet_sha256",
    )
    assert _verifier.REQUIRED_MODEL_FOLD_SHA_KEYS == ("predictions_sha256",)


# ---------------------------------------------------------------------------
# Verifier semantics — exercise the script against the real bundle when
# present; skip with a clear message when not (clean checkout / CI).
# ---------------------------------------------------------------------------

def _bundle_present() -> bool:
    return (PROJECT_ROOT / _verifier.V2_ETH_BUNDLE_REL).is_dir()


def _torch_pandas_present() -> bool:
    try:
        import torch  # noqa: F401
        import pandas  # noqa: F401
        import pyarrow  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _bundle_present(), reason="v2 ETH bundle not on disk")
def test_verifier_bundle_exists_passes():
    ok, msg = _verifier.check_bundle_exists(PROJECT_ROOT)
    assert ok, msg


@pytest.mark.skipif(not _bundle_present(), reason="v2 ETH bundle not on disk")
def test_verifier_bundle_metadata_passes():
    ok, msg = _verifier.check_bundle_metadata(PROJECT_ROOT)
    assert ok, msg


@pytest.mark.skipif(not _bundle_present(), reason="v2 ETH bundle not on disk")
def test_verifier_file_shas_pass():
    """Catches scp corruption / partial copies / hand-edits.
    Coverage spans BOTH bundles: the model bundle's per-member
    checkpoint+marker AND its per-fold predictions, plus the extract
    bundle's per-fold normstats + parquet."""
    ok, msg = _verifier.check_file_shas(PROJECT_ROOT)
    assert ok, msg
    # Sanity-pin the count so a future schema change doesn't silently
    # reduce coverage.
    assert "29 runtime-loaded files" in msg, f"file count drift: {msg!r}"


@pytest.mark.skipif(not _bundle_present(), reason="v2 ETH bundle not on disk")
def test_verifier_sha_chain_passes():
    """Same chain check the integration loader runs at warmup."""
    ok, msg = _verifier.check_sha_chain(PROJECT_ROOT)
    assert ok, msg


def test_verifier_v2_differs_from_v1_logic():
    """Pure-logic check; no bundle on disk required."""
    ok, _ = _verifier.check_v2_differs_from_v1(PROJECT_ROOT)
    assert ok


@pytest.mark.skipif(
    not _bundle_present() or not _torch_pandas_present(),
    reason="v2 ETH bundle or torch/pandas not available",
)
def test_verifier_load_with_override_passes(monkeypatch):
    """End-to-end: env override → CalMLPPredictor loads v2 bundle.
    monkeypatch ensures env vars are isolated."""
    monkeypatch.delenv("CALMLP_BUNDLE_DIR_ETH", raising=False)
    monkeypatch.delenv("CALMLP_BUNDLE_DIR", raising=False)
    ok, msg = _verifier.check_load_with_override(PROJECT_ROOT)
    assert ok, msg


# ---------------------------------------------------------------------------
# Cross-asset isolation — set CALMLP_BUNDLE_DIR_ETH and verify the OTHER
# three predictors still resolve to '' (= CURRENT path on the VPS, which
# = v1 today). This pins the asymmetric-rollout invariant: only ETH moves.
# ---------------------------------------------------------------------------

def test_other_assets_unaffected_by_eth_override(monkeypatch):
    cal_mlp_dir = PROJECT_ROOT / "scripts" / "cal_mlp"
    _path_str = str(cal_mlp_dir)
    _added = _path_str not in sys.path
    if _added:
        sys.path.insert(0, _path_str)
    try:
        monkeypatch.setenv("CALMLP_BUNDLE_DIR_ETH", _verifier.V2_ETH_BUNDLE_REL)
        from integration import _resolve_bundle_dir

        assert _resolve_bundle_dir("ETH") == _verifier.V2_ETH_BUNDLE_REL
        for asset in ("BTC", "SOL", "XRP"):
            assert _resolve_bundle_dir(asset) == "", (
                f"asset={asset} resolved to {_resolve_bundle_dir(asset)!r}; "
                f"expected '' (only ETH should be overridden in Phase 1b)"
            )
    finally:
        if _added:
            try:
                sys.path.remove(_path_str)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Verifier CLI exit code — green path returns 0; pin the contract so a
# future refactor doesn't accidentally exit 0 on failure.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _bundle_present(), reason="v2 ETH bundle not on disk")
def test_verifier_cli_skip_load_succeeds():
    """--skip-load --skip-current-pointer: file SHA + chain checks
    without torch and without requiring CURRENT to point at v1
    (Mac-local CURRENT is intentionally v2 for development)."""
    result = subprocess.run(
        [sys.executable, str(VERIFIER_PATH), "--skip-load", "--skip-current-pointer"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=60,
    )
    assert result.returncode == 0, (
        f"exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.mark.skipif(
    not _bundle_present() or not _torch_pandas_present(),
    reason="v2 ETH bundle or torch/pandas not available",
)
def test_verifier_cli_full_run_succeeds():
    """Full path including end-to-end load. Skip the CURRENT-points-at-v1
    check on Mac (where CURRENT is v2 for local development); the
    full-no-skip path is exercised on VPS post-deploy."""
    result = subprocess.run(
        [sys.executable, str(VERIFIER_PATH), "--skip-current-pointer"],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )
    assert result.returncode == 0, (
        f"exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_verifier_cli_fails_on_missing_bundle(tmp_path, monkeypatch):
    """Negative-path: under a fake project root with no models/, the
    verifier exits non-zero. Uses KALSHI_PROJECT_ROOT to pin the root
    deterministically (no upward filesystem walk that could hit the real
    repo on shared CI)."""
    (tmp_path / "pyproject.toml").write_text('[tool]\nname = "fake"\n')
    (tmp_path / "market_config.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class C:\n"
        "    market_blend_w: float = 0.40\n"
        "MARKET_CONFIGS = {'15m': C()}\n"
    )
    env = os.environ.copy()
    env["KALSHI_PROJECT_ROOT"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(VERIFIER_PATH), "--skip-load", "--skip-current-pointer"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=30,
    )
    assert result.returncode != 0, (
        f"expected non-zero exit on missing bundle; got 0\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "bundle dir missing" in combined, f"expected 'bundle dir missing' in output:\n{combined}"


def test_verifier_honors_kalshi_project_root(tmp_path):
    """Pin: KALSHI_PROJECT_ROOT env var overrides the upward walk so
    test fixtures + sandbox shells aren't fooled by ancestors that
    happen to look like a repo root. Mirrors the integration.py
    convention."""
    env = os.environ.copy()
    env["KALSHI_PROJECT_ROOT"] = str(tmp_path)
    # Run the verifier; bundle is missing under tmp_path so it must fail.
    result = subprocess.run(
        [sys.executable, str(VERIFIER_PATH), "--skip-load", "--skip-current-pointer"],
        capture_output=True,
        text=True,
        cwd="/",  # NOT inside the real repo, to prove cwd isn't being used
        env=env,
        timeout=30,
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert str(tmp_path) in combined, (
        f"verifier didn't honor KALSHI_PROJECT_ROOT; output:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Operator-env-pin: the literal string the operator will export on VPS
# matches what the verifier expects. If anyone updates the verifier
# constant without updating the runbook (or vice versa), this fails.
# ---------------------------------------------------------------------------

def test_operator_env_value_string():
    """The string the operator is told to set in the .env file (key
    `CALMLP_BUNDLE_DIR_ETH=...`) is exactly the verifier's
    V2_ETH_BUNDLE_REL. The KB doc and operator runbook both reference
    this constant; this test is the cross-check."""
    expected = "models/cal_mlp_ETH/2026-05-03T16:28:21.622306Z-f08fbc00"
    assert _verifier.V2_ETH_BUNDLE_REL == expected


# ---------------------------------------------------------------------------
# Current-pointer check — separate test family because it's host-specific
# (Mac CURRENT = v2 by design; VPS CURRENT must be v1 for rollback safety).
# ---------------------------------------------------------------------------

def _write_v1_current_for_all_assets(tmp_path):
    for asset, train_id in _verifier.PROD_V1_TRAIN_IDS.items():
        d = tmp_path / "models" / f"cal_mlp_{asset}"
        d.mkdir(parents=True)
        (d / "CURRENT").write_text(train_id + "\n")


def test_current_pointer_check_fails_on_mac_when_pointing_to_v2(tmp_path):
    """Synthetic: write CURRENTs for all 4 assets, set ETH to v2's
    train_id; check fails. This is what Mac looks like today (all 4
    Mac CURRENTs are v2-trained), and is why the VPS runbook MUST run
    this check BEFORE setting the env override."""
    _write_v1_current_for_all_assets(tmp_path)
    eth_current = tmp_path / "models" / "cal_mlp_ETH" / "CURRENT"
    eth_current.write_text(_verifier.V2_ETH_TRAIN_ID + "\n")
    ok, msg = _verifier.check_current_pointer_is_v1(tmp_path)
    assert not ok
    assert _verifier.V2_ETH_TRAIN_ID in msg
    assert "rollback" in msg


def test_current_pointer_check_passes_when_pointing_to_v1(tmp_path):
    """Synthetic: all 4 CURRENTs pointing at their v1 train_ids — passes.
    This is what we expect on the VPS today."""
    _write_v1_current_for_all_assets(tmp_path)
    ok, msg = _verifier.check_current_pointer_is_v1(tmp_path)
    assert ok, msg
    # All 4 assets must be reported in the success message.
    assert "4 assets" in msg


def test_current_pointer_check_fails_when_missing(tmp_path):
    """No CURRENT files at all → fails for all 4 assets. The runbook
    never deletes CURRENT, so this is a 'something is very wrong' signal."""
    ok, msg = _verifier.check_current_pointer_is_v1(tmp_path)
    assert not ok
    assert "CURRENT missing" in msg


def test_current_pointer_check_detects_non_eth_drift(tmp_path):
    """Pin: a non-ETH CURRENT drifting (e.g., accidental scp of a
    Mac-trained BTC bundle) is detected by the check. This is the
    cross-asset rollback contract."""
    _write_v1_current_for_all_assets(tmp_path)
    btc_current = tmp_path / "models" / "cal_mlp_BTC" / "CURRENT"
    btc_current.write_text("2026-05-04T99:99:99.999999Z-deadbeef\n")
    ok, msg = _verifier.check_current_pointer_is_v1(tmp_path)
    assert not ok
    assert "BTC" in msg


def test_no_stale_shell_overrides_passes_when_clean(monkeypatch):
    """All 5 vars unset → check passes."""
    project_root = PROJECT_ROOT  # the actual project root works fine
    ok, msg = _verifier.check_no_stale_shell_overrides(project_root)
    assert ok, msg


def test_no_stale_shell_overrides_passes_when_eth_matches_pin(monkeypatch):
    """ETH set to exactly V2_ETH_BUNDLE_REL → no drift, passes."""
    monkeypatch.setenv("CALMLP_BUNDLE_DIR_ETH", _verifier.V2_ETH_BUNDLE_REL)
    ok, msg = _verifier.check_no_stale_shell_overrides(PROJECT_ROOT)
    assert ok, msg


def test_no_stale_shell_overrides_fails_when_eth_drifted(monkeypatch):
    """ETH set to a different path → fails with explicit drift message."""
    monkeypatch.setenv("CALMLP_BUNDLE_DIR_ETH", "models/cal_mlp_ETH/some-other-id")
    ok, msg = _verifier.check_no_stale_shell_overrides(PROJECT_ROOT)
    assert not ok
    assert "differs from pin" in msg


def test_no_stale_shell_overrides_advisory_on_other_assets(monkeypatch):
    """Stray BTC/SOL/XRP shell exports → check PASSES (the bot reads
    systemd .env, not interactive shell), but advisory message is
    emitted so the operator can clean up at their leisure. Pin the
    user-facing message contract so a future refactor doesn't silently
    change what the operator sees."""
    monkeypatch.setenv("CALMLP_BUNDLE_DIR_BTC", "/some/path")
    ok, msg = _verifier.check_no_stale_shell_overrides(PROJECT_ROOT)
    assert ok, msg
    assert msg.startswith("no Phase-1b-blocking shell overrides; advisory:")
    assert "won't affect systemd-loaded bot" in msg
    assert "CALMLP_BUNDLE_DIR_BTC" in msg


def test_no_stale_shell_overrides_fails_on_global(monkeypatch):
    """Global CALMLP_BUNDLE_DIR set → fails (would resolve for any
    asset that doesn't have a per-asset override)."""
    monkeypatch.setenv("CALMLP_BUNDLE_DIR", "/some/global/path")
    ok, msg = _verifier.check_no_stale_shell_overrides(PROJECT_ROOT)
    assert not ok
    assert "global override" in msg


def test_load_check_isolates_all_calmlp_env_vars(tmp_path, monkeypatch):
    """Pin: check_load_with_override snapshots+clears ALL FIVE
    CALMLP_BUNDLE_DIR* env vars on entry, restores them on exit.
    Without this, an operator with `CALMLP_BUNDLE_DIR_BTC` exported
    in their shell would see a misleading 'resolver leaked override
    to BTC' failure."""
    # Operator has BTC override exported.
    monkeypatch.setenv("CALMLP_BUNDLE_DIR_BTC", "/some/operator/path")
    # We can't actually run check_load_with_override against tmp_path
    # without bundles, but we can verify the snapshot/restore contract
    # by stubbing the bundle dir minimally and asserting the env var
    # comes back after the function (regardless of pass/fail).
    prior_btc = os.environ.get("CALMLP_BUNDLE_DIR_BTC")
    # Run against tmp_path which has no bundle — function MUST return
    # (False, ...) AND MUST still restore env vars (LOW-6 R4 fix).
    ok, msg = _verifier.check_load_with_override(tmp_path)
    assert not ok, f"expected failure on missing bundle, got success: {msg}"
    after_btc = os.environ.get("CALMLP_BUNDLE_DIR_BTC")
    assert after_btc == prior_btc, (
        f"BTC env var not restored: prior={prior_btc!r} after={after_btc!r}"
    )
    # ETH should not leak as the V2 path; should be restored to whatever
    # the autouse fixture cleared it to (= unset).
    assert os.environ.get("CALMLP_BUNDLE_DIR_ETH") is None


def test_project_root_resolves_through_symlink(tmp_path):
    """Pin: `Path(__file__).resolve()` correctly handles symlinks; the
    verifier discovers the same project_root regardless of whether it
    was invoked via a direct path or a symlink. Important if the
    operator ever places the verifier behind a symlinked alias on VPS."""
    (tmp_path / "pyproject.toml").write_text('[tool]\nname = "fake"\n')
    (tmp_path / "market_config.py").write_text(
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class C:\n"
        "    market_blend_w: float = 0.40\n"
        "MARKET_CONFIGS = {'15m': C()}\n"
    )
    real_dir = tmp_path / "scripts" / "cal_mlp"
    real_dir.mkdir(parents=True)
    real_script = real_dir / "phase1b_verify_eth_v2.py"
    real_script.write_text(VERIFIER_PATH.read_text())
    sym_dir = tmp_path / "alias"
    sym_dir.mkdir()
    sym_link = sym_dir / "verify.py"
    sym_link.symlink_to(real_script)

    env = os.environ.copy()
    env.pop("KALSHI_PROJECT_ROOT", None)
    result = subprocess.run(
        [sys.executable, str(sym_link), "--skip-load", "--skip-current-pointer"],
        capture_output=True, text=True, cwd="/", env=env, timeout=30,
    )
    combined = result.stdout + result.stderr
    assert str(tmp_path) in combined, (
        f"verifier didn't resolve symlink to real script's parent; "
        f"output:\n{combined}"
    )
