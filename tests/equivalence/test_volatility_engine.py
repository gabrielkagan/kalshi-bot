"""Equivalence harness — VolatilityEngine.

Pillar 3 of the testing-foundation-sprint
(kb/decisions/testing-foundation-sprint-may09.md). Two layers:

1. **Static-method snapshot** — six pure ``@staticmethod`` math kernels
   on VolatilityEngine (``_parzen_kernel``,
   ``_estimate_noise_variance``, ``_realized_quarticity``,
   ``_optimal_rk_bandwidth``, ``_realized_kernel``,
   ``_bipower_variation``) snapshotted across deterministic
   synthetic return series. These are the right surface for
   equivalence: they are stateless, time-independent, and
   return numerical outputs. The orchestrating ``update()`` is
   intentionally NOT in the snapshot — it touches disk
   (``rk_state.json``, ``jump_adaptive_state.json``), reads
   ``time.time()``, and mutates ``self._returns`` /
   ``self._cache``. A snapshot of an impure method is fragile;
   the math kernels above are where drift would actually surface.

2. **Property tests (hypothesis)** — bounded random inputs to the
   same six static methods, asserting invariants (RK output ≥ 0,
   bandwidth ≥ 1 when applicable, kernel ∈ [0, 1], etc.).
   Hand-written strategies — engine inputs are tightly bounded
   (5-second log returns rarely exceed ±5%), so polyfactory's
   default unbounded floats would generate impossible scenarios.

Snapshot regeneration: see ``tests/equivalence/REGEN.md``. Never
auto-regen from an agent run.

Why no parquet corpus for VolatilityEngine: the engine consumes a
stateful price-tick buffer that is not stored in ``state.db``
(``self._returns[asset]`` is in-memory only, persisted to a JSON
sidecar). Re-deriving 1000 buffer states from
``position_price_observations`` is expensive and pulls in disk-IO
state. Synthetic seeded scenarios cover the math surface that
matters for extraction-equivalence.
"""
from __future__ import annotations

import math
from typing import Dict, List

import pytest

# Heavy-dep mocking lives in conftest.py — runs at import.

import numpy as np
from hypothesis import given, settings, strategies as st


# ── Engine import ────────────────────────────────────────────────────
from bot import VolatilityEngine
from bot.constants import (
    RK_NOISE_VAR_FLOOR,
    VOL_WINDOW_15MIN,
    VOL_WINDOW_5MIN,
    VOL_WINDOW_1MIN,
)


# ─────────────────────────────────────────────────────────────────────
#  SCENARIO BUILDERS
# ─────────────────────────────────────────────────────────────────────

# Six labeled return series with varying length + volatility regime.
# Seeded numpy random so the corpus is byte-deterministic across
# pytest runs and machines.
def _build_scenarios() -> Dict[str, List[float]]:
    """Construct deterministic 5-second-log-return series spanning the
    regime grid the engine actually sees in production.

    Length 180 = 15 minutes (the engine's max buffer). Shorter series
    cover the early-window degenerate paths."""
    rng = np.random.default_rng(seed=42)
    scenarios: Dict[str, List[float]] = {}

    # Calm-market BTC: 5-second log returns ~ N(0, 1e-5)
    scenarios["btc_calm_180"] = list(rng.normal(0.0, 1e-5, 180))
    # Normal-market ETH: ~5x BTC's calm vol
    scenarios["eth_normal_180"] = list(rng.normal(0.0, 5e-5, 180))
    # Elevated-vol SOL: ~2e-4 stddev
    scenarios["sol_elevated_180"] = list(rng.normal(0.0, 2e-4, 180))
    # Jumpy XRP: heavy-tailed, single ±0.5% spike at index 90
    base = list(rng.normal(0.0, 5e-5, 180))
    base[90] = 0.005
    scenarios["xrp_jump_at_90"] = base
    # Short series — exercises the n<window degenerate path
    scenarios["short_60"] = list(rng.normal(0.0, 1e-4, 60))
    # All-positive momentum (positive autocovariance → noise floor)
    scenarios["momentum_120"] = [1e-4 * (1.0 + 0.01 * i) for i in range(120)]
    # Alternating sign — strong negative autocovariance → high omega²
    scenarios["alternating_120"] = [(-1.0) ** i * 1e-4 for i in range(120)]
    # Edge: empty + single element (used to verify defensive paths)
    scenarios["empty"] = []
    scenarios["single"] = [1e-4]

    return scenarios


