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
            f"`python3 scripts/sample_engine_inputs.py --db state.db`"
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
    code paths. Tests using this fixture exercise outcomes 1 + 2
    (learned-method) of the ProbabilityEngine cascade.

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
