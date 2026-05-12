"""Equivalence harness — VolatilityEngine instance-method coverage.

Pillar 5 fu (ticket 86b9vgxyf, 2026-05-10). Closes "Cluster A" of the
mutmut survivor map in ``bot/engines/volatility.py`` — the
``__init__`` + stateful instance-method surface (~lines 118-300) that
the original Pillar 3 harness (``test_volatility_engine.py``) left
dark because it only exercised the six pure ``@staticmethod`` math
kernels.

RCA — why Cluster A was dark
----------------------------
Constructing a ``VolatilityEngine`` requires a ``CoinbaseFeed`` +
optional ``DeribitDVOLFetcher``. In production, both open network
resources (websocket + HTTP polling thread) at ``__init__`` time.
The Pillar 3 harness intentionally avoided building an instance —
the math kernels are pure ``@staticmethod`` and don't need one, so
the harness called them directly via ``VolatilityEngine.<method>``
and never paid the ctor cost.

That left mutmut survivors across:

- ``__init__`` per-asset deque defaults — flipping ``maxlen=
  VOL_WINDOW_15MIN`` to a literal, swapping ``ASSETS`` iteration
  order, or initializing ``_jump_events`` to ``None`` are all silent
  because no test ever inspected the resulting attributes.
- ``_record_jump_event`` history-trim — ``len(events) >
  JUMP_MAX_HISTORY`` vs ``>=``, ``del events[:-N]`` vs
  ``del events[:N]``: every variant produces the same list LENGTH
  asymptotically; only behavioural tests catch the off-by-one.
- ``_adaptive_subsample_return`` modular arithmetic — flipping
  ``% JUMP_ADAPTIVE_SUBSAMPLE == 0`` to ``!= 0`` or to ``== 1``
  changes which 5s tick triggers a 15s emission, but the function
  returns either ``None`` or a sum of the last N — both look
  syntactically valid.
- ``_adaptive_jump_test`` threshold dispatch — the ``max(
  sigma_threshold, pctile_threshold)`` combine is a classic mutmut
  target (``min``, ``+``, returning either alone).
- ``_adaptive_decay_multiplier`` exponential-decay accumulator —
  ``math.exp(-(now - ts) / TAU)`` is dense with mutable constants
  (flipping the sign of ``-(now - ts)`` looks identical at the
  symbolic level but produces growth instead of decay).
- ``_estimate_beta`` — the clamp ``max(0.5, min(3.0, beta))`` and
  the ``var_r <= 0`` early-return are common survivor sites.

Closing the cluster
-------------------
This file constructs a ``VolatilityEngine`` via the
``volatility_engine_with_stubs`` fixture (added in
``conftest.py``) and exercises each of the above surfaces with
hand-picked deterministic inputs. The tests are **behavioural**, not
snapshot — extension 2 of the original ticket (snapshot the full
``_compute()`` output dict) requires snapshot regen and is filed
separately (per CLAUDE.md, snapshot regen is human-with-diff-review,
not autonomous).

Layered with property tests (hypothesis) for the static methods that
Cluster A also depends on transitively (the ``_realized_kernel`` +
``_bipower_variation`` path inside ``_compute()`` — Extension 3 of
the original ticket).
"""
from __future__ import annotations

import math
from typing import List

import pytest

# Heavy-dep mocking lives in conftest.py — runs at import.
import numpy as np
from hypothesis import HealthCheck, given, settings, strategies as st

from bot.constants import (
    BETA_LOOKBACK_RETURNS,
    JUMP_ADAPTIVE_DECAY_MAX_BOOST,
    JUMP_ADAPTIVE_DECAY_MIN_BOOST,
    JUMP_ADAPTIVE_DECAY_TAU,
    JUMP_ADAPTIVE_MAG_CAP,
    JUMP_ADAPTIVE_MAG_SCALE_BASE,
    JUMP_ADAPTIVE_MAX_HISTORY,
    JUMP_ADAPTIVE_SUBSAMPLE,
    JUMP_MAX_HISTORY,
    VOL_WINDOW_15MIN,
)
from bot.config import ASSETS


# ─────────────────────────────────────────────────────────────────────
#  __init__ — per-asset deque defaults
# ─────────────────────────────────────────────────────────────────────