_SCENARIOS = _build_scenarios()
_WINDOWS = (VOL_WINDOW_1MIN, VOL_WINDOW_5MIN, VOL_WINDOW_15MIN)


# ─────────────────────────────────────────────────────────────────────
#  STATIC-METHOD SNAPSHOTS
# ─────────────────────────────────────────────────────────────────────

def test_parzen_kernel_grid_snapshot(num_regression):
    """``_parzen_kernel`` over a 41-point grid covering the support
    [-2, 2] (the kernel is zero outside [-1, 1] but the grid extends
    so the snapshot also pins the cutoff)."""
    xs = np.linspace(-2.0, 2.0, 41)
    ys = np.array([VolatilityEngine._parzen_kernel(float(x)) for x in xs])
    num_regression.check({"x": xs, "kernel": ys},
                         default_tolerance={"rtol": 1e-12, "atol": 0.0})


# Snapshot strategy is split by output magnitude:
#
# * Floor-magnitude methods — ``_estimate_noise_variance`` (returns
#   ω², floor at ``RK_NOISE_VAR_FLOOR=1e-20``) and
#   ``_realized_quarticity`` (sum of fourth-power returns, ~1e-15 to
#   1e-20 for realistic 5-second log returns) — go through
#   ``num_regression`` with ``atol=1e-22``. ``data_regression`` with
#   12-decimal rounding (the original choice) silently floors all
#   values <5e-13 to 0.0, destroying drift signal. ``num_regression``
#   uses numpy ``isclose`` (atol + rtol*|b|), which preserves
#   floor-magnitude variation while still tolerating real
#   floating-point noise.
#
# * Sigma-scale methods — ``_realized_kernel`` and
#   ``_bipower_variation`` (both return ``sqrt(non-negative)``, so
#   ~1e-5 to 1e-3 for realistic returns) — stay on ``data_regression``
#   with 12-decimal rounding. Above the precision-loss boundary,
#   YAML-keyed-by-scenario diffs are more readable and row-order
#   coupling is irrelevant.
#
# * ``_optimal_rk_bandwidth`` returns int — ``data_regression`` with
#   no rounding; YAML int round-trip is exact.
#
# * ``_parzen_kernel`` snapshot is num_regression at
#   ``rtol=1e-12, atol=0`` (continuous x-grid, no row-order concern,
#   pure ULP-stable math).
#
# Row order in the num_regression snapshots is load-bearing —
# ``test_volatility_engine_snapshot_row_order`` pins it.

# All snapshots round to 12 decimals where applicable. Below
# float64 ULP for most values; well above scipy/libm-level noise.
_ROUND_DEC = 12


def _round(x) -> float:
    return round(float(x), _ROUND_DEC)


def _scenario_outputs(method, *, with_window: bool, **extra):
    """Build a {scenario_key → output} mapping for data_regression.
    Each scenario is its own key — adding one to ``_SCENARIOS`` adds
    one key, doesn't shift the rest."""
    out = {}
    for sname in sorted(_SCENARIOS.keys()):
        if with_window:
            for w in _WINDOWS:
                out[f"{sname}|w={w}"] = _round(method(_SCENARIOS[sname], w, **extra))
        else:
            out[sname] = _round(method(_SCENARIOS[sname], **extra))
    return out


def _scenario_array(method, *, with_window: bool, **extra):
    """Build a parallel (keys: list[str], values: np.ndarray) for
    num_regression. Row order is alphabetical by scenario — pinned
    by ``test_volatility_engine_snapshot_row_order``."""
    keys: List[str] = []
    vals: List[float] = []
    for sname in sorted(_SCENARIOS.keys()):
        if with_window:
            for w in _WINDOWS:
                keys.append(f"{sname}|w={w}")
                vals.append(float(method(_SCENARIOS[sname], w, **extra)))
        else:
            keys.append(sname)
            vals.append(float(method(_SCENARIOS[sname], **extra)))
    return keys, np.asarray(vals, dtype=np.float64)


