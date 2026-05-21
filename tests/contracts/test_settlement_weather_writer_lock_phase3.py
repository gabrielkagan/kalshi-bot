"""Ticket 86ba1xdwp — settlement weather Phase 3 writer-lock 2-phase split.

Pins the structural shape of the weather-settlement loop in
``bot/settlement.py::SettlementTracker._poll_evaluated_opportunities``.

The bug (2026-05-21): the loop interleaves HTTP fetches
(``_wx_eng._fetcher.fetch_observed_high(...)``) with shared-conn writes
(``self._state.conn.execute("UPDATE evaluated_opportunities SET
wx_actual_high_temp=...")``) and commits ONCE after the loop. Python's
``sqlite3`` deferred-isolation auto-tx holds the writer lock from the
first UPDATE through the last commit — across N × HTTP latency. Cascade
victims: every separate-connection writer (``weather_engine._save_bias``,
``market_obs_snapshotter``, ``phantom_reconcile_monitor``,
``CALMLP_POSTHOC``) busy-waits up to ``busy_timeout``=10000ms.

Fix shape (this test pins it):
  Phase 3a (HTTP-only, NO DB writes): collect ``_wx_observations``.
  Phase 3b (DB-only, tight per-row tx): UPDATE + commit per row, then
  ``update_bias`` (its separate-conn INSERT can now acquire cleanly).

STRUCTURAL anchors per ``feedback_long_arc_adv_review_durable_fixes`` —
the guard targets function names + branch conditions, NOT line numbers
(line cites drift on every adjacent edit and produced 4 onion-ring
rounds in the F0.1 chain).

Pre-fix this whole file is RED. Post-fix it's GREEN; future maintainers
who collapse the two phases back into one will trip the guard.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SETTLEMENT_PY = REPO_ROOT / "bot" / "settlement.py"
METHOD_NAME = "_poll_evaluated_opportunities"


def _load_method() -> ast.FunctionDef:
    """Return the AST FunctionDef for the method containing the weather loop."""
    tree = ast.parse(SETTLEMENT_PY.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == METHOD_NAME:
            return node
    raise AssertionError(
        f"Could not find {METHOD_NAME!r} in {SETTLEMENT_PY}. Method may have been "
        f"renamed; update METHOD_NAME in this test in lockstep."
    )


def _for_loops_over(method: ast.FunctionDef, *target_names: str) -> list[ast.For]:
    """Return every ``for ... in <Name(id in target_names)>`` in the method."""
    out: list[ast.For] = []
    for node in ast.walk(method):
        if isinstance(node, ast.For):
            iter_node = node.iter
            # accept `for x in _weather_updates` or `for x in _wx_observations`
            if isinstance(iter_node, ast.Name) and iter_node.id in target_names:
                out.append(node)
    return out


def _calls_in(scope: ast.AST, attr_name: str) -> list[ast.Call]:
    """Every ``Call`` whose func is a chain ending in ``.<attr_name>(...)``."""
    out: list[ast.Call] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == attr_name:
                out.append(node)
    return out


def _has_call_chain(scope: ast.AST, *attr_chain: str) -> bool:
    """True if ``scope`` contains a Call whose func attribute chain ends in
    ``attr_chain`` (innermost-last).

    e.g. ``_has_call_chain(loop, "_state", "conn", "commit")`` matches
    ``self._state.conn.commit()``.
    """
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        # walk the attribute chain backward from the call's func
        attrs: list[str] = []
        cur: ast.AST = node.func
        while isinstance(cur, ast.Attribute):
            attrs.append(cur.attr)
            cur = cur.value
        # innermost attr is first in attrs; we want it to end (innermost-last)
        # with the requested chain. Reverse to outer-to-inner order, then check tail.
        if list(reversed(attrs))[-len(attr_chain):] == list(attr_chain):
            return True
    return False


# ───────────────────────── tests ─────────────────────────────────────


def test_method_exists() -> None:
    """Anchor: the containing method must still be named
    ``_poll_evaluated_opportunities``. If it gets renamed, this test +
    METHOD_NAME constant move together in one commit."""
    method = _load_method()
    assert method.name == METHOD_NAME


def test_two_for_loops_over_weather_collections() -> None:
    """Phase 3a + Phase 3b are TWO distinct loops.

    Pre-fix: ONE loop iterates ``_weather_updates`` and does both HTTP
    fetch + DB write inline.
    Post-fix: ONE loop iterates ``_weather_updates`` (Phase 3a, HTTP
    only, builds ``_wx_observations``), then a SECOND loop iterates
    ``_wx_observations`` (Phase 3b, DB only). Total = 2.
    """
    method = _load_method()
    loops = _for_loops_over(method, "_weather_updates", "_wx_observations")
    assert len(loops) == 2, (
        f"Expected exactly 2 for-loops over (_weather_updates | _wx_observations) "
        f"in {METHOD_NAME} — got {len(loops)}. The 2-phase split (Phase 3a HTTP "
        f"only / Phase 3b DB only) is the contract; a single loop re-introduces "
        f"the writer-storm bug fixed in 86ba1xdwp."
    )


def test_no_loop_mixes_fetch_and_update_bias() -> None:
    """No SINGLE for-loop body contains BOTH ``fetch_observed_high(...)``
    and ``update_bias(...)``. That coexistence is the bug-reintroduction
    smell: any per-row HTTP-then-write loop is a writer-storm.
    """
    method = _load_method()
    loops = _for_loops_over(method, "_weather_updates", "_wx_observations")
    for i, loop in enumerate(loops):
        has_fetch = bool(_calls_in(loop, "fetch_observed_high"))
        has_bias = bool(_calls_in(loop, "update_bias"))
        assert not (has_fetch and has_bias), (
            f"Loop #{i} in {METHOD_NAME} contains BOTH fetch_observed_high() "
            f"and update_bias() — that re-introduces the 86ba1xdwp writer-storm. "
            f"Keep HTTP (fetch_observed_high) in Phase 3a and DB writes "
            f"(update_bias, the UPDATE statement) in Phase 3b."
        )


def test_phase3b_commits_per_row() -> None:
    """The Phase 3b loop body must call ``self._state.conn.commit()``
    INSIDE the loop (per-row commit), releasing the writer lock between
    rows. The Phase 3b loop is identified as the loop containing an
    UPDATE on ``evaluated_opportunities`` (the SQL write).
    """
    method = _load_method()
    loops = _for_loops_over(method, "_weather_updates", "_wx_observations")
    phase3b_candidates = []
    for loop in loops:
        # Phase 3b = the loop that contains a self._state.conn.execute(...) call
        if _has_call_chain(loop, "_state", "conn", "execute"):
            phase3b_candidates.append(loop)
    assert phase3b_candidates, (
        f"No for-loop in {METHOD_NAME} contains a self._state.conn.execute(...) "
        f"call against the weather collections. The Phase 3b DB loop must exist."
    )
    for loop in phase3b_candidates:
        assert _has_call_chain(loop, "_state", "conn", "commit"), (
            f"Phase 3b loop in {METHOD_NAME} does NOT commit per-row. "
            f"self._state.conn.commit() must appear INSIDE the loop body so "
            f"the writer lock is released between rows (closes 86ba1xdwp)."
        )


def test_wx_dirty_flag_removed() -> None:
    """The pre-fix code accumulated writes under a ``_wx_dirty`` flag and
    committed once at the end. The post-fix per-row-commit design has no
    use for this flag — its presence means the old single-loop pattern
    is still live somewhere.
    """
    method = _load_method()
    method_src = ast.unparse(method)
    assert "_wx_dirty" not in method_src, (
        f"_wx_dirty flag is still present in {METHOD_NAME}. The fix replaces "
        f"the deferred-commit pattern with per-row commits in Phase 3b; the "
        f"flag should be deleted in the same edit."
    )


def test_phase3b_commit_precedes_update_bias() -> None:
    """In the Phase 3b loop body, ``self._state.conn.commit()`` must appear
    BEFORE any ``update_bias(...)`` call in TEXTUAL READING ORDER —
    including when one or both are nested inside child blocks
    (``if``/``try``).

    This is the durability anchor against the cascade-reopen smell flagged
    in R1-N1 + tightened in R2-N1: pin commit-BEFORE-update_bias by
    comparing (lineno, col_offset) keys, NOT top-level statement indices.
    A pre-R2-N1 version of this assertion used statement-index comparison
    and false-passed on the nested-in-conditional shape
    ``for ...: self._state.conn.execute(...); if cond: update_bias(...); self._state.conn.commit()``
    (commit and update_bias share the outer try's statement index, so
    `commit_idx <= update_bias_idx` was trivially true while textually the
    commit ran AFTER update_bias). Comparing (lineno, col_offset) closes
    the hole — any source position where commit appears textually AFTER
    update_bias inside the same loop body fails LOUDLY.
    """
    method = _load_method()
    loops = _for_loops_over(method, "_weather_updates", "_wx_observations")
    phase3b_loops = [
        loop for loop in loops
        if _has_call_chain(loop, "_state", "conn", "execute")
    ]
    assert phase3b_loops, "Phase 3b DB loop missing — earlier test should have caught this."
    for loop in phase3b_loops:
        commit_pos: tuple | None = None
        update_bias_pos: tuple | None = None
        # Recursively walk the entire loop body subtree to find the FIRST
        # textual occurrence of each call. AST node lineno/col_offset is
        # populated for every Call node (CPython ast module guarantee), so
        # tuple comparison gives true textual reading order across nesting.
        for sub in ast.walk(loop):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if not isinstance(f, ast.Attribute):
                continue
            # Python AST populates (lineno, col_offset) for every Call
            # node; tuple comparison gives textual reading order across
            # arbitrary nesting (if / try / nested for).
            pos = (sub.lineno, sub.col_offset)
            if f.attr == "commit":
                # Restrict to self._state.conn.commit() specifically (other
                # .commit() flavors aren't load-bearing for the lock).
                attrs: list[str] = []
                cur: ast.AST = sub.func
                while isinstance(cur, ast.Attribute):
                    attrs.append(cur.attr)
                    cur = cur.value
                if list(reversed(attrs))[-3:] == ["_state", "conn", "commit"]:
                    if commit_pos is None or pos < commit_pos:
                        commit_pos = pos
            elif f.attr == "update_bias":
                if update_bias_pos is None or pos < update_bias_pos:
                    update_bias_pos = pos
        assert commit_pos is not None, (
            f"Phase 3b loop missing self._state.conn.commit() — earlier "
            f"assertion should have caught this."
        )
        if update_bias_pos is not None:
            assert commit_pos < update_bias_pos, (
                f"In Phase 3b loop body of {METHOD_NAME}, "
                f"self._state.conn.commit() at {commit_pos} must "
                f"precede update_bias() at {update_bias_pos} in textual "
                f"reading order. Reversing this order re-opens the "
                f"writer-storm cascade: update_bias's separate-conn "
                f"INSERT would contend with the still-held shared-conn "
                f"writer lock from the UPDATE."
            )
