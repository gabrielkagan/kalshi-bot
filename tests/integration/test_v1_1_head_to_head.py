"""Bit D (86ba0jn50) of HYPE/DOGE cal_mlp v1.1 retrain umbrella 86ba0jmyq.

Pins the contract for `scripts/audit/v1_1_hype_doge_head_to_head.py`:

- `compute_brier_headtohead(asset)` returns a structured dict with all
  required keys (asset, n_test, baseline_brier, v1_1_brier, delta_brier,
  bootstrap_ci_95, baseline_w, sub_buckets, recommendation, rationale).
- Bootstrap CI is a 2-tuple of floats; sub-buckets partition the test set.
- `recommendation` ∈ {'SHIP', 'HOLD'}; SHIP requires delta < 0 AND CI
  upper < 0 AND no sub-bucket regression > +0.02.
- Defensive Mac-only guards (script refuses VPS paths for inputs).
- AST pin: script body does NOT write to any `CURRENT` pointer file
  (production touch forbidden — pointer flip is a separate operator step).

Plan doc: `kb/decisions/v1-1-D-head-to-head-plan.md`.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "audit" / "v1_1_hype_doge_head_to_head.py"

sys.path.insert(0, str(REPO_ROOT))


# ── Helpers ──────────────────────────────────────────────────────────


def _synthetic_test_df(n: int = 200, asset: str = "HYPE", seed: int = 42) -> pd.DataFrame:
    """Synthesize a test-fold dataframe matching `extract_data.build_feature_frame` shape."""
    rng = np.random.default_rng(seed)
    market_price = rng.integers(76, 99, size=n)
    raw_prob = rng.beta(8, 2, size=n)
    outcome = (rng.random(size=n) < raw_prob).astype(int)
    return pd.DataFrame({
        "asset": asset,
        "ticker": [f"KX{asset}15M-FAKE-{i}" for i in range(n)],
        "raw_prob": raw_prob,
        "calibrated_prob": raw_prob,
        "market_price": market_price,
        "market_result": np.where(outcome == 1, "yes", "no"),
        "side": "yes",
        "outcome": outcome,
        "split": "test",
        "price_tier": np.digitize(market_price, [80, 90, 96], right=True).astype(int),
        "seconds_to_close": rng.integers(60, 900, size=n),
        "stc_bucket": rng.integers(0, 4, size=n),
        "spot_distance_to_strike_sigma": rng.normal(0.5, 0.3, size=n),
        "abs_spot_distance_to_strike_sigma": rng.normal(0.5, 0.3, size=n).clip(0, None),
        "time_decayed_proximity": rng.normal(0.3, 0.2, size=n),
        "prob_breakeven_gap": rng.normal(0.05, 0.05, size=n),
        "hour_sin": rng.uniform(-1, 1, size=n),
        "hour_cos": rng.uniform(-1, 1, size=n),
        "vol_regime": "normal",
        "vol_regime_int": 0,
    })


# ── Existence + AST pins ─────────────────────────────────────────────


def test_script_exists():
    assert SCRIPT_PATH.is_file(), (
        f"Bit D script not found at {SCRIPT_PATH}. Umbrella 86ba0jmyq "
        "Bit D must ship scripts/audit/v1_1_hype_doge_head_to_head.py."
    )


def test_script_no_current_pointer_write():
    """AST guard: script must NOT write to any CURRENT file under
    models/cal_mlp_*/. The CURRENT-pointer flip is a separate operator
    action gated on this Bit's finding."""
    src = SCRIPT_PATH.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            # str.write or open() with 'w'/'a' + CURRENT in path
            if isinstance(f, ast.Attribute) and f.attr in ("write_text", "write"):
                # check arg for "CURRENT" string literal
                src_text = ast.unparse(node) if hasattr(ast, "unparse") else ""
                assert "CURRENT" not in src_text, (
                    f"AST guard: script writes to a CURRENT pointer at "
                    f"line {node.lineno} ({src_text!r}). Bit D's commit "
                    f"MUST NOT flip CURRENT — that's a separate operator "
                    f"action gated on this Bit's finding."
                )


# ── API surface ──────────────────────────────────────────────────────


