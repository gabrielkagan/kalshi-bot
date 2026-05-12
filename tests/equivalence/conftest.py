"""Shared fixtures for the engine equivalence harness.

Pillar 3 of the testing-foundation-sprint
(kb/decisions/testing-foundation-sprint-may09.md). This conftest:

* mocks the runtime-only deps (websockets, cryptography) so the
  ``bot`` package can be imported in a test process without pulling
  the live deps in
* nullifies the ``_CALIBRATION_ENGINE`` mutable singleton + the
  ``_resolve_cal_engine`` lookup so ``ProbabilityEngine.compute()``
  takes the deterministic passthrough/fixed-beta branches rather
  than coupling the snapshot to mutable calibrator state.
* exposes ``frozen_cal_engine`` + ``install_frozen_cal_engine``
  fixtures (opt-in) for tests that need to exercise the
  learned-method branches of the cascade — Bit 6.3 oracle handoff,
  vendored-snapshot flavor (a hand-crafted Platt state dict, written
  to a tmp file at fixture scope, loaded via the ordinary
  ``CalibrationEngine(state_path=...)`` ctor).
* loads the committed parquet corpus
  (``tests/fixtures/engine_inputs.parquet``) once per session and
  exposes it as the ``probability_corpus`` fixture (rows that meet
  every required-non-null gate the engine itself enforces).

**Bit 6.3 path-B post-merge note**: ``_CALIBRATION_ENGINE`` and
``_resolve_cal_engine`` were relocated from ``bot/_impl.py`` to
``bot/engines/calibration.py``. Patches now target
``bot.engines.calibration.X`` instead of ``bot._impl.X``. Both
``bot._impl`` and ``bot.engines.probability`` reach these names via
the ``_cal_state = bot.engines.calibration`` alias — patching the
underlying module attribute propagates to both consumers in one shot.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
import bot.feeds  # noqa: F401
import bot.fetchers  # noqa: F401


# ── Heavy-dep mocking ────────────────────────────────────────────────
# bot/_impl.py imports websockets / cryptography eagerly. The equivalence
# harness only exercises pure-math surfaces, so stub them.
_HEAVY_MOD_NAMES = (
    "websockets",
    "websocket",
    "requests",
    "cryptography",
    "cryptography.hazmat",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.asymmetric",
    "cryptography.hazmat.primitives.asymmetric.padding",
)
for _mod in _HEAVY_MOD_NAMES:
    sys.modules.setdefault(_mod, MagicMock())


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PARQUET_PATH = REPO_ROOT / "tests" / "fixtures" / "engine_inputs.parquet"


@pytest.fixture(scope="session")
def parquet_path() -> Path:
    """Absolute path to the committed equivalence corpus."""
    if not PARQUET_PATH.exists():
        raise pytest.UsageError(
            f"engine_inputs.parquet missing at {PARQUET_PATH}; regenerate via "
            f"`python3 scripts/ops/sample_engine_inputs.py --db state.db`"
        )
    return PARQUET_PATH


@pytest.fixture(scope="session")
def corpus_rows(parquet_path: Path) -> List[Dict[str, Any]]:
    """Whole parquet, row-major, as dicts. Cached for the session
    because the parquet read takes ~10ms and we don't want to pay it
    in every test."""
    import pyarrow.parquet as pq

    table = pq.read_table(parquet_path)
    return table.to_pylist()


@pytest.fixture(scope="session")
def probability_corpus(corpus_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Subset of the corpus where every ProbabilityEngine.compute()
    required input is present. The sampler script already filters
    for this, but we re-assert here so a future schema drift surfaces
    as a fixture-level error rather than as a test crash."""
    required = ("asset", "product_type", "spot_price", "threshold",
                "volatility", "seconds_to_close")
    rows = [r for r in corpus_rows
            if all(r.get(k) is not None for k in required)]
    if not rows:
        raise pytest.UsageError(
            "probability_corpus is empty after applying required-input "
            "filter; regenerate the parquet"
        )
    return rows


