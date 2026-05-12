"""Tests for the scan-productive watchdog (fix #3 from
kb/failures/ws-cache-drift-silent-scan-2026-04-24.md).

Complementary to `_check_15m_silence_alert` (existing, fires on 10+
min old last-eval). That one catches *how long* silence has been.
This one catches the *shape* much faster: N consecutive scan ticks
where 15M windows ARE discovered but the scanner produced zero DB
rows for 15m. That's the exact signal the 2026-04-24 22:12 UTC
incident produced — windows visible, scan silently bailing.

`_check_scan_productive_15m(active_windows, tick_start_ts)` is called
once per scan tick. Logic:
  - If no 15M windows in active_windows: reset counter (catalog gap
    is upstream, not a productivity issue), skip.
  - If bot uptime < SCAN_UNPRODUCTIVE_MIN_UPTIME: skip (RK warmup).
  - Count 15M rows written this tick via SQL on eval + rejection
    tables filtered by time > tick_start_ts AND product_type='15m'.
  - If any rows written: reset counter to 0.
  - If zero rows written: increment counter. Alert on >= threshold.

Threshold: 5 consecutive ticks ~ 2.5 min at 30s cadence — 4× faster
detection than the existing silence watchdog.
"""

import datetime
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bot
from bot.scanner import OpportunityScanner
import bot.notifier  # noqa: F401


_ACTIVE_15M = [{"product_type": "15m", "asset": "BTC"}]
_NO_15M = [{"product_type": "hourly", "asset": "BTC"}]
_CATALOG_GAP = []


def _make_scanner(
    uptime_minutes: float = 30.0,
    wrote_rows_this_tick: int = 0,
    wrote_product_type: str = "15m",
) -> OpportunityScanner:
    """Bare scanner with a fresh temp DB. Pre-populates N rows in
    evaluated_opportunities with a timestamp NEWER than what the test
    will pass as tick_start_ts, simulating "this tick wrote N rows"."""
    s = OpportunityScanner.__new__(OpportunityScanner)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.execute(
        "CREATE TABLE evaluated_opportunities "
        "(ticker TEXT, evaluation_time TEXT, product_type TEXT)"
    )
    conn.execute(
        "CREATE TABLE rejected_opportunities "
        "(ticker TEXT, rejection_time TEXT, product_type TEXT)"
    )
    # Set start-of-process timestamp so uptime calculation is deterministic.
    s._scan_15m_process_start_ts = time.time() - (uptime_minutes * 60)
    s._scan_15m_unproductive_count = 0
    s._scan_15m_iter_heartbeat_ts = 0.0
    # Provide a minimal StateManager stub exposing conn.
    state = MagicMock()
    state.conn = conn
    s._state = state
    # Pre-populate DB with wrote_rows_this_tick rows timestamped NEWER
    # than the tick_start_ts we'll pass (the caller will pass
    # tick_start_ts ~ now - 5s; populate with now).
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")
    for i in range(wrote_rows_this_tick):
        conn.execute(
            "INSERT INTO evaluated_opportunities VALUES (?, ?, ?)",
            (f"KXBTC15M-TEST-{i}", now_iso, wrote_product_type))
    conn.commit()
    return s


def _tick_start_ts(seconds_ago: float = 5.0) -> str:
    """ISO-Z timestamp representing start-of-tick."""
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=seconds_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TestCounterBehavior(unittest.TestCase):
    """Counter increment / reset / threshold logic."""

    def test_unproductive_tick_increments_counter(self):
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        self.assertEqual(s._scan_15m_unproductive_count, 1)
        s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        self.assertEqual(s._scan_15m_unproductive_count, 2)

    def test_productive_tick_resets_counter(self):
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        s._scan_15m_unproductive_count = 3
        # Next tick writes a row — counter resets.
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=1)
        s._scan_15m_unproductive_count = 3
        s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        self.assertEqual(s._scan_15m_unproductive_count, 0)

    def test_rejected_rows_count_as_productive(self):
        """A tick where scan hit the no_orderbook or no_best_ask silent-bail
        path (fix #2 now writes a rejection row) is STILL productive —
        scan is producing DB evidence, even if it's a rejection."""
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        # Manually insert a rejection row to simulate fix #2 firing.
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        s._state.conn.execute(
            "INSERT INTO rejected_opportunities VALUES (?, ?, ?)",
            ("KXBTC15M-TEST-REJ", now_iso, "15m"))
        s._state.conn.commit()
        s._scan_15m_unproductive_count = 2
        s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        self.assertEqual(s._scan_15m_unproductive_count, 0)

    def test_no_15m_windows_resets_counter(self):
        """Catalog gap (Kalshi silent between windows) is upstream, not
        a productivity issue. Must not increment AND must reset (so we
        don't carry a stale count into the next live window)."""
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        s._scan_15m_unproductive_count = 4
        s._check_scan_productive_15m(_CATALOG_GAP, _tick_start_ts())
        self.assertEqual(s._scan_15m_unproductive_count, 0)

    def test_only_non_15m_windows_resets_counter(self):
        """Hourly-only windows but no 15M → catalog-gap-equivalent for
        this watchdog."""
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        s._scan_15m_unproductive_count = 4
        s._check_scan_productive_15m(_NO_15M, _tick_start_ts())
        self.assertEqual(s._scan_15m_unproductive_count, 0)

    def test_below_threshold_no_alert(self):
        """Only 3 unproductive ticks — below default 5-tick threshold.
        No Telegram fires."""
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot.notifier, "_TELEGRAM", fake_telegram):
            for _ in range(3):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        fake_telegram.send.assert_not_called()

    def test_other_product_types_dont_count_as_15m_productive(self):
        """Weather row written this tick does NOT make 15M productive."""
        s = _make_scanner(
            uptime_minutes=30.0, wrote_rows_this_tick=1,
            wrote_product_type="weather")
        s._scan_15m_unproductive_count = 2
        s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        self.assertEqual(s._scan_15m_unproductive_count, 3)