def test_engine_init_per_asset_returns_deque_present(volatility_engine_with_stubs):
    """Every asset in ``ASSETS`` has an empty ``deque`` in
    ``_returns`` keyed at construction time. Catches a mutmut that
    initializes the dict to ``{}`` or skips the loop.
    """
    eng = volatility_engine_with_stubs
    for asset in ASSETS:
        assert asset in eng._returns, (
            f"Asset {asset} missing from _returns dict — __init__ may "
            f"have stopped iterating over ASSETS or replaced the loop "
            f"with an empty dict literal."
        )
        # Empty after construction (sidecar redirected to tmp_path)
        assert len(eng._returns[asset]) == 0, (
            f"_returns[{asset}] non-empty after construction; sidecar "
            f"isolation failed."
        )


def test_engine_init_returns_deque_has_correct_maxlen(volatility_engine_with_stubs):
    """The ``maxlen`` on every per-asset ``_returns`` deque is
    ``VOL_WINDOW_15MIN``. Catches a mutmut that swaps the constant
    for a literal or for ``VOL_WINDOW_5MIN``.
    """
    eng = volatility_engine_with_stubs
    for asset in ASSETS:
        assert eng._returns[asset].maxlen == VOL_WINDOW_15MIN, (
            f"_returns[{asset}].maxlen = {eng._returns[asset].maxlen}, "
            f"expected {VOL_WINDOW_15MIN}"
        )


def test_engine_init_jump_events_is_per_asset_list(volatility_engine_with_stubs):
    """``_jump_events`` initializes to ``{asset: []}`` — empty list,
    NOT ``None``. Catches a mutmut that flips the default factory.
    """
    eng = volatility_engine_with_stubs
    for asset in ASSETS:
        assert asset in eng._jump_events
        assert eng._jump_events[asset] == []
        assert isinstance(eng._jump_events[asset], list)


def test_engine_init_adaptive_state_zero(volatility_engine_with_stubs):
    """Tick counters and total-jumps counters start at zero for every
    asset. Catches a mutmut that initializes counters to 1, or that
    only sets BTC.
    """
    eng = volatility_engine_with_stubs
    for asset in ASSETS:
        assert eng._adaptive_tick_counter[asset] == 0
        assert eng._adaptive_total_jumps[asset] == 0
        assert eng._adaptive_ewma_var[asset] is None
        assert eng._adaptive_jump_events[asset] == []


# ─────────────────────────────────────────────────────────────────────
#  _record_jump_event — legacy history-trim invariant
# ─────────────────────────────────────────────────────────────────────

def test_record_jump_event_appends_in_order(volatility_engine_with_stubs):
    """``_record_jump_event`` appends timestamps in call order.
    Catches a mutmut that swaps ``append`` for ``insert(0, ...)``.
    """
    eng = volatility_engine_with_stubs
    eng._record_jump_event("BTC", 1000.0)
    eng._record_jump_event("BTC", 2000.0)
    eng._record_jump_event("BTC", 3000.0)
    assert eng._jump_events["BTC"] == [1000.0, 2000.0, 3000.0]


def test_record_jump_event_trims_to_max_history(volatility_engine_with_stubs):
    """When ``len(events) > JUMP_MAX_HISTORY``, the trim retains the
    LAST ``JUMP_MAX_HISTORY`` entries (oldest dropped). Catches a
    mutmut that uses ``>=`` (off-by-one) or ``del events[:N]``
    (drops newest, wrong direction).
    """
    eng = volatility_engine_with_stubs
    for i in range(JUMP_MAX_HISTORY + 5):
        eng._record_jump_event("ETH", float(i))
    events = eng._jump_events["ETH"]
    assert len(events) == JUMP_MAX_HISTORY, (
        f"Trim left {len(events)} events; expected {JUMP_MAX_HISTORY}"
    )
    # Last entry must be the most recent we recorded
    assert events[-1] == float(JUMP_MAX_HISTORY + 5 - 1)
    # First entry must be the (N+5 - JUMP_MAX_HISTORY)th — i.e., 5
    assert events[0] == 5.0


def test_record_jump_event_handles_missing_asset(volatility_engine_with_stubs):
    """If ``_jump_events`` is missing a key, the method initializes
    it lazily. Catches a mutmut that drops the ``if events is None``
    branch.
    """
    eng = volatility_engine_with_stubs
    eng._jump_events.pop("BTC", None)
    eng._record_jump_event("BTC", 42.0)
    assert eng._jump_events["BTC"] == [42.0]


