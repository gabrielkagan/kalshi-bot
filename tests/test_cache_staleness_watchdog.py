"""Step #5 of architectural rebuild — cache staleness watchdog.

The data-plane / decision-plane split (steps 1-4) has worker threads
populate caches that scan() reads from. Safety net needed: if a
worker thread silently dies (hang, bug, deadlock), its cache becomes
stale forever, and scan() trades on increasingly old data.

This step establishes the contract: caches feeding scan() have a
`last_updated` timestamp; before scan() runs, the orchestrator
checks freshness against a per-cache budget. If any cache is stale,
the orchestrator FAILS CLOSED:
  - scan() is not called (no trade decisions on stale data)
  - A `CACHE_STALE` warning fires (operator visibility)
  - The bot continues running (data-acquisition workers may still
    recover the cache; failing closed is per-tick, not permanent)

V1 scope — `MainLoop._active_windows`:
  Populated by `_refresh_active_windows` worker thread (commit
  f216a8d). Refresh cadence: ~30s. Staleness budget: 60s (allow
  one missed refresh + slack). If worker thread dies, scan stops
  in <60s instead of trading on indefinitely-stale window list.

Out of scope for V1 (additive future work):
  - Balance cache (_get_balance_cached)
  - Orderbook cache (KalshiFeed WS)
  - Coinbase spot-price cache
  Each adds its own freshness check + budget.

See kb/failures/scan-tick-stall-cluster-2026-04-25.md for the
incident class this prevents.
"""

import ast
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch
import bot.main_loop  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/main_loop.py")


class TestActiveWindowsStalenessConstant(unittest.TestCase):
    """A named module-level constant must define the staleness
    budget so it can be tuned without changing logic."""

    def test_active_windows_staleness_constant_defined(self):
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "ACTIVE_WINDOWS_STALENESS_BUDGET_S", src,
            "bot/_impl.py must define ACTIVE_WINDOWS_STALENESS_BUDGET_S "
            "as a module-level constant for the cache staleness "
            "watchdog (step #5).")


class TestRefreshUpdatesTimestamp(unittest.TestCase):
    """`_refresh_active_windows` must set `_active_windows_updated_at`
    on completion. Without this, the staleness check has no signal
    to detect freshness."""

    def test_refresh_method_assigns_active_windows_updated_at(self):
        """AST regression: the body of `_refresh_active_windows`
        must contain `self._active_windows_updated_at = ...`."""
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        for cls in ast.walk(tree):
            if (isinstance(cls, ast.ClassDef)
                    and cls.name == "MainLoop"):
                for node in cls.body:
                    if (isinstance(node, ast.FunctionDef)
                            and node.name == "_refresh_active_windows"):
                        # Walk the body for assignment to
                        # self._active_windows_updated_at.
                        found = False
                        for sub in ast.walk(node):
                            if not isinstance(sub, ast.Assign):
                                continue
                            for tgt in sub.targets:
                                if (isinstance(tgt, ast.Attribute)
                                        and tgt.attr ==
                                        "_active_windows_updated_at"
                                        and isinstance(tgt.value, ast.Name)
                                        and tgt.value.id == "self"):
                                    found = True
                        self.assertTrue(
                            found,
                            "`_refresh_active_windows` must "
                            "assign `self._active_windows_updated_at` "
                            "after refreshing the cache.")
                        return
        self.fail("MainLoop._refresh_active_windows not found")


class TestCacheStalenessCheckAtScanCallSite(unittest.TestCase):
    """`MainLoop._tick` must call a staleness check before invoking
    `scanner.scan(...)`. If the check returns 'stale', scan must
    NOT be invoked this tick."""

    def test_tick_calls_active_windows_freshness_check(self):
        """Tick must call a method named
        `_active_windows_is_stale` (or equivalent — assert by
        searching for the method invocation)."""
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "_active_windows_is_stale", src,
            "MainLoop must define and call "
            "`_active_windows_is_stale` somewhere in the tick "
            "path. This is the staleness check that gates the "
            "scan() call.")

    def test_tick_logs_cache_stale_warning(self):
        """When the staleness check fails, a CACHE_STALE warning
        must fire so operators see the failure-closed event."""
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "CACHE_STALE", src,
            "MainLoop must emit a `CACHE_STALE` warning when the "
            "staleness check fails (operator visibility for the "
            "fail-closed event).")