@pytest.fixture(autouse=True)
def isolate_calibration_singletons(monkeypatch):
    """Ensure ``ProbabilityEngine.compute()`` always sees a deterministic
    null calibrator by default.

    Post-Bit-6.3 path-B: patches target ``bot.engines.calibration.X``,
    NOT ``bot._impl.X``. ``ProbabilityEngine`` reaches the singleton
    via the ``_cal_state = bot.engines.calibration`` alias at module
    top, so patching the underlying module attribute is observed by
    every reader.

    **Coverage scope** — pinning ``_CALIBRATION_ENGINE = None`` and
    ``_resolve_cal_engine → None`` exercises only outcomes 4 + 5 of
    the 5-way cascade in ``probability.py`` (search anchor inside
    ``ProbabilityEngine.compute()``: ``_reg_engine = _cal_state._resolve_cal_engine``):

    - (1) registry learned-method                 — NOT exercised by default
    - (2) legacy ``_CALIBRATION_ENGINE`` learned  — NOT exercised by default
    - (3) legacy ``_CALIBRATION_ENGINE`` non-learned + BLR_BYPASS log — NOT exercised by default
    - (4) ``cal_eligible=False`` passthrough      — exercised
    - (5) fallback ``_calibrate`` (β=0.85 logistic) — exercised

    The corpus categorical snapshot
    (``test_probability_engine_compute_corpus_categorical.yml``)
    confirms the split — ``{fixed_beta: 303, passthrough: 697}``.
    The BLR clamp (search anchor: ``CAL_CLAMP``) and the
    market-discrepancy gate (search anchor: ``DISCREPANCY_PROB``)
    ride along incidentally; outcomes 1-3 are dark UNLESS a test
    opts in via ``install_frozen_cal_engine`` below.

    Autouse so individual tests can't forget the patch — snapshot
    stability is load-bearing.

    **Bit 6.3 oracle handoff (vendored-snapshot flavor)** — for tests
    that need to exercise outcomes 1 + 2 (learned-method calibrator),
    request ``install_frozen_cal_engine`` as a fixture. It stacks on
    top of this autouse patch with a deterministic Platt calibrator
    loaded from a hand-crafted state dict via the ordinary
    ``CalibrationEngine(state_path=...)`` ctor. The vendored-snapshot
    approach was chosen over subprocess-git-checkout for determinism
    + speed; see closeout doc rationale.

    **Implicit input** — ``compute()`` calls
    ``market_config.get_market_config(product_type)`` BEFORE the
    cascade (search anchor: ``_cal_cfg2 = get_market_config``), reading
    ``MARKET_CONFIGS`` (module-level dict). The fixture does NOT
    freeze that dict — today it is static at import, but if a future
    change makes it environment-dependent (Supabase fetch,
    per-machine override), the corpus snapshot becomes
    machine-dependent. If that happens, add
    ``monkeypatch.setattr(market_config, "MARKET_CONFIGS",
    <frozen_baseline>)`` here AND regenerate the snapshot in the
    same commit.
    """
    import bot.engines.calibration as _cal_state

    monkeypatch.setattr(_cal_state, "_CALIBRATION_ENGINE", None, raising=True)
    monkeypatch.setattr(
        _cal_state, "_resolve_cal_engine", lambda *a, **kw: None, raising=True,
    )
    yield


# ── Bit 6.3 oracle handoff fixtures (opt-in) ──────────────────────────
# Tests that pass ``install_frozen_cal_engine`` as a parameter swap the
# autouse-null patch for a deterministic Platt(A=0.85, B=0.0,
# trained=True) calibrator, exercising the learned-method branches of
# probability.py's cascade. The fixture is dependency-injected on
# top of the autouse patch — pytest applies fixture monkeypatches
# in fixture-resolution order, so the explicit override wins.

# Hand-crafted CalibrationEngine state. Platt with A=BETA_SLOPE, B=0
# matches the fixed-beta fallback's logit slope but with
# ``_platt_trained=True`` so ``is_learned_method_active()`` returns
# True. Deterministic and tiny (~500 bytes). The other learners
# (Beta / BLR / STC-Platt) stay un-trained so the dispatcher always
# routes to ``_platt_predict``.
_FROZEN_CAL_STATE: Dict[str, Any] = {
    "active_method": "platt",
    "platt": {"A": 0.85, "B": 0.0, "trained": True},
    "beta_cal": {"a": 1.0, "b": -1.0, "c": 0.0, "trained": False},
    "blr": {
        "mu": [1.0, 0.0],
        "precision": [[1.0, 0.0], [0.0, 1.0]],
        "trained": False,
    },
    "stc_platt": {"A": 0.85, "B": 0.0, "C": 0.0, "trained": False},
    "temperature": {"value": None, "brier": None},
    "prev_brier": None,
}


@pytest.fixture
def frozen_cal_engine(tmp_path):
    """Deterministic ``CalibrationEngine`` with a pinned Platt
    calibrator. Loaded via the ordinary ctor surface
    (``CalibrationEngine(state_path=...)``) — no hypothetical
    ``load_frozen`` API required. The state file is written to
    ``tmp_path`` so each test gets its own copy (no inter-test
    state leakage)."""
    from bot.engines.calibration import CalibrationEngine

    state_path = tmp_path / "frozen_cal_state.json"
    state_path.write_text(json.dumps(_FROZEN_CAL_STATE))
    return CalibrationEngine(
        state_path=str(state_path), label="EquivalenceFrozenOracle",
    )