# ─────────────────────────────────────────────────────────────────────
#  _record_adaptive_jump_event — adaptive history-trim invariant
# ─────────────────────────────────────────────────────────────────────

def test_record_adaptive_jump_event_appends_and_counts(volatility_engine_with_stubs):
    """``_record_adaptive_jump_event`` appends a ``(ts, boost)``
    tuple and increments ``_adaptive_total_jumps``. Boost is scaled
    by ``min(MAG_CAP, ratio) / MAG_SCALE_BASE * MAX_BOOST``.
    """
    eng = volatility_engine_with_stubs
    # Choose a ratio well below the cap so the cap branch doesn't fire
    ratio = JUMP_ADAPTIVE_MAG_CAP / 2.0
    assert ratio < JUMP_ADAPTIVE_MAG_CAP, "test premise violated"
    eng._record_adaptive_jump_event("SOL", 1000.0, ratio)
    events = eng._adaptive_jump_events["SOL"]
    assert len(events) == 1
    ts, boost = events[0]
    assert ts == 1000.0
    expected_boost = (
        JUMP_ADAPTIVE_DECAY_MAX_BOOST
        * ratio
        / JUMP_ADAPTIVE_MAG_SCALE_BASE
    )
    assert math.isclose(boost, expected_boost, rel_tol=1e-12)
    assert eng._adaptive_total_jumps["SOL"] == 1


def test_record_adaptive_jump_event_caps_ratio(volatility_engine_with_stubs):
    """The ratio is clamped to ``JUMP_ADAPTIVE_MAG_CAP`` before
    scaling. Catches a mutmut that drops the ``min`` or flips it to
    ``max``.
    """
    eng = volatility_engine_with_stubs
    huge_ratio = JUMP_ADAPTIVE_MAG_CAP * 10.0
    eng._record_adaptive_jump_event("XRP", 5.0, huge_ratio)
    _, boost = eng._adaptive_jump_events["XRP"][0]
    capped_expected = (
        JUMP_ADAPTIVE_DECAY_MAX_BOOST
        * JUMP_ADAPTIVE_MAG_CAP
        / JUMP_ADAPTIVE_MAG_SCALE_BASE
    )
    assert math.isclose(boost, capped_expected, rel_tol=1e-12)


def test_record_adaptive_jump_event_trims_to_max_history(volatility_engine_with_stubs):
    """History-trim mirrors the legacy detector — drop oldest when
    over ``JUMP_ADAPTIVE_MAX_HISTORY``.
    """
    eng = volatility_engine_with_stubs
    for i in range(JUMP_ADAPTIVE_MAX_HISTORY + 3):
        eng._record_adaptive_jump_event("BTC", float(i), 1.0)
    events = eng._adaptive_jump_events["BTC"]
    assert len(events) == JUMP_ADAPTIVE_MAX_HISTORY
    # Newest preserved
    assert events[-1][0] == float(JUMP_ADAPTIVE_MAX_HISTORY + 3 - 1)
    # Total jumps counter is NOT trimmed (cumulative)
    assert eng._adaptive_total_jumps["BTC"] == JUMP_ADAPTIVE_MAX_HISTORY + 3


# ─────────────────────────────────────────────────────────────────────
#  _adaptive_subsample_return — modular subsample emission
# ─────────────────────────────────────────────────────────────────────

def test_adaptive_subsample_return_none_until_subsample_tick(volatility_engine_with_stubs):
    """``_adaptive_subsample_return`` returns ``None`` on every tick
    except the SUBSAMPLE-th. Catches a mutmut on the ``%`` modulus.
    """
    eng = volatility_engine_with_stubs
    # Seed enough 5s returns so the buffer-length guard passes
    for i in range(JUMP_ADAPTIVE_SUBSAMPLE):
        eng._returns["BTC"].append(1e-5)
    # First (SUBSAMPLE - 1) ticks must return None
    for k in range(JUMP_ADAPTIVE_SUBSAMPLE - 1):
        out = eng._adaptive_subsample_return("BTC", 1e-5, 1000.0 + k)
        assert out is None, (
            f"Expected None on tick {k + 1} (< SUBSAMPLE); got {out}"
        )
    # SUBSAMPLE-th tick fires
    out = eng._adaptive_subsample_return("BTC", 1e-5, 2000.0)
    assert out is not None


