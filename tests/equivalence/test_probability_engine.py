"""Equivalence harness — ProbabilityEngine.

Pillar 3 of the testing-foundation-sprint
(kb/decisions/testing-foundation-sprint-may09.md). Two layers:

1. **Corpus snapshot** — runs ``ProbabilityEngine.compute()`` across
   every row in ``tests/fixtures/engine_inputs.parquet`` (1000
   stratified inputs from production state.db) and pins numeric
   outputs via pytest-regressions ``num_regression``. The
   ``isolate_calibration_singletons`` autouse fixture pins the
   mutable ``_CALIBRATION_ENGINE`` to None so the snapshot exercises
   the deterministic passthrough/fixed-beta cascade. Catches
   behavioral drift across engine extractions that signature-only
   griffe / AST gates miss.

2. **Property tests (hypothesis)** — the four pure static helpers
   (``_cdf_complement``, ``_dynamic_cap``, ``_calibrate``) under
   hand-bounded strategies. Asserts invariants (probability ∈ [0, 1],
   cap ≤ MAX_EFFECTIVE_PROB, etc.) on synthetic inputs that exceed
   the corpus's coverage. Hand-written strategies — not polyfactory
   — because engine inputs are tightly bounded (price ∈ [0.01, 0.99]
   on Kalshi-cents, volatility positive but small).

Snapshot regeneration: see ``tests/equivalence/REGEN.md``. Never
auto-regen from an agent run — diff-then-commit only.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

import pytest

# Heavy-dep mocking lives in conftest.py — runs at import.

import numpy as np
from hypothesis import example, given, settings, strategies as st


# ── Engine import ────────────────────────────────────────────────────
# The proxy chain `bot._BotProxy → bot._impl.ProbabilityEngine →
# bot.engines.ProbabilityEngine → bot.engines.probability.ProbabilityEngine`
# is the documented public path (Bit 6.2). Importing from
# `bot.engines.probability` directly would skip the late-binding
# fixture wiring on `bot._impl`, so we go through `bot`.
from bot import ProbabilityEngine
from config import MAX_EFFECTIVE_PROB


# ─────────────────────────────────────────────────────────────────────
#  CORPUS SNAPSHOT
# ─────────────────────────────────────────────────────────────────────

# Fields snapshotted as numbers (NaN-coerced). Categorical fields
# (calibration_method, tradeable, reason) go through data_regression
# in a separate test so num_regression doesn't choke on strings.
#
# Deliberately excluded — ``shadow_cal_prob`` and
# ``shadow_cal_temperature`` are populated only inside the
# *registry-engine learned-method* branch of compute()
# (probability.py:209-217). The autouse
# ``isolate_calibration_singletons`` fixture pins the registry engine
# to None, so under this harness those fields are guaranteed-None for
# every row — including them as numeric columns produces a wall of
# NaN that snapshots without information and misleads reviewers about
# coverage. Bit 6.3 owners: when the learned-method branch is
# exercised, add these fields back AND make the cohort split explicit
# in the test name.
_NUMERIC_FIELDS = (
    "z_score",
    "raw_prob",
    "calibrated_prob",
)


def _to_nan(x: Any) -> float:
    """num_regression compares NaN as equal-to-NaN; production
    ``ProbabilityEngine`` returns Python None on invalid inputs.
    Coerce so the snapshot is array-shaped."""
    if x is None:
        return float("nan")
    return float(x)


def test_probability_engine_compute_corpus_numeric(num_regression, probability_corpus):
    """Pin numeric outputs of compute() across the 1000-row corpus."""
    columns: Dict[str, List[float]] = {f: [] for f in _NUMERIC_FIELDS}

    for row in probability_corpus:
        result = ProbabilityEngine.compute(
            spot=row["spot_price"],
            threshold=row["threshold"],
            seconds_remaining=row["seconds_to_close"],
            blended_rv=row["volatility"],
            market_price_cents=row["market_price"],
            asset=row["asset"],
            product_type=row["product_type"],
        )
        for f in _NUMERIC_FIELDS:
            columns[f].append(_to_nan(result.get(f)))

    arrays = {k: np.asarray(v, dtype=np.float64) for k, v in columns.items()}
    # rtol=1e-9 sits 3 orders of magnitude below the engine's own
    # rounding floor (``round(z_score, 4)`` and
    # ``round(raw_prob/calibrated_prob, 6)`` in probability.py) and 3
    # orders of magnitude above the typical scipy-version float drift
    # for ``student_t.cdf`` / ``norminvgauss.cdf`` (~1e-13 to 1e-11
    # across 1.10/1.11/1.12 minor releases). Tighter would flake on
    # scipy upgrades; looser would mask real engine drift. If a future
    # refactor touches a rounding boundary, the snapshot will fail
    # loudly — review the diff.
    num_regression.check(arrays, default_tolerance={"rtol": 1e-9, "atol": 0.0})


def test_probability_engine_compute_corpus_categorical(data_regression, probability_corpus):
    """Pin categorical outputs (calibration_method, tradeable, reason)
    via data_regression's YAML serializer. Uses hashed buckets so the
    snapshot is constant-size regardless of corpus size."""
    from collections import Counter

    methods: Counter = Counter()
    tradeable: Counter = Counter()
    reasons: Counter = Counter()

    for row in probability_corpus:
        result = ProbabilityEngine.compute(
            spot=row["spot_price"],
            threshold=row["threshold"],
            seconds_remaining=row["seconds_to_close"],
            blended_rv=row["volatility"],
            market_price_cents=row["market_price"],
            asset=row["asset"],
            product_type=row["product_type"],
        )
        methods[result.get("calibration_method") or "<none>"] += 1
        tradeable[bool(result.get("tradeable"))] += 1
        # reasons are free-form, often include numbers — hash by prefix
        # so a number-formatting change doesn't ratchet the snapshot.
        reason = result.get("reason") or ""
        bucket = reason.split(" ", 1)[0] if reason else "<empty>"
        reasons[bucket] += 1

    data_regression.check({
        "calibration_method": dict(sorted(methods.items())),
        "tradeable": {str(k): v for k, v in sorted(tradeable.items())},
        "reason_first_token": dict(sorted(reasons.items())),
    })


def test_probability_engine_corpus_invariants(probability_corpus):
    """Cross-corpus invariants. Cheap, sub-second, no snapshot.

    These are stronger than the snapshot in one direction (every row
    is checked individually) and weaker in another (no equality
    pinning, just bounds). Together they're complementary."""
    for row in probability_corpus:
        result = ProbabilityEngine.compute(
            spot=row["spot_price"],
            threshold=row["threshold"],
            seconds_remaining=row["seconds_to_close"],
            blended_rv=row["volatility"],
            market_price_cents=row["market_price"],
            asset=row["asset"],
            product_type=row["product_type"],
        )
        # raw_prob and calibrated_prob are Optional but when set
        # MUST be on [0, 1]. tradeable is always bool.
        for f in ("raw_prob", "calibrated_prob"):
            v = result.get(f)
            if v is not None:
                assert 0.0 <= v <= 1.0, (
                    f"{f}={v} outside [0,1] for asset={row['asset']} "
                    f"product_type={row['product_type']}"
                )
        assert isinstance(result.get("tradeable"), bool)


