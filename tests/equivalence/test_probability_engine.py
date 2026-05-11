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
# Post-Bit-9.3-iii.b (2026-05-11) `_BotProxy` is retired; `bot.engines.probability`
# is the canonical home for `ProbabilityEngine` (Bit 6.2 extraction). The
# `import bot._impl` below pre-loads star-imports the equivalence harness
# needs via the residual shim (kept until Bit 9.3-iii.c).
from bot.engines.probability import ProbabilityEngine
from config import MAX_EFFECTIVE_PROB
import bot._impl  # noqa: F401  -- pre-loads star-imports for fixture wiring


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
    corpus. The method shares the calibration-singleton access pattern
    with ``compute()`` (post-Bit-6.3 path-B: reads
    ``_cal_state._CALIBRATION_ENGINE`` via the top-level
    ``from bot.engines import calibration as _cal_state`` alias),
    so a refactor of that pattern would silently drift
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


# ─────────────────────────────────────────────────────────────────────
#  CALIBRATED CASCADE SNAPSHOTS (Pillar 5 follow-up, 86b9vgxxz)
# ─────────────────────────────────────────────────────────────────────
# The default autouse fixture (``isolate_calibration_singletons``) pins
# the calibrator to None, so the snapshots above exercise only outcomes
# 4 + 5 of the ``ProbabilityEngine.compute()`` cascade (passthrough +
# fixed_beta). The mutmut baseline against ``bot/engines/probability.py``
# left the calibrated-tail mutants (range ~1228-1318 — every branch under
# the ``_reg_engine is not None`` and ``_CALIBRATION_ENGINE is not None``
# elif's, including the CAL_CLAMP / CAL_CLAMP_BLEND / DISCREPANCY tails)
# unkilled because no test ran code through them.
#
# These tests opt-in to the ``install_frozen_cal_engine`` and
# ``install_legacy_only_cal_engine`` fixtures from conftest.py to wire a
# deterministic Platt(A=0.85, B=0.0, trained=True) oracle into the
# cascade, then pin numeric + categorical outputs. The frozen-state
# choice (state file hand-crafted, loaded via the ordinary
# ``CalibrationEngine(state_path=...)`` ctor) makes the snapshot
# reproducible across machines and across calibration retrains in
# production: the snapshot binds to the FROZEN state dict, not to the
# live ``calibration_state.json`` on the VPS.
#
# Snapshot regen rule still applies — if these fail, investigate the
# divergence; never run ``pytest --force-regen`` autonomously.

# Numeric fields snapshotted under the frozen oracle. ``shadow_cal_prob``
# / ``shadow_cal_temperature`` populate only on outcome-1 rows (registry
# learned-method), so they live in the oracle test, not the default one.
_NUMERIC_FIELDS_ORACLE = (
    "z_score",
    "raw_prob",
    "calibrated_prob",
    "shadow_cal_prob",
    "shadow_cal_temperature",
)


def test_probability_engine_compute_corpus_numeric_with_oracle(
        num_regression, probability_corpus, install_frozen_cal_engine):
    """Pin numeric outputs of ``compute()`` across the 1000-row corpus
    with the frozen Platt oracle installed in both the registry and the
    legacy slot. Every row hits outcome 1 of the cascade because the
    patched ``_resolve_cal_engine`` returns the frozen engine regardless
    of ``product_type`` / ``cal_engine_enabled``.

    Coverage delta vs the default-fixture snapshot: exercises the
    ``_reg_engine.calibrate(...)`` call site, the shadow temperature
    branch, the CAL_CLAMP post-hoc clamp (raw_prob < 0.70 + delta >
    0.05), and the DISCREPANCY check (when calibrated > 0.93 and market
    < 70¢). The frozen engine's ``_apply_uncertainty_shrinkage`` is
    deterministic because no observations are loaded (``n < 50`` →
    ``u = 0.05``). CAL_CLAMP_BLEND (lines 249-258) does NOT fire under
    this fixture — 0 corpus rows land in the 0.70 ≤ raw_prob < 0.85
    band with delta > 0.05; tracked as a known blind spot in
    ``kb/findings/mutmut-baseline-may09.md``.
    """
    columns: Dict[str, List[float]] = {f: [] for f in _NUMERIC_FIELDS_ORACLE}

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
        for f in _NUMERIC_FIELDS_ORACLE:
            columns[f].append(_to_nan(result.get(f)))

    arrays = {k: np.asarray(v, dtype=np.float64) for k, v in columns.items()}
    # Same tolerance rationale as the default-fixture snapshot — see
    # ``test_probability_engine_compute_corpus_numeric``.
    num_regression.check(arrays, default_tolerance={"rtol": 1e-9, "atol": 0.0})