def test_adaptive_subsample_return_sums_last_n_returns(volatility_engine_with_stubs):
    """Subsample output is the sum of the last ``SUBSAMPLE`` entries
    in ``_returns[asset]``. Catches a mutmut on the slice end or sum.
    """
    eng = volatility_engine_with_stubs
    # Seed exactly SUBSAMPLE distinct returns
    seeds = [0.001 * (i + 1) for i in range(JUMP_ADAPTIVE_SUBSAMPLE)]
    for r in seeds:
        eng._returns["ETH"].append(r)
    # Drive the counter to the SUBSAMPLE-th tick
    for _ in range(JUMP_ADAPTIVE_SUBSAMPLE - 1):
        eng._adaptive_subsample_return("ETH", 0.0, 0.0)
    out = eng._adaptive_subsample_return("ETH", 0.0, 0.0)
    assert math.isclose(out, sum(seeds), rel_tol=1e-12), (
        f"Expected sum {sum(seeds)}, got {out}"
    )


def test_adaptive_subsample_return_none_when_buffer_short(volatility_engine_with_stubs):
    """Even on a SUBSAMPLE-th tick, if ``len(_returns) < SUBSAMPLE``
    the function returns None. Catches a mutmut that drops the
    short-buffer guard.
    """
    eng = volatility_engine_with_stubs
    # Empty buffer — drive counter to SUBSAMPLE
    for _ in range(JUMP_ADAPTIVE_SUBSAMPLE):
        out = eng._adaptive_subsample_return("XRP", 1e-5, 0.0)
    # Last call was the SUBSAMPLE-th tick but buffer is empty
    assert out is None


# ─────────────────────────────────────────────────────────────────────
#  _adaptive_jump_test — EWMA + percentile threshold dispatch
# ─────────────────────────────────────────────────────────────────────

def test_adaptive_jump_test_warmup_returns_no_jump(volatility_engine_with_stubs):
    """Before ``JUMP_ADAPTIVE_EWMA_INIT_RETURNS`` observations, the
    test always returns ``is_jump=False`` — the EWMA hasn't seeded.
    Catches a mutmut that drops the warmup guard.
    """
    eng = volatility_engine_with_stubs
    result = eng._adaptive_jump_test("BTC", 0.05, 1000.0)  # huge return
    # warmup → is_jump must be False even with a 5% return
    assert result["is_jump"] is False


def test_adaptive_jump_test_ewma_updates_after_init(volatility_engine_with_stubs):
    """After enough observations to seed the EWMA, ``ewma_var``
    becomes non-None and ``ewma_sigma`` > 0.
    """
    eng = volatility_engine_with_stubs
    # Feed 5 small returns to seed sample variance
    for r in [1e-5, -1e-5, 2e-5, -2e-5, 1.5e-5]:
        eng._adaptive_jump_test("ETH", r, 0.0)
    assert eng._adaptive_ewma_var["ETH"] is not None
    assert eng._adaptive_ewma_var["ETH"] > 0


def test_adaptive_jump_test_result_dict_keys(volatility_engine_with_stubs):
    """The result dict has a fixed key schema — catches a mutmut
    that renames a key (silently breaks the downstream `update()`
    consumer which reads by name).
    """
    eng = volatility_engine_with_stubs
    result = eng._adaptive_jump_test("SOL", 1e-5, 1.0)
    expected_keys = {
        "is_jump", "ewma_sigma", "sigma_threshold", "pctile_threshold",
        "effective_threshold", "magnitude_ratio", "n_obs_15s", "return_15s",
    }
    assert set(result.keys()) == expected_keys


def test_adaptive_jump_test_n_obs_increments(volatility_engine_with_stubs):
    """``n_obs_15s`` in the result equals the post-call buffer length —
    catches a mutmut that returns the pre-call count.
    """
    eng = volatility_engine_with_stubs
    r1 = eng._adaptive_jump_test("XRP", 1e-5, 1.0)
    assert r1["n_obs_15s"] == 1
    r2 = eng._adaptive_jump_test("XRP", 2e-5, 2.0)
    assert r2["n_obs_15s"] == 2


# ─────────────────────────────────────────────────────────────────────
#  _adaptive_decay_multiplier — exponential-decay accumulator
# ─────────────────────────────────────────────────────────────────────

