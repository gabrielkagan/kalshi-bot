"""Regression tests for the per-thread sqlite3.Connection design in StateManager.

Background — May 3 2026 incident:
    Pre-fix `StateManager` opened ONE `sqlite3.Connection` with
    `check_same_thread=False` and shared it across the main scan thread
    plus several daemon worker threads (notably `SettlementTracker._worker`).
    SQLite's transaction state is per-connection, so cross-thread access
    produced two real failure modes:

    1. `OperationalError: another row available` (CPython 3.11+) when
       one thread had a SELECT cursor mid-iteration and another thread
       executed any DML — the implicit BEGIN's internal step saw the
       leftover SQLITE_ROW state. Fired May 3 at insert_bot_order:5143.

    2. Cross-thread `commit()` SPLICED transactions: thread A's
       BEGIN IMMEDIATE → INSERT → ROLLBACK could be silently committed
       by thread B's `commit()` between the BEGIN and the INSERT,
       breaking atomicity guarantees. Verified via local repro on
       2026-05-03.

    3. Hard Python segfaults under stress (Python 3.9 regression test).

The fix (`StateManager._PerThreadStateConn` in bot.py): every thread
that touches `StateManager.conn` gets its own `sqlite3.Connection`
opened lazily via `threading.local()`. Existing `self.conn.execute(...)`
and `self._state.conn.execute(...)` patterns continue to work — they
transparently hit the calling thread's per-thread connection. WAL mode
on the underlying file handles file-level concurrency. Transaction
state is now per-thread: cross-thread commit splicing is structurally
impossible.

These tests exercise:
    1. Thread isolation — different threads see different sqlite3
       Connection objects.
    2. No commit splicing — Critique 1 scenario; thread A's BEGIN
       IMMEDIATE → ROLLBACK is not interfered with by thread B's
       commit on a separate write.
    3. No implicit-BEGIN collision — Critique 3 scenario; concurrent
       writers do not produce 'cannot start a transaction within a
       transaction'.
    4. SQL semantics — basic execute/fetch/commit work.
    5. PRAGMA per-thread — each thread's conn has WAL + busy_timeout
       set on first access.
    6. StateManager integration — the bot's StateManager actually uses
       the per-thread descriptor.
    7. Attribute forwarding — row_factory, in_transaction work.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# ── Thread isolation ──────────────────────────────────────────────────

class TestThreadIsolation:

    def test_each_thread_gets_its_own_underlying_connection(self, tmp_path):
        """The descriptor returns a different sqlite3.Connection per
        thread. Two threads accessing `descriptor._get()` MUST receive
        different objects so transaction state is independent."""
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "iso.db"))
        d.execute("CREATE TABLE t (id INTEGER)")
        d.commit()

        seen = {}

        def worker(name):
            seen[name] = id(d._get())

        # Force main-thread conn to materialize.
        seen["main"] = id(d._get())
        a = threading.Thread(target=worker, args=("a",))
        b = threading.Thread(target=worker, args=("b",))
        a.start(); b.start(); a.join(); b.join()

        # Three distinct conn instances expected.
        ids = {seen["main"], seen["a"], seen["b"]}
        assert len(ids) == 3, (
            f"expected 3 distinct conn ids, got {seen}"
        )

    def test_same_thread_reuses_its_connection(self, tmp_path):
        """Repeated accesses from one thread reuse the SAME conn — we
        do not open a new Connection per call."""
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "reuse.db"))
        c1 = d._get()
        c2 = d._get()
        c3 = d._get()
        assert c1 is c2 is c3


# ── No commit splicing (Critique 1 scenario) ─────────────────────────

class TestNoCrossThreadCommitSplicing:

    def _run_splice_workload(self, conn_obj):
        """Drive the May 3 splice scenario against a connection-shaped
        object (raw `sqlite3.Connection` for the pre-fix baseline OR
        `_PerThreadStateConn` for the post-fix design). Returns the
        final list of rows in the table.

        Workload:
          - A: BEGIN IMMEDIATE → INSERT row 1 → signal → wait for B →
               ROLLBACK
          - B: wait for A's signal → INSERT row 2 → COMMIT → signal A

        Pre-fix (shared raw conn): B's INSERT joins A's tx (Python
        sqlite3 implicit-tx behavior on a shared connection), B's
        COMMIT commits A's tx INCLUDING A's row 1, A's later ROLLBACK
        is a no-op. Final: [(1, 'A'), (2, 'B')] — A's row leaks despite
        the rollback intent.

        Post-fix (per-thread conn): A and B have separate connections.
        B's INSERT BLOCKS on A's WAL writer lock. A signals, waits for
        B (b_done), times out (B is blocked), runs rollback. Lock
        released. B unblocks, commits row 2. Final: [(2, 'B')] — A's
        rollback is honored.
        """
        a_started = threading.Event()
        b_done = threading.Event()

        def thread_a():
            conn_obj.execute("BEGIN IMMEDIATE")
            conn_obj.execute("INSERT INTO t VALUES (1, 'A')")
            a_started.set()
            b_done.wait(timeout=2.0)
            conn_obj.rollback()

        def thread_b():
            a_started.wait(timeout=5)
            try:
                conn_obj.execute("INSERT INTO t VALUES (2, 'B')")
                conn_obj.commit()
            except sqlite3.OperationalError:
                # Pre-fix race may surface 'database is locked' if the
                # baseline conn happens to use timeout=0; we don't care.
                pass
            b_done.set()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start(); tb.start(); ta.join(timeout=15); tb.join(timeout=15)
        return [
            tuple(r) for r in
            conn_obj.execute("SELECT id, v FROM t ORDER BY id").fetchall()
        ]

    def test_a_rollback_not_spliced_by_b_commit_post_fix(self, tmp_path):
        """The discriminating regression: A inserts row 1 INSIDE its
        BEGIN IMMEDIATE then rolls back. B's intervening commit must
        NOT splice A's row 1 into a committed state. Post-fix: A's
        per-thread conn rolls back row 1 cleanly. Final state has
        ONLY B's row 2.

        This test is paired with the pre-fix baseline below. The two
        produce DIFFERENT outcomes — that's how we know per-thread
        isolation is doing real work."""
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "splice_post.db"))
        d.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        d.commit()

        rows = self._run_splice_workload(d)
        assert rows == [(2, "B")], (
            f"post-fix must NOT splice A's row 1 into B's commit; "
            f"got {rows} — expected only [(2, 'B')]"
        )

    def test_pre_fix_baseline_demonstrates_splice_actually_happens(
        self, tmp_path,
    ):
        """Sanity baseline: drive the SAME workload against a raw shared
        sqlite3.Connection (the pre-fix pattern). Demonstrates the
        splice DOES happen there — i.e., that the test setup is
        non-trivial and the post-fix outcome above is meaningful, not
        a tautology.

        Pre-fix expected: A's row 1 leaks into B's commit because
        Python sqlite3 implicit-tx behavior makes B's INSERT join A's
        in-progress tx on the shared conn. Final: [(1, 'A'), (2, 'B')].
        """
        raw = sqlite3.connect(
            str(tmp_path / "splice_pre.db"),
            check_same_thread=False, timeout=30.0,
        )
        raw.execute("PRAGMA journal_mode=WAL")
        raw.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        raw.commit()

        rows = self._run_splice_workload(raw)
        assert (1, "A") in rows, (
            f"pre-fix baseline failed to demonstrate splice: got {rows}. "
            f"Expected A's row 1 to leak into B's commit. If A's row "
            f"is absent, the test workload is not actually exercising "
            f"the splice surface and the post-fix assertion is tautological."
        )
        raw.close()


# ── No implicit-BEGIN collision (Critique 3 scenario) ────────────────

class TestNoImplicitBeginCollision:

    def test_concurrent_writers_do_not_throw_tx_within_tx(self, tmp_path):
        """Stress: 4 threads doing a fixed number of concurrent writes
        (mix of explicit BEGIN IMMEDIATE and implicit). Pre-fix on a
        shared conn this produced 10K+ 'cannot start a transaction
        within a transaction' under similar load. Post-fix: zero such
        errors because each thread's conn manages its own transaction
        state. SQLITE 'database is locked' is expected under contention
        and retried at the application level; we assert only that the
        tx-within-tx error class is gone.

        Fixed iteration count (not wall-clock) avoids CI flake on
        slow machines."""
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "stress.db"))
        d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        d.commit()

        errors = {}
        next_id = [10000]
        nid_lock = threading.Lock()
        ITERS_PER_THREAD = 50

        def claim_id():
            with nid_lock:
                v = next_id[0]
                next_id[0] += 1
                return v

        def writer_explicit():
            for _ in range(ITERS_PER_THREAD):
                rid = claim_id()
                while True:
                    try:
                        d.execute("BEGIN IMMEDIATE")
                        d.execute(
                            "INSERT INTO t (id, v) VALUES (?, ?)",
                            (rid, rid),
                        )
                        d.commit()
                        break
                    except sqlite3.OperationalError as e:
                        msg = str(e).lower()
                        if "database is locked" in msg:
                            try:
                                d.rollback()
                            except Exception:
                                pass
                            continue  # retry
                        key = type(e).__name__ + ": " + str(e)[:80]
                        errors[key] = errors.get(key, 0) + 1
                        break

        def writer_implicit():
            for _ in range(ITERS_PER_THREAD):
                rid = claim_id()
                while True:
                    try:
                        d.execute(
                            "INSERT INTO t (id, v) VALUES (?, ?)",
                            (rid, rid),
                        )
                        d.commit()
                        break
                    except sqlite3.OperationalError as e:
                        if "database is locked" in str(e).lower():
                            continue
                        key = type(e).__name__ + ": " + str(e)[:80]
                        errors[key] = errors.get(key, 0) + 1
                        break

        ts = [
            threading.Thread(target=writer_explicit),
            threading.Thread(target=writer_explicit),
            threading.Thread(target=writer_implicit),
            threading.Thread(target=writer_implicit),
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
            assert not t.is_alive(), "writer thread did not finish"

        # No tx-within-tx errors.
        tx_within_tx = sum(
            n for k, n in errors.items()
            if "transaction within a transaction" in k.lower()
        )
        assert tx_within_tx == 0, (
            f"got {tx_within_tx} 'tx within a tx' errors — per-thread "
            f"isolation broken. All errors: {errors}"
        )
        # Each thread completes its iters → exactly 4 × ITERS_PER_THREAD
        # rows committed.
        n_rows = d.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        assert n_rows == 4 * ITERS_PER_THREAD, (
            f"expected exactly {4 * ITERS_PER_THREAD} committed rows, "
            f"got {n_rows} (errors: {errors})"
        )


# ── Cursor mid-iteration race (the original May-3 surface) ───────────

class TestCursorMidIterationRace:
    """The actual May-3 incident pattern: thread A holds an unfinalized
    SELECT cursor mid-iteration while thread B issues DML on what was
    pre-fix the SAME shared connection. On CPython 3.11+ this surfaced
    as `OperationalError: another row available`; on Python 3.9 the
    same C-level shared-step state has produced hard segfaults under
    stress.

    Pre-fix repro is hard to make deterministic across Python versions
    (the error fires only when thread B's implicit BEGIN's internal
    sqlite3_step is scheduled while A's cursor still has a pending
    row), so this test asserts only the post-fix invariant: with
    per-thread connections, A's cursor lives on conn_A and B's INSERT
    runs on conn_B — they cannot share statement state by construction.
    The test runs the workload aggressively and asserts NO error of
    any sqlite3-related class fires."""

    def test_cursor_mid_iter_no_error_with_per_thread_conn(self, tmp_path):
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "cur.db"))
        d.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
        for i in range(200):
            d.execute("INSERT INTO t (id, v) VALUES (?, ?)", (i, i))
        d.commit()

        errors = []

        def selector():
            # Iterate the cursor element-by-element, sleeping briefly
            # to maximize the window during which a row is "available
            # but not yet fetched" — exactly the pre-fix race trigger.
            for _ in range(20):
                try:
                    cur = d.execute("SELECT id, v FROM t WHERE id < 200")
                    seen = 0
                    for _row in cur:
                        seen += 1
                        if seen % 50 == 0:
                            time.sleep(0.001)
                    assert seen == 200
                except Exception as e:
                    errors.append(("selector", type(e).__name__, str(e)[:100]))

        def inserter():
            for i in range(200, 400):
                try:
                    d.execute("INSERT INTO t (id, v) VALUES (?, ?)", (i, i))
                    d.commit()
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e).lower():
                        continue  # expected under WAL contention
                    errors.append(("inserter", type(e).__name__, str(e)[:100]))
                except Exception as e:
                    errors.append(("inserter", type(e).__name__, str(e)[:100]))

        ts = [
            threading.Thread(target=selector),
            threading.Thread(target=selector),
            threading.Thread(target=inserter),
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
            assert not t.is_alive(), "thread did not finish"

        # Filter out the benign "database is locked" — assert no
        # cursor-step / tx-state corruption errors landed.
        bad = [
            e for e in errors
            if "database is locked" not in e[2].lower()
        ]
        assert bad == [], (
            f"per-thread isolation should make cursor-step state "
            f"corruption structurally impossible; got: {bad[:5]}"
        )


# ── SQL semantics ────────────────────────────────────────────────────

class TestSqlSemantics:

    def test_basic_insert_select_in_main_thread(self, tmp_path):
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "sql.db"))
        d.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        d.execute("INSERT INTO t VALUES (?, ?)", (1, "a"))
        d.execute("INSERT INTO t VALUES (?, ?)", (2, "b"))
        d.commit()
        rows = d.execute("SELECT id, v FROM t ORDER BY id").fetchall()
        # `_get()` sets row_factory=sqlite3.Row so rows are Row objects;
        # convert to plain tuples for value comparison.
        assert [tuple(r) for r in rows] == [(1, "a"), (2, "b")]

    def test_executemany_works(self, tmp_path):
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "em.db"))
        d.execute("CREATE TABLE t (id INTEGER, v INTEGER)")
        d.executemany(
            "INSERT INTO t VALUES (?, ?)",
            [(1, 10), (2, 20), (3, 30)],
        )
        d.commit()
        total = d.execute("SELECT SUM(v) FROM t").fetchone()[0]
        assert total == 60

    def test_rollback_works(self, tmp_path):
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "rb.db"))
        d.execute("CREATE TABLE t (id INTEGER)")
        d.commit()
        d.execute("INSERT INTO t VALUES (1)")
        d.rollback()
        n = d.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        assert n == 0


# ── PRAGMA per-thread ────────────────────────────────────────────────

class TestPragmaPerThread:

    def test_each_thread_gets_busy_timeout_and_wal(self, tmp_path):
        """First-touch from any thread MUST set PRAGMA busy_timeout
        (per CLAUDE.md "every sqlite3.connect() must set busy_timeout")
        and journal_mode=WAL. Verify on both main thread and a worker."""
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "pragma.db"))
        results = {}

        def check(name):
            results[name + "_busy_timeout"] = (
                d.execute("PRAGMA busy_timeout").fetchone()[0]
            )
            results[name + "_journal_mode"] = (
                d.execute("PRAGMA journal_mode").fetchone()[0].lower()
            )

        check("main")
        worker = threading.Thread(target=check, args=("worker",))
        worker.start(); worker.join()

        assert results["main_busy_timeout"] >= 5000
        assert results["worker_busy_timeout"] >= 5000
        assert results["main_journal_mode"] == "wal"
        assert results["worker_journal_mode"] == "wal"


# ── Attribute forwarding ─────────────────────────────────────────────

class TestAttributeForwarding:

    def test_row_factory_set_get_in_main_thread(self, tmp_path):
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "rf.db"))
        d.row_factory = sqlite3.Row
        assert d.row_factory is sqlite3.Row
        d.execute("CREATE TABLE t (id INTEGER, v TEXT)")
        d.execute("INSERT INTO t VALUES (1, 'x')")
        d.commit()
        row = d.execute("SELECT id, v FROM t").fetchone()
        # sqlite3.Row supports keyed access — proves row_factory
        # actually applied to the underlying conn.
        assert row["v"] == "x"

    def test_in_transaction_attribute_reflects_thread_state(self, tmp_path):
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "txa.db"))
        d.execute("CREATE TABLE t (id INTEGER)")
        d.commit()
        assert d.in_transaction is False
        d.execute("INSERT INTO t VALUES (1)")
        assert d.in_transaction is True
        d.commit()
        assert d.in_transaction is False


# ── StateManager integration ─────────────────────────────────────────

class TestStateManagerIntegration:

    def test_state_manager_uses_per_thread_descriptor(self, tmp_path):
        """Regression: any future refactor that reverts to a single
        shared sqlite3.Connection breaks this. The bot's
        `StateManager.conn` MUST be a `_PerThreadStateConn`."""
        import bot

        sm = bot.StateManager(str(tmp_path / "sm.db"))
        assert isinstance(sm.conn, bot._PerThreadStateConn), (
            f"StateManager.conn is {type(sm.conn).__name__}, "
            f"expected _PerThreadStateConn — descriptor reverted?"
        )

    def test_bot_py_does_not_open_shared_conn_with_check_same_thread_false(self):
        """Regression: catches a future revert to the pre-May-3 shared
        sqlite3.Connection pattern. With per-thread connections, every
        sqlite3.connect() in bot.py runs from the calling thread and
        the default `check_same_thread=True` is the correct choice.
        Adding `check_same_thread=False` would re-introduce the cursor-
        race + commit-splicing surface the May 3 fix closed.

        Only inspect actual `sqlite3.connect(` call sites (not
        docstrings or comments). Walks 8 lines from each call site to
        cover multi-line invocations."""
        import os
        bot_py = os.path.join(PROJECT_ROOT, "bot.py")
        with open(bot_py, encoding="utf-8") as f:
            lines = f.readlines()
        offenders = []
        for i, line in enumerate(lines, 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if "sqlite3.connect(" not in line:
                continue
            window = "".join(lines[i - 1:min(i + 8, len(lines))])
            if "check_same_thread=False" in window:
                offenders.append(f"bot.py:{i}")
        assert not offenders, (
            f"bot.py call sites use check_same_thread=False: {offenders}. "
            f"This re-opens the May 3 cursor-race surface. Per-thread "
            f"connections via _PerThreadStateConn rely on the default "
            f"check_same_thread=True (each thread owns its own conn)."
        )

    def test_state_manager_basic_ops_via_descriptor(self, tmp_path):
        """Sanity: real StateManager operations go through the
        descriptor and produce correct results."""
        import bot

        sm = bot.StateManager(str(tmp_path / "sm2.db"))
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities "
            "(id, ticker, event_ticker, asset, evaluation_time, product_type, "
            " filter_stage, side, status) "
            "VALUES (1, 'T', 'E', 'BTC', '2026-05-03T00:00:00Z', '15m', "
            " 'candidate', 'yes', 'evaluated')"
        )
        sm.conn.commit()
        n = sm.conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities"
        ).fetchone()[0]
        assert n == 1

    def test_main_and_worker_threads_isolated_via_state_manager(self, tmp_path):
        """End-to-end: bot's StateManager exhibits the per-thread
        isolation property when a worker thread does its own
        transaction. Regression against the May 3 cross-thread commit
        splicing failure mode through the actual public API.

        Uses a short sleep to let B's INSERT block on A's writer lock,
        rather than waiting for a 5s b_done timeout — same invariant,
        ~0.1s instead of ~5s wall-clock."""
        import bot

        sm = bot.StateManager(str(tmp_path / "sm3.db"))
        a_in_tx = {"v": None}
        a_started = threading.Event()

        def thread_a():
            sm.conn.execute("BEGIN IMMEDIATE")
            a_started.set()
            # Give B time to issue its INSERT — which will BLOCK on
            # this thread's writer lock under per-thread design.
            time.sleep(0.2)
            sm.conn.execute(
                "INSERT INTO evaluated_opportunities "
                "(id, ticker, event_ticker, asset, evaluation_time, "
                " product_type, filter_stage, side, status) "
                "VALUES (101, 'A', 'EA', 'BTC', '2026-05-03T00:00:00Z', "
                " '15m', 'candidate', 'yes', 'evaluated')"
            )
            a_in_tx["v"] = sm.conn._get().in_transaction
            sm.conn.rollback()

        def thread_b():
            a_started.wait(timeout=5)
            sm.conn.execute(
                "INSERT INTO evaluated_opportunities "
                "(id, ticker, event_ticker, asset, evaluation_time, "
                " product_type, filter_stage, side, status) "
                "VALUES (202, 'B', 'EB', 'ETH', '2026-05-03T00:00:00Z', "
                " '15m', 'candidate', 'yes', 'evaluated')"
            )
            sm.conn.commit()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start(); tb.start(); ta.join(timeout=10); tb.join(timeout=10)

        assert a_in_tx["v"] is True, (
            "thread A's BEGIN IMMEDIATE was closed by thread B's commit"
        )
        ids = [
            r[0] for r in sm.conn.execute(
                "SELECT id FROM evaluated_opportunities ORDER BY id"
            ).fetchall()
        ]
        assert ids == [202], f"expected only B's row 202, got {ids}"


# ── Defensive guards (Round 2 critiques) ─────────────────────────────

class TestDefensiveGuards:

    def test_getattr_recursion_guard_raises_clean_attribute_error(self, tmp_path):
        """If `_local` is somehow missing from `__dict__` (e.g., a
        future `__setstate__` that forgets to restore it, rogue test
        code, etc.), `__getattr__` MUST raise `AttributeError` directly
        — not stack-overflow via recursive calls into `_get()` which
        re-triggers the same lookup."""
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "rec.db"))
        # Simulate the corruption: drop `_local` from __dict__. Normal
        # attribute lookup for `_local` will now fail, so __getattr__
        # fires; without the guard, __getattr__ would call _get() which
        # again accesses self._local → another __getattr__ → recursion.
        del d.__dict__["_local"]
        with pytest.raises(AttributeError):
            d._local  # noqa: B018
        # The sqlite3-attribute access path also raises cleanly (no
        # recursion explosion) because _get() needs `_local`.
        with pytest.raises(AttributeError):
            d.in_transaction  # noqa: B018

    def test_context_manager_protocol_works(self, tmp_path):
        """`with state.conn:` must commit on success, rollback on
        exception — matching sqlite3.Connection's documented
        context-manager protocol. Pre-fix shared conn supported
        this; post-fix wrapper must too."""
        from bot import _PerThreadStateConn

        d = _PerThreadStateConn(str(tmp_path / "cm.db"))
        d.execute("CREATE TABLE t (id INTEGER)")
        d.commit()

        # Success path: with-block commits.
        with d as c:
            assert c is d, "with-block should yield the wrapper itself"
            d.execute("INSERT INTO t VALUES (1)")
        assert d.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

        # Exception path: with-block rolls back.
        with pytest.raises(RuntimeError):
            with d:
                d.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("boom")
        # Row 2 must have been rolled back.
        assert d.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