@pytest.fixture
def install_frozen_cal_engine(frozen_cal_engine, monkeypatch):
    """Stack on top of ``isolate_calibration_singletons`` (autouse)
    to inject the frozen Platt calibrator into both legacy
    (``_CALIBRATION_ENGINE``) and registry (``_resolve_cal_engine``)
    code paths. Tests using this fixture exercise outcome 1
    (registry learned-method) of the ProbabilityEngine cascade for
    every row, because the patched ``_resolve_cal_engine`` returns the
    frozen engine regardless of ``product_type`` / ``cal_engine_enabled``.
    The ``elif`` branches (outcomes 2/3) are short-circuited under this
    fixture; for outcome-2 coverage see ``install_legacy_15m_cal_engine``
    (the sister fixture that flips ``FIFTEEN_M_CALIBRATION_ENABLED`` so
    the cascade reaches the legacy 15M ``_CALIBRATION_ENGINE.calibrate``
    body; ``install_legacy_only_cal_engine`` actually exercises
    outcomes 3 + 4 under the default constant — R1 corrected the
    pre-R1 claim that pointed here).

    Returns the engine so the test can introspect / make assertions
    against the same instance that production code receives."""
    import bot.engines.calibration as _cal_state

    monkeypatch.setattr(
        _cal_state, "_CALIBRATION_ENGINE", frozen_cal_engine, raising=True,
    )
    monkeypatch.setattr(
        _cal_state,
        "_resolve_cal_engine",
        lambda *a, **kw: frozen_cal_engine,
        raising=True,
    )
    return frozen_cal_engine


# ── VolatilityEngine instance-method coverage (Pillar 5 fu — 86b9vgxyf) ──
# Cluster A of the mutmut survivor map in bot/engines/volatility.py
# (~lines 118-300, the `__init__` + stateful instance-method surface)
# was dark in the original Pillar 3 harness — only the six pure
# `@staticmethod` math kernels (_parzen_kernel, _estimate_noise_variance,
# _realized_quarticity, _optimal_rk_bandwidth, _realized_kernel,
# _bipower_variation) had coverage. Mutations of `_adaptive_jump_test`,
# `_adaptive_decay_multiplier`, `_estimate_beta`, the history-trim
# invariants, and the per-asset deque defaults in `__init__` all
# survived because no test ever constructed a VolatilityEngine
# instance — building one needs a `CoinbaseFeed` + an `Optional[
# DeribitDVOLFetcher]`, both of which open network/threading
# resources in production.
#
# The two stubs below are deliberately minimal — they implement only
# the surface the `VolatilityEngine` constructor + the instance methods
# the equivalence harness exercises actually touch. The full feed /
# fetcher behaviour (websocket reconnect, deribit polling) is out of
# scope for math-equivalence testing.


class _StubCoinbaseFeed:
    """Minimal stand-in for ``bot.feeds.coinbase.CoinbaseFeed``.

    The only surface ``VolatilityEngine`` reaches from its ctor is
    nothing (the feed is stashed unread); the ``update()`` method
    later calls ``feed.get_buffer(asset)``. Returning an empty list
    is enough for instance-method tests that don't invoke
    ``update()`` — and tests that DO can seed ``self._buffers[asset]``
    explicitly. ``is_connected`` is included so a future test that
    asserts the engine doesn't crash on a disconnected feed can use
    the same stub.
    """

    def __init__(self, buffers=None):
        # Maps asset → list[(ts, price)] tuples. Empty by default so
        # an unseeded buffer trips the ``len(buf) < VOL_RETURN_INTERVAL
        # + 1`` early-return in ``update()`` (the math-equivalence
        # default — instance-method tests bypass ``update()`` and
        # call the smaller methods directly).
        self._buffers = dict(buffers) if buffers else {}
        self.is_connected = True

    def get_buffer(self, asset):
        return list(self._buffers.get(asset, []))