def test_volatility_engine_snapshot_row_order():
    """Pin the row order of the num_regression snapshots. If a future
    contributor adds a scenario to ``_SCENARIOS``, this test fails
    BEFORE the snapshot tests so the diff isn't read as math drift."""
    expected = [
        "alternating_120", "btc_calm_180", "empty", "eth_normal_180",
        "momentum_120", "short_60", "single", "sol_elevated_180",
        "xrp_jump_at_90",
    ]
    assert sorted(_SCENARIOS.keys()) == expected, (
        f"Scenario list changed; regenerate noise_variance + quarticity "
        f"snapshots and update this test's expected list. Got: "
        f"{sorted(_SCENARIOS.keys())}"
    )


def test_estimate_noise_variance_snapshot(num_regression, data_regression):
    """``_estimate_noise_variance`` returns floor-magnitude ω² values
    (~1e-20). num_regression with atol=1e-22 preserves drift signal
    that data_regression rounding to 12 decimals would silently
    floor to 0.0."""
    keys, arr = _scenario_array(VolatilityEngine._estimate_noise_variance,
                                 with_window=False)
    num_regression.check({"omega_sq": arr},
                         default_tolerance={"rtol": 1e-9, "atol": 1e-22})
    data_regression.check({"row_order": keys},
                          basename="estimate_noise_variance_keys")


def test_realized_quarticity_snapshot(num_regression, data_regression):
    """``_realized_quarticity`` outputs at 1e-15 to 1e-20 magnitudes
    for realistic returns — same precision-floor reasoning as
    noise_variance."""
    keys, arr = _scenario_array(VolatilityEngine._realized_quarticity,
                                 with_window=True)
    num_regression.check({"rq": arr},
                         default_tolerance={"rtol": 1e-9, "atol": 1e-22})
    data_regression.check({"row_order": keys},
                          basename="realized_quarticity_keys")


def test_optimal_rk_bandwidth_snapshot(data_regression):
    """``_optimal_rk_bandwidth`` returns an int — store as int (not
    rounded) so the YAML diff shows integer-only values. Adding a
    scenario adds one key, doesn't shift the rest."""
    out = {}
    for sname in sorted(_SCENARIOS.keys()):
        omega_sq = VolatilityEngine._estimate_noise_variance(_SCENARIOS[sname])
        for w in _WINDOWS:
            out[f"{sname}|w={w}"] = int(VolatilityEngine._optimal_rk_bandwidth(
                _SCENARIOS[sname], w, omega_sq))
    data_regression.check(out)


def test_realized_kernel_snapshot(data_regression):
    """``_realized_kernel`` — sigma-scale autocovariance-weighted RV
    estimator. Outputs ~1e-5 to 1e-3, well above the 12-decimal
    rounding floor."""
    data_regression.check(_scenario_outputs(
        VolatilityEngine._realized_kernel, with_window=True))


def test_bipower_variation_snapshot(data_regression):
    """``_bipower_variation`` — sigma-scale BV estimator. Outputs
    ~1e-5 to 1e-3, same magnitude regime as _realized_kernel."""
    data_regression.check(_scenario_outputs(
        VolatilityEngine._bipower_variation, with_window=True))


# ─────────────────────────────────────────────────────────────────────
#  PROPERTY TESTS (hypothesis)
# ─────────────────────────────────────────────────────────────────────

# 5-second log returns on crypto rarely exceed ±5%; cap the strategy
# at ±0.05 so synthetic inputs match the engine's actual call regime.
_RETURN_STRAT = st.floats(min_value=-0.05, max_value=0.05,
                           allow_nan=False, allow_infinity=False)
_RETURNS_LIST_STRAT = st.lists(_RETURN_STRAT, min_size=2, max_size=200)
_WINDOW_STRAT = st.sampled_from(_WINDOWS)


@given(x=st.floats(min_value=-3.0, max_value=3.0,
                    allow_nan=False, allow_infinity=False))