def test_adaptive_decay_multiplier_no_events_is_unity(volatility_engine_with_stubs):
    """Empty event list → ``(1.0, "normal")``. Catches a mutmut that
    flips the early-return to ``(0.0, ...)`` or drops it entirely.
    """
    eng = volatility_engine_with_stubs
    mult, regime = eng._adaptive_decay_multiplier("BTC", 1000.0)
    assert mult == 1.0
    assert regime == "normal"


def test_adaptive_decay_multiplier_fresh_event_elevated(volatility_engine_with_stubs):
    """A fresh adaptive-jump event (boost above MIN_BOOST) flips the
    regime to ``"elevated"`` and the multiplier above 1.0.
    """
    eng = volatility_engine_with_stubs
    # Inject a single jump event with a large boost at t=1000
    big_boost = JUMP_ADAPTIVE_DECAY_MIN_BOOST * 100.0
    eng._adaptive_jump_events["ETH"] = [(1000.0, big_boost)]
    mult, regime = eng._adaptive_decay_multiplier("ETH", 1000.0)
    assert regime == "elevated"
    assert mult > 1.0
    # At t = ts, decay factor is exp(0) = 1.0 — multiplier ≈ 1 + boost
    assert math.isclose(mult, 1.0 + big_boost, rel_tol=1e-12)


def test_adaptive_decay_multiplier_decays_with_age(volatility_engine_with_stubs):
    """Multiplier strictly decreases as ``now - ts`` grows (boost
    decays via ``exp(-Δ / TAU)``). Catches a mutmut that flips the
    sign of the exponent or replaces ``exp`` with another function.
    """
    eng = volatility_engine_with_stubs
    boost = JUMP_ADAPTIVE_DECAY_MIN_BOOST * 100.0
    eng._adaptive_jump_events["SOL"] = [(1000.0, boost)]
    mult_t0, _ = eng._adaptive_decay_multiplier("SOL", 1000.0)
    mult_t1, _ = eng._adaptive_decay_multiplier("SOL", 1000.0 + JUMP_ADAPTIVE_DECAY_TAU)
    mult_t2, _ = eng._adaptive_decay_multiplier("SOL", 1000.0 + 5 * JUMP_ADAPTIVE_DECAY_TAU)
    assert mult_t0 > mult_t1 > mult_t2, (
        f"Decay not monotone: t0={mult_t0}, t1={mult_t1}, t2={mult_t2}"
    )


def test_adaptive_decay_multiplier_decays_to_normal(volatility_engine_with_stubs):
    """After enough decay tau-multiples, the remaining boost falls
    below MIN_BOOST and the regime returns to ``"normal"``.
    """
    eng = volatility_engine_with_stubs
    boost = JUMP_ADAPTIVE_DECAY_MIN_BOOST * 2.0  # barely above threshold
    eng._adaptive_jump_events["XRP"] = [(0.0, boost)]
    # 100x tau is well beyond the half-life; total_boost ≈ boost * exp(-100)
    mult, regime = eng._adaptive_decay_multiplier("XRP", 100.0 * JUMP_ADAPTIVE_DECAY_TAU)
    assert regime == "normal"
    assert mult == 1.0


def test_adaptive_decay_multiplier_skips_future_events(volatility_engine_with_stubs):
    """Events with ``ts > now`` are skipped from the decay sum.
    Catches a mutmut that flips the ``ts <= now`` guard.
    """
    eng = volatility_engine_with_stubs
    boost = JUMP_ADAPTIVE_DECAY_MIN_BOOST * 100.0
    # Event in the future
    eng._adaptive_jump_events["BTC"] = [(2000.0, boost)]
    mult, regime = eng._adaptive_decay_multiplier("BTC", 1000.0)
    assert regime == "normal"
    assert mult == 1.0


def test_adaptive_decay_multiplier_capped(volatility_engine_with_stubs):
    """Multi-event accumulation is capped at ``JUMP_ADAPTIVE_DECAY_CAP``.
    Catches a mutmut that drops the cap.
    """
    from bot.constants import JUMP_ADAPTIVE_DECAY_CAP

    eng = volatility_engine_with_stubs
    # Pile on many fresh huge boosts
    huge_boost = JUMP_ADAPTIVE_DECAY_CAP * 10.0
    eng._adaptive_jump_events["ETH"] = [(1000.0, huge_boost)] * 20
    mult, regime = eng._adaptive_decay_multiplier("ETH", 1000.0)
    assert mult == JUMP_ADAPTIVE_DECAY_CAP
    assert regime == "elevated"


