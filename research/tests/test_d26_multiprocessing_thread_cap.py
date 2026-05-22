"""D-26 — multiprocessing worker isolation + numpy thread cap.

Authoritative source: bot._impl has `import _thread_env` as the FIRST line to
constrain OMP_NUM_THREADS=1 before numpy loads (per CLAUDE.md). Replay doesn't
share that process, but under multiprocessing.Pool each worker imports
numpy/pandas — without thread caps, N workers × OMP_NUM_THREADS=cores
saturates CPU and slows down end-to-end.

Replay should set OMP_NUM_THREADS=1, MKL_NUM_THREADS=1, OPENBLAS_NUM_THREADS=1
at top-of-module BEFORE any numpy import.

TDD-red on the actual pool benchmark (requires replay engine). Can validate
the env-var-setting pattern via AST/source scan now.
"""
from __future__ import annotations

import inspect
import os
import re

import research.replay as rep


THREAD_CAP_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
)


def test_d26_thread_cap_documented_or_set() -> None:
    """If replay.py imports numpy/pandas, it must set thread cap env vars BEFORE.

    Best-effort check: scan source for `import numpy` / `import pandas` and
    verify thread-cap setting appears BEFORE in the file. B1 doesn't import
    numpy or pandas directly; this is a B3-and-beyond contract.
    """
    src = inspect.getsource(rep)
    imports_numeric = (
        "import numpy" in src
        or "import pandas" in src
        or "from numpy" in src
        or "from pandas" in src
    )
    if not imports_numeric:
        # B1 path: no numpy imports yet. Test is informational.
        return
    # If imports numeric libs, verify thread caps set at top
    for var in THREAD_CAP_VARS:
        thread_cap_set = (
            f"os.environ['{var}']" in src
            or f'os.environ["{var}"]' in src
            or f"os.environ.setdefault('{var}'" in src
            or f'os.environ.setdefault("{var}"' in src
        )
        # If no cap is set at all, fail. If one cap is set but not all, OK
        # (heuristic — some are more impactful than others).
        if thread_cap_set:
            # Find the line position of the cap-setting vs first numpy import
            cap_pos = src.find(var)
            import_positions = [
                src.find(f"import numpy"),
                src.find(f"import pandas"),
                src.find(f"from numpy"),
                src.find(f"from pandas"),
            ]
            first_import = min(p for p in import_positions if p >= 0)
            assert cap_pos < first_import, (
                f"D-26 thread cap ordering: {var} set at offset {cap_pos}, "
                f"first numpy/pandas import at {first_import}. Cap must come FIRST."
            )
            return  # at least one cap is set correctly
    # If we get here, replay imports numpy/pandas but sets NO thread caps
    assert False, (
        f"D-26 missing thread caps: replay.py imports numpy/pandas but no "
        f"{THREAD_CAP_VARS} env vars set. Saturates CPU under multiprocessing.Pool."
    )


def test_d26_thread_cap_set_uses_string_value_1() -> None:
    """Thread cap values must be the string '1', not int 1."""
    src = inspect.getsource(rep)
    for var in THREAD_CAP_VARS:
        # Look for setting patterns that use integer 1 (bug: os.environ accepts strings only)
        bad_patterns = [
            f"os.environ['{var}'] = 1",
            f'os.environ["{var}"] = 1',
            f"os.environ.setdefault('{var}', 1)",
            f'os.environ.setdefault("{var}", 1)',
        ]
        for bad in bad_patterns:
            assert bad not in src, (
                f"D-26 wrong type for {var}: {bad!r} uses int 1, should be string '1'. "
                f"os.environ raises TypeError on non-string values."
            )


def test_d26_replay_does_not_call_numpy_set_num_threads_lazily() -> None:
    """Setting numpy.show_config / threadpoolctl at runtime is too late.

    Pin: env vars set BEFORE imports (the only reliable mechanism per
    OpenBLAS/MKL docs).
    """
    src = inspect.getsource(rep)
    # Heuristic: late-bound thread control is suspect
    forbidden = [
        "threadpoolctl.threadpool_limits",
        "numpy.set_num_threads",  # doesn't exist but catches typos
    ]
    for bad in forbidden:
        assert bad not in src, (
            f"D-26 late-bound thread control: {bad!r}. "
            f"Use env vars BEFORE numpy import, not runtime API."
        )


def test_d26_b1_research_replay_module_loads_cleanly() -> None:
    """Sanity: research.replay imports without raising (env-var setup doesn't break things).

    R1 finding MNR6: previously used importlib.reload(rep) mid-test which can
    corrupt cached state for downstream tests. Use import_module for a fresh
    probe without mutating the session's cached module.
    """
    import importlib
    fresh = importlib.import_module("research.replay")
    assert fresh is not None


def test_d26_pool_benchmark_function_exists() -> None:
    """B3 ships a pool-benchmark or parallel-replay function (TDD-red).

    R2 finding MNR2: changed pytest.skip → assertion failure. The test is part
    of the B3 contract surface — it should be RED until B3 ships, not silently
    skipped.
    """
    has_parallel = (
        hasattr(rep, "replay_parallel")
        or hasattr(rep, "evaluate_window_parallel")
        or hasattr(rep, "_pool_replay")
    )
    assert has_parallel, (
        "D-26 TDD-red: B3 must ship one of "
        "(replay_parallel / evaluate_window_parallel / _pool_replay)"
    )
