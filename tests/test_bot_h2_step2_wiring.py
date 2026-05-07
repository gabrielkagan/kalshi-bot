"""Phase H-2 STEP 2: bot/_impl.py integration tests for snapshot computation.

STEP 1 shipped schema + plumbing (column defaults NULL).
STEP 2 (this commit) wires the actual snapshot computation:
- StateManager exposes a `set_bot_state_provider(provider)` setter.
- MainLoop wires the provider after construction so insert_evaluated_opportunity
  computes the snapshot internally on every 15M insert.
- BEGIN IMMEDIATE timing pattern measures lock_wait_ms (mandated per
  scripts docstring + design doc).
- MainLoop adds `_scan_iter` counter (init + increment) and
  `_open_positions_count_cache` (populated in `_compute_bot_state_features`).

These tests are AST-style on bot/_impl.py + a runtime test on the StateManager
provider mechanism (using a tmp DB).
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot_state_snapshot  # noqa: E402


@pytest.fixture(scope="module")
def bot_py_source() -> str:
    return (ROOT / "bot/_impl.py").read_text()


# ── Helper module location ───────────────────────────────────────────────


def test_helper_module_at_repo_root():
    """Step-2 moves the helper from scripts/ to repo root (matches H-3a
    pattern). bot/_impl.py imports it as a top-level module without sys.path
    mods. Verify the file lives at the expected location."""
    assert (ROOT / "bot_state_snapshot.py").exists(), (
        "bot_state_snapshot.py must live at repo root for bot/_impl.py imports"
    )
    # Old location should not exist (the move is the fix; if both exist,
    # tests pick up the wrong copy).
    assert not (ROOT / "scripts" / "_compute_bot_state_snapshot.py").exists(), (
        "old helper path scripts/_compute_bot_state_snapshot.py must be "
        "removed to avoid two-copies drift"
    )


# ── Provider callback wiring ─────────────────────────────────────────────


def test_state_manager_has_bot_state_provider_setter(bot_py_source):
    """StateManager must expose a setter for the bot-state-provider
    callable. Using a setter (not constructor injection) avoids a
    circular import between StateManager and MainLoop construction."""
    has_setter = "def set_bot_state_provider" in bot_py_source
    assert has_setter, (
        "StateManager must define set_bot_state_provider(provider) so "
        "MainLoop can wire the snapshot computation after both are "
        "constructed (avoids circular ref at __init__ time)"
    )


def test_state_manager_provider_attr_initialized_to_none(bot_py_source):
    """StateManager.__init__ must initialize the provider attribute to
    None so insert_evaluated_opportunity can early-out on un-wired
    callers (e.g., tests, sports/weather/spx engines that don't need
    bot microstate)."""
    has_init = "_bot_state_provider" in bot_py_source
    assert has_init, (
        "StateManager must initialize self._bot_state_provider in "
        "__init__ (default None — provider not yet wired)"
    )


def test_main_loop_wires_provider(bot_py_source):
    """MainLoop must call `self.state.set_bot_state_provider(...)` once
    the main loop is fully constructed. Without this call, every insert
    writes NULL to bot_state_snapshot_json — defeats step 2."""
    has_call = "set_bot_state_provider(" in bot_py_source
    assert has_call, (
        "MainLoop must call state.set_bot_state_provider(...) — without "
        "this, the column stays NULL on every insert"
    )


# ── BEGIN IMMEDIATE timing pattern ───────────────────────────────────────


def test_insert_uses_begin_immediate_timing(bot_py_source):
    """Phase H-2 mandates BEGIN IMMEDIATE timing for lock_wait_ms (per
    bot_state_snapshot.py module docstring "lock_wait_ms semantics —
    MANDATED PATTERN" and the design doc). Other patterns (timing the
    full INSERT, post-execute checkpoint) ARE NOT EQUIVALENT and would
    silently break v2 calibrator features."""
    # Locate insert_evaluated_opportunity body.
    m_def = bot_py_source.find("def insert_evaluated_opportunity")
    assert m_def >= 0
    # Search the body (~400-line window).
    body = bot_py_source[m_def:m_def + 30_000]
    assert "BEGIN IMMEDIATE" in body, (
        "Phase H-2 mandates BEGIN IMMEDIATE in insert_evaluated_opportunity "
        "to measure lock_wait_ms. See bot_state_snapshot.py 'lock_wait_ms "
        "semantics — MANDATED PATTERN'."
    )
    # perf_counter must be adjacent (within ~6 lines) to BEGIN IMMEDIATE
    # so the timing reflects ONLY the lock wait, not insert work.
    body_lines = body.splitlines()
    begin_idxs = [i for i, ln in enumerate(body_lines) if "BEGIN IMMEDIATE" in ln]
    assert begin_idxs, "BEGIN IMMEDIATE not found in insert body"
    ok = False
    for idx in begin_idxs:
        window = "\n".join(body_lines[max(0, idx - 6):idx + 6])
        if "perf_counter()" in window:
            ok = True
            break
    assert ok, (
        "BEGIN IMMEDIATE found but no perf_counter() within 6 lines. "
        "Mandated pattern requires both adjacent so lock_wait_ms reflects "
        "ONLY the BEGIN IMMEDIATE wait."
    )


# ── MainLoop attributes (scan_iter, open_positions cache) ────────────────


def test_main_loop_has_scan_iter_counter(bot_py_source):
    """MainLoop must have a _scan_iter counter incremented at the top of
    scan(). Snapshot reads this; without it, scan_iter is always None.

    Round-1 wiring critique #1+#2: validate CLASS SCOPE via AST.
    `scan()` lives on OpportunityScanner, not MainLoop. So:
    - The init `_scan_iter = 0` must be on MainLoop (or on a class that
      MainLoop owns and routes through, but practically: MainLoop).
    - The increment must NOT be on `self._scan_iter` inside
      OpportunityScanner.scan() — that would AttributeError. It MUST
      route through `self._ml._scan_iter` instead.
    """
    # 1. Substring presence (legacy quick-check).
    has_init = (
        "self._scan_iter = 0" in bot_py_source
        or "self._scan_iter: int = 0" in bot_py_source
    )
    assert has_init, "MainLoop must init self._scan_iter = 0"

    # 2. AST: locate scan() and verify NEITHER `self._scan_iter += 1` NOR
    #    `self._scan_iter = self._scan_iter + 1` (the Assign-with-BinOp
    #    form) appears inside OpportunityScanner.scan(). Round-2 #2/#3:
    #    catch both AugAssign AND Assign forms.
    bot_py_tree = ast.parse(bot_py_source)
    _assert_no_self_attr_write_in_scan(
        bot_py_tree, attr_name="_scan_iter",
    )

    # 3. Assert the correct routing exists somewhere.
    has_routed_increment = "self._ml._scan_iter += 1" in bot_py_source
    assert has_routed_increment, (
        "OpportunityScanner.scan() must route increment through self._ml._scan_iter"
    )


def _assert_no_self_attr_write_in_scan(tree, attr_name: str) -> None:
    """Walk OpportunityScanner.scan() and fail if any statement writes
    `self.<attr_name>` (either AugAssign `self.x += ...` or Assign
    `self.x = ...`). The attribute belongs on MainLoop; writing on
    OpportunityScanner.self crashes at runtime with AttributeError.

    Round-2 wiring review #2/#3: original AST check only inspected
    AugAssign and missed `Assign` forms (e.g., a future refactor
    rewriting `self.x += 1` as `self.x = self.x + 1`).
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == "OpportunityScanner"):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.FunctionDef) and sub.name == "scan"):
                continue
            for stmt in ast.walk(sub):
                target = None
                if isinstance(stmt, ast.AugAssign):
                    target = stmt.target
                elif isinstance(stmt, ast.Assign):
                    # Assign can have multiple targets (e.g., `a = b = 1`).
                    for t in stmt.targets:
                        if (
                            isinstance(t, ast.Attribute)
                            and t.attr == attr_name
                            and isinstance(t.value, ast.Name)
                            and t.value.id == "self"
                        ):
                            target = t
                            break
                if target is None:
                    continue
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == attr_name
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    pytest.fail(
                        f"OpportunityScanner.scan() writes self.{attr_name} — "
                        f"this AttributeErrors at runtime (the attribute "
                        f"belongs on MainLoop). Route through "
                        f"self._ml.{attr_name} instead."
                    )