# ─────────────────────────────────────────────────────────────────────
#  _estimate_beta — cross-asset cov/var ratio + clamping
# ─────────────────────────────────────────────────────────────────────

def test_estimate_beta_self_is_one(volatility_engine_with_stubs):
    """``_estimate_beta(asset, asset)`` short-circuits to 1.0.
    Catches a mutmut on the equality check.
    """
    eng = volatility_engine_with_stubs
    assert eng._estimate_beta("BTC", "BTC") == 1.0


def test_estimate_beta_insufficient_data_returns_one(volatility_engine_with_stubs):
    """When neither asset has ≥ 10 returns, beta defaults to 1.0.
    """
    eng = volatility_engine_with_stubs
    # Seed only 5 returns
    for r in [1e-5] * 5:
        eng._returns["ETH"].append(r)
        eng._returns["BTC"].append(r)
    assert eng._estimate_beta("ETH", "BTC") == 1.0


def test_estimate_beta_perfect_correlation_at_unit_slope(volatility_engine_with_stubs):
    """If ETH returns == BTC returns exactly, beta = 1.0 (within the
    clamp). Catches a mutmut on the covariance computation.
    """
    eng = volatility_engine_with_stubs
    rng = np.random.default_rng(7)
    returns = list(rng.normal(0.0, 1e-4, 60))
    for r in returns:
        eng._returns["BTC"].append(r)
        eng._returns["ETH"].append(r)
    beta = eng._estimate_beta("ETH", "BTC")
    assert math.isclose(beta, 1.0, abs_tol=1e-9)


def test_estimate_beta_clamped_above(volatility_engine_with_stubs):
    """Beta is clamped to 3.0 above. Catches a mutmut on the
    upper-bound constant.
    """
    eng = volatility_engine_with_stubs
    rng = np.random.default_rng(11)
    btc_returns = list(rng.normal(0.0, 1e-4, 60))
    # SOL returns = 10x BTC → raw beta = 10, clamp to 3.0
    sol_returns = [r * 10.0 for r in btc_returns]
    for rb, rs in zip(btc_returns, sol_returns):
        eng._returns["BTC"].append(rb)
        eng._returns["SOL"].append(rs)
    beta = eng._estimate_beta("SOL", "BTC")
    assert beta == 3.0


def test_estimate_beta_clamped_below(volatility_engine_with_stubs):
    """Beta is clamped to 0.5 below. Catches a mutmut on the
    lower-bound constant or the ``min``/``max`` ordering.
    """
    eng = volatility_engine_with_stubs
    rng = np.random.default_rng(13)
    btc_returns = list(rng.normal(0.0, 1e-4, 60))
    # XRP returns = 0.1x BTC → raw beta = 0.1, clamp to 0.5
    xrp_returns = [r * 0.1 for r in btc_returns]
    for rb, rx in zip(btc_returns, xrp_returns):
        eng._returns["BTC"].append(rb)
        eng._returns["XRP"].append(rx)
    beta = eng._estimate_beta("XRP", "BTC")
    assert beta == 0.5


def test_estimate_beta_zero_variance_ref_returns_one(volatility_engine_with_stubs):
    """If the reference asset's variance is 0 (constant returns), the
    function returns 1.0 (var_r <= 0 early-return). Catches a mutmut
    that drops the guard.

    Uses ``0.0`` returns (true zero variance — no FP residual) rather
    than a tiny constant; ``sum([1e-5]*60)/60`` produces a mean that
    isn't byte-identical to ``1e-5``, so ``(r - mean)`` leaves a
    sub-ULP residual that re-squares to a non-zero variance and
    sidesteps the ``var_r <= 0`` guard.
    """
    eng = volatility_engine_with_stubs
    for r in [0.0] * 60:
        eng._returns["BTC"].append(r)  # constant — exact-zero variance
    rng = np.random.default_rng(17)
    for r in rng.normal(0.0, 1e-4, 60):
        eng._returns["ETH"].append(float(r))
    assert eng._estimate_beta("ETH", "BTC") == 1.0


# ─────────────────────────────────────────────────────────────────────
#  Property tests (hypothesis) — Extension 3
# ─────────────────────────────────────────────────────────────────────

# 15-second log returns (sum of three 5-second returns) rarely exceed
# ±10%; cap the strategy at ±0.10 so synthetic inputs match the
# adaptive detector's actual call regime.
_R15S_STRAT = st.floats(min_value=-0.10, max_value=0.10,
                        allow_nan=False, allow_infinity=False)