class TestStalenessLogicCorrect(unittest.TestCase):
    """Functional test of the freshness check itself — fresh caches
    pass, stale caches fail."""

    def setUp(self):
        # Construct a minimal MainLoop-like object that has just
        # the staleness-check method and the timestamp attribute.
        # This avoids the heavy MainLoop.__init__ dependencies.
        import bot
        self.ml = bot.main_loop.MainLoop.__new__(bot.main_loop.MainLoop)
        # Provide minimum attributes the check needs.

    def test_fresh_cache_is_not_stale(self):
        self.ml._active_windows_updated_at = time.time()
        self.assertFalse(self.ml._active_windows_is_stale())

    def test_old_cache_is_stale(self):
        # Set timestamp far in the past — definitely stale.
        self.ml._active_windows_updated_at = time.time() - 3600
        self.assertTrue(self.ml._active_windows_is_stale())

    def test_uninitialized_cache_is_stale(self):
        """If `_active_windows_updated_at` is 0.0 (initial value
        before first refresh), the check must report stale —
        we shouldn't trade until the first successful refresh."""
        self.ml._active_windows_updated_at = 0.0
        self.assertTrue(
            self.ml._active_windows_is_stale(),
            "Uninitialized cache (timestamp=0) must be reported "
            "stale until the first refresh completes.")


class TestStalenessBudgetRelationship(unittest.TestCase):
    """Round 1 P1 fix: budget must be principled, not magic.
    Ties to MARKET_REFRESH_SECONDS so a refactor of the refresh
    cadence can't silently invalidate the budget. 4× allows up to
    3 missed refreshes — tolerates short Kalshi /events outages
    without false-tripping while still catching dead workers in
    ~2 minutes."""

    def test_budget_is_multiple_of_refresh_interval(self):
        import bot
        ratio = (bot.constants.ACTIVE_WINDOWS_STALENESS_BUDGET_S
                 / bot.constants.MARKET_REFRESH_SECONDS)
        self.assertGreaterEqual(
            ratio, 3.0,
            f"Budget must allow at least 2 missed refreshes (ratio>=3) "
            f"to tolerate Kalshi REST timeouts without false-tripping. "
            f"Current ratio: {ratio:.2f}x")
        self.assertLessEqual(
            ratio, 10.0,
            f"Budget must not exceed 10× refresh interval — anything "
            f"larger means we'd trade on stale data for 5+ minutes "
            f"after a worker dies. Current ratio: {ratio:.2f}x")


def _find_tick_func():
    """Helper: return the AST FunctionDef for MainLoop._tick."""
    with open(BOT_PY) as f:
        tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if (isinstance(cls, ast.ClassDef)
                and cls.name == "MainLoop"):
            for node in cls.body:
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "_tick"):
                    return node
    return None


class TestStalenessCheckBeforeScanCall(unittest.TestCase):
    """Round 2 [A6] hardening: AST-walk based ordering guarantee.
    Replaces the brittle `src.find(literal_string)` test which
    would silently pass if a refactor changed `scanner.scan(self._active_windows)`
    to `scanner.scan(local_var)` (the marker disappears, idx<0,
    test fails — but the watchdog logic could be wrong in subtler
    ways that only AST walking catches)."""

    def test_stale_check_call_precedes_scan_call_via_ast(self):
        """Walk MainLoop._tick. Find the FIRST `Call` node for
        `_active_windows_is_stale` and the FIRST `Call` node for
        `scanner.scan`. Assert the staleness call's lineno is less
        than the scan call's lineno."""
        tick = _find_tick_func()
        self.assertIsNotNone(tick, "MainLoop._tick not found")
        check_lineno = None
        scan_lineno = None
        for sub in ast.walk(tick):
            if not isinstance(sub, ast.Call):
                continue
            # Match `self._active_windows_is_stale()`
            if (isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "_active_windows_is_stale"
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "self"
                    and check_lineno is None):
                check_lineno = sub.lineno
            # Match `self.scanner.scan(...)`
            if (isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "scan"
                    and isinstance(sub.func.value, ast.Attribute)
                    and sub.func.value.attr == "scanner"
                    and scan_lineno is None):
                scan_lineno = sub.lineno
        self.assertIsNotNone(
            check_lineno,
            "`self._active_windows_is_stale()` call not found in "
            "MainLoop._tick")
        self.assertIsNotNone(
            scan_lineno,
            "`self.scanner.scan(...)` call not found in MainLoop._tick")
        self.assertLess(
            check_lineno, scan_lineno,
            f"Staleness check at line {check_lineno} must precede "
            f"scanner.scan at line {scan_lineno} in MainLoop._tick. "
            f"If you moved them, the watchdog is bypassed.")

    def test_stale_check_and_scan_in_same_function(self):
        """Sanity: both markers must live inside MainLoop._tick.
        A refactor that moves scan to a separate method without
        bringing the check along would silently disable fail-closed."""
        tick = _find_tick_func()
        self.assertIsNotNone(tick, "MainLoop._tick not found")
        body_src = ast.unparse(tick)
        self.assertIn(
            "_active_windows_is_stale", body_src,
            "MainLoop._tick must contain the "
            "`_active_windows_is_stale` check.")
        self.assertIn(
            "scanner.scan", body_src,
            "MainLoop._tick must contain the `scanner.scan` call.")