class TestAlertFiring(unittest.TestCase):
    """Threshold behavior and Telegram alerting."""

    def test_threshold_reached_fires_alert(self):
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot.notifier, "_TELEGRAM", fake_telegram):
            # 5 consecutive unproductive ticks.
            for _ in range(5):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        fake_telegram.send.assert_called()

    def test_alert_state_based_dedup_prevents_spam(self):
        """State-based dedup prevents spam: the watchdog uses the
        `_scan_15m_unproductive_entry_alerted` boolean flag (set after the
        first send) rather than a Telegram dedup_key. Per commit 159b411
        and ws-cache-drift-silent-scan-2026-04-24.md, the 60s dedup_key TTL
        would defeat back-to-back stuck-periods, so state flag is correct.
        Verify the entry alert fires AT MOST ONCE per stuck-period even if
        the threshold is crossed many times in a row."""
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot.notifier, "_TELEGRAM", fake_telegram):
            # 10 consecutive unproductive ticks — well past threshold (5).
            for _ in range(10):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        # Entry alert path must fire exactly once, not repeatedly.
        # (The state flag is the dedup; dedup_key kwarg is intentionally absent.)
        entry_alerts = [c for c in fake_telegram.send.call_args_list
                        if "15M SCAN UNPRODUCTIVE" in (c.args[0] if c.args else "")]
        self.assertEqual(len(entry_alerts), 1,
                         "entry alert must be state-deduped to exactly one send "
                         "per stuck period — see commit 159b411")

    def test_alert_fires_once_at_threshold_not_lower(self):
        """First 4 ticks no alert; 5th tick alerts."""
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot.notifier, "_TELEGRAM", fake_telegram):
            for _ in range(4):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
            self.assertEqual(fake_telegram.send.call_count, 0)
            s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
            self.assertGreaterEqual(fake_telegram.send.call_count, 1)


class TestStartupGrace(unittest.TestCase):
    """Post-restart the bot goes through RK warmup (up to ~3.5 min
    observed) during which 15M scans will legitimately not produce
    rows. Grace period is 7 min — gives ~3.5 min buffer over the
    observed warmup ceiling. Don't alert during that window."""

    def test_uptime_below_grace_does_not_alert(self):
        s = _make_scanner(uptime_minutes=2.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot.notifier, "_TELEGRAM", fake_telegram):
            for _ in range(10):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        fake_telegram.send.assert_not_called()

    def test_uptime_below_grace_does_not_even_increment(self):
        """During grace period, don't count ticks toward the threshold.
        Otherwise a bot that restarts during 15M coverage would alert
        the instant grace expires."""
        s = _make_scanner(uptime_minutes=2.0, wrote_rows_this_tick=0)
        for _ in range(10):
            s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        self.assertEqual(s._scan_15m_unproductive_count, 0)

    def test_uptime_at_5min_does_not_alert_anymore(self):
        """Regression — Apr 24 23:53 UTC false positive at 5.1 min
        uptime. Old grace was 300s and watchdog fired the instant it
        expired before RK finished warming. New grace is 420s; uptime
        of 5 min must not trigger an alert."""
        s = _make_scanner(uptime_minutes=5.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot.notifier, "_TELEGRAM", fake_telegram):
            for _ in range(10):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        fake_telegram.send.assert_not_called()

    def test_uptime_above_grace_alerts_normally(self):
        s = _make_scanner(uptime_minutes=10.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot.notifier, "_TELEGRAM", fake_telegram):
            for _ in range(5):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        fake_telegram.send.assert_called()


