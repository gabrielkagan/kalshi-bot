"""P1-A-fu2 — collector-start.sh must invoke python3 with -O flag.

Ticket `86ba1qgbp` (2026-05-20, follow-up to P1-A `86ba1pqqx`).

Background: P1-A fix #4 gated the per-frame envelope re-validation
behind ``_VALIDATE_ENVELOPE: bool = __debug__`` in
``collector/writer.py``. Python's ``__debug__`` is True by default and
False only when the interpreter is invoked with ``-O`` (or ``-OO``).
Without ``-O``, fix #4 is a no-op in production — the validation runs
on every frame, costing 3 dict lookups + 3 comparisons per frame.

This contract test pins the ``-O`` flag in ``collector-start.sh`` so
the production-effective P1-A speedup reaches the full 11.1× measured
in the micro-benchmark (vs ~8× without ``-O``).

-O safety survey (pre-flight 2026-05-20):
The ``-O`` flag strips ``assert`` statements PROCESS-WIDE — affects
both repo code AND pure-Python 3rd-party deps loaded into the process.
C/Rust extensions (cryptography, zstandard, orjson) are NOT affected
because their asserts are compiled into the extension binary, not the
Python bytecode our interpreter compiles.

Affected pure-Python assert counts (measured 2026-05-20):
- ``collector/``: 4 asserts (espn + weather archivers) — programmer-
  error guards (writer existence + catalog membership) where the
  natural failure mode without the assert is ``KeyError``.
- ``kalshi_wire/`` and ``coinbase_wire/``: 0 asserts.
- ``websockets/``: ~101 asserts — manually inspected (samples:
  ``assert self.state is CONNECTING``, ``assert n >= 0``,
  ``assert self.state is CLOSED``). All defensive type-narrowing /
  state-machine invariants enforced by other code paths, NOT
  control-flow gates. Safe to strip.
- ``requests/``: ~6 asserts — same defensive class.

Conclusion: no correctness regression from ``-O``. The 4 collector
asserts shift their failure mode from ``AssertionError`` to a later
``KeyError`` at first use — same crash-on-boot outcome under
``Restart=on-failure``. 3rd-party defensive asserts become silent
no-ops, preserving operational behavior.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_START = REPO_ROOT / "collector-start.sh"


def _read() -> str:
    assert COLLECTOR_START.exists(), (
        f"{COLLECTOR_START.relative_to(REPO_ROOT)} missing — P1-A-fu2 "
        "extends the D1.5 wrapper with the -O flag. If the file moved, "
        "update this contract test."
    )
    return COLLECTOR_START.read_text()


def test_python3_invoked_with_O_flag():
    """The ``exec python3`` line must include ``-O``.

    Accepted shapes:
      - ``exec python3 -O -m collector``
      - ``exec python3 -OO -m collector`` (stricter; also strips
        docstrings)
    Forbidden shape:
      - ``exec python3 -m collector`` (pre-P1-A-fu2; fix #4 is no-op)

    Without -O: ``__debug__`` is True, ``_VALIDATE_ENVELOPE`` is True,
    the 3 per-frame envelope.get(...) validations fire on every frame.
    With -O: ``__debug__`` is False, validation is stripped, P1-A fix
    #4 delivers its share of the 11.1× speedup.
    """
    text = _read()
    # Match `python3` followed by `-O` (or `-OO`) somewhere before
    # `-m collector`. Allow other flags between for forward-compat.
    pattern = re.compile(
        r"^exec\s+python3(\s+-\w+)*\s+-OO?\b(\s+-\w+)*\s+-m\s+collector\b",
        re.M,
    )
    assert pattern.search(text), (
        "exec python3 line must include -O (or -OO) flag. Without it, "
        "P1-A fix #4 (gated envelope re-validation under __debug__) is "
        "a no-op in production. See P1-A-fu2 ticket 86ba1qgbp."
    )


def test_O_flag_does_not_break_collector_module_invocation():
    """Sister-test of test_exec_python_m_collector — must still match
    after the -O insertion.

    The pre-fu2 regex ``python3\\s+-m\\s+collector`` would NOT match
    ``python3 -O -m collector`` (because -O is between python3 and -m).
    This test confirms the loosened regex below also matches.
    """
    text = _read()
    # Loosened: allow any short flags between python3 and -m.
    pattern = re.compile(
        r"^exec\s+python3(\s+-\w+)*\s+-m\s+collector\b", re.M
    )
    assert pattern.search(text), (
        "exec python3 ... -m collector regex no longer matches after "
        "P1-A-fu2 flag insertion. The sister test "
        "tests/contracts/test_collector_start_sh_invokes_python_m.py::"
        "test_exec_python_m_collector should be updated to use this "
        "loosened pattern in lockstep with this Bit."
    )
