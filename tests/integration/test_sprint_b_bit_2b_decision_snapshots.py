"""Sprint B Bit B.2b — order_decision_snapshots TDD scaffold (RED → GREEN).

Captures full orderbook ladder + spot context at every maker-vs-taker
decision moment, plus an opportunistic 30s post-decision tick stream
(JSON-blob approach, option (c) per ticket RCA).

RCA: bot/executor.py `execute()` is the single canonical "decide what
to do" gate. It routes through these decision points:

  * Direct-taker family (decision_type='taker_first'):
      - _execute_hourly_taker         line ~291
      - _execute_weather_no_taker     line ~349
      - _execute_hourly_no_taker      line ~389
      - _execute_dc_taker             line ~2343
      - _execute_tm_taker             line ~2491
      - _execute_lpne_taker           line ~2672
      - _execute_bracket_no_taker     line ~2760
      - SOL taker-first override      line ~903
      - direct_taker <180s            line ~1076
  * Maker-first family (decision_type='maker_first'):
      - Tier 1 normal maker           line ~1250
      - Tier 2 degraded maker         line ~1235
  * Escalation (decision_type='escalate'):
      - post_only_taker Tier 3        line ~1162
      - _escalate_to_taker_inner      line ~1989

A second snapshot row with the SAME decision_id and
`decision_type='escalate'` is emitted when a maker-first decision
later escalates — letting a learner reconstruct the full route
"maker_first @ t=0 → escalate @ t=15s" sequence.

The 30s tick stream is an opportunistic JSON list of
{t_offset_s, best_yes_ask, best_yes_bid, ask_depth} dicts, persisted
on the *initial* snapshot row's `followup_ticks_json` column. Tick
loop populates it via StateManager.append_decision_followup_tick()
during the 30s post-decision window. Approach (c) from ticket — no
new table, single-row queryability.

Retention: 90 days, pruned by StateManager.prune_old_decision_snapshots(),
invoked from MainLoop._log_daily_summary() (same daily housekeeping
hook used by other rotators).

Ship gate: 2 consecutive zero-CRITICAL/MAJOR adversarial rounds.
"""
from __future__ import annotations