class TestForLoopGatedByStalenessCheck(unittest.TestCase):
    """Round 2 [A1] regression: the `for window in self._active_windows`
    iteration block (which feeds vol.update with seconds_to_close)
    must run INSIDE the `else` branch of the staleness gate — NOT
    before it. Otherwise, on stale cache, the loop computes
    negative seconds_to_close (windows past close_time but cache
    not yet refreshed) and feeds them to VolatilityEngine,
    corrupting vol state for the next fresh tick."""

    def test_for_loop_iteration_is_inside_stale_gate_else_branch(self):
        """Walk MainLoop._tick. Find the staleness `If` node. The
        for-loop iterating `self._active_windows` (or a local
        snapshot of it) must appear in the If's `orelse` body, NOT
        as a sibling earlier in `_tick`'s body."""
        tick = _find_tick_func()
        self.assertIsNotNone(tick, "MainLoop._tick not found")
        gate_if = None
        for sub in ast.walk(tick):
            if not isinstance(sub, ast.If):
                continue
            test_src = ast.unparse(sub.test)
            if "_active_windows_is_stale" in test_src:
                gate_if = sub
                break
        self.assertIsNotNone(
            gate_if,
            "Staleness gate `if self._active_windows_is_stale():` "
            "not found in MainLoop._tick")
        # Round 3 [A6] hardening: AST-walk the orelse for any For
        # node whose iter is either:
        #   (a) `self._active_windows` directly, OR
        #   (b) a local Name that was assigned from
        #       `self._active_windows` earlier in the same orelse.
        # Accepts ANY local name (not just `_local_windows`) so
        # renaming the snapshot variable doesn't false-fail.
        local_snapshot_names = set()
        for stmt in gate_if.orelse:
            for sub in ast.walk(stmt):
                if not isinstance(sub, ast.Assign):
                    continue
                # Match `<name> = self._active_windows`
                if (isinstance(sub.value, ast.Attribute)
                        and sub.value.attr == "_active_windows"
                        and isinstance(sub.value.value, ast.Name)
                        and sub.value.value.id == "self"):
                    for tgt in sub.targets:
                        if isinstance(tgt, ast.Name):
                            local_snapshot_names.add(tgt.id)
        found_iter = False
        for stmt in gate_if.orelse:
            for sub in ast.walk(stmt):
                if not isinstance(sub, ast.For):
                    continue
                it = sub.iter
                # Direct iter on self._active_windows
                if (isinstance(it, ast.Attribute)
                        and it.attr == "_active_windows"
                        and isinstance(it.value, ast.Name)
                        and it.value.id == "self"):
                    found_iter = True
                # Iter on a local snapshot
                elif (isinstance(it, ast.Name)
                        and it.id in local_snapshot_names):
                    found_iter = True
        self.assertTrue(
            found_iter,
            "Inside the staleness gate's `else` branch, expected a "
            "`for ... in self._active_windows` OR a `for ... in "
            "<local>` where <local> was assigned from "
            "`self._active_windows`. Found local snapshots: "
            f"{local_snapshot_names}. Gate not protecting iteration.")

        # Also assert no for-loop iterates `self._active_windows`
        # BEFORE the gate (would re-introduce R2 [A1] regression).
        gate_lineno = gate_if.lineno
        for stmt in tick.body:
            if stmt.lineno >= gate_lineno:
                break
            for sub in ast.walk(stmt):
                if not isinstance(sub, ast.For):
                    continue
                it = sub.iter
                if (isinstance(it, ast.Attribute)
                        and it.attr == "_active_windows"
                        and isinstance(it.value, ast.Name)
                        and it.value.id == "self"):
                    self.fail(
                        f"Found `for ... in self._active_windows` "
                        f"BEFORE the staleness gate at line "
                        f"{sub.lineno}. R2 [A1] regression: stale "
                        f"windows feed negative STC into vol.update. "
                        f"Move iteration into the `else` branch.")


