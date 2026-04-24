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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
from bot import OpportunityScanner


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
        with patch.object(bot, "_TELEGRAM", fake_telegram):
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
        with patch.object(bot, "_TELEGRAM", fake_telegram):
            # 5 consecutive unproductive ticks.
            for _ in range(5):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        fake_telegram.send.assert_called()

    def test_alert_uses_dedup_key(self):
        """dedup_key prevents spam — Telegram notifier dedup'd within
        its window, even if the watchdog continues to fire."""
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot, "_TELEGRAM", fake_telegram):
            for _ in range(5):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        # At least one call with dedup_key keyword arg.
        calls_with_dedup = [c for c in fake_telegram.send.call_args_list
                            if c.kwargs.get("dedup_key")]
        self.assertGreaterEqual(len(calls_with_dedup), 1)

    def test_alert_fires_once_at_threshold_not_lower(self):
        """First 4 ticks no alert; 5th tick alerts."""
        s = _make_scanner(uptime_minutes=30.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot, "_TELEGRAM", fake_telegram):
            for _ in range(4):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
            self.assertEqual(fake_telegram.send.call_count, 0)
            s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
            self.assertGreaterEqual(fake_telegram.send.call_count, 1)


class TestStartupGrace(unittest.TestCase):
    """Post-restart the bot goes through RK warmup (~5 min) during
    which 15M scans will legitimately not produce rows. Don't alert
    during that window."""

    def test_uptime_below_grace_does_not_alert(self):
        s = _make_scanner(uptime_minutes=2.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot, "_TELEGRAM", fake_telegram):
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

    def test_uptime_above_grace_alerts_normally(self):
        s = _make_scanner(uptime_minutes=10.0, wrote_rows_this_tick=0)
        fake_telegram = MagicMock()
        with patch.object(bot, "_TELEGRAM", fake_telegram):
            for _ in range(5):
                s._check_scan_productive_15m(_ACTIVE_15M, _tick_start_ts())
        fake_telegram.send.assert_called()


class TestScanWiring(unittest.TestCase):
    """scan() must call _check_scan_productive_15m with (active_windows,
    tick_start_ts)."""

    def test_scan_calls_check_scan_productive_15m(self):
        import ast
        bot_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "bot.py")
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