def test_module_exposes_required_api():
    """Script must expose `compute_brier_headtohead(asset)` + `main()` CLI."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    import v1_1_hype_doge_head_to_head as m
    assert callable(getattr(m, "compute_brier_headtohead", None))
    assert callable(getattr(m, "main", None))


# ── Output schema ────────────────────────────────────────────────────


def test_compute_brier_headtohead_returns_required_keys(monkeypatch, tmp_path):
    """Output dict has the 10 required keys + the right types."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    import v1_1_hype_doge_head_to_head as m

    # Stub loaders.
    df = _synthetic_test_df()
    monkeypatch.setattr(m, "_load_test_fold", lambda asset, project_root=None: df)
    fake_predictor = MagicMock()
    # Return cal_prob slightly LOWER than raw (overconfidence shrinkage).
    fake_predictor.predict.side_effect = lambda raw_prob, **_: (
        max(0.01, min(0.99, raw_prob - 0.05)), 0.05, 0.5, 1.0
    )
    monkeypatch.setattr(m, "_load_predictor", lambda asset, project_root=None: fake_predictor)

    out = m.compute_brier_headtohead("HYPE")
    required = {
        "asset", "n_test", "baseline_brier", "v1_1_brier",
        "delta_brier", "bootstrap_ci_95", "baseline_w",
        "sub_buckets", "recommendation", "rationale",
    }
    missing = required - set(out.keys())
    assert not missing, f"missing keys: {missing}"
    assert out["asset"] == "HYPE"
    assert isinstance(out["n_test"], int) and out["n_test"] > 0
    assert isinstance(out["baseline_brier"], float)
    assert isinstance(out["v1_1_brier"], float)
    assert isinstance(out["delta_brier"], float)
    assert out["baseline_w"] == 0.80  # HYPE production blend


def test_baseline_w_doge():
    """DOGE production blend W=0.60."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    import v1_1_hype_doge_head_to_head as m
    df = _synthetic_test_df(asset="DOGE")
    import types
    fake_predictor = MagicMock()
    fake_predictor.predict.side_effect = lambda raw_prob, **_: (
        max(0.01, min(0.99, raw_prob - 0.05)), 0.05, 0.5, 1.0
    )
    # Use monkeypatch via the module-level attribute (simpler than fixture):
    m._load_test_fold = lambda asset, project_root=None: df
    m._load_predictor = lambda asset, project_root=None: fake_predictor
    out = m.compute_brier_headtohead("DOGE")
    assert out["baseline_w"] == 0.60


def test_bootstrap_ci_shape_and_range(monkeypatch):
    """Bootstrap CI is a 2-tuple of floats with lower < upper."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    import v1_1_hype_doge_head_to_head as m

    df = _synthetic_test_df()
    monkeypatch.setattr(m, "_load_test_fold", lambda asset, project_root=None: df)
    fake_predictor = MagicMock()
    fake_predictor.predict.side_effect = lambda raw_prob, **_: (
        raw_prob, 0.05, 0.5, 1.0
    )
    monkeypatch.setattr(m, "_load_predictor", lambda asset, project_root=None: fake_predictor)

    out = m.compute_brier_headtohead("HYPE", bootstrap_b=200)
    ci = out["bootstrap_ci_95"]
    assert isinstance(ci, tuple) and len(ci) == 2
    lo, hi = ci
    assert isinstance(lo, float) and isinstance(hi, float)
    assert lo <= hi


def test_sub_buckets_partition_test_set(monkeypatch):
    """Sum of per-bucket n equals total n_test."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    import v1_1_hype_doge_head_to_head as m

    df = _synthetic_test_df()
    monkeypatch.setattr(m, "_load_test_fold", lambda asset, project_root=None: df)
    fake_predictor = MagicMock()
    fake_predictor.predict.side_effect = lambda raw_prob, **_: (
        raw_prob, 0.05, 0.5, 1.0
    )
    monkeypatch.setattr(m, "_load_predictor", lambda asset, project_root=None: fake_predictor)

    out = m.compute_brier_headtohead("HYPE")
    sub_n = sum(b["n"] for b in out["sub_buckets"].values())
    assert sub_n == out["n_test"], (
        f"sub-bucket sum {sub_n} != n_test {out['n_test']}"
    )


# ── Recommendation logic ─────────────────────────────────────────────


def test_recommendation_is_ship_or_hold(monkeypatch):
    """`recommendation` is the enum {'SHIP', 'HOLD'}."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    import v1_1_hype_doge_head_to_head as m

    df = _synthetic_test_df()
    monkeypatch.setattr(m, "_load_test_fold", lambda asset, project_root=None: df)
    fake_predictor = MagicMock()
    fake_predictor.predict.side_effect = lambda raw_prob, **_: (
        raw_prob, 0.05, 0.5, 1.0
    )
    monkeypatch.setattr(m, "_load_predictor", lambda asset, project_root=None: fake_predictor)

    out = m.compute_brier_headtohead("HYPE")
    assert out["recommendation"] in ("SHIP", "HOLD")