_BOOST_STRAT = st.floats(min_value=0.0, max_value=10.0,
                         allow_nan=False, allow_infinity=False)
_AGE_STRAT = st.floats(min_value=0.0, max_value=10 * JUMP_ADAPTIVE_DECAY_TAU,
                       allow_nan=False, allow_infinity=False)


@given(r=_R15S_STRAT, t=st.floats(min_value=0.0, max_value=1e6,
                                  allow_nan=False, allow_infinity=False))
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_adaptive_jump_test_result_invariants(volatility_engine_with_stubs, r, t):
    """``_adaptive_jump_test`` always returns finite floats for all
    threshold-related fields (or ``+inf`` for the warmup case).
    ``n_obs_15s`` is always a positive int. ``is_jump`` is bool.
    """
    eng = volatility_engine_with_stubs
    out = eng._adaptive_jump_test("BTC", r, t)
    assert isinstance(out["is_jump"], bool)
    assert isinstance(out["n_obs_15s"], int)
    assert out["n_obs_15s"] >= 1
    assert out["return_15s"] == r
    # ewma_sigma is non-negative finite
    assert out["ewma_sigma"] >= 0.0
    assert math.isfinite(out["ewma_sigma"])
    # magnitude_ratio is non-negative (could be 0 in warmup)
    assert out["magnitude_ratio"] >= 0.0


@given(boost=_BOOST_STRAT, age=_AGE_STRAT)
@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_adaptive_decay_multiplier_bounded(volatility_engine_with_stubs, boost, age):
    """Output is always in ``[1.0, JUMP_ADAPTIVE_DECAY_CAP]``.
    The regime is one of ``{"normal", "elevated"}``.
    """
    from bot.constants import JUMP_ADAPTIVE_DECAY_CAP

    eng = volatility_engine_with_stubs
    eng._adaptive_jump_events["BTC"] = [(0.0, boost)]
    mult, regime = eng._adaptive_decay_multiplier("BTC", age)
    assert 1.0 <= mult <= JUMP_ADAPTIVE_DECAY_CAP
    assert regime in ("normal", "elevated")
    if regime == "normal":
        assert mult == 1.0
    else:
        assert mult > 1.0


@given(boosts=st.lists(_BOOST_STRAT, min_size=0, max_size=5),
       now=st.floats(min_value=0.0, max_value=1e3,
                     allow_nan=False, allow_infinity=False))
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_adaptive_decay_multiplier_monotone_in_age(volatility_engine_with_stubs,
                                                    boosts, now):
    """For any event list, the multiplier at ``now + Δ`` is ≤ the
    multiplier at ``now`` for Δ ≥ 0 (boosts can only decay, never
    grow).
    """
    eng = volatility_engine_with_stubs
    eng._adaptive_jump_events["ETH"] = [(0.0, b) for b in boosts]
    mult_now, _ = eng._adaptive_decay_multiplier("ETH", now)
    mult_later, _ = eng._adaptive_decay_multiplier("ETH", now + 100.0)
    assert mult_later <= mult_now + 1e-12, (
        f"Multiplier grew with age: now={mult_now}, later={mult_later}"
    )


@given(returns=st.lists(
    st.floats(min_value=-0.01, max_value=0.01,
              allow_nan=False, allow_infinity=False),
    min_size=10, max_size=BETA_LOOKBACK_RETURNS,
))
@settings(max_examples=50, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_estimate_beta_bounded_for_random_pairs(volatility_engine_with_stubs, returns):
    """For any random pair of return series of equal length, the
    output beta is in ``[0.5, 3.0]`` (the clamp). Use ETH = BTC
    scaled by a random factor — beta should reflect the scale within
    the clamp.
    """
    eng = volatility_engine_with_stubs
    # Reset buffers (autouse sidecar fixture already redirected paths,
    # but the same fixture instance can carry buffers across hypothesis
    # examples within one test).
    eng._returns["BTC"].clear()
    eng._returns["ETH"].clear()
    for r in returns:
        eng._returns["BTC"].append(r)
        eng._returns["ETH"].append(r * 1.5)  # raw beta should be ≈ 1.5
    beta = eng._estimate_beta("ETH", "BTC")
    assert 0.5 <= beta <= 3.0