class _StubDeribitDVOLFetcher:
    """Minimal stand-in for ``bot.fetchers.deribit.DeribitDVOLFetcher``.

    ``VolatilityEngine`` accepts an ``Optional[DeribitDVOLFetcher]`` —
    most instance-method tests pass ``None`` to skip the DVOL path
    entirely. The stub is provided for the small set of tests that
    want to exercise ``_get_implied_vol`` (single-asset lookup) +
    ``_get_implied_vol_hourly`` (1h rolling avg) deterministically.

    ``_hourly_dvol`` is a dict-of-list (NOT deque) because the
    production code only reads ``len(buf)`` / iterates / takes
    ``max`` + ``min`` over it — list satisfies all three.
    """

    def __init__(self, dvol=None, dvol_hourly=None):
        # asset → dvol (per-5s scale), or None for "no quote".
        self._dvol = dict(dvol) if dvol else {}
        # asset → hourly average (per-5s scale). Separately settable
        # because production code reads it via a different method.
        self._dvol_hourly = dict(dvol_hourly) if dvol_hourly else {}
        # Production code reaches into ``_hourly_dvol`` for sample
        # count + min/max — provide a 1-element list per asset so the
        # spread-logging branch in ``_compute`` doesn't trip on a
        # missing buffer.
        self._hourly_dvol = {a: [v] for a, v in self._dvol_hourly.items()}

    def get_dvol(self, asset):
        return self._dvol.get(asset)

    def get_dvol_hourly_avg(self, asset):
        return self._dvol_hourly.get(asset)


@pytest.fixture
def volatility_engine_with_stubs():
    """Construct a ``VolatilityEngine`` with deterministic stub feeds.

    Cluster A of the mutmut survivor map (instance-method surface in
    ``bot/engines/volatility.py``) was dark before this fixture
    existed. Instance-method tests that import this fixture exercise:

    - ``__init__`` per-asset deque defaults (one ``deque(maxlen=...)``
      per asset in ``ASSETS``)
    - ``_record_jump_event`` / ``_record_adaptive_jump_event``
      history-trim semantics
    - ``_adaptive_subsample_return`` tick-counter modular arithmetic
    - ``_adaptive_jump_test`` EWMA + percentile threshold dispatch
    - ``_adaptive_decay_multiplier`` exponential-decay accumulator
    - ``_estimate_beta`` cov/var ratio + clamping

    Session-scoped is intentionally NOT used here — the engine
    mutates ``self._returns`` + ``self._adaptive_*`` buffers on every
    method call, and a session-shared instance would leak state
    between tests. Per-test construction is cheap (the ctor is
    pure-Python dict/deque allocation; no IO unless ``rk_state.json``
    or ``jump_adaptive_state.json`` exists in the cwd).

    The fixture deliberately uses ``None`` for the DVOL fetcher — the
    DVOL-blending path is reached via ``_compute()`` which is
    parquet-corpus territory (out of scope for this fixture). Tests
    that need DVOL can construct ``_StubDeribitDVOLFetcher`` directly.

    Sidecar state files (``rk_state.json``,
    ``jump_adaptive_state.json``) are flushed to a tmp directory via
    monkeypatch so a stray file in the working tree can't poison the
    test — see the ``_isolate_rk_sidecar_files`` autouse fixture below.
    """
    from bot.engines.volatility import VolatilityEngine

    feed = _StubCoinbaseFeed()
    return VolatilityEngine(feed, dvol_fetcher=None)


@pytest.fixture(autouse=True)
def _isolate_rk_sidecar_files(tmp_path, monkeypatch):
    """Redirect the two JSON sidecars VolatilityEngine reads at ctor
    time (``rk_state.json`` + ``jump_adaptive_state.json``) to a
    per-test ``tmp_path``.

    Why autouse: ``VolatilityEngine.__init__`` calls
    ``self._load_rk_state()`` + ``self._load_adaptive_state()``
    unconditionally; if a stale ``rk_state.json`` exists in the cwd,
    every instance-method test sees pre-loaded buffers, making
    invariants like "freshly constructed engine has empty
    ``_returns[asset]``" fragile across machines / CI runs.

    The static-method tests above don't construct an instance, so the
    autouse is a no-op for them (no engine = no sidecar read). The
    fixture is safe to stack with the existing
    ``isolate_calibration_singletons`` autouse — they patch disjoint
    namespaces.
    """
    rk_path = tmp_path / "rk_state.json"
    jump_path = tmp_path / "jump_adaptive_state.json"
    # RK_STATE_PATH is a class attribute on VolatilityEngine
    # (search anchor: ``RK_STATE_PATH = "rk_state.json"``); the
    # adaptive sidecar path is sourced from bot.constants.
    import bot.engines.volatility as _vol_mod

    monkeypatch.setattr(
        _vol_mod.VolatilityEngine, "RK_STATE_PATH", str(rk_path), raising=True,
    )
    monkeypatch.setattr(
        _vol_mod, "JUMP_ADAPTIVE_STATE_PATH", str(jump_path), raising=True,
    )
    yield


