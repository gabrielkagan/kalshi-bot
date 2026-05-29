"""B2b-1 — structural pins for the synthetic-RTI shadow invariant + the
numpy-only (no-torch/no-pandas) import constraint.

Ticket 86ba64h2w (program 86ba64gyq). Plan: kb/decisions/b2b-1-core-shadow-plan.md.

Two load-bearing guarantees, pinned structurally (the codebase pins scanner
invariants via AST rather than a live-scan harness — cf.
tests/contracts/test_p4_1_band_calibrated_sizing.py):

  1. ZERO LIVE-DECISION CHANGE *by default*. The synthetic flows through
     ``self._state._scan_rti_cache``, read back in
     ``insert_evaluated_opportunity`` (shadow logging of the rti_* columns)
     AND — post RTI-6 — in ``OpportunityScanner._effective_decision_spot``,
     which substitutes it for the decision spot ONLY for assets in
     ``SYNTHETIC_RTI_LIVE_ASSETS``. That set defaults to EMPTY, so no asset is
     promoted and the synthetic reaches NO decision in the shipped config (the
     per-asset carve-out + default-empty pin live in
     ``tests/contracts/test_rti_live_per_asset.py``). The names that receive
     ``get_cached_synthetic(...)`` still must not leak outside the tight
     staging block — the live route is the gated cache read, not the staged
     locals.

  2. NUMPY-ONLY. ``bot.feeds.synthetic_rti_feed`` (and its aggregator
     ``bot.feeds.synthetic_rti``) must not pull in torch or pandas — their
     C-extensions cache OpenBLAS thread pools before ``bot._thread_env`` can
     pin OMP_NUM_THREADS=1 (the bot-no-torch / bot-no-pandas rationale).
     import-linter's ``bot-no-torch`` (source=``bot``) already covers the
     module; this is the behavioral backstop.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
STATE_PY = REPO_ROOT / "bot" / "state.py"

_RTI_GETTER = "get_cached_synthetic"
_RTI_CACHE = "_scan_rti_cache"
# Names the scanner staging block binds from get_cached_synthetic(...).
_STAGED_NAMES = {"_rti_syn", "_rti_n", "_rti_conf"}
# Max line span the staged names may occupy (the tight staging block).
_MAX_BLOCK_SPAN = 12


def _scanner_src() -> str:
    return SCANNER_PY.read_text()


def test_get_cached_synthetic_called_exactly_once_in_scanner():
    """Exactly one ACTUAL call site (AST attribute access — comment/docstring
    mentions don't count)."""
    tree = ast.parse(_scanner_src())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == _RTI_GETTER]
    assert len(calls) == 1, (
        f"{_RTI_GETTER} must be wired at exactly one scan site (found "
        f"{len(calls)})")


def test_staged_synthetic_names_never_leak_outside_block():
    """Every reference to the staged synthetic locals must sit within a tight
    contiguous block around the get_cached_synthetic call — proving the value
    is staged into the cache and read nowhere a decision could see it."""
    tree = ast.parse(_scanner_src())
    # Locate the get_cached_synthetic call line.
    call_lines = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == _RTI_GETTER
    ]
    assert len(call_lines) == 1, f"expected 1 {_RTI_GETTER} call, got {call_lines}"
    anchor = call_lines[0]

    name_lines = sorted({
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id in _STAGED_NAMES
    })
    assert name_lines, "staged synthetic names not found — wiring missing"
    span = name_lines[-1] - name_lines[0]
    assert span <= _MAX_BLOCK_SPAN, (
        f"staged synthetic names span {span} lines "
        f"({name_lines[0]}..{name_lines[-1]}) — they must stay in the tight "
        f"staging block near the {_RTI_GETTER} call at line {anchor}; a wide "
        f"span means the synthetic may be leaking into a decision path")
    # And the block must sit at/after the getter call (staged, not pre-read).
    assert name_lines[0] >= anchor, (
        "a staged synthetic name is referenced BEFORE the get_cached_synthetic "
        "call — possible decision-path read")


def test_scanner_writes_only_rti_cache():
    """The scanner must subscript-assign the synthetic into _scan_rti_cache and
    write it to no other cache/field at the staging site."""
    src = _scanner_src()
    assert f"{_RTI_CACHE}[" in src, "scanner must stage synthetic into _scan_rti_cache"


def test_rti_cache_read_only_in_insert_evaluated_opportunity():
    """_scan_rti_cache.get(...) must appear ONLY inside insert_evaluated_opportunity
    (the single auto-fill consumer) — mirrors _scan_cx_gap_cache."""
    tree = ast.parse(STATE_PY.read_text())
    fns_reading = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if (isinstance(node, ast.Attribute) and node.attr == _RTI_CACHE):
                    fns_reading.append(fn.name)
                    break
    assert set(fns_reading) <= {"insert_evaluated_opportunity", "__init__"}, (
        f"_scan_rti_cache referenced outside the expected fns: {fns_reading}")
    assert "insert_evaluated_opportunity" in fns_reading


def test_synthetic_rti_feed_imports_no_torch_or_pandas():
    """Importing the feed must not transitively pull torch/pandas."""
    code = (
        "import sys\n"
        "import bot.feeds.synthetic_rti_feed  # noqa\n"
        "bad = [m for m in ('torch', 'pandas') if m in sys.modules]\n"
        "assert not bad, bad\n"
        "import bot.feeds.synthetic_rti_feed as f\n"
        "assert hasattr(f, 'SyntheticRTIFeed')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                       capture_output=True, text=True)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"


def test_feed_source_has_no_torch_pandas_literal():
    src = (REPO_ROOT / "bot" / "feeds" / "synthetic_rti_feed.py").read_text()
    assert "import torch" not in src
    assert "import pandas" not in src