class TestFailClosedScopeIsScanOnly(unittest.TestCase):
    """Round 1 P0 fix: when staleness gate trips, only the scan
    block is skipped — settlement/PPO/executor.tick must still run.
    Asserted via AST: `_active_windows_is_stale` must NOT be
    immediately followed by a bare `return` at the function level
    (which would exit the entire tick body).

    Why this matters: settlement processing is idempotent and
    time-sensitive. Skipping it for the duration of a Kalshi
    /events outage compounds the existing settlement-watermark-race
    bug into stuck positions. The gate must scope only to the
    scan call, not the whole tick."""

    def test_stale_check_does_not_return_from_tick(self):
        """Walk MainLoop._tick. Find the `if self._active_windows_is_stale()`
        node. Its body must NOT contain a top-level `return` —
        else the rest of _tick (PPO, weather PPO, etc.) is
        bypassed when the gate trips."""
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        found_check = False
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "MainLoop"):
                continue
            for node in cls.body:
                if (not isinstance(node, ast.FunctionDef)
                        or node.name != "_tick"):
                    continue
                # Walk the tick body. For any If whose test mentions
                # `_active_windows_is_stale`, examine its body.
                for sub in ast.walk(node):
                    if not isinstance(sub, ast.If):
                        continue
                    test_src = ast.unparse(sub.test)
                    if "_active_windows_is_stale" not in test_src:
                        continue
                    found_check = True
                    for body_node in sub.body:
                        self.assertNotIsInstance(
                            body_node, ast.Return,
                            "P0 regression: `if "
                            "self._active_windows_is_stale(): "
                            "return` exits the entire _tick body, "
                            "skipping settlement/PPO/weather PPO. "
                            "Scope the fail-closed to the scan "
                            "block only — set candidates=None and "
                            "let the rest of _tick continue.")
        self.assertTrue(
            found_check,
            "No `if self._active_windows_is_stale()` found in "
            "MainLoop._tick — the gate is missing entirely.")


class TestRefreshTimestampOrderedAfterMerges(unittest.TestCase):
    """Round 2 [A2] regression: TOCTOU race between worker thread
    publishing the merged window list and main thread reading
    `_active_windows_updated_at`. The fix builds the merged list
    locally and atomically swaps + timestamps. AST asserts the
    timestamp assignment appears AFTER the SPX/weather merges in
    `_refresh_active_windows`, not in between."""

    def test_timestamp_set_after_active_windows_swap(self):
        """The line `self._active_windows_updated_at = time.time()`
        must appear AFTER the line `self._active_windows = ...` in
        the body of `_refresh_active_windows`. Otherwise a reader
        could observe `updated_at` as fresh while `_active_windows`
        still points at the previous list (or worse, mid-mutation)."""
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "MainLoop"):
                continue
            for fn in cls.body:
                if (not isinstance(fn, ast.FunctionDef)
                        or fn.name != "_refresh_active_windows"):
                    continue
                # Find the assignment to self._active_windows AND
                # the assignment to self._active_windows_updated_at.
                # Both must exist; updated_at lineno > windows lineno.
                windows_assign_lineno = None
                ts_assign_lineno = None
                for sub in ast.walk(fn):
                    if not isinstance(sub, ast.Assign):
                        continue
                    for tgt in sub.targets:
                        if (isinstance(tgt, ast.Attribute)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id == "self"):
                            if tgt.attr == "_active_windows":
                                # Take the LAST occurrence — the
                                # publish should be the last write.
                                windows_assign_lineno = sub.lineno
                            elif tgt.attr == "_active_windows_updated_at":
                                ts_assign_lineno = sub.lineno
                self.assertIsNotNone(
                    windows_assign_lineno,
                    "`self._active_windows = ...` assignment not "
                    "found in `_refresh_active_windows`")
                self.assertIsNotNone(
                    ts_assign_lineno,
                    "`self._active_windows_updated_at = ...` "
                    "assignment not found in "
                    "`_refresh_active_windows`")
                self.assertGreater(
                    ts_assign_lineno, windows_assign_lineno,
                    f"TOCTOU regression: timestamp assignment at "
                    f"line {ts_assign_lineno} must come AFTER the "
                    f"final `self._active_windows = ...` swap at "
                    f"line {windows_assign_lineno}. Otherwise a "
                    f"reader sees fresh-timestamp + old-list, "
                    f"silently scanning on an incomplete merge.")
                return
        self.fail("MainLoop._refresh_active_windows not found")

    def test_no_extend_on_self_active_windows_in_refresh(self):
        """The fix uses local `new_windows` for SPX/weather extends.
        `self._active_windows.extend(...)` would re-introduce the
        race (concurrent extend on the live list while main thread
        iterates it). Reject if any such call exists in
        `_refresh_active_windows`."""
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "MainLoop"):
                continue
            for fn in cls.body:
                if (not isinstance(fn, ast.FunctionDef)
                        or fn.name != "_refresh_active_windows"):
                    continue
                for sub in ast.walk(fn):
                    if not isinstance(sub, ast.Call):
                        continue
                    if (isinstance(sub.func, ast.Attribute)
                            and sub.func.attr == "extend"
                            and isinstance(sub.func.value, ast.Attribute)
                            and sub.func.value.attr == "_active_windows"
                            and isinstance(sub.func.value.value, ast.Name)
                            and sub.func.value.value.id == "self"):
                        self.fail(
                            f"Found `self._active_windows.extend(...)` "
                            f"in `_refresh_active_windows` at line "
                            f"{sub.lineno}. This re-introduces the "
                            f"Round 2 [A2] race. Build merges into "
                            f"a local list, then swap atomically.")
                return
        self.fail("MainLoop._refresh_active_windows not found")


