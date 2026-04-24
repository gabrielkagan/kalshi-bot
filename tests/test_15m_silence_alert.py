"""Tests for the 15M scan-silence Telegram alert.

Regression guard for the 2026-04-24 17:30 UTC 15M outage: bot was
still running with WS "connected" but producing zero 15M evaluations
for 32 minutes. Weather/hourly/SPX kept working, so the dashboard
looked healthy — no alert fired. User only noticed because PnL didn't
move.

`_check_15m_silence_alert` fires a Telegram alert when no 15M
evaluated_opportunities row has been inserted in ≥10 min. Telegram
notifier's dedup_key prevents spam.
"""

import datetime
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
from bot import OpportunityScanner


def _make_scanner_with_eval_age(age_minutes: float) -> OpportunityScanner:
    """Stub scanner with an evaluated_opportunities table containing one
    KX*15M row whose evaluation_time is `age_minutes` old."""
    s = OpportunityScanner.__new__(OpportunityScanner)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.execute(
        "CREATE TABLE evaluated_opportunities "
        "(ticker TEXT, evaluation_time TEXT)"
    )
    ts = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(minutes=age_minutes))
    conn.execute(
        "INSERT INTO evaluated_opportunities VALUES (?, ?)",
        ("KXBTC15M-26APR241400-00",
         ts.isoformat(timespec="microseconds").replace("+00:00", "Z")))
    conn.commit()
    s._state = MagicMock()
    s._state.conn = conn
    s._kalshi_feed = MagicMock()
    s._kalshi_feed.is_connected = True
    return s


class TestSilent15MAlert(unittest.TestCase):

    def test_recent_eval_no_alert(self):
        """Last eval 2 min ago → no alert."""
        s = _make_scanner_with_eval_age(age_minutes=2)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert()
            mock_tele.send.assert_not_called()

    def test_9_min_age_no_alert(self):
        """Right under the 10-min threshold → no alert."""
        s = _make_scanner_with_eval_age(age_minutes=9)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert()
            mock_tele.send.assert_not_called()

    def test_11_min_age_alert_fires(self):
        """Over the 10-min threshold → Telegram alert fires."""
        s = _make_scanner_with_eval_age(age_minutes=11)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert()
            mock_tele.send.assert_called_once()
            # Verify dedup key present so repeated calls don't spam
            call = mock_tele.send.call_args
            self.assertEqual(call.kwargs.get("dedup_key"), "silent_15m_alert")

    def test_30_min_age_alert_contains_duration(self):
        """Alert message surfaces the silence duration for context."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert()
            mock_tele.send.assert_called_once()
            msg = mock_tele.send.call_args.args[0]
            self.assertIn("15M SCAN SILENT", msg)
            # duration should be ~30 min in the message
            self.assertIn("min", msg)

    def test_no_rows_no_alert(self):
        """Empty DB (fresh install) → can't compute age → no alert.
        (We'd rather miss this than fire on every tick of a fresh bot.)"""
        s = _make_scanner_with_eval_age(age_minutes=0)
        s._state.conn.execute("DELETE FROM evaluated_opportunities")
        s._state.conn.commit()
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert()
            mock_tele.send.assert_not_called()

    def test_missing_telegram_no_crash(self):
        """_TELEGRAM is None (no token configured) → don't crash,
        just log. Alert silently dropped."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        with patch.object(bot, "_TELEGRAM", None):
            # Should not raise
            s._check_15m_silence_alert()

    def test_telegram_send_error_swallowed(self):
        """If Telegram send raises (network blip), the scan loop is
        not affected."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            mock_tele.send.side_effect = RuntimeError("telegram down")
            # Should not raise
            s._check_15m_silence_alert()

    def test_db_error_swallowed(self):
        """SQL error during the staleness query doesn't crash scan."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._state.conn.close()   # force future queries to error
        # Should not raise
        s._check_15m_silence_alert()


if __name__ == "__main__":
    unittest.main()
