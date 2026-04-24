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

# Default active_windows for tests that exercise the 15M-present branch.
# A non-empty list with product_type="15m" bypasses the catalog-gap guard
# (added 2026-04-24) so the silence alert can fire on age threshold alone.
_ACTIVE_15M = [{"product_type": "15m", "asset": "BTC"}]


def _make_scanner_with_eval_age(
    age_minutes: float, uptime_minutes: float = 30.0,
) -> OpportunityScanner:
    """Stub scanner with an evaluated_opportunities table containing one
    KX*15M row whose evaluation_time is `age_minutes` old. Bot uptime is
    simulated as `uptime_minutes` (default 30, well past the 15-min
    SILENCE_ALERT_MIN_UPTIME threshold)."""
    import time as _t
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
    # Seed the process-start timestamp so the uptime gate behaves
    # deterministically. 30 min uptime is past the 15-min floor.
    s._silence_alert_process_start_ts = _t.time() - (uptime_minutes * 60)
    return s


class TestSilent15MAlert(unittest.TestCase):

    def test_recent_eval_no_alert(self):
        """Last eval 2 min ago → no alert."""
        s = _make_scanner_with_eval_age(age_minutes=2)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_9_min_age_no_alert(self):
        """Right under the 10-min threshold → no alert."""
        s = _make_scanner_with_eval_age(age_minutes=9)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_11_min_age_alert_fires(self):
        """Over the 10-min threshold → Telegram alert fires."""
        s = _make_scanner_with_eval_age(age_minutes=11)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()
            # Verify dedup key present so repeated calls don't spam
            call = mock_tele.send.call_args
            self.assertEqual(call.kwargs.get("dedup_key"), "silent_15m_alert")

    def test_30_min_age_alert_contains_duration(self):
        """Alert message surfaces the silence duration for context."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
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
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_missing_telegram_no_crash(self):
        """_TELEGRAM is None (no token configured) → don't crash,
        just log. Alert silently dropped."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        with patch.object(bot, "_TELEGRAM", None):
            # Should not raise
            s._check_15m_silence_alert(_ACTIVE_15M)

    def test_telegram_send_error_swallowed(self):
        """If Telegram send raises (network blip), the scan loop is
        not affected."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            mock_tele.send.side_effect = RuntimeError("telegram down")
            # Should not raise
            s._check_15m_silence_alert(_ACTIVE_15M)

    def test_db_error_swallowed(self):
        """SQL error during the staleness query doesn't crash scan."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._state.conn.close()   # force future queries to error
        # Should not raise
        s._check_15m_silence_alert(_ACTIVE_15M)


class TestSilent15MAlertStartupGuard(unittest.TestCase):
    """Regression guard: the alert must NOT fire right after a bot
    restart even if the last eval timestamp is ancient. This was observed
    live on 2026-04-24 18:14:51 UTC when the 938690b deploy restart
    produced a false-positive Telegram alert 27 seconds into uptime.

    The fix requires the bot process to have been running at least
    SILENCE_ALERT_MIN_UPTIME_SECONDS (15 min) before the alert can fire
    — enough time for a normal scan to produce at least one eval.
    """

    def test_no_alert_if_bot_just_restarted(self):
        """Last eval 30 min ago, bot uptime 5 min — don't alert (bot
        hasn't had a chance to run yet)."""
        s = _make_scanner_with_eval_age(age_minutes=30, uptime_minutes=5)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_no_alert_at_14_min_uptime(self):
        """Just under the 15-min uptime floor — still no alert even
        with old eval."""
        s = _make_scanner_with_eval_age(age_minutes=30, uptime_minutes=14)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_alert_fires_at_16_min_uptime(self):
        """Just past the 15-min uptime floor with stale eval → alert."""
        s = _make_scanner_with_eval_age(age_minutes=30, uptime_minutes=16)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_alert_message_includes_uptime(self):
        """Alert surfaces bot uptime so operators can distinguish a
        real outage from a just-restarted edge case (even though the
        guard should prevent the latter)."""
        s = _make_scanner_with_eval_age(age_minutes=30, uptime_minutes=20)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self.assertIn("Bot uptime:", msg)

    def test_process_start_ts_set_on_first_call_if_missing(self):
        """If an older bot version instance doesn't have the attr, the
        method sets it to now — which naturally means the guard blocks
        alerting for the next 15 min. Protects against stale-instance
        false positives across the first deploy of this code."""
        s = OpportunityScanner.__new__(OpportunityScanner)
        s._state = MagicMock()
        s._state.conn.execute.side_effect = lambda *a, **kw: MagicMock(
            fetchone=lambda: None)
        s._kalshi_feed = MagicMock()
        self.assertFalse(hasattr(s, "_silence_alert_process_start_ts"))
        s._check_15m_silence_alert(_ACTIVE_15M)
        self.assertTrue(hasattr(s, "_silence_alert_process_start_ts"))


class TestSilent15MAlertCatalogGap(unittest.TestCase):
    """Regression guard: when Kalshi's /events?status=open returns zero
    open 15M windows (the just-expired window has been dropped via
    seconds_to_close < 0 and Kalshi hasn't published the next window
    yet), the bot is healthy but produces no 15M evals. Apr 24 2026:
    11 such gaps in 24h, all firing false-positive Telegram alerts.

    The catalog-gap guard suppresses the loud alert when
    active_windows contains zero product_type='15m' entries, logging
    a quieter KALSHI_15M_CATALOG_GAP line instead.
    """

    def test_catalog_gap_no_alert_when_zero_15m_windows(self):
        """Silence > threshold but Kalshi has zero 15M windows →
        upstream catalog gap, no Telegram alert."""
        s = _make_scanner_with_eval_age(age_minutes=15)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert([])
            mock_tele.send.assert_not_called()

    def test_catalog_gap_no_alert_with_only_hourly_windows(self):
        """Active windows are all hourly/weather/spx — no 15M means it's
        a Kalshi catalog gap, not bot silence."""
        s = _make_scanner_with_eval_age(age_minutes=15)
        non_15m = [
            {"product_type": "hourly", "asset": "BTC"},
            {"product_type": "weather", "asset": "NYC_TEMP"},
            {"product_type": "spx_hourly", "asset": "SPX"},
        ]
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(non_15m)
            mock_tele.send.assert_not_called()

    def test_alert_fires_when_15m_window_present(self):
        """Real silence: 15M window exists but no eval rows → alert."""
        s = _make_scanner_with_eval_age(age_minutes=15)
        active = [{"product_type": "15m", "asset": "BTC"}]
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(active)
            mock_tele.send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