class TestEmptyRefreshLogIsEdgeTriggered(unittest.TestCase):
    """Round 4 [A3] regression: the 'Market refresh returned 0
    active windows' log line is edge-triggered — fires once on
    transition from non-empty to empty, once on transition back
    to non-empty. Otherwise a sustained Kalshi outage at 30s
    refresh cadence floods the log with 120 identical lines/hr.
    CACHE_STALE provides the sustained-state alert."""

    def test_empty_refresh_log_is_state_attr_based(self):
        """AST: `_refresh_active_windows` must reference an
        edge-trigger flag (something like
        `self._empty_refresh_in_progress`) when handling the
        n==0 branch. Without this state attr, the log will fire
        every refresh cycle."""
        with open(BOT_PY) as f:
            src = f.read()
        self.assertIn(
            "_empty_refresh_in_progress", src,
            "Expected an edge-trigger state attribute "
            "(e.g., `_empty_refresh_in_progress`) so the empty-"
            "refresh log only fires on transitions, not every "
            "30s refresh cycle. R4 [A3] regression.")


class TestEmptyRefreshDoesNotBumpTimestamp(unittest.TestCase):
    """Round 3 [A3] regression: when `_refresh_active_windows`
    produces 0 windows (Kalshi /events returned empty for all
    series), the watchdog timestamp must NOT be updated. Otherwise
    the watchdog says 'fresh' while the cache is empty — silent
    scan-idle with no operator alert.

    Asserted via AST: the assignment `self._active_windows_updated_at
    = time.time()` in `_refresh_active_windows` must live inside
    a conditional branch that is NOT entered when the list is
    empty (i.e., guarded by `if n != 0` or equivalent)."""

    def test_timestamp_assignment_is_guarded_by_nonempty_check(self):
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "MainLoop"):
                continue
            for fn in cls.body:
                if (not isinstance(fn, ast.FunctionDef)
                        or fn.name != "_refresh_active_windows"):
                    continue
                # Find every assignment to
                # `self._active_windows_updated_at`. For each, walk
                # up its parent chain to confirm at least one
                # ancestor is an `If` node whose test references
                # the length of the new windows list (literal `n`,
                # `len(...)`, or similar). We use a parent map.
                parents = {}
                for parent in ast.walk(fn):
                    for child in ast.iter_child_nodes(parent):
                        parents[id(child)] = parent
                ts_assigns = []
                for sub in ast.walk(fn):
                    if not isinstance(sub, ast.Assign):
                        continue
                    for tgt in sub.targets:
                        if (isinstance(tgt, ast.Attribute)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id == "self"
                                and tgt.attr == "_active_windows_updated_at"):
                            ts_assigns.append(sub)
                self.assertTrue(
                    ts_assigns,
                    "`self._active_windows_updated_at = ...` "
                    "assignment not found in "
                    "`_refresh_active_windows`")
                # At least one ts_assign must be guarded by an If
                # whose test mentions `n`, `len(`, or `0` — i.e.,
                # an empty-list guard.
                guarded = False
                for ts in ts_assigns:
                    p = parents.get(id(ts))
                    while p is not None and p is not fn:
                        if isinstance(p, ast.If):
                            test_src = ast.unparse(p.test)
                            if ("len(" in test_src
                                    or "n ==" in test_src
                                    or "n !=" in test_src
                                    or "n >" in test_src
                                    or "n <" in test_src):
                                guarded = True
                                break
                        p = parents.get(id(p))
                self.assertTrue(
                    guarded,
                    "R3 [A3]: timestamp assignment must be guarded "
                    "by an empty-list check (e.g., `if n == 0: ... "
                    "else: self._active_windows_updated_at = ...`). "
                    "Otherwise an empty refresh result silently "
                    "marks the cache as fresh.")
                return
        self.fail("MainLoop._refresh_active_windows not found")