# ─────────────────────────────────────────────────────────────────────
#  PROPERTY TESTS (hypothesis)
# ─────────────────────────────────────────────────────────────────────

# Hand-bounded strategies. polyfactory would generate impossible inputs
# (negative prices, sec_remaining > year) without filter overhead;
# explicit bounds match what the bot actually feeds the engine. Bounds
# derived from the observed 30-day corpus (see
# ``tests/fixtures/engine_inputs.meta.json`` strata block) — not
# narrower than the corpus, otherwise the property test claims hold
# only for crypto/15m/hourly while weather windows (24h, sec=86399)
# and elevated-vol regimes (vol up to ~0.11) escape.
_PRICE_STRAT = st.floats(min_value=0.01, max_value=200_000.0,
                          allow_nan=False, allow_infinity=False)
# Up to 24h (weather product_type windows close once per day)
_SECS_STRAT = st.floats(min_value=1.0, max_value=86400.0,
                         allow_nan=False, allow_infinity=False)
# Up to 0.15 (≈3x the most extreme elevated-regime corpus value to
# leave headroom for future regime classifier changes)
_RV_STRAT = st.floats(min_value=1e-7, max_value=0.15,
                       allow_nan=False, allow_infinity=False)
_ASSET_STRAT = st.sampled_from(["BTC", "ETH", "SOL", "XRP", None])
# "sports" is a real product_type in market_config.MARKET_CONFIGS — included
# for property-test branch coverage of `_dynamic_cap`'s
# ("hourly","spx_hourly","weather") vs everything-else schedule split.
# Note: the corpus parquet has no sports rows (sports is observation-only and
# does not generate evaluated_opportunities entries today), so sports inputs
# come exclusively from this property test, not the corpus snapshot.
_PTYPE_STRAT = st.sampled_from(["15m", "hourly", "spx_hourly", "weather", "sports", None])
_PROB_STRAT = st.floats(min_value=0.0, max_value=1.0,
                         allow_nan=False, allow_infinity=False)