def test_scan_loop_start_routed_through_main_loop(bot_py_source):
    """Round-1 wiring #1 paired regression: same instance-confusion class
    of bug for `_scan_loop_start`. Must be routed through self._ml.
    Round-2 #2: now also AST-walks scan() to catch ANY direct write to
    `self._scan_loop_start` — both AugAssign and Assign forms."""
    has_routed = "self._ml._scan_loop_start = " in bot_py_source
    assert has_routed, (
        "OpportunityScanner.scan() must set self._ml._scan_loop_start "
        "(not self._scan_loop_start — wrong instance)"
    )
    bot_py_tree = ast.parse(bot_py_source)
    _assert_no_self_attr_write_in_scan(
        bot_py_tree, attr_name="_scan_loop_start",
    )


def test_main_loop_caches_open_positions_count(bot_py_source):
    """MainLoop._compute_bot_state_features (or equivalent) must populate
    self._open_positions_count_cache so the snapshot avoids per-insert
    SQL hits."""
    has_cache = "_open_positions_count_cache" in bot_py_source
    assert has_cache, (
        "MainLoop must populate self._open_positions_count_cache to "
        "avoid per-insert SQL round-trips for open positions count"
    )


# ── Provider runtime contract ───────────────────────────────────────────


def test_provider_runtime_smoke(tmp_path):
    """Runtime smoke test: a fake StateManager with the provider hooked
    up writes the JSON returned by the provider into the column.
    Validates the contract without booting all of bot/_impl.py."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE evaluated_opportunities (
            ticker TEXT, filter_stage TEXT, side TEXT, product_type TEXT,
            data_provenance TEXT, bot_state_snapshot_json TEXT,
            UNIQUE(ticker, filter_stage, side)
        )
    """)
    conn.commit()

    # Simulate the provider as a closure capturing a "main loop" stub.
    class StubMainLoop:
        _scan_iter = 42
        _scan_loop_start = None
        _cooldown_assets = set()
        _open_positions_count_cache = 7
        executor = None
        kalshi_feed = None

    main_loop = StubMainLoop()

    def provider():
        # Provider returns dict; insert site patches lock_wait_ms +
        # serializes inside the writer lock.
        return bot_state_snapshot.compute_bot_state_snapshot(
            main_loop, lock_wait_ms=None
        )

    # Simulated insert-site flow: pre-compute, BEGIN IMMEDIATE, patch, serialize.
    _h2_snap_dict = provider()
    _measured_lock_wait_ms = 1.5
    _h2_snap_dict["lock_wait_ms"] = _measured_lock_wait_ms
    snap_json = json.dumps(_h2_snap_dict, allow_nan=False)

    conn.execute(
        "INSERT INTO evaluated_opportunities "
        "(ticker, filter_stage, side, product_type, data_provenance, "
        " bot_state_snapshot_json) VALUES (?, ?, ?, ?, ?, ?)",
        ("KXBTC15M-T1", "candidate", "yes", "15m", "live_ws", snap_json),
    )
    conn.commit()

    row = conn.execute(
        "SELECT bot_state_snapshot_json FROM evaluated_opportunities"
    ).fetchone()
    assert row is not None
    parsed = json.loads(row[0])
    assert parsed["scan_iter"] == 42
    assert parsed["open_positions_count"] == 7
    assert parsed["lock_wait_ms"] == 1.5
    assert parsed["schema_version"] == 1


# ── Activation regression (skipped test in test_h2_integration_lock_wait_pattern) ─


def test_h2_integration_lock_wait_pattern_test_is_active():
    """Phase H-2 ships a regression test gated by `@pytest.mark.skip`
    until step-2 wiring lands. After step 2 ships, the skip decorator
    must be removed so the regression actively guards against drift."""
    src = (ROOT / "tests" / "test_h2_integration_lock_wait_pattern.py").read_text()
    # The test must NOT have an unconditional @pytest.mark.skip on the
    # regression. Allow @pytest.mark.skipif with a False condition — but
    # plain `@pytest.mark.skip(reason="Enables when bot/_impl.py H-2 ...")` is
    # the deactivated marker we need to remove.
    assert '@pytest.mark.skip(reason="Enables when bot/_impl.py H-2' not in src, (
        "tests/test_h2_integration_lock_wait_pattern.py still has the "
        "step-1 skip decorator. Step 2 must remove it so the regression "
        "actively guards the BEGIN IMMEDIATE pattern."
    )