def test_recommendation_hold_when_v1_1_worse(monkeypatch):
    """If v1.1 cal_prob is consistently worse than baseline across the
    synthetic fixture, recommend HOLD. (Synthetic-fixture context: the
    fake predictor returns 0.5 on all rows; this is NOT a claim about
    real HYPE/DOGE comparison — see kb/findings/v1-1-hype-doge-head-to-head-may19.md
    L99 SUB-RETRACTED PARAPHRASES for the live-data version.)"""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    import v1_1_hype_doge_head_to_head as m

    df = _synthetic_test_df()
    monkeypatch.setattr(m, "_load_test_fold", lambda asset, project_root=None: df)
    fake_predictor = MagicMock()
    # Return EXTREME wrong predictions (always 0.5 on high-conviction rows).
    fake_predictor.predict.side_effect = lambda raw_prob, **_: (0.5, 0.05, 0.3, 0.7)
    monkeypatch.setattr(m, "_load_predictor", lambda asset, project_root=None: fake_predictor)

    out = m.compute_brier_headtohead("HYPE")
    # Synthetic data is high-conviction (mean raw 0.8); always-0.5 prediction
    # has higher Brier than the production blend.
    assert out["delta_brier"] > 0, (
        f"expected v1.1 worse, got delta_brier={out['delta_brier']}"
    )
    assert out["recommendation"] == "HOLD"


# ── Defensive guard ──────────────────────────────────────────────────


def test_align_w_overrides_predictor_market_blend_w(monkeypatch):
    """Bit D followup F1: --align-w flag overrides predictor.market_blend_w
    to MARKET_BLEND_W_BY_ASSET[asset] (HYPE=0.80, DOGE=0.60) so v1.1's
    internal blend matches production's asset-specific blend (instead of
    the bundle's hardcoded 0.40)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    import v1_1_hype_doge_head_to_head as m

    df = _synthetic_test_df(asset="HYPE")
    monkeypatch.setattr(m, "_load_test_fold", lambda asset, project_root=None: df)
    fake_predictor = MagicMock()
    fake_predictor.market_blend_w = 0.40  # bundle default
    fake_predictor.predict.side_effect = lambda raw_prob, **_: (
        raw_prob, 0.05, 0.5, 1.0
    )
    monkeypatch.setattr(m, "_load_predictor", lambda asset, project_root=None: fake_predictor)

    # With align_w=True, market_blend_w should be set to 0.80 (HYPE).
    m.compute_brier_headtohead("HYPE", align_w=True)
    assert fake_predictor.market_blend_w == 0.80, (
        f"align_w=True should set predictor.market_blend_w to 0.80 (HYPE production); "
        f"got {fake_predictor.market_blend_w}"
    )


def test_align_w_default_false_preserves_bundle_w(monkeypatch):
    """Default behavior (align_w omitted) preserves bundle's market_blend_w
    so original Bit D verdict is reproducible."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    import v1_1_hype_doge_head_to_head as m

    df = _synthetic_test_df(asset="HYPE")
    monkeypatch.setattr(m, "_load_test_fold", lambda asset, project_root=None: df)
    fake_predictor = MagicMock()
    fake_predictor.market_blend_w = 0.40
    fake_predictor.predict.side_effect = lambda raw_prob, **_: (raw_prob, 0.05, 0.5, 1.0)
    monkeypatch.setattr(m, "_load_predictor", lambda asset, project_root=None: fake_predictor)

    m.compute_brier_headtohead("HYPE")  # default
    assert fake_predictor.market_blend_w == 0.40, (
        f"align_w default=False should preserve bundle market_blend_w=0.40; "
        f"got {fake_predictor.market_blend_w}"
    )


def test_refuses_vps_path():
    """`_refuse_vps_path` rejects /home/botuser/ paths (Mac-only invariant)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    import v1_1_hype_doge_head_to_head as m
    with pytest.raises((ValueError, SystemExit)):
        m._refuse_vps_path("/home/botuser/kalshi-bot-repo/state.db")
    # Mac paths pass:
    m._refuse_vps_path("/tmp/test.db")  # should not raise
    m._refuse_vps_path("/Users/gabrielkagan/Documents/kalshi-bot/state.db")