@given(
    spot=_PRICE_STRAT, threshold=_PRICE_STRAT,
    seconds_remaining=_SECS_STRAT, blended_rv=_RV_STRAT,
    asset=_ASSET_STRAT, product_type=_PTYPE_STRAT,
)
@settings(max_examples=200, deadline=None)
def test_compute_bounded_outputs(spot, threshold, seconds_remaining,
                                 blended_rv, asset, product_type):
    """compute() must return a probability in [0, 1] (or None) for every
    bounded input — never a NaN/inf, never out of range."""
    result = ProbabilityEngine.compute(
        spot=spot, threshold=threshold,
        seconds_remaining=seconds_remaining, blended_rv=blended_rv,
        asset=asset, product_type=product_type,
    )
    for f in ("raw_prob", "calibrated_prob"):
        v = result.get(f)
        if v is not None:
            assert 0.0 <= v <= 1.0
            assert not math.isnan(v)
            assert not math.isinf(v)
    assert isinstance(result.get("tradeable"), bool)


@given(z=st.floats(min_value=-10.0, max_value=10.0,
                    allow_nan=False, allow_infinity=False),
       asset=_ASSET_STRAT)
@settings(max_examples=200, deadline=None)
def test_cdf_complement_in_unit_interval(z, asset):
    """1 - CDF(z) ∈ [0, 1] for every finite z and any asset routing."""
    val = ProbabilityEngine._cdf_complement(z, asset)
    assert 0.0 <= val <= 1.0
    assert not math.isnan(val)


@given(seconds_remaining=_SECS_STRAT, product_type=_PTYPE_STRAT)
@settings(max_examples=200, deadline=None)
def test_dynamic_cap_bounded(seconds_remaining, product_type):
    """_dynamic_cap returns a probability cap in (0.5, 1] — always
    > 50% (no buy-side cap below the no-edge line) and never > 1."""
    cap = ProbabilityEngine._dynamic_cap(seconds_remaining, product_type=product_type)
    assert 0.5 < cap <= 1.0


@given(raw_prob=_PROB_STRAT)
@example(raw_prob=0.5)  # ensure the fixed-point assertion below actually fires
@settings(max_examples=200, deadline=None)
def test_calibrate_idempotent_on_unit(raw_prob):
    """``_calibrate`` (fixed β=0.85 logistic) must:
       * return a probability in [0, MAX_EFFECTIVE_PROB]
       * preserve the 0.5 fixed point (logit(0.5) = 0)

    The ``@example(raw_prob=0.5)`` decorator forces hypothesis to
    include the fixed point in every run — without it, the
    ``abs(raw_prob - 0.5) < 1e-9`` guard is essentially dead code
    under hypothesis's default float strategy on [0, 1].
    """
    calibrated = ProbabilityEngine._calibrate(raw_prob)
    assert 0.0 <= calibrated <= MAX_EFFECTIVE_PROB

    # Fixed-point check at 0.5 (within float tolerance — BETA_SLOPE
    # scales the logit and inverse-logit both, so 0.5 maps to 0.5).
    if abs(raw_prob - 0.5) < 1e-9:
        assert abs(calibrated - 0.5) < 1e-9


def test_calibrate_monotone():
    """Spot-check monotonicity: ``_calibrate`` is monotonically
    non-decreasing in raw_prob. Property test would also catch this
    but the spot-check is cheaper to read in a postmortem."""
    grid = [i / 100.0 for i in range(0, 101)]
    out = [ProbabilityEngine._calibrate(p) for p in grid]
    for a, b in zip(out, out[1:]):
        assert b + 1e-12 >= a, f"non-monotone: {a} → {b}"


def test_counterfactual_prob_corpus_numeric(num_regression, probability_corpus):
    """Pin numeric outputs of ``counterfactual_prob`` across the
    corpus. The method shares the late-binding pattern with
    ``compute()`` (reads ``_bot_impl._CALIBRATION_ENGINE`` at call
    time), so a refactor of that pattern would silently drift
    ``counterfactual_prob`` if ``compute()`` were the only pinned
    surface. The counterfactual uses an *alternative* blended_rv —
    we synthesize one as 1.5× the row's actual ``volatility`` so the
    snapshot exercises a real value, not the same as ``compute()``."""
    counterfactuals: List[float] = []
    for row in probability_corpus:
        val = ProbabilityEngine.counterfactual_prob(
            spot=row["spot_price"],
            threshold=row["threshold"],
            seconds_remaining=row["seconds_to_close"],
            alt_blended_rv=row["volatility"] * 1.5,
            asset=row["asset"],
            product_type=row["product_type"],
        )
        counterfactuals.append(_to_nan(val))
    arr = np.asarray(counterfactuals, dtype=np.float64)
    num_regression.check({"counterfactual_prob": arr},
                         default_tolerance={"rtol": 1e-9, "atol": 0.0})