@settings(max_examples=200, deadline=None)
def test_parzen_kernel_in_unit_interval(x):
    """``_parzen_kernel(x) ∈ [0, 1]`` for every finite x; equals 1
    on |x| ≤ 0.5; equals 0 on |x| > 1."""
    val = VolatilityEngine._parzen_kernel(x)
    assert 0.0 <= val <= 1.0
    if abs(x) <= 0.5:
        assert val == 1.0
    if abs(x) > 1.0:
        assert val == 0.0


@given(returns=_RETURNS_LIST_STRAT)
@settings(max_examples=200, deadline=None)
def test_noise_variance_floored(returns):
    """``_estimate_noise_variance`` returns ≥ ``RK_NOISE_VAR_FLOOR``
    on any finite return series."""
    omega_sq = VolatilityEngine._estimate_noise_variance(returns)
    assert omega_sq >= RK_NOISE_VAR_FLOOR
    assert not math.isnan(omega_sq)
    assert not math.isinf(omega_sq)


@given(returns=_RETURNS_LIST_STRAT, window=_WINDOW_STRAT)
@settings(max_examples=100, deadline=None)
def test_realized_quarticity_non_negative(returns, window):
    """RQ is a sum of fourth-power returns scaled by n/3 — must be ≥ 0."""
    rq = VolatilityEngine._realized_quarticity(returns, window)
    assert rq >= 0.0
    assert not math.isnan(rq)
    assert not math.isinf(rq)


@given(returns=_RETURNS_LIST_STRAT, window=_WINDOW_STRAT)
@settings(max_examples=100, deadline=None)
def test_optimal_rk_bandwidth_non_negative_int(returns, window):
    """Bandwidth is always a non-negative int. n<2 falls through to 0
    by the early-return branch; otherwise ≥ 1."""
    omega_sq = VolatilityEngine._estimate_noise_variance(returns)
    h = VolatilityEngine._optimal_rk_bandwidth(returns, window, omega_sq)
    assert isinstance(h, int)
    assert h >= 0


@given(returns=_RETURNS_LIST_STRAT, window=_WINDOW_STRAT)
@settings(max_examples=100, deadline=None)
def test_realized_kernel_non_negative(returns, window):
    """RK output is sqrt-of-clamped-non-negative — must be ≥ 0 finite."""
    rk = VolatilityEngine._realized_kernel(returns, window)
    assert rk >= 0.0
    assert not math.isnan(rk)
    assert not math.isinf(rk)


@given(returns=_RETURNS_LIST_STRAT, window=_WINDOW_STRAT)
@settings(max_examples=100, deadline=None)
def test_bipower_variation_non_negative(returns, window):
    """BV is sum of |r_i|·|r_{i+1}| terms scaled by π/2 / (n-1) —
    always non-negative."""
    bv = VolatilityEngine._bipower_variation(returns, window)
    assert bv >= 0.0
    assert not math.isnan(bv)
    assert not math.isinf(bv)


def test_realized_kernel_matches_zero_variance_walk():
    """Spot-check: a constant-zero return series produces RK = 0
    exactly. Catches a reduce-bug regression where empty-sum default
    might leak a non-zero residual."""
    zeros = [0.0] * 60
    rk = VolatilityEngine._realized_kernel(zeros, VOL_WINDOW_5MIN)
    assert rk == 0.0


def test_bipower_variation_iid_approximation():
    """For iid Gaussian returns with stddev σ, BV ≈ σ. This is the
    estimator's defining property — sanity-check at one scale.
    Tolerance loose because n=120 has finite-sample error.

    Uses ``numpy.random.default_rng(7)`` rather than CPython's
    ``random.gauss`` because the latter caches a Box-Muller pair
    across calls (sensitive to call sequence — fragile under future
    test infra changes); numpy's PCG64 is version-stable."""
    sigma = 1e-4
    rng = np.random.default_rng(7)
    returns = list(rng.normal(0.0, sigma, 120))
    bv = VolatilityEngine._bipower_variation(returns, VOL_WINDOW_15MIN)
    # 25% relative tolerance — n=120 is small for a sample-stat
    # convergence test, and BV's bias correction is exact only
    # asymptotically. 25% is wide enough to absorb the finite-sample
    # noise without masking a true bug.
    assert abs(bv - sigma) / sigma < 0.25