def test_probability_engine_compute_corpus_categorical_with_oracle(
        data_regression, probability_corpus, install_frozen_cal_engine):
    """Pin categorical outputs (``calibration_method``, ``tradeable``,
    ``reason``) with the frozen oracle installed. The
    ``calibration_method`` distribution should be entirely of the form
    ``"{product_type}_platt"`` (set by the outcome-1 branch:
    ``f"{product_type}_{_reg_engine.active_method}"``), with
    DISCREPANCY-flagged rows surfacing in the ``reason_first_token``
    bucket as ``"model"``."""
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
        reason = result.get("reason") or ""
        bucket = reason.split(" ", 1)[0] if reason else "<empty>"
        reasons[bucket] += 1

    data_regression.check({
        "calibration_method": dict(sorted(methods.items())),
        "tradeable": {str(k): v for k, v in sorted(tradeable.items())},
        "reason_first_token": dict(sorted(reasons.items())),
    })


def test_probability_engine_compute_corpus_numeric_legacy_only(
        num_regression, probability_corpus, install_legacy_only_cal_engine):
    """Pin numeric outputs of ``compute()`` with the frozen Platt oracle
    wired into the LEGACY ``_CALIBRATION_ENGINE`` slot only —
    ``_resolve_cal_engine`` stays at the autouse-null stub AND
    ``FIFTEEN_M_CALIBRATION_ENABLED`` stays at the production-default
    False.

    **Coverage scope — corrected R1**: forces the cascade past
    outcome 1 (registry resolver returns None) into the legacy elif at
    line 216. The ``FIFTEEN_M_CALIBRATION_ENABLED=False`` short-circuit
    at line 217 then forces 15m rows past outcome 2 (the learned-method
    body at lines 218-220, structurally unreachable here) into
    **outcome 3** — passthrough with BLR_BYPASS diagnostic (lines
    222-230). Non-15m rows (``cal_eligible=False``) fall through to
    outcome 4 (passthrough at lines 231-233).

    Empirical: under this fixture, 248 of 303 15m rows produce
    ``calibrated_prob == raw_prob`` exactly, the other 55 produce
    ``calibrated_prob == min(raw_prob, dynamic_cap)``. None show the
    frozen-Platt sigmoid signature.

    For actual outcome 2 coverage, see
    ``test_probability_engine_compute_corpus_numeric_outcome_2`` below.

    ``shadow_cal_prob`` is intentionally NOT included here — that field
    is populated only inside outcome 1, so under this fixture every row
    has it as None. Adding it would flood the snapshot with NaN."""
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
    num_regression.check(arrays, default_tolerance={"rtol": 1e-9, "atol": 0.0})


def test_counterfactual_prob_corpus_numeric_with_oracle(
        num_regression, probability_corpus, install_legacy_only_cal_engine):
    """Pin ``counterfactual_prob`` outputs across the corpus with the
    frozen Platt oracle installed in the legacy slot. Unlike
    ``compute()``, ``counterfactual_prob`` always reads
    ``_cal_state._CALIBRATION_ENGINE`` directly (no resolver branch), so
    the legacy-only fixture is sufficient — outcome 1 is unreachable
    here by design. Pinning this surface guards against silent drift in
    the calibrated-counterfactual code path that the default-fixture
    counterfactual snapshot can't reach."""
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


