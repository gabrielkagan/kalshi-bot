"""Phase H-2 integration regression scaffold — BEGIN IMMEDIATE lock-wait pattern.

Status: SKIPPED until bot/_impl.py H-2 integration commits land.

Purpose: once the operator wires `bot_state_snapshot_json` into
`StateManager.insert_evaluated_opportunity`, the insert site MUST measure
`lock_wait_ms` using the BEGIN IMMEDIATE timing pattern documented in
`bot_state_snapshot.py` (module docstring, "lock_wait_ms
semantics — MANDATED PATTERN") and `kb/decisions/phase-h2-bot-microstate-fwd-may02.md`.

Pattern (binding):

    t0 = time.perf_counter()
    conn.execute("BEGIN IMMEDIATE")
    lock_wait_ms = (time.perf_counter() - t0) * 1000.0
    conn.execute("INSERT INTO evaluated_opportunities (...) VALUES (...)")
    conn.execute("COMMIT")

Why this regression matters: v2 calibrator training pins on `lock_wait_ms`
as a feature. If the operator (or a future refactor) measures lock contention
via a different pattern (e.g. timing the full INSERT, timing post-commit
checkpoint), the distribution shifts under v2 and feature joins become
apples-to-oranges. This test enforces the pattern at the bot/_impl.py source
level via grep, so a drift triggers on the next test run rather than
silently corrupting training data.

Activation procedure (once integration ships):
  1. Remove the @pytest.mark.skip decorator below.
  2. Run the test — it greps bot/_impl.py for `BEGIN IMMEDIATE` adjacent to
     `perf_counter()` in the insert path.
  3. If it fails, the integration commit broke the pattern — fix bot/_impl.py.

Until removed, this test is a NO-OP scaffold (skipped, not failed).
"""

from __future__ import annotations

import os
import re

import pytest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOT_PY_PATH = os.path.join(PROJECT_ROOT, "bot/_impl.py")


def test_insert_site_uses_begin_immediate_for_lock_wait():
    """Grep bot/_impl.py's insert path for BEGIN IMMEDIATE adjacent to perf_counter.

    Once the operator wires the H-2 snapshot into
    `StateManager.insert_evaluated_opportunity`, the insert site MUST
    measure `lock_wait_ms` via the MANDATED pattern: a `perf_counter()`
    immediately before `BEGIN IMMEDIATE`, with the elapsed delta computed
    immediately after the lock is granted.

    This regression test:
      1. Reads bot/_impl.py.
      2. Locates the insert_evaluated_opportunity body (heuristic: find
         the def + scan a window of ~400 lines after).
      3. Asserts that within that window, `BEGIN IMMEDIATE` appears
         within ~6 lines of a `perf_counter()` call (allowing a small
         buffer for whitespace + comments).
    """
    with open(BOT_PY_PATH) as fh:
        src = fh.read()

    # Locate insert_evaluated_opportunity.
    m = re.search(r"def\s+insert_evaluated_opportunity\b", src)
    assert m, "insert_evaluated_opportunity not found in bot/_impl.py"
    body_start = m.start()
    # Bumped from 20_000 → 40_000 chars to cover the full function body.
    # The function is ~700 lines (huge VALUES tuple + ON CONFLICT clause)
    # which exceeds 20_000 chars.
    body_end = body_start + 40_000
    body = src[body_start:body_end]

    # Find BEGIN IMMEDIATE occurrences in the body.
    begin_imm_lines = [
        i for i, line in enumerate(body.splitlines())
        if "BEGIN IMMEDIATE" in line
    ]
    assert begin_imm_lines, (
        "Phase H-2 integration drift: insert_evaluated_opportunity "
        "does not contain `BEGIN IMMEDIATE`. The MANDATED lock_wait_ms "
        "timing pattern requires it. See "
        "bot_state_snapshot.py (module docstring)."
    )

    # For each BEGIN IMMEDIATE, require a perf_counter() within 6 lines.
    body_lines = body.splitlines()
    ok = False
    for idx in begin_imm_lines:
        window_start = max(0, idx - 6)
        window_end = min(len(body_lines), idx + 6)
        window_text = "\n".join(body_lines[window_start:window_end])
        if "perf_counter()" in window_text:
            ok = True
            break
    assert ok, (
        "Phase H-2 integration drift: `BEGIN IMMEDIATE` found in "
        "insert_evaluated_opportunity, but no `perf_counter()` within "
        "6 lines. The MANDATED pattern requires both calls adjacent so "
        "lock_wait_ms reflects ONLY the BEGIN IMMEDIATE wait. See "
        "kb/decisions/phase-h2-bot-microstate-fwd-may02.md "
        "(\"lock_wait_ms semantics\")."
    )