class TestSubscribeShortCircuitOnEmpty(unittest.TestCase):
    """Round 4 [A2] regression (Phase 2.8 R-review A1 update):
    `_subscribe_discovery_orderbooks` must short-circuit when
    active_tickers is empty AND prior subscription set was
    non-empty.

    Pre-Phase 2.8: guard checked `_discovery_ob_tickers` (the
    private previous-cycle view).
    Phase 2.8: guard checks `all_subscribed` (the authoritative
    accessor) — `_discovery_ob_tickers` is empty on first cycle
    after restart even if other paths have populated
    `_subscribed_tickers`, so it can't be the empty-active
    sentinel anymore. Both designs preserve the same invariant:
    transient Kalshi /events failures don't cause mass-unsub."""

    def test_subscribe_short_circuits_on_empty_active_with_prior(self):
        """AST: walk `_subscribe_discovery_orderbooks`. The body
        must contain an early-return branch guarded by a
        condition mentioning `active_tickers` and the
        authoritative-subscribed-set sentinel."""
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "MainLoop"):
                continue
            for fn in cls.body:
                if (not isinstance(fn, ast.FunctionDef)
                        or fn.name != "_subscribe_discovery_orderbooks"):
                    continue
                # Look for an If node whose test mentions
                # `active_tickers` AND either the authoritative
                # `all_subscribed` (Phase 2.8) or the legacy
                # `_discovery_ob_tickers` (pre-2.8). Body must
                # contain a Return.
                found_guard = False
                for sub in ast.walk(fn):
                    if not isinstance(sub, ast.If):
                        continue
                    test_src = ast.unparse(sub.test)
                    if "active_tickers" not in test_src:
                        continue
                    if not ("all_subscribed" in test_src
                            or "_discovery_ob_tickers" in test_src):
                        continue
                    for body_node in ast.walk(sub):
                        if isinstance(body_node, ast.Return):
                            found_guard = True
                            break
                    if found_guard:
                        break
                self.assertTrue(
                    found_guard,
                    "Expected an early-return guard in "
                    "_subscribe_discovery_orderbooks with a "
                    "condition mentioning `active_tickers` and "
                    "either `all_subscribed` (Phase 2.8) or the "
                    "legacy `_discovery_ob_tickers`. Without this, "
                    "an empty refresh causes mass-unsubscribe.")
                return
        self.fail(
            "MainLoop._subscribe_discovery_orderbooks not found")


class TestInitAttrCoverage(unittest.TestCase):
    """Round 2 [A7] regression: the dedup attributes
    `_cache_stale_episode_started_at` and
    `_cache_stale_last_heartbeat_at` MUST be initialized in
    `MainLoop.__init__`. Tests that bypass __init__ via
    `MainLoop.__new__` would otherwise hide a regression where
    these attrs are removed from init — production code would
    AttributeError on first stale tick."""

    def test_init_assigns_dedup_attrs(self):
        """AST: walk MainLoop.__init__ for assignments to
        `self._cache_stale_episode_started_at` and
        `self._cache_stale_last_heartbeat_at`."""
        required = {
            "_cache_stale_episode_started_at",
            "_cache_stale_last_heartbeat_at",
            "_cache_stale_episode_logged",
            "_active_windows_updated_at",
            "_empty_refresh_in_progress",
        }
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        found = set()
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "MainLoop"):
                continue
            for fn in cls.body:
                if (not isinstance(fn, ast.FunctionDef)
                        or fn.name != "__init__"):
                    continue
                for sub in ast.walk(fn):
                    # Plain `self.x = ...`
                    if isinstance(sub, ast.Assign):
                        for tgt in sub.targets:
                            if (isinstance(tgt, ast.Attribute)
                                    and isinstance(tgt.value, ast.Name)
                                    and tgt.value.id == "self"
                                    and tgt.attr in required):
                                found.add(tgt.attr)
                    # Annotated `self.x: T = ...`
                    elif isinstance(sub, ast.AnnAssign):
                        tgt = sub.target
                        if (isinstance(tgt, ast.Attribute)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id == "self"
                                and tgt.attr in required):
                            found.add(tgt.attr)
        missing = required - found
        self.assertEqual(
            missing, set(),
            f"Required init attrs missing from MainLoop.__init__: "
            f"{missing}. Without these, the first call to "
            f"`_maybe_log_cache_stale` AttributeErrors and crashes "
            f"the tick loop. Restore the assignments in __init__.")


def _capture_warnings():
    """Helper: install a temporary handler that captures all
    WARNING+ records. Use as a context manager via _CaptureCtx
    below. assertLogs() enters BEFORE log emission; we sometimes
    want to assert ZERO lines emitted, which assertLogs makes
    awkward (it requires at least one record on its own logger).
    Returning the records list lets the test pin exact counts."""
    import logging as _logging
    records = []
    class _Capture(_logging.Handler):
        def emit(self, record):
            records.append(record)
    cap = _Capture(level=_logging.WARNING)
    root = _logging.getLogger()
    root.addHandler(cap)
    return cap, records


def _release_capture(cap):
    import logging as _logging
    _logging.getLogger().removeHandler(cap)