def test_probability_engine_passthrough_confirmation_smoke(
        probability_corpus, monkeypatch, frozen_cal_engine):
    """Confirm outcome 3 (BLR_BYPASS passthrough) produces the
    ``"passthrough"`` calibration_method label for every 15m corpus
    row when the legacy slot holds a calibrator.

    **Coverage scope — corrected R1**: under
    ``FIFTEEN_M_CALIBRATION_ENABLED=False`` (the production default,
    untouched here), the cascade gate at ``probability.py:217``
    short-circuits to False regardless of
    ``is_learned_method_active()``. So both
    learned-method-active=True and learned-method-active=False land in
    the same outcome-3 (BLR_BYPASS) branch. The previous
    ``is_learned_method_active → False`` monkeypatch was therefore
    **redundant** — it's been removed to make the test honest.

    What this test still does usefully: asserts the cascade does not
    raise on every 15m corpus row and that the categorical
    ``calibration_method`` label is consistent. Distinct from
    ``test_probability_engine_compute_corpus_numeric_legacy_only`` —
    that test pins NUMERIC outputs; this one is a categorical
    smoke-check. (The diagnostic ``_blr_would`` recomputation inside
    the branch has no observable output beyond a log line, so the
    branch body itself is invariant-checked rather than numerically
    pinned.)"""
    import bot.engines.calibration as _cal_state

    monkeypatch.setattr(_cal_state, "_CALIBRATION_ENGINE", frozen_cal_engine, raising=True)
    # _resolve_cal_engine stays at the autouse-null stub.
    # FIFTEEN_M_CALIBRATION_ENABLED stays at the production-default False,
    # which short-circuits the line-217 gate and routes 15m rows to
    # outcome 3 (passthrough + BLR_BYPASS) — same outcome as if
    # is_learned_method_active() returned False.

    cal_eligible_methods = []
    for row in probability_corpus:
        if row["product_type"] != "15m":
            continue
        result = ProbabilityEngine.compute(
            spot=row["spot_price"],
            threshold=row["threshold"],
            seconds_remaining=row["seconds_to_close"],
            blended_rv=row["volatility"],
            market_price_cents=row["market_price"],
            asset=row["asset"],
            product_type=row["product_type"],
        )
        cal_eligible_methods.append(result.get("calibration_method"))

    assert cal_eligible_methods, "no 15m rows in corpus — fixture sanity broken"
    # Every 15m row routed through outcome 3 must show passthrough.
    assert all(m == "passthrough" for m in cal_eligible_methods), (
        f"outcome 3 should produce passthrough; got distribution: "
        f"{set(cal_eligible_methods)}"
    )


def test_probability_engine_compute_corpus_numeric_outcome_2(
        num_regression, probability_corpus, install_legacy_15m_cal_engine):
    """Pin numeric outputs of ``compute()`` with
    ``FIFTEEN_M_CALIBRATION_ENABLED`` flipped to True AND the frozen
    Platt oracle wired into the legacy ``_CALIBRATION_ENGINE`` slot.

    **First test in the harness to actually exercise outcome 2** — the
    legacy 15M learned-method body at ``probability.py:218-220``:

    ::

        calibrated_prob = _cal_state._CALIBRATION_ENGINE.calibrate(
            raw_prob, cap=dynamic_cap, seconds_to_close=seconds_remaining)
        result["calibration_method"] = _cal_state._CALIBRATION_ENGINE.active_method

    Under the production-default ``FIFTEEN_M_CALIBRATION_ENABLED=False``
    the cascade gate at line 217 short-circuits to False and outcome 2
    is structurally unreachable. R1 caught this — previous tests
    claiming outcome-2 coverage actually exercised outcome 3.

    Coverage delta vs ``..._legacy_only``: 15m rows now produce
    ``calibration_method = "platt"`` (the engine's ``active_method``),
    NOT ``"passthrough"``, and ``calibrated_prob`` reflects the frozen
    Platt sigmoid + dynamic_cap clamp + ``_apply_uncertainty_shrinkage``
    (n=0 observations → ``u=0.05``). Non-15m rows still take outcome 4
    (passthrough) because ``cal_eligible=False`` for those product_types.

    ``shadow_cal_prob`` is intentionally NOT included — same rationale
    as ``..._legacy_only`` (populated only inside outcome 1)."""
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
    num_regression.check(arrays, default_tolerance={"rtol": 1e-9, "atol": 0.0})
