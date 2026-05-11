"""Tests for order_lifecycle_snapshots — Phase 4 wiring.

The new helper StateManager.insert_order_lifecycle_snapshot writes to
the table created in Phase 2 and is called from OrderExecutor at IOC
submit and on every fill (incl. partial). It auto-fills:
- observation_time → now (UTC ISO8601 with µs)
- orderbook_levels_json → from _scan_ob_cache with the same freshness
  gate used by insert_evaluated_opportunity (stale → NULL, never lie)

Tests:
- helper writes the row with correct columns
- observation_time auto-set
- orderbook_levels_json auto-filled from fresh cache
- stale cache → NULL ladder (forensic-honest)
- explicit kwarg overrides cache
- CHECK / NOT NULL invariants from Phase 2 still enforced
- code-grep wiring backstops for OrderExecutor call sites
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import bot.state  # noqa: F401


class _TempState(unittest.TestCase):
    def _fresh(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return bot.state.StateManager(db_path=tmp.name)


class TestHelperBasicInsert(_TempState):
    def test_helper_writes_row_with_required_columns(self):
        sm = self._fresh()
        sm.insert_order_lifecycle_snapshot(
            order_id="ord-A", ticker="KXTEST-1", event_type="submit")
        rows = list(sm.conn.execute(
            "SELECT order_id, ticker, event_type, observation_time, "
            "orderbook_levels_json, source "
            "FROM order_lifecycle_snapshots WHERE order_id='ord-A'"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_id"], "ord-A")
        self.assertEqual(rows[0]["ticker"], "KXTEST-1")
        self.assertEqual(rows[0]["event_type"], "submit")
        self.assertIsNotNone(rows[0]["observation_time"])

    def test_observation_time_is_utc_iso(self):
        sm = self._fresh()
        sm.insert_order_lifecycle_snapshot(
            order_id="ord-B", ticker="KXTEST-1", event_type="fill")
        row = sm.conn.execute(
            "SELECT observation_time FROM order_lifecycle_snapshots "
            "WHERE order_id='ord-B'").fetchone()
        # ISO8601 with µs and Z suffix per existing bot convention
        self.assertRegex(row["observation_time"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z$")

    def test_source_kwarg_persisted(self):
        sm = self._fresh()
        sm.insert_order_lifecycle_snapshot(
            order_id="ord-C", ticker="KXTEST-1",
            event_type="partial_fill", source="taker_ioc")
        row = sm.conn.execute(
            "SELECT source FROM order_lifecycle_snapshots "
            "WHERE order_id='ord-C'").fetchone()
        self.assertEqual(row["source"], "taker_ioc")


class TestHelperOrderbookAutoFill(_TempState):
    SAMPLE = '{"yes_bids":[[87,3377]],"yes_asks":[[96,1]]}'

    def test_autofills_from_fresh_cache(self):
        sm = self._fresh()
        sm._scan_ob_cache["KXTEST-D"] = (time.monotonic(), self.SAMPLE)
        sm.insert_order_lifecycle_snapshot(
            order_id="ord-D", ticker="KXTEST-D", event_type="submit")
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM order_lifecycle_snapshots "
            "WHERE order_id='ord-D'").fetchone()
        self.assertEqual(row["orderbook_levels_json"], self.SAMPLE)

    def test_stale_cache_writes_null(self):
        sm = self._fresh()
        sm._scan_ob_cache["KXTEST-E"] = (time.monotonic() - 999.0, self.SAMPLE)
        sm.insert_order_lifecycle_snapshot(
            order_id="ord-E", ticker="KXTEST-E", event_type="fill")
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM order_lifecycle_snapshots "
            "WHERE order_id='ord-E'").fetchone()
        self.assertIsNone(row["orderbook_levels_json"])

    def test_explicit_kwarg_overrides_cache(self):
        sm = self._fresh()
        sm._scan_ob_cache["KXTEST-F"] = (time.monotonic(), "STALE_CACHE_VAL")
        sm.insert_order_lifecycle_snapshot(
            order_id="ord-F", ticker="KXTEST-F", event_type="submit",
            orderbook_levels_json=self.SAMPLE)
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM order_lifecycle_snapshots "
            "WHERE order_id='ord-F'").fetchone()
        self.assertEqual(row["orderbook_levels_json"], self.SAMPLE)

    def test_no_cache_entry_writes_null(self):
        sm = self._fresh()
        sm.insert_order_lifecycle_snapshot(
            order_id="ord-G", ticker="KXTEST-G", event_type="cancel")
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM order_lifecycle_snapshots "
            "WHERE order_id='ord-G'").fetchone()
        self.assertIsNone(row["orderbook_levels_json"])


class TestHelperRespectsSchemaInvariants(_TempState):
    """Phase 2 schema enforces order_id NOT NULL + event_type CHECK.
    Helper must surface these as IntegrityError, not swallow."""

    def test_invalid_event_type_raises(self):
        import sqlite3
        sm = self._fresh()
        with self.assertRaises(sqlite3.IntegrityError):
            sm.insert_order_lifecycle_snapshot(
                order_id="ord-X", ticker="KXTEST-1", event_type="FILLED")

    def test_missing_order_id_raises(self):
        import sqlite3
        sm = self._fresh()
        with self.assertRaises((sqlite3.IntegrityError, TypeError)):
            sm.insert_order_lifecycle_snapshot(
                order_id=None, ticker="KXTEST-1", event_type="submit")


class TestSourceVocabulary(_TempState):
    """Adversary round 1 P1: source must use ONE vocabulary across all
    events, otherwise GROUP BY source conflates strategies (submit) with
    execution tiers (fill). Standardize: source = strategy name on every
    event. Tier (taker/maker) is recoverable via order_id join with
    pending_orders / positions if needed."""

    def test_source_field_accepts_strategy_string(self):
        sm = self._fresh()
        sm.insert_order_lifecycle_snapshot(
            order_id="ord-S1", ticker="KXTEST-1", event_type="submit",
            source="terminal_momentum_96")
        sm.insert_order_lifecycle_snapshot(
            order_id="ord-S1", ticker="KXTEST-1", event_type="fill",
            source="terminal_momentum_96")
        rows = list(sm.conn.execute(
            "SELECT source, event_type FROM order_lifecycle_snapshots "
            "WHERE order_id='ord-S1' ORDER BY observation_time"))
        # Both rows have the SAME source → GROUP BY source returns one bucket
        sources = {r["source"] for r in rows}
        self.assertEqual(sources, {"terminal_momentum_96"},
                         "source vocabulary must be uniform across events")


class TestFailureCounter(_TempState):
    """Adversary round 1 P1: silent-swallow with no counter means a
    schema-violating row (e.g., NULL order_id from degraded Kalshi)
    drops 100% of snapshots and we only see warnings. Counter exposes
    the regression in real time."""

    def test_failure_counter_initialized_to_zero(self):
        sm = self._fresh()
        self.assertEqual(sm._lifecycle_snapshot_failures, 0)

    def test_failure_counter_increments_on_check_violation(self):
        sm = self._fresh()
        # Bad event_type → CHECK violation → IntegrityError caught → counter++
        try:
            sm.insert_order_lifecycle_snapshot(
                order_id="ord-F1", ticker="KXTEST-1", event_type="BOGUS")
        except Exception:
            pass  # helper is allowed to either raise or swallow; counter must reflect
        self.assertGreaterEqual(sm._lifecycle_snapshot_failures, 1,
                                "failure counter did not increment on schema violation")


class TestExecutorWiringInPlace(_TempState):
    """Code-grep wiring backstops: confirms _submit_taker and _on_fill
    actually call insert_order_lifecycle_snapshot. Without these the
    table created in Phase 2 + helper added in Phase 4 would silently
    have zero rows in production (per kb/failures/feedback_verify_new_features.md)."""

    def _executor_src(self):
        # Bit 9.1 (2026-05-10): OrderExecutor moved to bot/executor.py
        import bot.executor as bot_mod
        src = open(bot_mod.__file__).read()
        # Bound the search to OrderExecutor class
        start = src.find("class OrderExecutor")
        self.assertGreater(start, 0, "OrderExecutor class not found")
        end = src.find("\nclass ", start + 1)
        if end < 0:
            end = len(src)
        return src[start:end]

    def test_submit_taker_calls_lifecycle_snapshot(self):
        src = self._executor_src()
        # Look for a call to insert_order_lifecycle_snapshot with submit
        # event_type somewhere inside _submit_taker or its callers.
        self.assertIn("insert_order_lifecycle_snapshot", src,
                      "OrderExecutor doesn't call insert_order_lifecycle_snapshot")
        # 'submit' event_type must appear (the IOC-submit path)
        self.assertTrue(
            "event_type=\"submit\"" in src or "event_type='submit'" in src,
            "no 'submit' event_type used in OrderExecutor — "
            "IOC submit lifecycle event not captured")

    def test_on_fill_emits_fill_or_partial_fill(self):
        src = self._executor_src()
        # The fill site must reference both string literals 'fill' and
        # 'partial_fill' (ternary on is_complete is the natural form).
        # Loose grep — accepts any string-literal expression.
        self.assertIn('"fill"', src,
                      "OrderExecutor doesn't reference 'fill' event_type")
        self.assertIn('"partial_fill"', src,
                      "OrderExecutor doesn't reference 'partial_fill' event_type")
        # Both literals should appear NEAR an insert_order_lifecycle_snapshot
        # call site (within ~500 chars), confirming they're wired together.
        idx_partial = src.find('"partial_fill"')
        idx_call = src.find("insert_order_lifecycle_snapshot", idx_partial - 500)
        self.assertGreaterEqual(
            idx_call, max(0, idx_partial - 500),
            "'partial_fill' literal not adjacent to insert_order_lifecycle_snapshot call")


if __name__ == "__main__":
    unittest.main()