class TestCacheStaleLogDedup(unittest.TestCase):
    """Round 1+2+3 dedup: log discipline.
      • Calling the gate every tick at hours-long stalls would
        emit 3,600+ identical lines/hr without dedup.
      • R3 [A4] symmetric flicker: BOTH start and recovery hold
        back until the episode outlives `_CACHE_STALE_MIN_EPISODE_S`.
        Brief flickers leave no trace on either side.
      • Sustained stalls produce a coherent triple:
        start → heartbeat (every 60s) → recovery."""

    def setUp(self):
        import bot
        self.ml = bot.main_loop.MainLoop.__new__(bot.main_loop.MainLoop)
        self.ml._active_windows_updated_at = 0.0
        self.ml._cache_stale_episode_started_at = 0.0
        self.ml._cache_stale_last_heartbeat_at = 0.0
        self.ml._cache_stale_episode_logged = False

    def test_brief_flicker_emits_zero_lines(self):
        """R3 [A4]: a stale→fresh transition within
        `_CACHE_STALE_MIN_EPISODE_S` must emit ZERO log lines on
        either side — symmetric flicker dedup. R4 [A6]: also
        verifies ALL three dedup attrs reset on recovery."""
        cap, records = _capture_warnings()
        try:
            # Start: first stale tick records start time but does
            # NOT log (deferred until threshold).
            self.ml._maybe_log_cache_stale()
            # Several more ticks within the flicker threshold.
            self.ml._maybe_log_cache_stale()
            self.ml._maybe_log_cache_stale()
            # Recovery: episode_logged is still False, so no
            # recovery line either.
            self.ml._maybe_log_cache_fresh_recovery()
        finally:
            _release_capture(cap)
        cache_lines = [r for r in records
                       if "CACHE_STALE" in r.getMessage()
                       or "CACHE_FRESH" in r.getMessage()]
        self.assertEqual(
            cache_lines, [],
            f"Brief flicker must emit zero CACHE_STALE/CACHE_FRESH "
            f"lines, got: {[r.getMessage() for r in cache_lines]}")
        # R4 [A6]: all three dedup attrs must reset on recovery.
        # If the heartbeat/logged resets are accidentally removed,
        # the next stale episode would inherit stale state and
        # either skip the start line or emit a phantom heartbeat.
        self.assertEqual(
            self.ml._cache_stale_episode_started_at, 0.0,
            "Recovery must reset episode tracker.")
        self.assertEqual(
            self.ml._cache_stale_last_heartbeat_at, 0.0,
            "Recovery must reset last_heartbeat_at — else the "
            "next episode's heartbeat fires at unexpected time.")
        self.assertFalse(
            self.ml._cache_stale_episode_logged,
            "episode_logged must reset to False on recovery — "
            "else the next episode skips the deferred-start line.")

    def test_sustained_episode_emits_start_and_recovery(self):
        """R3 [A4]: an episode that outlives the threshold must
        emit BOTH a start line and a recovery line — symmetric.
        Backdate `_episode_started_at` past the flicker window
        so the deferred-start condition is satisfied."""
        # Tick 1: record start time, no log yet.
        self.ml._maybe_log_cache_stale()
        # Backdate so the next tick crosses the threshold.
        self.ml._cache_stale_episode_started_at = (
            time.time() - 30.0)
        cap, records = _capture_warnings()
        try:
            # Tick 2: episode_age >= 10s → start line fires.
            self.ml._maybe_log_cache_stale()
            # Recovery: episode_logged is now True → recovery fires.
            self.ml._maybe_log_cache_fresh_recovery()
        finally:
            _release_capture(cap)
        starts = [r for r in records
                  if "CACHE_STALE" in r.getMessage()
                  and "HEARTBEAT" not in r.getMessage()]
        recoveries = [r for r in records
                      if "CACHE_FRESH" in r.getMessage()]
        self.assertEqual(
            len(starts), 1,
            f"Expected 1 CACHE_STALE start line, got {len(starts)}")
        self.assertEqual(
            len(recoveries), 1,
            f"Expected 1 CACHE_FRESH recovery line, got "
            f"{len(recoveries)}")

    def test_repeated_stale_calls_emit_one_start_then_quiet(self):
        """Once the start line fires, subsequent ticks within
        the heartbeat window emit nothing more (until 60s
        elapses or recovery)."""
        # Backdate so the start fires on the first call.
        self.ml._maybe_log_cache_stale()
        self.ml._cache_stale_episode_started_at = (
            time.time() - 30.0)
        cap, records = _capture_warnings()
        try:
            for _ in range(100):
                self.ml._maybe_log_cache_stale()
        finally:
            _release_capture(cap)
        starts = [r for r in records
                  if "CACHE_STALE" in r.getMessage()
                  and "HEARTBEAT" not in r.getMessage()]
        self.assertEqual(
            len(starts), 1,
            f"Expected exactly 1 start line across 100 ticks, "
            f"got {len(starts)}")

    def test_uninitialized_message_does_not_emit_inf(self):
        """R2 [A4]: when timestamp is 0.0 (never refreshed),
        the log line must say 'never refreshed' / 'uninitialized'
        — NEVER 'age=inf'. R3 [A4]: must wait until the deferred
        start fires (backdate the episode)."""
        self.ml._active_windows_updated_at = 0.0
        # Tick 1 records start; tick 2 (after backdate) emits.
        self.ml._maybe_log_cache_stale()
        self.ml._cache_stale_episode_started_at = (
            time.time() - 30.0)
        with self.assertLogs(level="WARNING") as cm:
            self.ml._maybe_log_cache_stale()
        msgs = [r.getMessage() for r in cm.records
                if "CACHE_STALE" in r.getMessage()
                and "HEARTBEAT" not in r.getMessage()]
        self.assertEqual(
            len(msgs), 1,
            f"Expected 1 start line, got {msgs}")
        self.assertNotIn(
            "inf", msgs[0],
            f"Uninitialized message must not contain literal 'inf' "
            f"— got: {msgs[0]!r}")
        self.assertTrue(
            ("uninitialized" in msgs[0].lower()
             or "never refreshed" in msgs[0].lower()),
            f"Uninitialized message must say 'uninitialized' or "
            f"'never refreshed' — got: {msgs[0]!r}")

    def test_recovery_no_log_if_no_episode(self):
        """If no stale episode was started, recovery is a no-op."""
        cap, records = _capture_warnings()
        try:
            self.ml._maybe_log_cache_fresh_recovery()
        finally:
            _release_capture(cap)
        self.assertEqual(
            [r for r in records
             if "CACHE_FRESH" in r.getMessage()],
            [],
            "Recovery on the happy path (no prior episode) must "
            "not log — happy path is silent.")

    def test_heartbeat_fires_60s_after_deferred_start(self):
        """R4 [A1, A7] coverage: after the deferred-start line
        fires, the heartbeat must fire 60s LATER (not 60s after
        episode start). Round 4 reviewer noted this code path
        was previously untested."""
        # Tick 1 records start; tick 2 (after backdating) fires
        # the deferred start.
        self.ml._maybe_log_cache_stale()
        self.ml._cache_stale_episode_started_at = (
            time.time() - 30.0)
        self.ml._maybe_log_cache_stale()  # fires start line
        self.assertTrue(
            self.ml._cache_stale_episode_logged,
            "After deferred-start fires, episode_logged must be True")
        # Verify a tick immediately after start does NOT emit
        # heartbeat (last_heartbeat_at was just reset).
        cap, records = _capture_warnings()
        try:
            self.ml._maybe_log_cache_stale()
        finally:
            _release_capture(cap)
        immediate_lines = [r for r in records
                           if "HEARTBEAT" in r.getMessage()]
        self.assertEqual(
            immediate_lines, [],
            "Heartbeat must not fire immediately after deferred start")
        # Now backdate `_cache_stale_last_heartbeat_at` so the
        # next tick crosses the 60s heartbeat threshold.
        self.ml._cache_stale_last_heartbeat_at = (
            time.time() - 65.0)
        cap, records = _capture_warnings()
        try:
            self.ml._maybe_log_cache_stale()
        finally:
            _release_capture(cap)
        heartbeat_lines = [r for r in records
                           if "HEARTBEAT" in r.getMessage()]
        self.assertEqual(
            len(heartbeat_lines), 1,
            f"Heartbeat must fire exactly once when 60s have "
            f"elapsed since last_heartbeat_at; got {len(heartbeat_lines)}")
        # Subsequent ticks within next 60s window must be quiet.
        cap, records = _capture_warnings()
        try:
            for _ in range(50):
                self.ml._maybe_log_cache_stale()
        finally:
            _release_capture(cap)
        more_heartbeats = [r for r in records
                           if "HEARTBEAT" in r.getMessage()]
        self.assertEqual(
            more_heartbeats, [],
            "Heartbeat must not re-fire within 60s window")

    def test_heartbeat_does_not_fire_before_deferred_start(self):
        """R4 [A1] sanity: heartbeat must NOT fire while the
        episode is still in the flicker-suppression window
        (episode_logged=False). Otherwise we'd see HEARTBEAT
        lines before any START line — confusing log order."""
        self.ml._maybe_log_cache_stale()
        # episode_logged is False; the start line hasn't fired.
        # Force last_heartbeat_at far in the past.
        self.ml._cache_stale_last_heartbeat_at = (
            time.time() - 600.0)
        cap, records = _capture_warnings()
        try:
            self.ml._maybe_log_cache_stale()
        finally:
            _release_capture(cap)
        heartbeats = [r for r in records
                      if "HEARTBEAT" in r.getMessage()]
        self.assertEqual(
            heartbeats, [],
            "Heartbeat must not fire while episode_logged is False")


if __name__ == "__main__":
    unittest.main()