@pytest.fixture
def install_legacy_only_cal_engine(frozen_cal_engine, monkeypatch):
    """Variant of ``install_frozen_cal_engine`` that wires the frozen
    calibrator into the LEGACY ``_CALIBRATION_ENGINE`` singleton only
    — ``_resolve_cal_engine`` stays nulled by
    ``isolate_calibration_singletons`` (returns None).

    **Coverage scope — corrected R1**: with the production constant
    ``FIFTEEN_M_CALIBRATION_ENABLED = False`` (``bot/constants.py:891``)
    untouched, the cascade gate at ``probability.py:217`` —
    ``if FIFTEEN_M_CALIBRATION_ENABLED and ...`` — short-circuits to
    False for every row, so outcome 2 (the legacy 15M
    ``_CALIBRATION_ENGINE.calibrate(...)`` body, lines 218-220) is
    **structurally unreachable** under this fixture. What actually
    fires is:

    - rows with ``cal_eligible=True`` (15M): outcome 3
      (passthrough + BLR_BYPASS diagnostic, lines 222-230).
    - rows with ``cal_eligible=False`` (hourly / spx_hourly / weather /
      sports): outcome 4 (the ``elif not cal_eligible`` passthrough,
      lines 231-233).

    Empirical proof: under this fixture across the 303 15m corpus rows,
    248 produce ``calibrated_prob == raw_prob`` exactly and the other
    55 differ only by ``dynamic_cap`` clamping
    (``calibrated_prob = min(raw_prob, dynamic_cap)``). No row shows
    the frozen-Platt sigmoid signature — outcome 2 is dark.

    To exercise outcome 2 (the legacy 15M learned-method body), use
    ``install_legacy_15m_cal_engine`` below, which additionally
    monkeypatches ``FIFTEEN_M_CALIBRATION_ENABLED`` to True.

    ``counterfactual_prob`` reads ``_CALIBRATION_ENGINE`` directly (not
    via the resolver and not gated on ``FIFTEEN_M_CALIBRATION_ENABLED``),
    so this fixture IS load-bearing for pinning calibrated
    counterfactuals — see
    ``test_counterfactual_prob_corpus_numeric_with_oracle``."""
    import bot.engines.calibration as _cal_state

    monkeypatch.setattr(
        _cal_state, "_CALIBRATION_ENGINE", frozen_cal_engine, raising=True,
    )
    # _resolve_cal_engine stays at the autouse-installed lambda (returns None),
    # forcing the cascade past outcome 1 into the legacy elif. The
    # FIFTEEN_M_CALIBRATION_ENABLED=False short-circuit then forces 15m
    # rows past outcome 2 into outcome 3 (passthrough + BLR_BYPASS).
    return frozen_cal_engine


@pytest.fixture
def install_legacy_15m_cal_engine(frozen_cal_engine, monkeypatch):
    """Sister fixture to ``install_legacy_only_cal_engine`` that
    additionally flips ``FIFTEEN_M_CALIBRATION_ENABLED`` to True so the
    cascade gate at ``probability.py:217`` no longer short-circuits.

    Under this fixture, 15m rows reach outcome 2 — the
    ``_CALIBRATION_ENGINE.calibrate(...)`` body at lines 218-220 — and
    the frozen Platt oracle (A=0.85, B=0.0, trained=True) actually
    runs. Non-15m rows still take outcome 4 (passthrough) because
    ``cal_eligible=False`` for those product_types.

    The monkeypatch targets ``bot.engines.probability`` directly
    because the cascade reads the constant via a module-local rebinding
    (``from bot.constants import ... FIFTEEN_M_CALIBRATION_ENABLED``
    at probability.py:104-109) — patching ``bot.constants`` would not
    propagate to the already-imported probability module.
    ``bot.constants`` itself is left untouched so other tests sharing
    the session module cache don't observe the override.

    Closes the R1 coverage gap: previously
    ``install_legacy_only_cal_engine`` was advertised as exercising
    outcome 2 but actually exercised outcome 3 due to the constant
    short-circuit. This fixture makes outcome 2 reachable for the
    first time in the equivalence harness."""
    import bot.engines.calibration as _cal_state
    import bot.engines.probability as _prob

    monkeypatch.setattr(
        _cal_state, "_CALIBRATION_ENGINE", frozen_cal_engine, raising=True,
    )
    monkeypatch.setattr(
        _prob, "FIFTEEN_M_CALIBRATION_ENABLED", True, raising=True,
    )
    # _resolve_cal_engine stays at the autouse-installed lambda (returns None),
    # so outcome 1 is skipped and the cascade enters the legacy elif at line
    # 216, where the gate at 217 now evaluates True for 15m rows.
    return frozen_cal_engine
