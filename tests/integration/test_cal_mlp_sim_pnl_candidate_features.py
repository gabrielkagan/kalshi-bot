"""sim_pnl.py — `_prepare_candidate_features` helper tests.

Pre-fix bug: `run_sim_pnl` (sim_pnl.py:446) crashed with `KeyError:
'market_price'` inside `apply_norm` (normalize.py:173). Two compounded
issues:

  1. Premature rename — the SQL-pulled candidate_df was renamed
     `market_price → entry_price_cents` BEFORE `apply_norm`, but
     `apply_norm` iterates `CONT_FEATURE_COLS = ['market_price', ...]`
     (features.py:113-122) and reads `out['market_price']`.

  2. Missing canonical features — even with the rename deferred, the
     SQL SELECT didn't include `spot_distance_to_strike_sigma` or
     `prob_breakeven_gap`, and didn't compute the 4 derived features
     (`abs_spot_distance_to_strike_sigma`, `time_decayed_proximity`,
     `hour_sin`, `hour_cos`). The canonical derivation lives at
     `extract_data.py:396-406`.

`_prepare_candidate_features(candidate_df)` is the new helper that:
  - Duplicates `market_price → entry_price_cents` and
    `yes_spread_cents → spread_cents` (preserves source columns so
    `apply_norm` can find them; mirrors the precedent set by
    yesterday's `compute_method_output` testability extraction).
  - Computes `price_tier`, `stc_bucket`, `vol_regime_int` (existing
    logic, just relocated into the helper).
  - Winsorizes `spot_distance_to_strike_sigma` to ±SIGMA_WINSOR_ABS_CAP
    BEFORE deriving `abs_spot_distance_to_strike_sigma` and
    `time_decayed_proximity` — train/serve invariant per
    `CLAUDE.md` "cal_mlp feature transforms ship in ONE commit".
  - Computes `hour_sin`/`hour_cos` from `hour_of_day_utc`.

This file locks both the runtime contract and the AST shape.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "cal_mlp"))


# ── Functional regression ──────────────────────────────────────────────


class TestPrepareCandidateFeaturesContract:
    """The helper's runtime contract: input rows from the SQL SELECT
    (plus the market_result filter) → output rows that satisfy
    apply_norm's column requirements + downstream replay's column
    requirements."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("pandas")
        pytest.importorskip("numpy")
        pytest.importorskip("torch")  # sim_pnl.py imports torch at module load

    def _make_post_sql_df(self):
        """Build a DataFrame matching the columns sim_pnl's SQL pull
        produces (post-fix, including the new spot_distance/prob_breakeven
        columns) AFTER the market_result filter at line ~398."""
        import pandas as pd

        return pd.DataFrame({
            'ticker': ['KXBTC15M-1', 'KXETH15M-2', 'KXSOL15M-3'],
            'evaluation_time': [
                '2026-05-04T16:00:00Z',
                '2026-05-04T16:05:00Z',
                '2026-05-04T16:10:00Z',
            ],
            'asset': ['BTC', 'ETH', 'SOL'],
            'side': ['yes', 'yes', 'no'],
            'strategy': ['decided_t1', 'decided_t1', 'TAKER_NOW'],
            'market_price': [85, 92, 96],
            'seconds_to_close': [600.0, 300.0, 120.0],
            'vol_regime': ['normal', 'elevated', 'normal'],
            'z_score': [1.2, 1.8, 0.5],
            'yes_spread_cents': [2, 1, 3],
            'calibrated_prob': [0.85, 0.91, 0.97],
            'calibration_method': ['cal_v1', 'cal_v1', 'cal_v1'],
            'raw_prob': [0.83, 0.89, 0.96],
            'breakeven_wr': [0.85, 0.92, 0.96],
            'fee_adjusted_edge': [0.02, 0.04, 0.01],
            'kelly_f': [0.05, 0.08, 0.02],
            'is_weekend': [0, 0, 1],
            'hour_of_day_utc': [16, 16, 16],
            'market_result': ['yes', 'yes', 'no'],
            'available_balance_cents': [50000, 50000, 50000],
            # NEW columns added to SELECT:
            'spot_distance_to_strike_sigma': [0.5, -1.2, 30.0],  # third row > cap
            'prob_breakeven_gap': [0.0, 0.01, -0.005],
        })

    def test_helper_produces_all_cont_feature_cols(self):
        """apply_norm iterates CONT_FEATURE_COLS; every entry must be a
        column on the output df."""
        from features import CONT_FEATURE_COLS
        from sim_pnl import _prepare_candidate_features

        df = self._make_post_sql_df()
        out = _prepare_candidate_features(df)

        missing = [c for c in CONT_FEATURE_COLS if c not in out.columns]
        assert not missing, (
            f"_prepare_candidate_features must produce all CONT_FEATURE_COLS "
            f"so apply_norm can find them. Missing: {missing}"
        )

    def test_helper_preserves_market_price_alongside_entry_price_cents(self):
        """apply_norm needs `market_price`. Downstream `_replay_one_path`
        reads `entry_price_cents` (sim_pnl.py:649,655,656,...). BOTH
        column names must exist on the output df — that's the whole point
        of duplicating instead of renaming."""
        from sim_pnl import _prepare_candidate_features

        df = self._make_post_sql_df()
        out = _prepare_candidate_features(df)

        assert 'market_price' in out.columns, (
            "_prepare_candidate_features must NOT rename market_price away — "
            "apply_norm reads it via CONT_FEATURE_COLS."
        )
        assert 'entry_price_cents' in out.columns, (
            "_prepare_candidate_features must produce entry_price_cents — "
            "downstream _replay_one_path reads this column."
        )
        # Equal values — entry_price_cents is the alias.
        assert (out['market_price'] == out['entry_price_cents']).all()

    def test_helper_preserves_yes_spread_alongside_spread_cents(self):
        from sim_pnl import _prepare_candidate_features

        df = self._make_post_sql_df()
        out = _prepare_candidate_features(df)

        assert 'yes_spread_cents' in out.columns
        assert 'spread_cents' in out.columns
        assert (out['yes_spread_cents'] == out['spread_cents']).all()

    def test_helper_emits_price_tier_stc_bucket_vol_regime_int(self):
        """These three were already produced by the pre-fix code; lock
        them so the helper extraction doesn't regress them."""
        from sim_pnl import _prepare_candidate_features

        df = self._make_post_sql_df()
        out = _prepare_candidate_features(df)

        assert 'price_tier' in out.columns
        assert 'stc_bucket' in out.columns
        assert 'vol_regime_int' in out.columns
        # spec checks (right=True semantics, PRICE_BIN_CUTOFFS=[80,90,96])
        # row 0: market_price=85 → tier 1
        # row 1: market_price=92 → tier 2
        # row 2: market_price=96 → tier 2  (right=True: 96 ≤ 96 lands in bin 2)
        assert list(out['price_tier']) == [1, 2, 2]
        # stc=600 → bucket 2 (right=True: 600 ≤ 600), 300 → 1, 120 → 0
        assert list(out['stc_bucket']) == [2, 1, 0]
        # vol_regime: 'normal' → 0, 'elevated' → 1
        assert list(out['vol_regime_int']) == [0, 1, 0]

    def test_helper_dtypes_match_extract_data_canonical(self):
        """train/serve dtype parity — extract_data.py:365,376,377 emits
        int8 for price_tier/stc_bucket/vol_regime_int. Helper must too."""
        import numpy as np
        from sim_pnl import _prepare_candidate_features

        out = _prepare_candidate_features(self._make_post_sql_df())
        assert out['price_tier'].dtype == np.int8, out['price_tier'].dtype
        assert out['stc_bucket'].dtype == np.int8, out['stc_bucket'].dtype
        assert out['vol_regime_int'].dtype == np.int8, out['vol_regime_int'].dtype

    def test_helper_preserves_prob_breakeven_gap_value(self):
        """`prob_breakeven_gap` is in CONT_FEATURE_COLS but the helper
        does NOT derive it — it must pass through from SQL untouched.
        Lock the value-equality so a regression that zeroed/overwrote it
        mid-derivation would surface here. (Was missed by the
        column-presence-only check.)"""
        from sim_pnl import _prepare_candidate_features

        df = self._make_post_sql_df()
        original = df['prob_breakeven_gap'].copy()
        out = _prepare_candidate_features(df)
        assert (out['prob_breakeven_gap'].values == original.values).all(), (
            "prob_breakeven_gap must pass through helper unchanged."
        )

    def test_helper_preserves_nan_in_pass_through_columns(self):
        """The SQL pull deliberately does NOT IS-NOT-NULL filter on
        `spot_distance_to_strike_sigma` and `prob_breakeven_gap` (train
        parity — extract_data also passes NULL through, apply_norm fillna-
        imputes). Helper must NOT crash on NaN values in these columns
        AND must preserve NaN identity (not silently coerce to 0)."""
        import numpy as np
        import pandas as pd

        from sim_pnl import _prepare_candidate_features

        df = self._make_post_sql_df()
        # Inject NaN into one row each for the two unfiltered columns.
        df.loc[0, 'spot_distance_to_strike_sigma'] = np.nan
        df.loc[1, 'prob_breakeven_gap'] = np.nan
        out = _prepare_candidate_features(df)
        # spot_distance_to_strike_sigma: helper winsorizes via clip().
        # NaN is neither > cap nor < -cap, so .clip() leaves it as NaN.
        assert pd.isna(out['spot_distance_to_strike_sigma'].iloc[0])
        # abs_spot_distance_to_strike_sigma: derived from winsorized sd
        # via .abs(). abs(NaN) → NaN.
        assert pd.isna(out['abs_spot_distance_to_strike_sigma'].iloc[0])
        # time_decayed_proximity: derived from winsorized sd × scalar.
        # NaN × float → NaN.
        assert pd.isna(out['time_decayed_proximity'].iloc[0])
        # prob_breakeven_gap: helper passes through untouched.
        assert pd.isna(out['prob_breakeven_gap'].iloc[1])

    def test_helper_uses_features_module_bin_cutoffs(self):
        """Inlining the cutoff lists (vs importing from features) is a
        latent drift bug — extract_data.py:60-65 imports them. If
        features.py ever changes cutoffs, helper must pick up the change
        too. Lock by computing expected tiers from features.PRICE_BIN_CUTOFFS
        rather than hardcoded values."""
        import numpy as np
        import features
        from sim_pnl import _prepare_candidate_features

        df = self._make_post_sql_df()
        out = _prepare_candidate_features(df)
        expected_price_tier = np.digitize(
            df['market_price'].astype(float).to_numpy(),
            features.PRICE_BIN_CUTOFFS, right=True,
        ).astype(np.int8)
        expected_stc_bucket = np.digitize(
            df['seconds_to_close'].astype(float).to_numpy(),
            features.STC_BIN_CUTOFFS, right=True,
        ).astype(np.int8)
        assert (out['price_tier'].values == expected_price_tier).all()
        assert (out['stc_bucket'].values == expected_stc_bucket).all()

    def test_helper_handles_empty_dataframe(self):
        """SQL pull may return zero rows (legitimate: test window with
        no candidates after market_result filter). Helper must not raise
        on empty input — np.digitize on empty arrays + winsorize on
        empty Series should be no-ops."""
        import pandas as pd

        from sim_pnl import _prepare_candidate_features

        df = self._make_post_sql_df().iloc[:0].copy()
        assert len(df) == 0
        out = _prepare_candidate_features(df)
        assert len(out) == 0
        # Required columns still present (empty Series).
        for col in ['price_tier', 'stc_bucket', 'vol_regime_int',
                    'entry_price_cents', 'spread_cents',
                    'abs_spot_distance_to_strike_sigma',
                    'time_decayed_proximity', 'hour_sin', 'hour_cos']:
            assert col in out.columns, col

    def test_helper_digitize_at_exact_boundaries(self):
        """np.digitize(right=True) places boundary VALUES in the LOWER
        bin. Lock: market_price=80 → tier 0, =90 → tier 1, =96 → tier 2;
        =81 → tier 1, =97 → tier 3. stc=120 → 0, =300 → 1, =600 → 2;
        =121 → 1, =601 → 3."""
        import pandas as pd

        from sim_pnl import _prepare_candidate_features

        boundary_df = pd.DataFrame({
            'ticker': [f'T{i}' for i in range(10)],
            'evaluation_time': ['2026-05-04T16:00:00Z'] * 10,
            'asset': ['BTC'] * 10, 'side': ['yes'] * 10,
            'strategy': ['decided_t1'] * 10,
            # 5 prices: 80, 81, 90, 96, 97
            'market_price': [80, 81, 90, 96, 97, 85, 85, 85, 85, 85],
            # 5 stc values (last 5 rows): 120, 121, 300, 600, 601
            'seconds_to_close': [600., 600., 600., 600., 600.,
                                 120., 121., 300., 600., 601.],
            'vol_regime': ['normal'] * 10, 'z_score': [1.0] * 10,
            'yes_spread_cents': [2] * 10, 'calibrated_prob': [0.85] * 10,
            'calibration_method': ['cal_v1'] * 10, 'raw_prob': [0.83] * 10,
            'breakeven_wr': [0.85] * 10, 'fee_adjusted_edge': [0.02] * 10,
            'kelly_f': [0.05] * 10, 'is_weekend': [0] * 10,
            'hour_of_day_utc': [16] * 10, 'market_result': ['yes'] * 10,
            'available_balance_cents': [50000] * 10,
            'spot_distance_to_strike_sigma': [0.5] * 10,
            'prob_breakeven_gap': [0.0] * 10,
        })
        out = _prepare_candidate_features(boundary_df)
        # Price boundaries (rows 0-4): 80 → 0, 81 → 1, 90 → 1, 96 → 2, 97 → 3
        assert list(out['price_tier'].iloc[:5]) == [0, 1, 1, 2, 3]
        # STC boundaries (rows 5-9): 120 → 0, 121 → 1, 300 → 1, 600 → 2, 601 → 3
        assert list(out['stc_bucket'].iloc[5:]) == [0, 1, 1, 2, 3]