class TestHeartbeatProductivity(unittest.TestCase):
    """Regression — Apr 24 00:08 UTC false positive. The dedup at
    insert_evaluated_opportunity sites can suppress DB rows for a
    window's entire 15-min lifetime once each (ticker, stage) tuple
    has been seen. Watchdog measuring DB row count alone fires
    incorrectly even though scan() is iterating windows healthily.

    Fix: scan() updates `_scan_15m_iter_heartbeat_ts = time.time()` at
    the top of each 15M window iteration body. Watchdog treats a
    heartbeat newer than `tick_start_ts` as productive."""

    def test_heartbeat_resets_counter_even_with_zero_db_rows(self):
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        # Simulate scan() having iterated a 15M window AFTER the prev
        # tick start — heartbeat should mark this tick productive.
        s._scan_15m_iter_heartbeat_ts = time.time()
        s._scan_15m_unproductive_count = 4  # one tick away from alert
        s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        self.assertEqual(s._scan_15m_unproductive_count, 0,
                         "heartbeat should reset counter even though "
                         "no DB rows were written this tick")

    def test_no_heartbeat_still_alerts(self):
        """Inverse — when scan() never reaches the 15M iteration body
        (real silent bail upstream), heartbeat is stale and the
        watchdog must still fire after threshold."""
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        # Heartbeat is OLDER than tick_start_ts (stale).
        s._scan_15m_iter_heartbeat_ts = time.time() - 100
        fake_telegram = MagicMock()
        with patch.object(bot.notifier, "_TELEGRAM", fake_telegram):
            for _ in range(6):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        fake_telegram.send.assert_called()

    def test_db_row_alone_is_still_sufficient(self):
        """Backward-compat — if scan DOES write a DB row, that's still
        productive even without a heartbeat (e.g., on first tick of a
        fresh window before heartbeat is set)."""
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=1)
        s._scan_15m_iter_heartbeat_ts = 0.0  # never set
        s._scan_15m_unproductive_count = 4
        s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        self.assertEqual(s._scan_15m_unproductive_count, 0)


class TestScanHeartbeatWiring(unittest.TestCase):
    """scan() body must update `_scan_15m_iter_heartbeat_ts =
    time.time()` for 15M windows. AST regression to prevent future
    refactors from removing the heartbeat assignment."""

    def test_scan_updates_heartbeat_for_15m_windows(self):
        import ast
        with open(bot.scanner.__file__) as f:
            tree = ast.parse(f.read())
        scan_fn = None
        for cls in ast.walk(tree):
            if (isinstance(cls, ast.ClassDef)
                    and cls.name == "OpportunityScanner"):
                for node in cls.body:
                    if (isinstance(node, ast.FunctionDef)
                            and node.name == "scan"):
                        scan_fn = node
                        break
        self.assertIsNotNone(scan_fn,
                             "OpportunityScanner.scan not found")
        found = False
        for sub in ast.walk(scan_fn):
            if not isinstance(sub, ast.Assign):
                continue
            for tgt in sub.targets:
                if (isinstance(tgt, ast.Attribute)
                        and tgt.attr == "_scan_15m_iter_heartbeat_ts"
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "self"):
                    found = True
                    break
        self.assertTrue(found,
                        "scan() must update self._scan_15m_iter_heartbeat_ts "
                        "to keep watchdog accurate when dedup suppresses "
                        "DB writes. See ws-cache-drift-silent-scan-2026-04-24 "
                        "Apr 24 00:08 UTC false positive.")


class TestSlowTickInstrumentation(unittest.TestCase):
    """Regression — scan() must log a SLOW_SCAN_TICK warning when the
    gap between consecutive ticks exceeds 2s. Diagnoses the
    clock_drift / event-loop stall pattern documented in PM Fix 5."""

    def test_scan_contains_slow_tick_log(self):
        # Bit 9.3 retarget (2026-05-10): MainLoop._tick (which emits the
        # SLOW_SCAN_TICK warning) moved to bot/main_loop.py.
        import bot.main_loop
        with open(bot.main_loop.__file__) as f:
            src = f.read()
        self.assertIn("SLOW_SCAN_TICK", src,
                      "MainLoop._tick must emit a SLOW_SCAN_TICK warning when "
                      "tick-to-tick gap > 2s — see PM Fix 5 (event-loop "
                      "stall diagnostic).")


class TestScanWiring(unittest.TestCase):
    """scan() must call _check_scan_productive_15m with (active_windows,
    tick_start_ts)."""

    def test_scan_calls_check_scan_productive_15m(self):
        import ast
        bot_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "bot", "scanner", "__init__.py")
        with open(bot_py) as f:
            tree = ast.parse(f.read())
        scan = None
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef) and cls.name == "OpportunityScanner":
                for node in cls.body:
                    if isinstance(node, ast.FunctionDef) and node.name == "scan":
                        scan = node
                        break
        self.assertIsNotNone(scan, "OpportunityScanner.scan not found")
        calls = [
            n for n in ast.walk(scan)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_check_scan_productive_15m"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "self"
        ]
        self.assertGreaterEqual(
            len(calls), 1,
            "scan() must call self._check_scan_productive_15m — "
            "otherwise the watchdog never runs. "
            "See kb/failures/ws-cache-drift-silent-scan-2026-04-24.md fix #3.")


if __name__ == "__main__":
    unittest.main()
