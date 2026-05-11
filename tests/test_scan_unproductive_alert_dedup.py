"""Tests for state-based alert dedup in `_check_scan_productive_15m`.

Incident: 2026-04-27 06:56-06:59 UTC, the 15M unproductive watchdog
fired four Telegram alerts (count=5, 47, 89, 128) over a 3-minute
stuck-period because dedup was a 60-second TTL on a fixed key. The
operator received four nighttime pages for one event that the
recovery layers (R1 force_resubscribe, R2 force_reconnect) were
already handling autonomously.

Post-fix contract for `_check_scan_productive_15m`:
  - Entry: Telegram fires ONCE when count first crosses
    SCAN_UNPRODUCTIVE_THRESHOLD (5).
  - Escalation: Telegram fires ONCE more when count crosses
    SCAN_UNPRODUCTIVE_RECONNECT_THRESHOLD (10) — operator wants to
    know the WS reconnect just got triggered.
  - Quiet during burn: counts 6, 7, 8, 9, 11, 12, …, 128 produce
    NO further Telegrams. (R1/R2 recovery still fires; only the
    Telegram noise is suppressed.)
  - Recovery: Telegram fires ONCE on first productive tick after a
    burn, surfacing peak_count so the operator can size the event.
  - State resets: a fresh stuck-period after recovery fires fresh
    entry/escalation/recovery alerts (no stale suppression).
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402
from bot.scanner import OpportunityScanner  # noqa: E402
import bot.notifier  # noqa: F401


_ACTIVE_15M = [
    {
        "product_type": "15m",
        "asset": "BTC",
        "markets": [{"ticker": "KXBTC15M-26APR270300-00"}],
    }
]


def _now_iso(offset_s: float = 0.0) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=offset_s
    )
    return ts.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _make_scanner(uptime_minutes: float = 30.0) -> OpportunityScanner:
    """Stub scanner sufficient to exercise `_check_scan_productive_15m`.

    Empty eval/rejection tables (rows_written=0) and heartbeat in the
    past (so heartbeat_recent=False on every call) → each call without
    a productive intervention increments `_scan_15m_unproductive_count`.
    """
    s = OpportunityScanner.__new__(OpportunityScanner)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.execute("""
        CREATE TABLE evaluated_opportunities (
            ticker TEXT, evaluation_time TEXT, product_type TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE rejected_opportunities (
            ticker TEXT, rejection_time TEXT, product_type TEXT,
            rejection_reason TEXT
        )
    """)
    conn.commit()
    s._state = MagicMock()
    s._state.conn = conn
    s._kalshi_feed = MagicMock()
    s._kalshi_feed.is_connected = True
    s._scan_15m_iter_heartbeat_ts = 0.0
    s._scan_15m_process_start_ts = time.time() - (uptime_minutes * 60)
    s._scan_15m_unproductive_count = 0
    s._scan_15m_last_recovery_ts = 0.0
    s._scan_15m_reconnect_triggered = False
    s._scan_15m_prev_ws_connected = True
    return s


def _drive_unproductive_ticks(s: OpportunityScanner, n: int) -> None:
    """Run `_check_scan_productive_15m` n times with heartbeat absent
    and zero DB rows. Each call increments `_scan_15m_unproductive_count`.
    """
    for _ in range(n):
        # tick_start_ts is "now" — heartbeat (=0.0) < tick_start, so
        # heartbeat_recent is False.
        ts = _now_iso()
        s._check_scan_productive_15m(_ACTIVE_15M, ts)


class TestUnproductiveAlertDedup(unittest.TestCase):

    # ---- Failing tests for the new behavior ------------------------

    def test_single_alert_at_threshold_then_silence_until_reconnect(self):
        """Counts 5..9 inclusive → one Telegram alert at the
        threshold-crossing, none for counts 6..9."""
        s = _make_scanner()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            _drive_unproductive_ticks(s, 9)  # crosses threshold (5), not reconnect (10)
            self.assertEqual(s._scan_15m_unproductive_count, 9)
            self.assertEqual(
                mock_tele.send.call_count, 1,
                f"Expected 1 alert (entry); got {mock_tele.send.call_count} "
                f"calls: {mock_tele.send.call_args_list}",
            )

    def test_second_alert_at_reconnect_threshold(self):
        """Counts 5..15 → two alerts: one at threshold, one at
        reconnect threshold (10)."""
        s = _make_scanner()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            _drive_unproductive_ticks(s, 15)
            self.assertEqual(s._scan_15m_unproductive_count, 15)
            self.assertEqual(
                mock_tele.send.call_count, 2,
                f"Expected 2 alerts (entry+reconnect); got "
                f"{mock_tele.send.call_count}: "
                f"{mock_tele.send.call_args_list}",
            )

    def test_long_burn_still_only_two_alerts(self):
        """The Apr 27 incident shape: 128 consecutive unproductive
        ticks → still only 2 Telegrams (entry + reconnect)."""
        s = _make_scanner()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            _drive_unproductive_ticks(s, 128)
            self.assertEqual(s._scan_15m_unproductive_count, 128)
            self.assertEqual(
                mock_tele.send.call_count, 2,
                f"128 ticks should still produce only 2 Telegrams; "
                f"got {mock_tele.send.call_count}",
            )

    def test_recovery_alert_fires_once(self):
        """After a burn, a productive tick fires ONE recovery alert
        with the peak count."""
        s = _make_scanner()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            _drive_unproductive_ticks(s, 15)
            entry_count = mock_tele.send.call_count  # 2
            # Simulate a productive tick: bump heartbeat to "now"
            s._scan_15m_iter_heartbeat_ts = time.time() + 1
            s._check_scan_productive_15m(_ACTIVE_15M, _now_iso())
            self.assertEqual(s._scan_15m_unproductive_count, 0)
            self.assertEqual(
                mock_tele.send.call_count - entry_count, 1,
                "Productive tick after burn should fire one recovery alert",
            )
            recovery_msg = mock_tele.send.call_args.args[0]
            self.assertIn("RECOVERED", recovery_msg.upper())
            # Peak count (15) should appear in the recovery message.
            self.assertIn("15", recovery_msg)

    def test_recovery_alert_does_not_fire_on_steady_productive(self):
        """If the watchdog has never alerted, productive ticks must
        NOT fire spurious recovery messages on every call."""
        s = _make_scanner()
        s._scan_15m_iter_heartbeat_ts = time.time() + 1
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            for _ in range(5):
                s._check_scan_productive_15m(_ACTIVE_15M, _now_iso())
            mock_tele.send.assert_not_called()

    def test_recovery_alert_does_not_fire_below_threshold(self):
        """If a tiny blip raised the counter to 3 but never reached
        the threshold (5), recovery must not fire (no alert was
        ever sent for the operator to "recover" from)."""
        s = _make_scanner()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            _drive_unproductive_ticks(s, 3)  # below threshold of 5
            self.assertEqual(mock_tele.send.call_count, 0)
            # Productive tick:
            s._scan_15m_iter_heartbeat_ts = time.time() + 1
            s._check_scan_productive_15m(_ACTIVE_15M, _now_iso())
            self.assertEqual(s._scan_15m_unproductive_count, 0)
            self.assertEqual(
                mock_tele.send.call_count, 0,
                "Sub-threshold blip with productive recovery: no alert",
            )

    def test_two_independent_stuck_periods_get_fresh_alerts(self):
        """First burn: entry+reconnect+recovery = 3 alerts. After
        clean recovery, a SECOND burn must fire fresh alerts (state
        reset, not stale-suppressed)."""
        s = _make_scanner()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            # Burn 1
            _drive_unproductive_ticks(s, 15)
            s._scan_15m_iter_heartbeat_ts = time.time() + 1
            s._check_scan_productive_15m(_ACTIVE_15M, _now_iso())
            calls_after_burn1 = mock_tele.send.call_count  # entry+reconnect+recovery = 3
            self.assertEqual(calls_after_burn1, 3)
            # Reset heartbeat to past so next ticks are unproductive again
            s._scan_15m_iter_heartbeat_ts = 0.0
            # Burn 2
            _drive_unproductive_ticks(s, 15)
            s._scan_15m_iter_heartbeat_ts = time.time() + 1
            s._check_scan_productive_15m(_ACTIVE_15M, _now_iso())
            self.assertEqual(
                mock_tele.send.call_count - calls_after_burn1, 3,
                f"Burn 2 should re-fire 3 fresh alerts; instead got "
                f"{mock_tele.send.call_count - calls_after_burn1}",
            )

    def test_entry_alert_message_contains_diagnostic(self):
        """Operator receives one alert at entry — the message must
        carry the diagnostic info (ticks, windows, uptime, KB ref)."""
        s = _make_scanner()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            _drive_unproductive_ticks(s, 5)
            self.assertEqual(mock_tele.send.call_count, 1)
            msg = mock_tele.send.call_args.args[0]
            self.assertIn("UNPRODUCTIVE", msg.upper())
            self.assertIn("15M", msg)

    # ---- Existing-behavior preservation ----------------------------

    def test_recovery_resets_count_and_recovery_state(self):
        """A productive tick must reset the counter AND R1/R2 state
        so the next stuck period starts fresh (regression for the
        existing reset path)."""
        s = _make_scanner()
        _drive_unproductive_ticks(s, 12)
        self.assertEqual(s._scan_15m_unproductive_count, 12)
        self.assertTrue(s._scan_15m_reconnect_triggered)
        s._scan_15m_iter_heartbeat_ts = time.time() + 1
        with patch.object(bot.notifier, "_TELEGRAM"):
            s._check_scan_productive_15m(_ACTIVE_15M, _now_iso())
        self.assertEqual(s._scan_15m_unproductive_count, 0)
        self.assertFalse(s._scan_15m_reconnect_triggered)
        self.assertEqual(s._scan_15m_last_recovery_ts, 0.0)

    def test_rotation_transition_during_burn_does_not_strand_entry_flag(self):
        """Adversarial [A1]: counter+entry alert fires; then rotation
        boundary (n_15m==0 or n_eligible==0) interrupts; then bug
        resumes. Operator must see a fresh entry alert on the second
        burn — the rotation reset must clear the entry flag."""
        s = _make_scanner()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            _drive_unproductive_ticks(s, 6)  # crosses threshold (5)
            self.assertEqual(mock_tele.send.call_count, 1)
            self.assertTrue(s._scan_15m_unproductive_entry_alerted)
            # Rotation transition: n_15m == 0 path
            s._check_scan_productive_15m([], _now_iso())
            # Counter + alert state must be fully reset, but no
            # recovery telegram (this is benign masking, not real
            # recovery).
            self.assertEqual(s._scan_15m_unproductive_count, 0)
            self.assertFalse(s._scan_15m_unproductive_entry_alerted)
            self.assertEqual(mock_tele.send.call_count, 1,
                             "Rotation reset must NOT fire recovery")
            # Bug resumes after rotation:
            _drive_unproductive_ticks(s, 6)
            self.assertEqual(
                mock_tele.send.call_count, 2,
                "Second burn must re-fire entry alert; got "
                f"{mock_tele.send.call_count} calls",
            )

    def test_rapid_back_to_back_burns_within_60s_both_alert(self):
        """Adversarial [A4]: TelegramNotifier has a 60s TTL on
        dedup_key. State-based dedup must work without the key, so
        two stuck-periods within 60s both fire entry+recovery alerts.

        We test this by passing through the real TelegramNotifier
        send() path (not mocking the dedup), via a stub that records
        every call regardless of dedup_key."""
        s = _make_scanner()
        # Use a real-shaped notifier whose dedup we can observe.
        sent = []

        class _ObservingTelegram:
            def send(self, message, silent=False, dedup_key=None):
                sent.append((message, dedup_key))

        with patch.object(bot.notifier, "_TELEGRAM", _ObservingTelegram()):
            # Burn 1
            _drive_unproductive_ticks(s, 6)
            s._scan_15m_iter_heartbeat_ts = time.time() + 1
            s._check_scan_productive_15m(_ACTIVE_15M, _now_iso())
            # Burn 2 (immediately, no real wall-clock delay)
            s._scan_15m_iter_heartbeat_ts = 0.0
            _drive_unproductive_ticks(s, 6)
            s._scan_15m_iter_heartbeat_ts = time.time() + 1
            s._check_scan_productive_15m(_ACTIVE_15M, _now_iso())

        # 4 sends: burn1 entry+recovery, burn2 entry+recovery
        self.assertEqual(
            len(sent), 4,
            f"Two burns within 60s must produce 4 telegrams; got "
            f"{len(sent)}: {sent}",
        )
        # No dedup_key on any of these so the real TelegramNotifier
        # 60s TTL doesn't drop them.
        for msg, key in sent:
            self.assertIsNone(
                key, f"Alert {msg!r} should have no dedup_key; got {key!r}"
            )

    def test_ws_reconnect_during_burn_fires_recovery(self):
        """Adversarial [A3]: WS-disconnect→reconnect transition during
        a stuck-period must fire recovery (operator sees closure)
        before resetting state."""
        s = _make_scanner()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            _drive_unproductive_ticks(s, 6)
            self.assertTrue(s._scan_15m_unproductive_entry_alerted)
            entry_count = mock_tele.send.call_count  # 1
            # Simulate WS-down then WS-up transition:
            s._scan_15m_prev_ws_connected = False
            s._kalshi_feed.is_connected = True
            s._check_scan_productive_15m(_ACTIVE_15M, _now_iso())
            self.assertEqual(
                mock_tele.send.call_count - entry_count, 1,
                "WS-reconnect transition must fire one recovery alert",
            )
            recovery_msg = mock_tele.send.call_args.args[0]
            self.assertIn("RECOVERED", recovery_msg.upper())
            # Peak (=6, the count before reset) should be in message.
            self.assertIn("6", recovery_msg)

    def test_uptime_gate_skips_check(self):
        """Pre-warmup uptime (< 7 min): function returns early and
        does NOT increment the counter (regression for existing gate)."""
        s = _make_scanner(uptime_minutes=5.0)  # below the 7-min floor
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            _drive_unproductive_ticks(s, 10)
            self.assertEqual(s._scan_15m_unproductive_count, 0)
            mock_tele.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