class TestPrepareCandidateFeaturesDerivations:
    """The 4 derived features must match the canonical formulas from
    `extract_data.py:396-406` (the train-time site). Any drift = silent
    train/serve skew on the v2 ablation comparison.

    Formula reference (extract_data.py:396-406):
        cap = features.SIGMA_WINSOR_ABS_CAP   # 25.0
        sd  = sd_raw.clip(lower=-cap, upper=cap)
        df['spot_distance_to_strike_sigma']     = sd      # overwritten
        df['abs_spot_distance_to_strike_sigma'] = sd.abs()
        df['time_decayed_proximity']            = sd * (1.0 - stc / 900.0)
        h = df['hour_of_day_utc'].astype(np.float32) % 24.0
        df['hour_sin'] = np.sin(2.0 * np.pi * h / 24.0)
        df['hour_cos'] = np.cos(2.0 * np.pi * h / 24.0)
    """

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("pandas")
        pytest.importorskip("numpy")
        pytest.importorskip("torch")

    def _make_df(self, sd_value, stc_value, hour_value):
        import pandas as pd

        return pd.DataFrame({
            'ticker': ['T1'], 'evaluation_time': ['2026-05-04T16:00:00Z'],
            'asset': ['BTC'], 'side': ['yes'], 'strategy': ['decided_t1'],
            'market_price': [85], 'seconds_to_close': [stc_value],
            'vol_regime': ['normal'], 'z_score': [1.0],
            'yes_spread_cents': [2], 'calibrated_prob': [0.85],
            'calibration_method': ['cal_v1'], 'raw_prob': [0.83],
            'breakeven_wr': [0.85], 'fee_adjusted_edge': [0.02],
            'kelly_f': [0.05], 'is_weekend': [0],
            'hour_of_day_utc': [hour_value],
            'market_result': ['yes'], 'available_balance_cents': [50000],
            'spot_distance_to_strike_sigma': [sd_value],
            'prob_breakeven_gap': [0.0],
        })

    def test_winsorize_clips_extreme_positive(self):
        """sd=30.0, cap=25.0 → clipped to 25.0. Train/serve invariant
        per CLAUDE.md (cal_mlp feature transforms ship in ONE commit)."""
        import features
        from sim_pnl import _prepare_candidate_features

        out = _prepare_candidate_features(
            self._make_df(sd_value=30.0, stc_value=600.0, hour_value=16),
        )
        cap = features.SIGMA_WINSOR_ABS_CAP
        assert out['spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(cap)
        assert out['abs_spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(cap)

    def test_winsorize_clips_extreme_negative(self):
        import features
        from sim_pnl import _prepare_candidate_features

        out = _prepare_candidate_features(
            self._make_df(sd_value=-50.0, stc_value=600.0, hour_value=16),
        )
        cap = features.SIGMA_WINSOR_ABS_CAP
        assert out['spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(-cap)
        assert out['abs_spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(cap)

    def test_winsorize_passes_through_in_band(self):
        import features
        from sim_pnl import _prepare_candidate_features

        cap = features.SIGMA_WINSOR_ABS_CAP
        # Use a value safely inside the band.
        sd = 1.5
        out = _prepare_candidate_features(
            self._make_df(sd_value=sd, stc_value=600.0, hour_value=16),
        )
        assert out['spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(sd)

    def test_abs_spot_distance_after_winsor(self):
        from sim_pnl import _prepare_candidate_features

        out = _prepare_candidate_features(
            self._make_df(sd_value=-1.5, stc_value=600.0, hour_value=16),
        )
        assert out['abs_spot_distance_to_strike_sigma'].iloc[0] == pytest.approx(1.5)

    def test_time_decayed_proximity_formula(self):
        """time_decayed_proximity = winsorized_sd * (1 - stc/900).
        At stc=900 → 0; at stc=0 → sd; at stc=450 → sd*0.5."""
        from sim_pnl import _prepare_candidate_features

        # stc=450, sd=2.0 → tdp = 2.0 * (1 - 0.5) = 1.0
        out = _prepare_candidate_features(
            self._make_df(sd_value=2.0, stc_value=450.0, hour_value=16),
        )
        assert out['time_decayed_proximity'].iloc[0] == pytest.approx(1.0, abs=1e-5)

        # stc=900, any sd → tdp = 0
        out2 = _prepare_candidate_features(
            self._make_df(sd_value=2.0, stc_value=900.0, hour_value=16),
        )
        assert out2['time_decayed_proximity'].iloc[0] == pytest.approx(0.0, abs=1e-5)

        # stc=0, sd=2.0 → tdp = 2.0
        out3 = _prepare_candidate_features(
            self._make_df(sd_value=2.0, stc_value=0.0, hour_value=16),
        )
        assert out3['time_decayed_proximity'].iloc[0] == pytest.approx(2.0, abs=1e-5)

    def test_time_decayed_proximity_uses_winsorized_sd(self):
        """If winsorization is applied AFTER tdp computation, tdp would
        use the raw 30 → wrong value. Lock the order."""
        import features
        from sim_pnl import _prepare_candidate_features

        cap = features.SIGMA_WINSOR_ABS_CAP
        # sd_raw=30 → winsorized to cap=25; stc=450 → tdp = 25 * 0.5 = 12.5
        out = _prepare_candidate_features(
            self._make_df(sd_value=30.0, stc_value=450.0, hour_value=16),
        )
        expected = cap * 0.5
        assert out['time_decayed_proximity'].iloc[0] == pytest.approx(
            expected, abs=1e-4,
        )

    def test_hour_sin_cos_at_zero(self):
        """At hour=0: sin(0)=0, cos(0)=1."""
        from sim_pnl import _prepare_candidate_features

        out = _prepare_candidate_features(
            self._make_df(sd_value=0.0, stc_value=600.0, hour_value=0),
        )
        assert out['hour_sin'].iloc[0] == pytest.approx(0.0, abs=1e-5)
        assert out['hour_cos'].iloc[0] == pytest.approx(1.0, abs=1e-5)

    def test_hour_sin_cos_at_six(self):
        """At hour=6: sin(2π·6/24)=sin(π/2)=1, cos(π/2)=0."""
        from sim_pnl import _prepare_candidate_features

        out = _prepare_candidate_features(
            self._make_df(sd_value=0.0, stc_value=600.0, hour_value=6),
        )
        assert out['hour_sin'].iloc[0] == pytest.approx(1.0, abs=1e-5)
        assert out['hour_cos'].iloc[0] == pytest.approx(0.0, abs=1e-5)

    def test_hour_sin_cos_modulo_24(self):
        """hour_value=25 → 25 % 24 = 1, same as hour=1.
        Mirrors `h = df['hour_of_day_utc'].astype(np.float32) % 24.0`.
        Anchored against an absolute-value reference so a buggy mod
        (e.g., %23) wouldn't pass purely from self-consistency."""
        import math

        from sim_pnl import _prepare_candidate_features

        out_25 = _prepare_candidate_features(
            self._make_df(sd_value=0.0, stc_value=600.0, hour_value=25),
        )
        out_1 = _prepare_candidate_features(
            self._make_df(sd_value=0.0, stc_value=600.0, hour_value=1),
        )
        # Self-consistency.
        assert out_25['hour_sin'].iloc[0] == pytest.approx(
            out_1['hour_sin'].iloc[0], abs=1e-5,
        )
        assert out_25['hour_cos'].iloc[0] == pytest.approx(
            out_1['hour_cos'].iloc[0], abs=1e-5,
        )
        # Absolute reference — a buggy mod (e.g., %23) would yield
        # different hour value here and fail.
        assert out_25['hour_sin'].iloc[0] == pytest.approx(
            math.sin(2.0 * math.pi * 1.0 / 24.0), abs=1e-5,
        )
        assert out_25['hour_cos'].iloc[0] == pytest.approx(
            math.cos(2.0 * math.pi * 1.0 / 24.0), abs=1e-5,
        )


# ── Byte-parity with canonical Phase 2 derivation ──────────────────────


class TestExtractDataByteParity:
    """Per CLAUDE.md "cal_mlp feature transforms ship in ONE commit
    across [now] FIVE sites": run BOTH `extract_data.build_feature_frame`
    (canonical) and `_prepare_candidate_features` (sim_pnl) on the SAME
    input and assert byte-equality on every overlapping derived column.

    This catches lock-step drift that the per-formula tests miss when
    both sites change identically-wrong (e.g., %24→%23 in both)."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("pandas")
        pytest.importorskip("numpy")
        pytest.importorskip("torch")

    def _make_required_source_rows(self):
        """Build rows with all REQUIRED_SOURCE_COLS keys (extract_data.py:216-233).
        Values exercise: in-band sd, out-of-band sd (winsorize), normal+elevated
        vol_regime, multiple price tiers, stc near boundaries, varied hours."""
        return [
            {
                'ticker': 'KXBTC15M-r0', 'evaluation_time': '2026-05-04T16:00:00Z',
                'asset': 'BTC', 'side': 'yes', 'strategy': 'decided_t1',
                'product_type': '15m',
                'market_price': 85, 'seconds_to_close': 600.0,
                'vol_regime': 'normal', 'z_score': 1.2,
                'yes_spread_cents': 2, 'calibrated_prob': 0.85,
                'raw_prob': 0.83, 'breakeven_wr': 0.85,
                'fee_adjusted_edge': 0.02, 'kelly_f': 0.05,
                'is_weekend': 0, 'hour_of_day_utc': 16, 'day_of_week': 1,
                'market_result': 'yes', 'settled_time': '2026-05-04T16:15:00Z',
                'available_balance_cents': 50000,
                'spot_momentum_60s_bps': 0.0, 'spot_momentum_5m_bps': 0.0,
                'spot_realized_range_15m_bps': 0.0,
                'btc_spot_change_5m_bps': 0.0, 'btc_realized_vol_15m': 0.001,
                'window_max_buf_pct': 0.5, 'window_min_buf_pct': -0.5,
                'minutes_above_strike': 5.0,
                'spot_distance_to_strike_sigma': 0.5,
                'prob_breakeven_gap': 0.0,
                'spot_coinbase_kraken_gap_bps': 0.0,
                'kalshi_flow_depth_velocity': 0.0,
                'data_provenance': 'live_ws',
            },
            {
                'ticker': 'KXETH15M-r1', 'evaluation_time': '2026-05-04T16:05:00Z',
                'asset': 'ETH', 'side': 'yes', 'strategy': 'decided_t1',
                'product_type': '15m',
                'market_price': 92, 'seconds_to_close': 300.0,
                'vol_regime': 'elevated', 'z_score': 1.8,
                'yes_spread_cents': 1, 'calibrated_prob': 0.91,
                'raw_prob': 0.89, 'breakeven_wr': 0.92,
                'fee_adjusted_edge': 0.04, 'kelly_f': 0.08,
                'is_weekend': 0, 'hour_of_day_utc': 16, 'day_of_week': 1,
                'market_result': 'yes', 'settled_time': '2026-05-04T16:20:00Z',
                'available_balance_cents': 50000,
                'spot_momentum_60s_bps': 0.0, 'spot_momentum_5m_bps': 0.0,
                'spot_realized_range_15m_bps': 0.0,
                'btc_spot_change_5m_bps': 0.0, 'btc_realized_vol_15m': 0.001,
                'window_max_buf_pct': 0.5, 'window_min_buf_pct': -0.5,
                'minutes_above_strike': 4.0,
                'spot_distance_to_strike_sigma': -1.2,  # negative
                'prob_breakeven_gap': 0.01,
                'spot_coinbase_kraken_gap_bps': 0.0,
                'kalshi_flow_depth_velocity': 0.0,
                'data_provenance': 'live_ws',
            },
            {
                'ticker': 'KXSOL15M-r2', 'evaluation_time': '2026-05-04T16:10:00Z',
                'asset': 'SOL', 'side': 'no', 'strategy': 'TAKER_NOW',
                'product_type': '15m',
                'market_price': 96, 'seconds_to_close': 120.0,
                'vol_regime': 'normal', 'z_score': 0.5,
                'yes_spread_cents': 3, 'calibrated_prob': 0.97,
                'raw_prob': 0.96, 'breakeven_wr': 0.96,
                'fee_adjusted_edge': 0.01, 'kelly_f': 0.02,
                'is_weekend': 1, 'hour_of_day_utc': 16, 'day_of_week': 6,
                'market_result': 'no', 'settled_time': '2026-05-04T16:25:00Z',
                'available_balance_cents': 50000,
                'spot_momentum_60s_bps': 0.0, 'spot_momentum_5m_bps': 0.0,
                'spot_realized_range_15m_bps': 0.0,
                'btc_spot_change_5m_bps': 0.0, 'btc_realized_vol_15m': 0.001,
                'window_max_buf_pct': 0.5, 'window_min_buf_pct': -0.5,
                'minutes_above_strike': 1.0,
                'spot_distance_to_strike_sigma': 30.0,  # > cap 25
                'prob_breakeven_gap': -0.005,
                'spot_coinbase_kraken_gap_bps': 0.0,
                'kalshi_flow_depth_velocity': 0.0,
                'data_provenance': 'live_ws',
            },
        ]

    def test_overlapping_derived_columns_byte_equal(self):
        import numpy as np
        import pandas as pd

        import extract_data
        from sim_pnl import _prepare_candidate_features

        rows = self._make_required_source_rows()
        # Canonical site — extract_data.build_feature_frame.
        canonical_df = extract_data.build_feature_frame(rows)
        # Helper site — feed the SAME row dicts as a DataFrame.
        helper_df = pd.DataFrame(rows)
        helper_df = _prepare_candidate_features(helper_df)

        # Both DERIVED columns (helper's contribution) AND source-col
        # PASS-THROUGH (columns both sites should leave alone). Pass-through
        # parity catches a future bug like
        # `df['market_price'] = df['market_price'] * 100` on either site.
        overlap = [
            # Derived (canonical block at extract_data.py:396-406):
            'price_tier', 'stc_bucket', 'vol_regime_int',
            'spot_distance_to_strike_sigma',  # winsorized in BOTH sites
            'abs_spot_distance_to_strike_sigma',
            'time_decayed_proximity',
            'hour_sin', 'hour_cos',
            # Source pass-through (both sites must NOT mutate):
            'market_price', 'seconds_to_close',
            'prob_breakeven_gap', 'hour_of_day_utc',
        ]
        for col in overlap:
            c = canonical_df[col].to_numpy()
            h = helper_df[col].to_numpy()
            # np.array_equal is dtype-blind (int8 vs int64 with same values
            # passes). Lock dtype too — a regression that drops `.astype(np.int8)`
            # from either site is exactly the kind of silent train/serve skew
            # CLAUDE.md flags.
            assert c.dtype == h.dtype, (
                f"train/serve dtype-parity violation on `{col}`:\n"
                f"  extract_data.build_feature_frame: dtype={c.dtype}\n"
                f"  _prepare_candidate_features:     dtype={h.dtype}"
            )
            # Use array_equal for exact value equality (no float tolerance —
            # the formulas should be deterministic and identical).
            assert np.array_equal(c, h), (
                f"train/serve byte-parity violation on `{col}`:\n"
                f"  extract_data.build_feature_frame: {c}\n"
                f"  _prepare_candidate_features:     {h}\n"
                f"Both sites must mirror extract_data.py:396-406; any "
                f"drift = silent train/serve skew (CLAUDE.md "
                f"five-site lock-step rule)."
            )

    def test_side_int_parity_with_extract_data(self):
        """side_int is derived OUTSIDE the helper (in run_sim_pnl after
        the helper call) — extract_data.py:377 produces it inside
        build_feature_frame. Both compute `(side == 'yes').astype(int8)`.
        Sibling parity check so divergence (e.g., extract_data starts
        case-insensitive comparison and sim_pnl doesn't) surfaces here.

        Replicates the run_sim_pnl post-helper line:
          candidate_df['side_int'] = (candidate_df['side'].astype(str)
                                      == 'yes').astype(np.int8)
        """
        import numpy as np
        import pandas as pd

        import extract_data

        rows = self._make_required_source_rows()
        canonical_df = extract_data.build_feature_frame(rows)
        # Mirror the post-helper code path for side_int:
        helper_df = pd.DataFrame(rows)
        side_int_serve = (
            helper_df['side'].astype(str) == 'yes'
        ).astype(np.int8).to_numpy()
        side_int_train = canonical_df['side_int'].to_numpy()
        # Dtype lock — np.array_equal would pass on int8 vs int64 with
        # equal values, hiding a silent regression.
        assert side_int_train.dtype == np.int8, side_int_train.dtype
        assert side_int_serve.dtype == np.int8, side_int_serve.dtype
        assert np.array_equal(side_int_train, side_int_serve), (
            f"train/serve byte-parity violation on `side_int`:\n"
            f"  extract_data: {side_int_train}\n"
            f"  sim_pnl post-helper: {side_int_serve}"
        )


# ── AST guards ────────────────────────────────────────────────────────


class TestSimPnlSqlAstGuard:
    """sim_pnl.py's SQL SELECT inside `run_sim_pnl` MUST include the new
    columns. Walks the AST to the FIRST `pd.read_sql` Call in run_sim_pnl
    and substring-checks the LITERAL SELECT string — not the whole
    function source. A bare-source substring check fires on comments,
    WHERE clauses, or even the helper docstring header, masking the
    actual SELECT contract."""

    SIM_PNL_PATH = PROJECT_ROOT / "scripts" / "cal_mlp" / "sim_pnl.py"

    def _select_query_literal(self) -> str:
        """Return the first string-literal positional arg passed to
        `pd.read_sql` (or `read_sql`) inside run_sim_pnl. That's the
        SELECT query string proper."""
        src = self.SIM_PNL_PATH.read_text()
        tree = ast.parse(src)
        for fn in tree.body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name == 'run_sim_pnl'):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                func_name = None
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id
                if func_name != 'read_sql':
                    continue
                if not node.args or not isinstance(node.args[0], ast.Constant):
                    continue
                if not isinstance(node.args[0].value, str):
                    continue
                return node.args[0].value
            raise AssertionError(
                "run_sim_pnl found but no `read_sql(<str literal>, ...)` "
                "call inside — refactor target moved? Update guard."
            )
        raise AssertionError(
            "run_sim_pnl FunctionDef not found at module level."
        )

    def test_sql_select_includes_spot_distance_to_strike_sigma(self):
        sql = self._select_query_literal()
        # Locate the SELECT-list region (between SELECT and FROM) so
        # WHERE-clause mentions don't satisfy this guard.
        upper = sql.upper()
        sel_start = upper.find('SELECT')
        from_start = upper.find('FROM')
        assert sel_start >= 0 and from_start > sel_start, (
            f"SQL literal missing SELECT/FROM keywords: {sql[:200]!r}"
        )
        select_list = sql[sel_start:from_start]
        assert 'spot_distance_to_strike_sigma' in select_list, (
            "run_sim_pnl's SQL SELECT-list must include "
            "`spot_distance_to_strike_sigma` (apply_norm reads it via "
            f"CONT_FEATURE_COLS). SELECT region was: {select_list!r}"
        )

    def test_sql_select_includes_prob_breakeven_gap(self):
        sql = self._select_query_literal()
        upper = sql.upper()
        sel_start = upper.find('SELECT')
        from_start = upper.find('FROM')
        select_list = sql[sel_start:from_start]
        assert 'prob_breakeven_gap' in select_list, (
            "run_sim_pnl's SQL SELECT-list must include "
            "`prob_breakeven_gap`. SELECT region was: {select_list!r}"
        )


class TestSimPnlPrepareCandidateInvocation:
    """Lock-step: run_sim_pnl must CALL `_prepare_candidate_features`
    on the candidate_df, capture the return value into `candidate_df`,
    and do so BEFORE invoking `apply_norm`. Same precedent as
    compute_method_output's call-site AST guard
    (test_cal_mlp_validate_cfg_fp.py)."""

    SIM_PNL_PATH = PROJECT_ROOT / "scripts" / "cal_mlp" / "sim_pnl.py"

    def _run_sim_pnl_fn(self) -> ast.FunctionDef:
        src = self.SIM_PNL_PATH.read_text()
        tree = ast.parse(src)
        for fn in tree.body:
            if isinstance(fn, ast.FunctionDef) and fn.name == 'run_sim_pnl':
                return fn
        raise AssertionError(
            "run_sim_pnl FunctionDef not found at module level."
        )

    def _call_lineno_in_run_sim_pnl(self, target_func: str) -> int:
        """Return the lineno of the FIRST Call to `target_func` (matching
        either Name or Attribute.attr) inside run_sim_pnl. -1 if not
        found."""
        fn = self._run_sim_pnl_fn()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = None
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name == target_func:
                return node.lineno
        return -1

    def test_run_sim_pnl_assigns_helper_return_to_candidate_df(self):
        """The helper mutates IN-PLACE so a discarded-return call would
        still work today, BUT a future refactor that returns a NEW
        DataFrame would silently break apply_norm. Lock the assignment
        shape `candidate_df = _prepare_candidate_features(candidate_df)`.

        Layered defense (note for future maintainers): this AST guard
        catches the SHAPE; the functional tests in
        TestPrepareCandidateFeaturesContract catch the SEMANTICS
        (e.g., a `return None` regression would surface there as
        AttributeError on `out.columns`). Neither alone is sufficient."""
        fn = self._run_sim_pnl_fn()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            if len(node.targets) != 1:
                continue
            tgt = node.targets[0]
            if not (isinstance(tgt, ast.Name) and tgt.id == 'candidate_df'):
                continue
            rhs = node.value
            if not isinstance(rhs, ast.Call):
                continue
            if isinstance(rhs.func, ast.Name) and rhs.func.id == '_prepare_candidate_features':
                return  # found
        raise AssertionError(
            "run_sim_pnl must contain "
            "`candidate_df = _prepare_candidate_features(candidate_df)` "
            "(reassign return value). A discarded-return call works today "
            "because the helper mutates in-place, but a refactor that "
            "switches to returning a new df would silently break apply_norm."
        )

    def test_helper_called_before_apply_norm(self):
        """Order matters: the helper produces market_price (and the 4
        derived features) that apply_norm reads. If a future refactor
        swaps the order, apply_norm KeyErrors at runtime but every
        existing test still passes."""
        helper_ln = self._call_lineno_in_run_sim_pnl('_prepare_candidate_features')
        apply_norm_ln = self._call_lineno_in_run_sim_pnl('apply_norm')
        assert helper_ln > 0, "_prepare_candidate_features call not found in run_sim_pnl"
        assert apply_norm_ln > 0, "apply_norm call not found in run_sim_pnl"
        assert helper_ln < apply_norm_ln, (
            f"_prepare_candidate_features (line {helper_ln}) must be called "
            f"BEFORE apply_norm (line {apply_norm_ln}). The helper produces "
            f"the columns apply_norm reads; reverse order = KeyError at runtime."
        )

    def test_audit_dict_includes_n_null_imputed_keys(self):
        """The new audit fields `n_null_imputed_spot_distance_to_strike_sigma`
        and `n_null_imputed_prob_breakeven_gap` MUST be keyed in the `out`
        dict. AST guard against a typo / silent rename (e.g., variable
        name `n_null_imputed_spot_distance` accidentally propagates as
        the dict key, dropping the `_to_strike_sigma` suffix). Walks
        for the first Dict literal inside run_sim_pnl whose keys include
        the existing `'excluded_null_market_price'` (anchor key) and
        asserts both new keys are present alongside it."""
        fn = self._run_sim_pnl_fn()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Dict):
                continue
            keys = set()
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    keys.add(k.value)
            if 'excluded_null_market_price' not in keys:
                continue
            assert 'n_null_imputed_spot_distance_to_strike_sigma' in keys, (
                f"audit `out` dict must include "
                f"`n_null_imputed_spot_distance_to_strike_sigma`. "
                f"Found keys (around the anchor): "
                f"{sorted(k for k in keys if 'null' in k.lower())}"
            )
            assert 'n_null_imputed_prob_breakeven_gap' in keys, (
                f"audit `out` dict must include "
                f"`n_null_imputed_prob_breakeven_gap`. "
                f"Found keys (around the anchor): "
                f"{sorted(k for k in keys if 'null' in k.lower())}"
            )
            return
        raise AssertionError(
            "audit `out` dict (anchor key `excluded_null_market_price`) "
            "not found inside run_sim_pnl — has the audit construction "
            "moved? Update this guard."
        )

    def test_run_sim_pnl_does_not_rename_market_price_away(self):
        """The pre-fix bug. Locks against re-introduction of
        `candidate_df.rename(columns={'market_price': 'entry_price_cents', ...})`
        which would strip `market_price` and break apply_norm again."""
        fn = self._run_sim_pnl_fn()
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute)
                    and node.func.attr == 'rename'):
                continue
            for kw in node.keywords:
                if kw.arg != 'columns':
                    continue
                if not isinstance(kw.value, ast.Dict):
                    continue
                for k in kw.value.keys:
                    if isinstance(k, ast.Constant) and k.value == 'market_price':
                        raise AssertionError(
                            "run_sim_pnl must NOT rename `market_price` away "
                            "— apply_norm reads it via CONT_FEATURE_COLS. "
                            "Use `df['entry_price_cents'] = df['market_price']` "
                            "(duplicate) instead."
                        )
