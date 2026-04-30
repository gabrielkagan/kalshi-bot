"""TDD for P5 (R-p7-deploy-r11): winsorize spot_distance_to_strike_sigma
at extract time.

Background: at terminal STC (seconds_to_close → 0), the sigma denominator
collapses, producing pseudo-infinite z-scores up to ±3,337 across all 4
assets in production data. Per-asset outliers >|20σ|:

    BTC: 22 / 4155 (0.5%)
    ETH: 27 / 3751 (0.7%)
    SOL: 62 / 4723 (1.3%)
    XRP: 80 / 4616 (1.7%)

Plus 7-18 per asset with |sigma| > 100. After z-scoring across the column,
these outliers inflate std by 100×+, collapsing all real signal to ~0.

Fix: winsorize at ±SIGMA_WINSOR_ABS_CAP = 25 in extract_data.build_feature_frame
BEFORE the abs() and time_decayed_proximity derivations.

This changes cfg_fp; v2/v3 bundles with the winsorize will be a new fingerprint.
"""
import sys
from pathlib import Path

import pytest


CAL_MLP_DIR = Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp'
if str(CAL_MLP_DIR) not in sys.path:
    sys.path.insert(0, str(CAL_MLP_DIR))


def _require_extract_deps():
    """extract_data.py needs pandas/numpy/pyarrow. Skip when unavailable."""
    pytest.importorskip("numpy")
    pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")


def _make_min_row(**overrides):
    """Construct a minimal row dict that build_feature_frame accepts.
    Tests inject custom sigma values via overrides."""
    base = {
        'ticker': 'KX-T',
        'evaluation_time': '2026-04-29T12:00:00Z',
        'asset': 'BTC',
        'side': 'yes',
        'strategy': 'tm',
        'product_type': '15m',
        'market_price': 96,
        'seconds_to_close': 60.0,
        'spot_distance_to_strike_sigma': 0.5,
        'prob_breakeven_gap': -0.04,
        'raw_prob': 0.92,
        'calibrated_prob': 0.92,
        'vol_regime': 'normal',
        'volatility': 0.001,
        'market_result': 'yes',
        'settled_time': '2026-04-29T12:15:00Z',
        'available_balance_cents': 100000,
        'hour_of_day_utc': 12,
    }
    base.update(overrides)
    return base


def test_sigma_winsor_abs_cap_constant_exists():
    """features.py must expose SIGMA_WINSOR_ABS_CAP as the magic number;
    no hidden literal in extract_data.py. cfg_fp must capture it."""
    import features
    assert hasattr(features, 'SIGMA_WINSOR_ABS_CAP')
    assert features.SIGMA_WINSOR_ABS_CAP > 0
    # 25 is the chosen value (above empirical max benign ~19, below the
    # 30+ outlier tail). Don't loosen without redoing the analysis.
    assert features.SIGMA_WINSOR_ABS_CAP == 25.0


def test_cfg_fp_includes_sigma_winsor_cap():
    """cfg_fp must change when SIGMA_WINSOR_ABS_CAP changes — otherwise
    bundles with different winsor values would compare as identical at
    Phase 6 A/B (silent train-distribution mismatch)."""
    import features
    import importlib
    fp_25 = features.compute_cfg_fp(include_sub_floor=True)
    # Monkey-patch the cap, reload, get the new fp.
    original = features.SIGMA_WINSOR_ABS_CAP
    try:
        features.SIGMA_WINSOR_ABS_CAP = 50.0
        fp_50 = features.compute_cfg_fp(include_sub_floor=True)
    finally:
        features.SIGMA_WINSOR_ABS_CAP = original
    assert fp_25 != fp_50, (
        "Changing SIGMA_WINSOR_ABS_CAP must change cfg_fp. Otherwise "
        "Phase 6 A/B can't distinguish bundles trained on different "
        "winsor caps — silent train-distribution mismatch."
    )