import ast
import datetime
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bot  # noqa: E402
import bot.state as state_mod  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class _TempDB(unittest.TestCase):
    def _fresh_path(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def _columns(self, sm, table):
        return {row["name"]: row["type"]
                for row in sm.conn.execute(f"PRAGMA table_info({table})")}


# ── Cluster 1: schema ──────────────────────────────────────────────────────

class TestOrderDecisionSnapshotsTable(_TempDB):
    def test_table_exists_with_required_columns(self):
        sm = state_mod.StateManager(db_path=self._fresh_path())
        cols = self._columns(sm, "order_decision_snapshots")
        for required in ("id", "decision_id", "ticker", "asset",
                         "decision_time", "decision_type",
                         "orderbook_levels_json", "spot_price",
                         "seconds_to_close", "vol_regime", "source",
                         "followup_ticks_json"):
            self.assertIn(required, cols, f"missing column: {required}")
        pk_rows = list(sm.conn.execute(
            "PRAGMA table_info(order_decision_snapshots)"))
        id_row = next(r for r in pk_rows if r["name"] == "id")
        self.assertEqual(id_row["pk"], 1)

    def test_decision_type_check_constraint(self):
        import sqlite3
        sm = state_mod.StateManager(db_path=self._fresh_path())
        for dt in ("maker_first", "taker_first", "escalate", "shadow"):
            sm.conn.execute(
                "INSERT INTO order_decision_snapshots "
                "(decision_id, ticker, asset, decision_time, decision_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"d-{dt}", "KXTEST-1", "BTC", "2026-05-12T00:00:00Z", dt))
        sm.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            sm.conn.execute(
                "INSERT INTO order_decision_snapshots "
                "(decision_id, ticker, asset, decision_time, decision_type) "
                "VALUES (?, ?, ?, ?, ?)",
                ("d-bad", "KXTEST-1", "BTC", "2026-05-12T00:00:00Z", "MAKER"))

    def test_indexes_exist(self):
        sm = state_mod.StateManager(db_path=self._fresh_path())
        idx = {row["name"] for row in sm.conn.execute(
            "PRAGMA index_list(order_decision_snapshots)")}
        self.assertTrue(any("decision_id" in n for n in idx),
                        f"no decision_id index found: {idx}")
        self.assertTrue(any("ticker" in n for n in idx),
                        f"no ticker index found: {idx}")

    def test_required_columns_not_null(self):
        sm = state_mod.StateManager(db_path=self._fresh_path())
        cols = list(sm.conn.execute(
            "PRAGMA table_info(order_decision_snapshots)"))
        notnull = {r["name"]: r["notnull"] for r in cols}
        for required in ("decision_id", "ticker", "asset",
                         "decision_time", "decision_type"):
            self.assertEqual(notnull[required], 1,
                             f"{required} should be NOT NULL")

    def test_create_table_inside_create_tables(self):
        """Sanity: lives in StateManager._create_tables → shares parent
        connection (WAL + busy_timeout=30000 already set in __init__)."""
        src = (REPO_ROOT / "bot" / "state.py").read_text()
        idx_ct = src.find("def _create_tables")
        idx_t = src.find(
            "CREATE TABLE IF NOT EXISTS order_decision_snapshots", idx_ct)
        self.assertGreater(idx_t, idx_ct,
                           "CREATE TABLE order_decision_snapshots must "
                           "live inside _create_tables")
        for line_no, line in enumerate(src.splitlines(), 1):
            if ("order_decision_snapshots" in line
                    and "sqlite3.connect" in line):
                self.fail(f"bot/state.py:{line_no} opens a separate "
                          "sqlite3 connection — must use self.conn")


# ── Cluster 2: helper insert + followup-append ────────────────────────────

class TestInsertDecisionSnapshot(_TempDB):
    def test_basic_insert_via_helper(self):
        sm = state_mod.StateManager(db_path=self._fresh_path())
        self.assertTrue(hasattr(sm, "insert_decision_snapshot"))
        sm.insert_decision_snapshot(
            decision_id="dec-abc",
            ticker="KXBTC15M-26MAY121200-100000",
            asset="BTC",
            decision_type="maker_first",
            spot_price=100123.4,
            seconds_to_close=300.0,
            vol_regime="medium",
            source="terminal_momentum_96",
            orderbook_levels_json='{"yes_bids":[[87,100]],"yes_asks":[[96,1]]}',
        )
        rows = list(sm.conn.execute(
            "SELECT * FROM order_decision_snapshots WHERE decision_id=?",
            ("dec-abc",)))
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["decision_type"], "maker_first")
        self.assertEqual(r["asset"], "BTC")
        self.assertEqual(r["source"], "terminal_momentum_96")
        self.assertEqual(r["spot_price"], 100123.4)
        self.assertEqual(r["seconds_to_close"], 300.0)
        self.assertIsNotNone(r["decision_time"])  # auto-filled

    def test_escalation_second_row_shares_decision_id(self):
        sm = state_mod.StateManager(db_path=self._fresh_path())
        sm.insert_decision_snapshot(
            decision_id="dec-xyz", ticker="KXBTC15M-X", asset="BTC",
            decision_type="maker_first", source="ladder")
        sm.insert_decision_snapshot(
            decision_id="dec-xyz", ticker="KXBTC15M-X", asset="BTC",
            decision_type="escalate", source="ladder")
        rows = list(sm.conn.execute(
            "SELECT decision_type FROM order_decision_snapshots "
            "WHERE decision_id=? ORDER BY id",
            ("dec-xyz",)))
        self.assertEqual([r["decision_type"] for r in rows],
                         ["maker_first", "escalate"])

    def test_autofill_orderbook_from_scan_cache(self):
        """When orderbook_levels_json is None, helper auto-fills from
        _scan_ob_cache (same freshness-gated read path as the existing
        order_lifecycle_snapshots helper). Mirrors bot/state.py
        _get_fresh_ob_ladder() contract."""
        import time
        sm = state_mod.StateManager(db_path=self._fresh_path())
        sm._scan_ob_cache["KXBTC-A"] = (
            time.monotonic(),
            '{"yes_bids":[[88,50]],"yes_asks":[[95,10]]}')
        sm.insert_decision_snapshot(
            decision_id="dec-1", ticker="KXBTC-A", asset="BTC",
            decision_type="taker_first", source="sol_taker_override")
        r = list(sm.conn.execute(
            "SELECT orderbook_levels_json FROM order_decision_snapshots "
            "WHERE decision_id='dec-1'"))[0]
        self.assertIsNotNone(r["orderbook_levels_json"])
        self.assertIn("yes_asks", r["orderbook_levels_json"])


class TestAppendDecisionFollowupTick(_TempDB):
    def test_append_30s_tick_stream(self):
        sm = state_mod.StateManager(db_path=self._fresh_path())
        self.assertTrue(hasattr(sm, "append_decision_followup_tick"))
        sm.insert_decision_snapshot(
            decision_id="dec-followup", ticker="KXBTC-A", asset="BTC",
            decision_type="maker_first", source="ladder")
        sm.append_decision_followup_tick(
            decision_id="dec-followup",
            t_offset_s=5.0, best_yes_ask=96, best_yes_bid=88,
            ask_depth=10)
        sm.append_decision_followup_tick(
            decision_id="dec-followup",
            t_offset_s=10.0, best_yes_ask=95, best_yes_bid=88,
            ask_depth=20)
        r = list(sm.conn.execute(
            "SELECT followup_ticks_json FROM order_decision_snapshots "
            "WHERE decision_id='dec-followup'"))[0]
        ticks = json.loads(r["followup_ticks_json"])
        self.assertEqual(len(ticks), 2)
        self.assertEqual(ticks[0]["t_offset_s"], 5.0)
        self.assertEqual(ticks[1]["best_yes_ask"], 95)

    def test_append_caps_at_30s_window(self):
        """Ticket: 30s post-decision tick stream. Helper rejects ticks
        beyond the 30s window (defensive — caller should also gate)."""
        sm = state_mod.StateManager(db_path=self._fresh_path())
        sm.insert_decision_snapshot(
            decision_id="dec-cap", ticker="KXBTC-A", asset="BTC",
            decision_type="maker_first", source="ladder")
        # Within window: accepted
        sm.append_decision_followup_tick(
            decision_id="dec-cap", t_offset_s=29.0,
            best_yes_ask=96, best_yes_bid=88, ask_depth=10)
        # Beyond window: silently dropped (no exception, no append)
        sm.append_decision_followup_tick(
            decision_id="dec-cap", t_offset_s=45.0,
            best_yes_ask=99, best_yes_bid=88, ask_depth=10)
        r = list(sm.conn.execute(
            "SELECT followup_ticks_json FROM order_decision_snapshots "
            "WHERE decision_id='dec-cap'"))[0]
        ticks = json.loads(r["followup_ticks_json"])
        self.assertEqual(len(ticks), 1)


# ── Cluster 3: AST-grep — every decision path emits a snapshot ────────────

class TestExecutorAllDecisionPointsInstrumented(unittest.TestCase):
    """Planted-defect AST check: every direct/maker/escalate path in
    bot/executor.py must call _emit_decision_snapshot (or
    insert_decision_snapshot) at least once.
    """
    EXPECTED_PATHS = [
        "_execute_hourly_taker",
        "_execute_weather_no_taker",
        "_execute_hourly_no_taker",
        "_execute_dc_taker",
        "_execute_tm_taker",
        "_execute_lpne_taker",
        "_execute_bracket_no_taker",
    ]
    # SOL taker-first + direct-taker <180s + maker tier-1/2 cluster all
    # live inside `execute()` itself.
    MIN_EMITS_IN_EXECUTE = 3
    MIN_EMITS_IN_ESCALATE = 1

    def setUp(self):
        src = (REPO_ROOT / "bot" / "executor.py").read_text()
        self.tree = ast.parse(src)

    def _find_func(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def _emit_call_count(self, func_node):
        if func_node is None:
            return 0
        count = 0
        for n in ast.walk(func_node):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Attribute):
                    if f.attr in ("insert_decision_snapshot",
                                  "_emit_decision_snapshot"):
                        count += 1
                elif isinstance(f, ast.Name):
                    if f.id == "_emit_decision_snapshot":
                        count += 1
        return count

    def test_each_taker_first_path_emits_snapshot(self):
        for fname in self.EXPECTED_PATHS:
            node = self._find_func(fname)
            self.assertIsNotNone(node, f"function {fname} not found")
            self.assertGreaterEqual(
                self._emit_call_count(node), 1,
                f"{fname} must emit at least 1 decision snapshot "
                "(call insert_decision_snapshot or _emit_decision_snapshot)")

    def test_execute_emits_for_sol_direct_and_maker(self):
        node = self._find_func("execute")
        self.assertIsNotNone(node)
        self.assertGreaterEqual(
            self._emit_call_count(node), self.MIN_EMITS_IN_EXECUTE,
            "execute() must emit decision snapshots for SOL "
            "taker-first, direct-taker, and maker tier paths "
            f"(≥{self.MIN_EMITS_IN_EXECUTE}).")

    def test_escalation_path_emits_snapshot(self):
        node = self._find_func("_escalate_to_taker_inner")
        self.assertIsNotNone(node)
        self.assertGreaterEqual(
            self._emit_call_count(node), self.MIN_EMITS_IN_ESCALATE,
            "_escalate_to_taker_inner must emit an 'escalate' decision "
            "snapshot (mirrors the original decision_id).")


# ── Cluster 4: runtime — escalation snapshot reuses original decision_id ──

class TestDecisionIdThreadedThroughCandidate(unittest.TestCase):
    """When a maker-first decision later escalates to taker, the
    second snapshot row's decision_id MUST match the first — so a
    learner can join the two events into a single sequence. The
    candidate dict carries 'decision_id' once execute() seeds it; the
    escalation path reads it back from order['candidate']."""

    def test_decision_id_referenced_in_execute_and_escalate(self):
        src = (REPO_ROOT / "bot" / "executor.py").read_text()
        self.assertIn("decision_id", src,
                      "bot/executor.py must reference decision_id")
        tree = ast.parse(src)
        seen_in = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                body_src = ast.unparse(node)
                if "decision_id" in body_src:
                    seen_in.add(node.name)
        for required in ("execute", "_escalate_to_taker_inner"):
            self.assertIn(required, seen_in,
                          f"{required} must reference decision_id "
                          "to thread it through the escalation path")


# ── Cluster 5: retention ──────────────────────────────────────────────────

class TestRetentionPruneOldDecisionSnapshots(_TempDB):
    def test_prune_drops_rows_older_than_90d(self):
        sm = state_mod.StateManager(db_path=self._fresh_path())
        self.assertTrue(hasattr(sm, "prune_old_decision_snapshots"))
        now = datetime.datetime.now(datetime.timezone.utc)
        old = (now - datetime.timedelta(days=95)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        recent = (now - datetime.timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        for did, dt in [("old-1", old), ("old-2", old),
                        ("recent-1", recent)]:
            sm.conn.execute(
                "INSERT INTO order_decision_snapshots "
                "(decision_id, ticker, asset, decision_time, decision_type) "
                "VALUES (?, ?, ?, ?, ?)",
                (did, "KXBTC-A", "BTC", dt, "maker_first"))
        sm.conn.commit()
        deleted = sm.prune_old_decision_snapshots(days=90)
        self.assertEqual(deleted, 2)
        remaining = [r["decision_id"] for r in sm.conn.execute(
            "SELECT decision_id FROM order_decision_snapshots "
            "ORDER BY decision_id")]
        self.assertEqual(remaining, ["recent-1"])

    def test_prune_idempotent_when_nothing_old(self):
        sm = state_mod.StateManager(db_path=self._fresh_path())
        deleted = sm.prune_old_decision_snapshots(days=90)
        self.assertEqual(deleted, 0)

    def test_main_loop_invokes_prune_in_daily_summary(self):
        """Retention runs from MainLoop._log_daily_summary (daily
        housekeeping hook). AST check the call is present."""
        src = (REPO_ROOT / "bot" / "main_loop.py").read_text()
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "_log_daily_summary"):
                body_src = ast.unparse(node)
                if "prune_old_decision_snapshots" in body_src:
                    found = True
                    break
        self.assertTrue(found,
                        "MainLoop._log_daily_summary must call "
                        "prune_old_decision_snapshots() for 90d retention")


# ── Cluster 5b: runtime — _emit_decision_snapshot helper exists + wired ──

class TestEmitDecisionSnapshotHelper(_TempDB):
    """Runtime check: OrderExecutor._emit_decision_snapshot calls
    StateManager.insert_decision_snapshot with the correct decision_type
    and reuses an already-present decision_id."""

    def test_emit_seeds_decision_id_if_missing(self):
        from bot.executor import OrderExecutor
        from unittest import mock
        sm = state_mod.StateManager(db_path=self._fresh_path())
        ex = OrderExecutor.__new__(OrderExecutor)  # bypass __init__
        ex._state = sm
        candidate = {"ticker": "KXBTC-A", "asset": "BTC",
                     "strategy": "ladder"}
        ex._emit_decision_snapshot(candidate, "maker_first")
        self.assertIn("decision_id", candidate)
        rows = list(sm.conn.execute(
            "SELECT decision_type, asset, source FROM "
            "order_decision_snapshots WHERE decision_id=?",
            (candidate["decision_id"],)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision_type"], "maker_first")
        self.assertEqual(rows[0]["asset"], "BTC")
        self.assertEqual(rows[0]["source"], "ladder")

    def test_emit_reuses_existing_decision_id(self):
        from bot.executor import OrderExecutor
        sm = state_mod.StateManager(db_path=self._fresh_path())
        ex = OrderExecutor.__new__(OrderExecutor)
        ex._state = sm
        candidate = {"ticker": "KXBTC-A", "asset": "BTC",
                     "strategy": "ladder", "decision_id": "fixed-id-abc"}
        ex._emit_decision_snapshot(candidate, "maker_first")
        ex._emit_decision_snapshot(candidate, "escalate")
        rows = list(sm.conn.execute(
            "SELECT decision_type FROM order_decision_snapshots "
            "WHERE decision_id='fixed-id-abc' ORDER BY id"))
        self.assertEqual([r["decision_type"] for r in rows],
                         ["maker_first", "escalate"])

    def test_emit_failure_does_not_break_caller(self):
        """Capture failure must never break order flow. If
        insert_decision_snapshot raises, the helper logs and swallows."""
        from bot.executor import OrderExecutor
        from unittest import mock
        sm = state_mod.StateManager(db_path=self._fresh_path())
        ex = OrderExecutor.__new__(OrderExecutor)
        ex._state = sm
        # Force an exception from inside insert_decision_snapshot
        with mock.patch.object(sm, "insert_decision_snapshot",
                                side_effect=RuntimeError("synthetic")):
            try:
                ex._emit_decision_snapshot(
                    {"ticker": "KXBTC-A", "asset": "BTC",
                     "strategy": "x"}, "maker_first")
            except Exception as e:
                self.fail(f"emit must swallow downstream failure: {e}")


# ── Cluster 6: migration idempotency ──────────────────────────────────────

class TestDecisionSnapshotsMigrationIdempotent(_TempDB):
    def test_re_init_does_not_error(self):
        path = self._fresh_path()
        state_mod.StateManager(db_path=path)
        try:
            state_mod.StateManager(db_path=path)
        except Exception as e:
            self.fail(f"Second StateManager init raised: {e}")

    def test_existing_rows_survive_reinit(self):
        path = self._fresh_path()
        sm1 = state_mod.StateManager(db_path=path)
        sm1.insert_decision_snapshot(
            decision_id="survive-1", ticker="KXBTC-A", asset="BTC",
            decision_type="maker_first", source="ladder")
        sm1.conn.close()
        sm2 = state_mod.StateManager(db_path=path)
        rows = list(sm2.conn.execute(
            "SELECT decision_id FROM order_decision_snapshots "
            "WHERE decision_id='survive-1'"))
        self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
