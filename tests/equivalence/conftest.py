"""Shared fixtures for the engine equivalence harness.

Pillar 3 of the testing-foundation-sprint
(kb/decisions/testing-foundation-sprint-may09.md). This conftest:

* mocks the runtime-only deps (websockets, cryptography) so the
  ``bot`` package can be imported in a test process without pulling
  the live deps in
* nullifies the ``_CALIBRATION_ENGINE`` mutable singleton + the
  ``_resolve_cal_engine`` lookup so ``ProbabilityEngine.compute()``
  takes the deterministic passthrough/fixed-beta branches rather
  than coupling the snapshot to mutable calibrator state. Bit 6.3
  will extend this fixture to inject a frozen CalibrationEngine
  oracle once the engine is extracted.
* loads the committed parquet corpus
  (``tests/fixtures/engine_inputs.parquet``) once per session and
  exposes it as the ``probability_corpus`` fixture (rows that meet
  every required-non-null gate the engine itself enforces).
"""
from __future__ import annotations

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
    null calibrator. The Bit 6.2 late-binding
    (``from bot import _impl as _bot_impl``) reads two names off
    ``bot._impl`` at call time, so they must be patched *before* any
    equivalence-test call.

    **Coverage scope** — pinning ``_CALIBRATION_ENGINE = None`` and
    ``_resolve_cal_engine → None`` exercises only outcomes 4 + 5 of
    the 5-way cascade in ``probability.py:204-238``:

    - (1) registry learned-method                 — NOT exercised
    - (2) legacy ``_CALIBRATION_ENGINE`` learned  — NOT exercised
    - (3) legacy ``_CALIBRATION_ENGINE`` non-learned + BLR_BYPASS log — NOT exercised
    - (4) ``cal_eligible=False`` passthrough      — exercised
    - (5) fallback ``_calibrate`` (β=0.85 logistic) — exercised

    The corpus categorical snapshot
    (``test_probability_engine_compute_corpus_categorical.yml``)
    confirms the split — ``{fixed_beta: 303, passthrough: 697}``.
    The BLR clamp (probability.py:244-260) and the market-discrepancy
    gate (264-272) ride along incidentally; outcomes 1-3 are dark.

    Autouse so individual tests can't forget the patch — snapshot
    stability is load-bearing.

    **Bit 6.3 oracle handoff** — when ``CalibrationEngine`` extracts
    to ``bot/engines/calibration.py``, extend this fixture to inject
    a *deterministic frozen* learned-method calibrator. Sketch
    (subprocess-git-checkout flavor; vendored-snapshot is the other
    option — see ``REGEN.md``). NOTE: ``CalibrationEngine.load_frozen``
    in the sketch is hypothetical — Bit 6.3 must define this surface
    (or its equivalent) as part of the extraction; ``CalibrationEngine``
    today exposes ``__init__(state_path=…)`` + ``_load_state``::

        @pytest.fixture
        def frozen_cal_engine():
            # load CalibrationEngine state captured at a pre-extraction SHA
            from bot.engines.calibration import CalibrationEngine  # post-Bit-6.3
            return CalibrationEngine.load_frozen("tests/fixtures/cal_engine_v1.json")

        # then in isolate_calibration_singletons, replace the lambda:
        monkeypatch.setattr(_bot_impl, "_resolve_cal_engine",
                            lambda *a, **kw: frozen_cal_engine, raising=True)

    Pillar 3 deliberately does NOT pick between subprocess-checkout
    vs vendored-snapshot — that's a Bit 6.3 author call.

    **Implicit input** — ``compute()`` calls
    ``market_config.get_market_config(product_type)`` at
    probability.py:203 (BEFORE the cascade), reading
    ``MARKET_CONFIGS`` (module-level dict). The fixture does NOT
    freeze that dict — today it is static at import, but if a future
    change makes it environment-dependent (Supabase fetch,
    per-machine override), the corpus snapshot becomes
    machine-dependent. If that happens, add
    ``monkeypatch.setattr(market_config, "MARKET_CONFIGS",
    <frozen_baseline>)`` here AND regenerate the snapshot in the
    same commit.
    """
    from bot import _impl as _bot_impl

    monkeypatch.setattr(_bot_impl, "_CALIBRATION_ENGINE", None, raising=True)
    monkeypatch.setattr(
        _bot_impl, "_resolve_cal_engine", lambda *a, **kw: None, raising=True,
    )
    yield