def test_extract_clips_positive_sigma_above_cap():
    """A sigma value of 100 (well above 25 cap) must be clipped to 25
    in the resulting frame."""
    _require_extract_deps()
    import extract_data
    import pandas as pd
    rows = [_make_min_row(spot_distance_to_strike_sigma=100.0)]
    df = extract_data.build_feature_frame(rows)
    assert df['spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(25.0)


def test_extract_clips_negative_sigma_below_cap():
    """A sigma value of -3337 must be clipped to -25."""
    _require_extract_deps()
    import extract_data
    import pandas as pd
    rows = [_make_min_row(spot_distance_to_strike_sigma=-3337.0)]
    df = extract_data.build_feature_frame(rows)
    assert df['spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(-25.0)


def test_extract_preserves_in_range_sigma():
    """Values within ±25 must pass through unchanged."""
    _require_extract_deps()
    import extract_data
    import pandas as pd
    for val in (-24.99, -10.0, -0.5, 0.0, 1.5, 19.0, 24.9):
        rows = [_make_min_row(spot_distance_to_strike_sigma=val)]
        df = extract_data.build_feature_frame(rows)
        assert df['spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(val), (
            f"sigma={val} should pass through; got {df['spot_distance_to_strike_sigma'].iloc[0]}"
        )


def test_winsorize_propagates_to_abs_and_tdp():
    """abs_spot_distance and time_decayed_proximity are derived from the
    POST-winsorize sigma. Without this, the derived features still see
    the catastrophic outlier values.
    """
    _require_extract_deps()
    import extract_data
    import pandas as pd
    # sigma=-100 should become -25 after winsorize.
    # abs = |−25| = 25.
    # time_decayed_proximity = -25 × (1 - stc/900) = -25 × (1 - 60/900) = -23.33
    rows = [_make_min_row(
        spot_distance_to_strike_sigma=-100.0,
        seconds_to_close=60.0,
    )]
    df = extract_data.build_feature_frame(rows)
    assert df['spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(-25.0)
    assert df['abs_spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(25.0), (
        "abs_spot_distance must be derived from CLIPPED sigma, not raw."
    )
    expected_tdp = -25.0 * (1.0 - 60.0 / 900.0)
    assert df['time_decayed_proximity'].iloc[0] == pytest.approx(expected_tdp, rel=1e-3), (
        "time_decayed_proximity must be derived from CLIPPED sigma, not raw. "
        f"Expected {expected_tdp}, got {df['time_decayed_proximity'].iloc[0]}"
    )


def test_apply_sigma_winsor_dtype_robustness():
    """R-p7-deploy-r11 R4 (MED): the helper must handle Python float,
    numpy.float32, and numpy.float64 consistently — and produce
    output equivalent to pd.Series.clip across the full edge-case set
    (NaN, +inf, -inf, large finite, small).

    Without this lock, a future dtype refactor in either the train or
    serve path could silently diverge on edge values.
    """
    pytest.importorskip("numpy")
    import numpy as np
    import features

    # Python float
    assert features.apply_sigma_winsor(float('inf')) == 25.0
    assert features.apply_sigma_winsor(float('-inf')) == -25.0
    nan_out = features.apply_sigma_winsor(float('nan'))
    assert nan_out != nan_out  # NaN-passthrough (NaN != NaN)

    # numpy.float32
    assert features.apply_sigma_winsor(np.float32(100.0)) == 25.0
    assert features.apply_sigma_winsor(np.float32(-100.0)) == -25.0
    assert features.apply_sigma_winsor(np.float32('inf')) == 25.0
    assert features.apply_sigma_winsor(np.float32('-inf')) == -25.0
    nan32_out = features.apply_sigma_winsor(np.float32('nan'))
    assert nan32_out != nan32_out  # NaN-passthrough

    # numpy.float64
    assert features.apply_sigma_winsor(np.float64(100.0)) == 25.0
    assert features.apply_sigma_winsor(np.float64(-3337.0)) == -25.0


def test_apply_sigma_winsor_consistent_with_pandas_clip():
    """R-p7-deploy-r11 R4 (MED): the helper must produce identical
    output to `pd.Series.clip(-cap, +cap)` for every input the train
    path could see. Otherwise train (pandas) and serve (helper) diverge
    on edge cases.
    """
    pytest.importorskip("numpy")
    pytest.importorskip("pandas")
    import numpy as np
    import pandas as pd
    import features

    cap = features.SIGMA_WINSOR_ABS_CAP
    test_inputs = [0.0, 1.5, -10.0, 25.0, -25.0, 25.0001, 100.0, -3337.0,
                   float('inf'), float('-inf')]
    series = pd.Series(test_inputs, dtype=np.float32)
    pandas_out = series.clip(lower=-cap, upper=cap).tolist()
    helper_out = [features.apply_sigma_winsor(np.float32(x)) for x in test_inputs]
    for i, (p, h) in enumerate(zip(pandas_out, helper_out)):
        assert p == pytest.approx(h, rel=1e-6, abs=1e-6), (
            f"input[{i}]={test_inputs[i]}: pandas_clip={p} vs helper={h}"
        )


def test_features_module_exposes_apply_sigma_winsor_helper():
    """R-p7-deploy-r11 R3 (C1): centralize winsorize in features.py so all
    three call sites (extract, post_hoc_processor, should_block_tm96)
    apply IDENTICAL clipping. A single helper means future cap changes
    propagate without missing a site."""
    import features
    assert hasattr(features, 'apply_sigma_winsor'), (
        "features.py must expose apply_sigma_winsor(sd) -> sd_clipped so "
        "all train+serve sites use the same clipping logic. Otherwise the "
        "round-2 review's CRITICAL skew (model trained on ±25, served ±3337) "
        "stays open."
    )
    # NULL-safe
    assert features.apply_sigma_winsor(None) is None
    # In-range pass-through
    assert features.apply_sigma_winsor(10.0) == 10.0
    assert features.apply_sigma_winsor(-10.0) == -10.0
    # Boundary inclusive (R3 L2 nit)
    assert features.apply_sigma_winsor(25.0) == 25.0
    assert features.apply_sigma_winsor(-25.0) == -25.0
    # Out-of-range clipped
    assert features.apply_sigma_winsor(100.0) == 25.0
    assert features.apply_sigma_winsor(-3337.0) == -25.0
    assert features.apply_sigma_winsor(25.000001) == 25.0


def test_should_block_tm96_winsorizes_sigma():
    """R-p7-deploy-r11 R3 (C1): the synchronous gate must clip its
    inline-computed sigma to ±SIGMA_WINSOR_ABS_CAP before populating
    row_features. Otherwise: model trained on clipped sigma + served
    raw sigma → train/serve skew."""
    import sys
    from pathlib import Path
    cal_mlp = Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp'
    if str(cal_mlp) not in sys.path:
        sys.path.insert(0, str(cal_mlp))
    import integration

    class StubPredictor:
        def __init__(self):
            self.train_id = 'test'
            self.asset = 'BTC'
            self.predict_calls = []
        def predict(self, raw_prob, ticker, side, entry_price_cents, row_features):
            self.predict_calls.append({'row_features': dict(row_features)})
            return (0.95, 0.01, 0.90, 1.0)

    pred = StubPredictor()
    # Set up inputs that produce huge raw sigma (low blended_rv + low STC).
    # buf_pct = (spot - threshold) / threshold * 100 = (2400-2399.99)/2399.99 * 100
    #         = 4.17e-4
    # sigma_denom = blended_rv * sqrt(stc/5) * 100 = 1e-9 * sqrt(0.001/5) * 100
    #             = 4.47e-13
    # spot_dist_sigma = buf_pct / sigma_denom = 9.3e8  (massive)
    integration.should_block_tm96(
        predictor=pred,
        raw_prob=0.95, calibrated_prob=0.95, ticker='X',
        market_price=96, seconds_to_close=0.001,
        spot=2400.0, threshold=2399.99,
        blended_rv=1e-9, vol_regime='normal',
    )
    rf = pred.predict_calls[0]['row_features']
    sigma = rf.get('spot_distance_to_strike_sigma')
    assert sigma is not None
    # Must be clipped to ≤ SIGMA_WINSOR_ABS_CAP (25.0).
    assert abs(sigma) <= 25.0 + 1e-6, (
        f"should_block_tm96 row_features spot_distance_to_strike_sigma={sigma} "
        f"must be clipped to ±25; train/serve skew otherwise. R3 CRITICAL."
    )
    # And the derived features must reflect the clipped value.
    abs_dist = rf.get('abs_spot_distance_to_strike_sigma')
    assert abs_dist is not None and abs(abs_dist) <= 25.0 + 1e-6
    tdp = rf.get('time_decayed_proximity')
    assert tdp is not None
    # tdp = clipped_sigma * (1 - stc/900); with stc=0.001 → factor ≈ 1.0
    assert abs(tdp) <= 25.0 + 1e-6


def test_post_hoc_processor_winsorizes_sigma_when_loading_db_value():
    """R-p7-deploy-r11 R3 (C1): the DB stores RAW sigma (compute_derived_features
    in bot.py writes unclipped values). The post-hoc processor must clip
    on read before passing to predict() so the model never sees an
    out-of-distribution sigma at serve time."""
    import ast
    from pathlib import Path
    src_path = Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp' / 'post_hoc_processor.py'
    src = src_path.read_text()
    # The processor must call apply_sigma_winsor (or equivalent clip) on the
    # spot_dist_sigma value before it lands in row_features.
    assert 'apply_sigma_winsor' in src, (
        "post_hoc_processor.py must call features.apply_sigma_winsor on the "
        "DB-loaded spot_distance_to_strike_sigma. Otherwise raw sigma values "
        "(±3,337 in prod) reach the predictor — the model has only ever seen "
        "±25 during training. R3 CRITICAL train/serve skew."
    )


def test_extract_uses_features_module_attr_not_import_copy():
    """R-p7-deploy-r11 R3-H1: extract_data.py must read SIGMA_WINSOR_ABS_CAP
    via `features.SIGMA_WINSOR_ABS_CAP` at call time, NOT via a top-level
    `from features import SIGMA_WINSOR_ABS_CAP` (which captures the value
    at import time and silently diverges from cfg_fp under monkey-patch).
    """
    extract_path = (
        Path(__file__).resolve().parents[1] / 'scripts' / 'cal_mlp' / 'extract_data.py'
    )
    src = extract_path.read_text()
    # The from-import line should NOT include SIGMA_WINSOR_ABS_CAP (or, if it
    # does, the call site must still use features.* attr access — but we
    # forbid the import as a stricter contract).
    import re
    from_import = re.search(
        r'from features import\s*\(([\s\S]*?)\)',
        src,
    )
    if from_import:
        names = from_import.group(1)
        assert 'SIGMA_WINSOR_ABS_CAP' not in names, (
            "extract_data.py must NOT `from features import SIGMA_WINSOR_ABS_CAP` "
            "(import-time binding diverges from features.SIGMA_WINSOR_ABS_CAP "
            "after monkey-patches; cfg_fp + extract drift apart silently). "
            "Use `import features` and access `features.SIGMA_WINSOR_ABS_CAP`."
        )
    # Bare `import features` must be present.
    assert re.search(r'^import features\b', src, re.MULTILINE), (
        "extract_data.py must `import features` so SIGMA_WINSOR_ABS_CAP is "
        "looked up via attr access at call time."
    )
    # Must use features.SIGMA_WINSOR_ABS_CAP somewhere.
    assert 'features.SIGMA_WINSOR_ABS_CAP' in src, (
        "extract_data.py must read the cap via features.SIGMA_WINSOR_ABS_CAP "
        "(attr access at call time). Without this, monkey-patching the "
        "constant in tests doesn't change the actual extract behavior."
    )


def test_extract_winsorize_respects_runtime_cap_change():
    """R-p7-deploy-r11 R3-H1: monkey-patching features.SIGMA_WINSOR_ABS_CAP
    must change the actual extract behavior (proves attr-time lookup, not
    import-time copy)."""
    _require_extract_deps()
    import features
    import extract_data
    import pandas as pd
    rows = [_make_min_row(spot_distance_to_strike_sigma=50.0)]
    original = features.SIGMA_WINSOR_ABS_CAP
    try:
        features.SIGMA_WINSOR_ABS_CAP = 100.0
        df = extract_data.build_feature_frame(rows)
        # With cap=100, sigma=50 stays 50.
        assert df['spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(50.0), (
            f"extract_data should observe runtime-mutated cap=100 and "
            f"NOT clip 50 → 25; got {df['spot_distance_to_strike_sigma'].iloc[0]}"
        )
    finally:
        features.SIGMA_WINSOR_ABS_CAP = original


def test_winsorize_handles_null_sigma():
    """Rows with NULL sigma must not crash — they're imputed downstream
    via the missing-indicator pathway (or dropped if too many NULLs).
    Winsorize must be NULL-safe."""
    _require_extract_deps()
    import extract_data
    import pandas as pd
    rows = [_make_min_row(spot_distance_to_strike_sigma=None)]
    # Should not raise.
    df = extract_data.build_feature_frame(rows)
    # NULL stays NULL post-winsorize.
    assert pd.isna(df['spot_distance_to_strike_sigma'].iloc[0])
