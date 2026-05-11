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

import ast
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
from bot.scanner import OpportunityScanner  # Default active_windows for tests that exercise the 15M-present branch.
import bot.notifier  # noqa: F401
# A non-empty list with product_type="15m" bypasses the catalog-gap guard
# (added 2026-04-24) so the silence alert can fire on age threshold alone.
_ACTIVE_15M = [{"product_type": "15m", "asset": "BTC"}]


def _make_scanner_with_eval_age(
    age_minutes: float, uptime_minutes: float = 30.0,
    rejection_age_minutes: float = None,
    rejection_reason: str = "low_probability_15m",
    n_rejection_rows: int = 1,
) -> OpportunityScanner:
    """Stub scanner with an evaluated_opportunities table containing one
    KX*15M row whose evaluation_time is `age_minutes` old. Bot uptime is
    simulated as `uptime_minutes` (default 30, well past the 15-min
    SILENCE_ALERT_MIN_UPTIME threshold).

    If `rejection_age_minutes` is set, seeds `n_rejection_rows`
    rejected_opportunities rows (default 1, set higher to simulate
    silent-bail floods that hit every ticker every tick). Used by tests
    that prove rejection-only scan activity counts as 'scan alive'
    (regression for the 2026-04-26 false-positive burst where 21 min of
    rejection-only writes triggered SILENT alerts) and that bail floods
    still alert (regression for the 2026-04-24 silent-scan PM)."""
    import time as _t
    s = OpportunityScanner.__new__(OpportunityScanner)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = sqlite3.connect(tmp.name)
    conn.execute(
        "CREATE TABLE evaluated_opportunities "
        "(ticker TEXT, evaluation_time TEXT)"
    )
    conn.execute(
        "CREATE TABLE rejected_opportunities "
        "(ticker TEXT, rejection_time TEXT, rejection_reason TEXT)"
    )
    ts = (datetime.datetime.now(datetime.timezone.utc)
          - datetime.timedelta(minutes=age_minutes))
    conn.execute(
        "INSERT INTO evaluated_opportunities VALUES (?, ?)",
        ("KXBTC15M-26APR241400-00",
         ts.isoformat(timespec="microseconds").replace("+00:00", "Z")))
    if rejection_age_minutes is not None:
        for i in range(n_rejection_rows):
            # Spread rows over a small window so MAX() picks the freshest
            # while floods exercise volume. Latest row is at exactly
            # `rejection_age_minutes`; earlier rows fan out backwards.
            rts = (datetime.datetime.now(datetime.timezone.utc)
                   - datetime.timedelta(
                       minutes=rejection_age_minutes,
                       seconds=i * 0.5))
            conn.execute(
                "INSERT INTO rejected_opportunities VALUES (?, ?, ?)",
                (f"KXBTC15M-26APR241400-0{i:02d}",
                 rts.isoformat(
                     timespec="microseconds").replace("+00:00", "Z"),
                 rejection_reason))
    conn.commit()
    s._state = MagicMock()
    s._state.conn = conn
    s._kalshi_feed = MagicMock()
    s._kalshi_feed.is_connected = True
    # Seed the process-start timestamp so the uptime gate behaves
    # deterministically. 30 min uptime is past the 15-min floor.
    s._silence_alert_process_start_ts = _t.time() - (uptime_minutes * 60)
    # Heartbeat: simulates `_scan_15m_iter_heartbeat_ts` set by scan()
    # in the per-window loop body (~bot/_impl.py:9909). Initialized to 0.0
    # in `OpportunityScanner.__init__` (~bot/_impl.py:9087). Default 0.0
    # here matches "scan body has not iterated a 15M window since
    # process start." Tests that need a fresh heartbeat set it
    # explicitly.
    s._scan_15m_iter_heartbeat_ts = 0.0
    return s


class TestSilent15MAlert(unittest.TestCase):

    def test_recent_eval_no_alert(self):
        """Last eval 2 min ago → no alert."""
        s = _make_scanner_with_eval_age(age_minutes=2)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_9_min_age_no_alert(self):
        """Right under the 10-min threshold → no alert."""
        s = _make_scanner_with_eval_age(age_minutes=9)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_11_min_age_alert_fires(self):
        """Over the 10-min threshold → Telegram alert fires."""
        s = _make_scanner_with_eval_age(age_minutes=11)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()
            # Verify dedup key present so repeated calls don't spam
            call = mock_tele.send.call_args
            self.assertEqual(call.kwargs.get("dedup_key"), "silent_15m_alert")

    def test_30_min_age_alert_contains_duration(self):
        """Alert message surfaces the silence duration for context."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_missing_telegram_no_crash(self):
        """_TELEGRAM is None (no token configured) → don't crash,
        just log. Alert silently dropped."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        with patch.object(bot.notifier, "_TELEGRAM", None):
            # Should not raise
            s._check_15m_silence_alert(_ACTIVE_15M)

    def test_telegram_send_error_swallowed(self):
        """If Telegram send raises (network blip), the scan loop is
        not affected."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_no_alert_at_14_min_uptime(self):
        """Just under the 15-min uptime floor — still no alert even
        with old eval."""
        s = _make_scanner_with_eval_age(age_minutes=30, uptime_minutes=14)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_alert_fires_at_16_min_uptime(self):
        """Just past the 15-min uptime floor with stale eval → alert."""
        s = _make_scanner_with_eval_age(age_minutes=30, uptime_minutes=16)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_alert_message_includes_uptime(self):
        """Alert surfaces bot uptime so operators can distinguish a
        real outage from a just-restarted edge case (even though the
        guard should prevent the latter)."""
        s = _make_scanner_with_eval_age(age_minutes=30, uptime_minutes=20)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(non_15m)
            mock_tele.send.assert_not_called()

    def test_alert_fires_when_15m_window_present(self):
        """Real silence: 15M window exists but no eval rows → alert."""
        s = _make_scanner_with_eval_age(age_minutes=15)
        active = [{"product_type": "15m", "asset": "BTC"}]
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(active)
            mock_tele.send.assert_called_once()


class TestSilent15MAlertRejectionActivity(unittest.TestCase):
    """Regression guard: 2026-04-26 ~07:30-07:50 UTC false-positive burst.

    A 21-minute window where evaluated_opportunities had no new 15M rows
    but rejected_opportunities was actively being written (rows at 07:30,
    07:45, 07:46) — proof the scanner was alive and producing rejections.
    The watchdog still alerted because its SQL only consulted
    evaluated_opportunities, contradicting its own docstring ("Any
    filter_stage counts (rejections included)"). The alert is only
    valuable if it fires on real failures; rejection-only periods are
    legitimate quiet markets, not bot silence.
    """

    def test_rejection_activity_counts_as_scan_alive(self):
        """Eval 30 min stale, but a rejection 2 min ago → scanner is
        producing 15M output via rejections → no alert.
        This is the 2026-04-26 false-positive shape."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_both_eval_and_rejection_stale_alert_fires(self):
        """Eval 30 min stale AND rejection 30 min stale → genuinely
        silent → alert."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=30)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_recent_eval_stale_rejection_no_alert(self):
        """Eval 2 min ago, rejection 30 min ago → recent activity via
        evals → no alert (preserves pre-fix behavior for the
        eval-dominant case)."""
        s = _make_scanner_with_eval_age(
            age_minutes=2, rejection_age_minutes=30)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_rejection_at_threshold_boundary(self):
        """Eval 30 min stale, rejection 9 min ago (just under threshold)
        → no alert — rejection still counts as productive scan."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=9)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_rejection_just_past_threshold_alert_fires(self):
        """Eval 30 min stale, rejection 11 min stale → both past 10-min
        threshold → alert fires."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=11)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()


class TestSilent15MAlertSilentBailDetection(unittest.TestCase):
    """Regression guard: the 2026-04-24 22:12 UTC WS-cache-drift outage
    must remain detectable. Fix #2 of that postmortem (commit 9dd396b)
    added insert_rejection() calls in the silent-bail paths so they
    leave a DB trace — rejection_reason='no_orderbook' or 'no_best_ask'.

    A naive "rejections count as scan alive" relaxation would mask this
    failure: silent-bail floods rejected_opportunities with no_orderbook
    rows, the watchdog sees recent rejection_time, and stays silent —
    exactly when an alert is most needed.

    These tests REQUIRE that bail-style rejection reasons do NOT count
    as scan-alive. Healthy reasons (low_probability_15m, edge_too_low,
    etc.) DO count.
    """

    def test_no_orderbook_flood_still_alerts(self):
        """Stale eval + recent rejection but reason='no_orderbook' →
        silent-bail signature → must alert. Seeds 3 rows to clear
        `_BAIL_MIN_ROWS_WHEN_STALE` (R7 [A2]) — single-tick blips
        below threshold are intentionally not BAIL FLOOD-classified."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_orderbook",
            n_rejection_rows=3)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_no_best_ask_flood_still_alerts(self):
        """Same shape with the other silent-bail reason."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_best_ask",
            n_rejection_rows=3)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_healthy_reason_suppresses_alert(self):
        """Stale eval + recent rejection with healthy reason
        (low_probability_15m) → quiet market, scan alive → no alert.
        This is the 2026-04-26 false-positive shape."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="low_probability_15m")
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_threshold_unparsable_flood_still_alerts(self):
        """`threshold_unparsable` is a third silent-bail reason
        (bot/_impl.py:9997). It indicates the scanner couldn't parse the
        strike from the ticker — bail-shaped, not healthy. If Kalshi
        renames the ticker format, all 4 active 15M tickers hit this
        before dedup caps each one (`_eval_opp_seen`); 3 rows is the
        smallest count that proves multi-ticker spread (per R7 [A2]
        threshold rationale)."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)  # all 4 15M tickers hit
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_single_blip_below_threshold_no_bail_alert(self):
        """R7 [A2]: 1 bail row + stale eval is BELOW threshold (3).
        Must NOT fire BAIL FLOOD — that would mis-classify a single
        transient orderbook fetch failure as a cache-drift recurrence
        and send the operator to the wrong KB article. Falls through
        to generic SILENT instead."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_orderbook",
            n_rejection_rows=1)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()
            # Generic SILENT, not BAIL FLOOD.
            call = mock_tele.send.call_args
            self.assertEqual(
                call.kwargs.get("dedup_key"), "silent_15m_alert")

    def test_no_orderbook_flood_volume_still_alerts(self):
        """The actual bug shape is per-tick floods (4 assets × N ticks).
        Volume test prevents regressions like accidental LIMIT 1 OFFSET
        clauses or row-count gates in the watchdog SQL."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_orderbook",
            n_rejection_rows=200)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()


def _seed_bail_rows(conn, reason: str, n: int,
                    rejection_age_minutes: float = 2,
                    ticker_prefix: str = "KXBTC15M-26APR241400-"):
    """Append n rejected_opportunities rows with the given reason and
    age, using distinct ticker suffixes (incl. full reason name) to
    avoid namespace collisions across reasons. Adversarial review C8:
    earlier `reason[:3]` truncation collided `no_orderbook` with
    `no_best_ask` (both → "no_") — when callers seed both, ticker
    collisions could silently lose rows under a future UNIQUE
    constraint. Full reason in suffix is namespace-safe.
    """
    base_ts = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(minutes=rejection_age_minutes))
    for i in range(n):
        ts = (base_ts - datetime.timedelta(seconds=i * 0.5))
        conn.execute(
            "INSERT INTO rejected_opportunities VALUES (?, ?, ?)",
            (f"{ticker_prefix}{reason}-{i:04d}",
             ts.isoformat(timespec="microseconds").replace("+00:00", "Z"),
             reason))
    conn.commit()


class TestBailFloodMessageDifferentiation(unittest.TestCase):
    """Regression for 2026-05-04 ~19:47 UTC live alert: the BAIL FLOOD
    message hardcoded "Likely: WS-cache-drift recurrence" and pointed
    operators to the cache-drift KB doc, but the actual cause was
    Kalshi-side threshold-publish delay — `floor_strike=null` and
    `yes_sub_title="Target price: TBD"` for all 4 newly-opened 15M
    windows. Restart would have been useless. Operator only diagnosed
    correctly by ad-hoc curling Kalshi REST.

    The watchdog already detected the silent-bail signature correctly
    (`_BAIL_REJECTION_REASONS` includes `threshold_unparsable`); only
    the message was misleading. These tests pin the per-shape routing
    so the message matches the diagnosis.

    Routing rule: route to THRESHOLD UNPARSABLE message only when (a)
    threshold_unparsable count clears the bail threshold AND (b) zero
    WS-cache-shape rows present. Mixed shapes default to BAIL FLOOD
    (cache-drift dominates because it carries higher fix-urgency:
    missed trades + risk of bad fills vs. wait-for-Kalshi).
    """

    def test_threshold_unparsable_only_routes_to_threshold_message(self):
        """4 threshold_unparsable rows + 0 WS rows → THRESHOLD UNPARSABLE
        message, NOT WS-cache-drift. Matches May 4 incident shape."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()
            call = mock_tele.send.call_args
            msg = call.args[0]
            self.assertIn("THRESHOLD UNPARSABLE", msg)
            self.assertNotIn("WS-cache-drift", msg)
            self.assertNotIn("BAIL FLOOD", msg)
            # Different dedup_key so this can fire concurrently with a
            # WS-shape alert if both pathologies happen back-to-back.
            self.assertEqual(
                call.kwargs.get("dedup_key"),
                "silent_15m_threshold_unparsable_alert")

    def test_threshold_message_includes_kalshi_probe_guidance(self):
        """The whole point of the new message is to send the operator
        to the right diagnostic. It must include the curl probe
        instructions and the wait-vs-code-fix decision tree."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self.assertIn("api.elections.kalshi.com", msg)
            self.assertIn("floor_strike", msg)
            self.assertIn("TBD", msg)
            # Explicit no-restart guidance — May 4 lesson.
            self.assertIn("Restart will NOT help", msg)

    def test_ws_cache_drift_volume_preserves_bail_flood_message(self):
        """200 no_orderbook rows + 0 threshold rows → existing BAIL
        FLOOD message preserved (backward compat with the 2026-04-24
        cache-drift recurrence detector)."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_orderbook",
            n_rejection_rows=200)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            call = mock_tele.send.call_args
            msg = call.args[0]
            self.assertIn("BAIL FLOOD", msg)
            self.assertIn("WS-cache-drift", msg)
            self.assertNotIn("THRESHOLD UNPARSABLE", msg)
            self.assertEqual(
                call.kwargs.get("dedup_key"),
                "silent_15m_bail_flood_alert")

    def test_no_best_ask_signature_preserves_bail_flood_message(self):
        """Same as above with the other WS-cache bail reason. Locks the
        full WS-shape path, not just no_orderbook."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_best_ask",
            n_rejection_rows=50)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self.assertIn("BAIL FLOOD", msg)
            self.assertIn("WS-cache-drift", msg)
            self.assertNotIn("THRESHOLD UNPARSABLE", msg)

    def test_mixed_threshold_and_ws_routes_to_bail_flood(self):
        """Both threshold_unparsable AND no_orderbook firing → BAIL
        FLOOD. WS-cache-drift carries higher fix-urgency (missed trades
        + bad-fill risk) than the wait-for-Kalshi case. Conservative
        routing: any WS-shape row at all defaults to BAIL FLOOD."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        # Add WS-cache rows on top of the threshold_unparsable seed.
        _seed_bail_rows(s._state.conn, "no_orderbook", n=10)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self.assertIn("BAIL FLOOD", msg)
            self.assertNotIn("THRESHOLD UNPARSABLE", msg)

    def test_bail_breakdown_query_returns_dict(self):
        """The new `_query_recent_bail_breakdown` method returns
        Dict[str, int] keyed by rejection_reason. Direct contract test
        independent of the alert path."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        _seed_bail_rows(s._state.conn, "no_orderbook", n=7)
        _seed_bail_rows(s._state.conn, "no_best_ask", n=3)
        breakdown = s._query_recent_bail_breakdown()
        self.assertIsInstance(breakdown, dict)
        self.assertEqual(breakdown.get("threshold_unparsable", 0), 4)
        self.assertEqual(breakdown.get("no_orderbook", 0), 7)
        self.assertEqual(breakdown.get("no_best_ask", 0), 3)

    def test_bail_breakdown_query_returns_empty_on_no_rows(self):
        """No bail rows at all → empty dict (not None, not exception).
        Caller must use `.get(reason, 0)` to handle missing keys."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="low_probability_15m",  # healthy, not bail
            n_rejection_rows=10)
        breakdown = s._query_recent_bail_breakdown()
        self.assertEqual(breakdown, {})

    def test_bail_breakdown_query_swallows_db_errors(self):
        """SQL error in the breakdown query mirrors the count query's
        behavior: return empty (caller's `.get(..., 0)` handles it),
        advance the throttle to avoid query storm."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        s._state.conn.close()  # force future queries to error
        # Should not raise; returns empty dict
        result = s._query_recent_bail_breakdown()
        self.assertEqual(result, {})

    def test_bail_breakdown_failure_advances_throttle_clock(self):
        """Adversarial review C5: docstring promises that on query
        failure we advance `_silence_bail_breakdown_last_ts` to `now`
        — required to prevent query storm during permanent failure
        (matches `_query_recent_bail_count` failure-path behavior).
        Pin the contract."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        s._state.conn.close()  # force future queries to error
        before = time.time()
        s._query_recent_bail_breakdown()
        after = time.time()
        ts = getattr(s, "_silence_bail_breakdown_last_ts", 0.0)
        self.assertGreaterEqual(ts, before)
        self.assertLessEqual(ts, after)

    def test_bail_breakdown_returns_cached_within_throttle(self):
        """Adversarial review C4: the throttle must actually return
        cached data — otherwise the per-tick scan loop hits a
        non-indexed GROUP BY query every tick. Add rows after first
        call; second call (within 30 s) must return the FIRST snapshot,
        not the freshly-augmented one."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        first = s._query_recent_bail_breakdown()
        self.assertEqual(first.get("threshold_unparsable", 0), 4)
        # Add 100 more threshold rows. If caching works, second call
        # within throttle returns first snapshot (=4); without caching
        # it would return 104.
        _seed_bail_rows(s._state.conn, "threshold_unparsable", n=100)
        second = s._query_recent_bail_breakdown()
        self.assertEqual(second.get("threshold_unparsable", 0), 4)

    def test_bail_breakdown_re_queries_after_throttle_window(self):
        """Inverse of the cache-hit test: after the throttle window
        expires, the next call must re-query and reflect new rows.
        Manually rewinds `_silence_bail_breakdown_last_ts` to simulate
        elapsed time without a real sleep."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        s._query_recent_bail_breakdown()
        _seed_bail_rows(s._state.conn, "threshold_unparsable", n=100)
        # Rewind throttle past _BAIL_QUERY_THROTTLE_SECONDS (30s).
        s._silence_bail_breakdown_last_ts = time.time() - 60
        refreshed = s._query_recent_bail_breakdown()
        self.assertEqual(refreshed.get("threshold_unparsable", 0), 104)

    # ── Discriminator boundary tests (adversarial review C1, C6) ─────

    def test_threshold_dominant_with_one_stray_ws_row_routes_threshold(self):
        """Adversarial C1: today's incident had 4 threshold + 0 WS, but
        the next similar incident might have 1 stray WS blip alongside
        the real Kalshi-TBD pattern. With ratio-based dominance
        (≥80% threshold = ≥4× WS), `(n_thr=4, n_ws=1)` still routes to
        the THRESHOLD message — operator gets the right diagnostic
        despite a single transient WS hiccup."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        _seed_bail_rows(s._state.conn, "no_orderbook", n=1)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self.assertIn("THRESHOLD UNPARSABLE", msg)
            self.assertNotIn("BAIL FLOOD", msg)

    def test_threshold_below_ratio_with_two_ws_routes_bail_flood(self):
        """Adversarial C6: just-past-the-ratio mixed shape. `(n_thr=4,
        n_ws=2)` fails the ≥4× rule (4 < 8) → BAIL FLOOD with breakdown
        in the message. Pins the exact boundary so any future tweak
        of `_THRESHOLD_SHAPE_DOMINANCE_RATIO` surfaces here
        intentionally rather than as a silent behavior change."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        _seed_bail_rows(s._state.conn, "no_orderbook", n=2)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self.assertIn("BAIL FLOOD", msg)
            self.assertNotIn("THRESHOLD UNPARSABLE", msg)
            # Self-consistency: header total = breakdown sum (C2)
            # Header shows "Primary stale + N silent-bail rejection
            # rows" where N must equal threshold + ws sum (4 + 2 = 6).
            self.assertIn("Primary stale + 6 silent-bail", msg)

    def test_message_header_total_matches_breakdown_sum(self):
        """Adversarial C2: the displayed total in the message header
        MUST equal the sum of the Breakdown line counts. Pre-fix the
        header used gate-cached `bail_count` from a different throttle
        window than the breakdown query — operator could see
        `Primary stale + 7 ... Breakdown: ...=4, ...=10, ...=0` (sum
        14, header 7). Now header is derived from breakdown sum.
        Pin that.

        Setup: 4 threshold + 50 no_orderbook = 54 total. Header must
        say "+ 54 silent-bail" AND breakdown line must sum to 54."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        _seed_bail_rows(s._state.conn, "no_orderbook", n=50)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            # Header
            self.assertIn("Primary stale + 54 silent-bail", msg)
            # Breakdown line: extract the per-reason counts and sum
            # them; must equal 54 by definition of the contract.
            import re as _re
            m = _re.search(r"Breakdown: (.+?)\n", msg)
            self.assertIsNotNone(m,
                "BAIL FLOOD message must include 'Breakdown:' line")
            counts = _re.findall(r"=(\d+)", m.group(1))
            self.assertEqual(sum(int(c) for c in counts), 54)

    # ── Shape-bucket constant contract (adversarial review C3) ───────

    def test_shape_buckets_partition_bail_reasons(self):
        """Adversarial C3: shape buckets must partition (cover, no
        overlap) `_BAIL_REJECTION_REASONS`. A new bail reason that's
        not in either bucket would be invisible to the discriminator
        — `bail_count` would still increment but n_thr and n_ws would
        not, leading to misclassification AND the Breakdown line
        showing zero for the new reason. Module-load assertion
        enforces this; this test pins the contract from the test
        side."""
        scanner = bot.scanner.OpportunityScanner
        ws = scanner._WS_SHAPE_BAIL_REASONS
        thr = scanner._THRESHOLD_SHAPE_BAIL_REASONS
        bail = set(scanner._BAIL_REJECTION_REASONS)
        self.assertEqual(ws | thr, bail,
            "Shape buckets must cover all _BAIL_REJECTION_REASONS")
        self.assertEqual(ws & thr, set(),
            "Shape buckets must be disjoint")

    def test_breakdown_str_iterates_all_bail_reasons(self):
        """Adversarial C3 follow-up: `_format_bail_breakdown` must
        surface every member of `_BAIL_REJECTION_REASONS` so a future
        new reason is visible in the alert without a separate edit
        site. Pin via direct call with a pre-built dict."""
        scanner = bot.scanner.OpportunityScanner
        formatted = scanner._format_bail_breakdown(
            {"threshold_unparsable": 5})
        for reason in scanner._BAIL_REJECTION_REASONS:
            self.assertIn(reason, formatted,
                f"Breakdown string must mention {reason}")
        # Missing keys in input dict must render as 0 (not absent).
        self.assertIn("no_orderbook=0", formatted)
        self.assertIn("no_best_ask=0", formatted)
        self.assertIn("threshold_unparsable=5", formatted)

    def test_breakdown_str_is_alphabetically_sorted(self):
        """Adversarial round-2 C5: `_format_bail_breakdown` must sort
        alphabetically, not by tuple order, so a maintainer reordering
        `_BAIL_REJECTION_REASONS` doesn't silently flip alert layout.
        Pin the order with explicit position assertions."""
        scanner = bot.scanner.OpportunityScanner
        formatted = scanner._format_bail_breakdown({
            "no_orderbook": 7,
            "threshold_unparsable": 1,
            "no_best_ask": 3,
        })
        idx_nba = formatted.index("no_best_ask=")
        idx_nob = formatted.index("no_orderbook=")
        idx_thr = formatted.index("threshold_unparsable=")
        # Alphabetical: no_best_ask < no_orderbook < threshold_unparsable
        self.assertLess(idx_nba, idx_nob)
        self.assertLess(idx_nob, idx_thr)

    # ── Failure-mode coverage (adversarial round-2 C1, C6, C7, C9) ──

    def test_bail_breakdown_failure_logs_warning_once(self):
        """Adversarial round-2 C1: breakdown query failure must surface
        in journalctl as a rate-limited warning. Without this, a
        permanent failure (e.g., schema change breaks the GROUP BY)
        leaves the operator with zero-everywhere alerts and no log
        evidence of why. Mirrors `_query_recent_bail_count`'s warn
        pattern. Sticky flag prevents per-tick log flood (~30/min).
        """
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        s._state.conn.close()  # force query failure
        # First call: warning logged, flag set.
        with self.assertLogs(level="WARNING") as cm:
            s._query_recent_bail_breakdown()
        self.assertTrue(any(
            "bail-breakdown query failed" in r.getMessage()
            for r in cm.records),
            "First failure should log a warning")
        self.assertTrue(getattr(
            s, "_silence_watchdog_warned_breakdown", False))
        # Second call within throttle: no new warning (cached empty).
        # (The throttle returns cached empty without re-querying or
        # re-logging.)

    def test_bail_breakdown_failure_resets_flag_on_recovery(self):
        """Adversarial round-2 C9: warn-flag must reset after a
        successful query. Without this, a transient failure followed
        by recovery leaves the flag stuck → next failure not logged.
        Mirrors count query's reset-on-success at bot/_impl.py:18494-18496.
        """
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        # Manually set flag as if a previous failure happened.
        s._silence_watchdog_warned_breakdown = True
        # Successful call should reset the flag.
        s._query_recent_bail_breakdown()
        self.assertFalse(getattr(
            s, "_silence_watchdog_warned_breakdown", True),
            "Successful query must reset the warn flag")

    def test_bail_breakdown_failure_with_operational_error(self):
        """Adversarial round-2 C6: production failure modes are
        `OperationalError: database is locked` and similar — not
        `ProgrammingError` from a closed conn. Use a wrapper that
        raises OperationalError to mirror production. Bare
        `except Exception:` catches both; this test pins the contract
        for the operator-realistic case."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        real_conn = s._state.conn

        class _RaisingConn:
            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

            def __getattr__(self, name):
                return getattr(real_conn, name)

        s._state.conn = _RaisingConn()
        result = s._query_recent_bail_breakdown()
        self.assertEqual(result, {})
        # Throttle clock advanced so the query storm is bounded
        # (matches count-query failure-path contract).
        self.assertGreater(
            getattr(s, "_silence_bail_breakdown_last_ts", 0.0), 0.0)

    def test_threshold_dominance_ratio_is_three(self):
        """Adversarial round-3 C4: original ratio of 4 (80%) was
        asymmetric at small n — at `(n_thr=3, n_ws=1)` the boundary
        rejected a real 75%-threshold case as BAIL FLOOD. Ratio
        loosened to 3 (75%) so a 3-ticker incident with 1 WS blip
        routes correctly. Pin the constant + boundary cases here."""
        scanner = bot.scanner.OpportunityScanner
        self.assertEqual(scanner._THRESHOLD_SHAPE_DOMINANCE_RATIO, 3)

    def test_three_threshold_one_ws_routes_threshold_at_new_ratio(self):
        """Adversarial round-3 C4: `(n_thr=3, n_ws=1)` is the case
        the loosened ratio is meant to fix. 3 ≥ 3×1 → THRESHOLD."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=3)
        _seed_bail_rows(s._state.conn, "no_orderbook", n=1)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self.assertIn("THRESHOLD UNPARSABLE", msg)
            self.assertNotIn("BAIL FLOOD", msg)

    def test_three_threshold_two_ws_routes_bail_flood(self):
        """Adversarial round-3 C4 inverse: just past the new 3×
        boundary. `(n_thr=3, n_ws=2)`: 3 < 3×2=6 → BAIL FLOOD.
        Pins that the loosening didn't go too far."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=3)
        _seed_bail_rows(s._state.conn, "no_orderbook", n=2)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self.assertIn("BAIL FLOOD", msg)
            self.assertNotIn("THRESHOLD UNPARSABLE", msg)

    def test_threshold_message_includes_affected_ticker_list(self):
        """Adversarial round-3 C3: the May 4 incident was only
        diagnosed by ad-hoc curl. Bare `<ticker>` template would
        recreate that friction. Embed the actual rejecting tickers
        and the curl example must use a real ticker, not `<ticker>`.

        Round-4 C6: tighten the assertion to the specific
        `Affected tickers:` LINE rather than `assertIn("KX", split[1])`
        — the loose check passed if "KX" appeared anywhere in the
        rest of the message (including the curl URL itself), so a
        regression to `<no recent tickers found>` would have passed.
        """
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            # Pull the SPECIFIC `Affected tickers:` line, not the
            # rest-of-message — this catches the empty-list regression.
            affected_line = next(
                (l for l in msg.split("\n")
                 if l.startswith("Affected tickers:")),
                None)
            self.assertIsNotNone(affected_line,
                "THRESHOLD message must include Affected tickers: line")
            # The fixture writes tickers like
            # KXBTC15M-26APR241400-threshold_unparsable-0000.
            # Real Kalshi prefix should be on the line itself.
            self.assertIn("KX", affected_line)
            self.assertNotIn("(cache empty", affected_line)
            # Bare placeholder must be replaced with a real ticker.
            self.assertNotIn("markets/<ticker>", msg)

    def test_threshold_header_label_derives_from_bucket(self):
        """Adversarial round-3 C2: header label is derived from
        `_THRESHOLD_SHAPE_BAIL_REASONS` (sorted), not a hardcoded
        `"threshold_unparsable"` literal. Locks the contract: if a
        future maintainer adds a 2nd reason to the bucket, the header
        will surface it without a parallel literal edit.

        Round-4 C5: patch BOTH `_BAIL_REJECTION_REASONS` and the
        bucket together so the partition invariant
        `WS | THRESHOLD == _BAIL_REJECTION_REASONS` is preserved at
        runtime. Earlier version patched only the bucket, leaving the
        runtime invariant violated — the test passed by exploiting
        that assertions are import-time only, not by exercising the
        intended contract.
        """
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        new_thr_bucket = frozenset({
            "threshold_unparsable", "another_threshold_reason"})
        new_bail_reasons = (
            "no_orderbook", "no_best_ask",
            "threshold_unparsable", "another_threshold_reason")
        with patch.object(
                bot.scanner.OpportunityScanner,
                "_THRESHOLD_SHAPE_BAIL_REASONS", new_thr_bucket), \
             patch.object(
                bot.scanner.OpportunityScanner,
                "_BAIL_REJECTION_REASONS", new_bail_reasons):
            # Verify the invariant holds at runtime under the patch
            # (would fail if either patch was forgotten).
            scanner = bot.scanner.OpportunityScanner
            self.assertEqual(
                scanner._WS_SHAPE_BAIL_REASONS
                | scanner._THRESHOLD_SHAPE_BAIL_REASONS,
                set(scanner._BAIL_REJECTION_REASONS),
                "Patched constants must preserve the partition contract")
            with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
                s._check_15m_silence_alert(_ACTIVE_15M)
                msg = mock_tele.send.call_args.args[0]
                # Both bucket members must appear in the header
                # (alphabetical order).
                header_line = msg.split("\n")[1]
                self.assertIn("another_threshold_reason", header_line)
                self.assertIn("threshold_unparsable", header_line)
                self.assertLess(
                    header_line.index("another_threshold_reason"),
                    header_line.index("threshold_unparsable"),
                    "Header label must be alphabetically sorted")

    def test_breakdown_failure_preserves_last_good_cache(self):
        """Adversarial round-3 C1/C5: a transient query failure must
        NOT clobber the last-good breakdown cache. Without this, a
        single locked-DB tick during a real Kalshi-TBD incident
        flipped routing from THRESHOLD (good cache) to BAIL FLOOD
        ('(breakdown unavailable)' annotation) and back when the next
        tick recovered — two contradictory dedup_keys for the same
        underlying incident. Pin: failure path returns the last-good
        cache, not empty."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        # First call: succeeds, populates cache with {tu: 4}.
        first = s._query_recent_bail_breakdown()
        self.assertEqual(first.get("threshold_unparsable", 0), 4)
        # Force the cache's throttle to expire so next call re-queries.
        s._silence_bail_breakdown_last_ts = 0.0
        # Wrap conn so the next breakdown query raises.
        real_conn = s._state.conn

        class _RaisingConn:
            def execute(self, sql, *args, **kwargs):
                if "GROUP BY rejection_reason" in sql:
                    raise sqlite3.OperationalError(
                        "database is locked")
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        s._state.conn = _RaisingConn()
        # Second call: query fails, but should return the LAST-GOOD
        # cache, not empty.
        second = s._query_recent_bail_breakdown()
        self.assertEqual(second.get("threshold_unparsable", 0), 4,
            "Failure path must preserve last-good breakdown")
        # Throttle clock advanced regardless.
        self.assertGreater(
            getattr(s, "_silence_bail_breakdown_last_ts", 0.0), 0.0)

    def test_breakdown_failure_first_call_returns_empty(self):
        """Adversarial round-3 C1/C5 boundary: when there is NO
        last-good cache (very first call ever, query fails), the
        method falls back to `{}` rather than crashing on missing
        attribute. Operator sees the (breakdown unavailable) message,
        which is the correct degraded behavior."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        s._state.conn.close()  # break first call
        # Confirm no last-good cache exists yet.
        self.assertFalse(
            hasattr(s, "_silence_bail_breakdown_last_dict"))
        result = s._query_recent_bail_breakdown()
        self.assertEqual(result, {})

    def test_threshold_shape_bucket_label_drift_tripwire(self):
        """Adversarial round-3 C2 belt-and-suspenders: even with the
        derived-from-bucket label, the message rendering MUST be
        consistent across bucket size. If a future PR adds a bail
        reason but mistakenly hardcodes the singular label somewhere
        else, this asserts the bucket-size matches the header. Today
        bucket size is 1; this test will fail loudly if someone
        extends `_THRESHOLD_SHAPE_BAIL_REASONS` without updating
        whatever would need to change."""
        scanner = bot.scanner.OpportunityScanner
        # If the bucket grows, all the test's assumptions about
        # 'threshold_unparsable' as the sole label need re-review.
        # Loud failure beats silent label drift.
        self.assertEqual(
            len(scanner._THRESHOLD_SHAPE_BAIL_REASONS), 1,
            "Bucket size changed — review all 'threshold_unparsable' "
            "literals in tests + KB docs for label drift before "
            "updating this assertion.")

    # ── Tickers query (round-4 C1, C2, C7) ──────────────────────────

    def test_tickers_query_failure_logs_warning_once(self):
        """Adversarial round-4 C1: the tickers query has a different
        SQL shape (GROUP BY + ORDER BY + LIMIT) than the breakdown
        query and can fail independently. Warn-flag mirrors the
        breakdown pattern so a permanent failure surfaces in
        journalctl rather than being masked by the breakdown query's
        success."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        s._state.conn.close()  # force query failure
        with self.assertLogs(level="WARNING") as cm:
            s._query_recent_threshold_unparsable_tickers()
        self.assertTrue(any(
            "threshold-tickers query failed" in r.getMessage()
            for r in cm.records),
            "First failure should log a warning for tickers query")
        self.assertTrue(getattr(
            s, "_silence_watchdog_warned_tickers", False))

    def test_tickers_query_failure_resets_flag_on_recovery(self):
        """Adversarial round-4 C1: warn-flag must reset on success
        so the next failure (e.g., later in a long-running process)
        also logs once. Mirrors breakdown query pattern."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        s._silence_watchdog_warned_tickers = True
        s._query_recent_threshold_unparsable_tickers()
        self.assertFalse(getattr(
            s, "_silence_watchdog_warned_tickers", True),
            "Successful tickers query must reset the warn flag")

    def test_tickers_query_failure_preserves_last_good(self):
        """Adversarial round-4 C1 (extension of round-3 C1/C5):
        transient failure must preserve last-good cache rather than
        clobber to []. Otherwise a momentary lock-DB during a
        sustained Kalshi-TBD incident would empty the affected list,
        flipping the THRESHOLD message to the empty-affected path
        (different probe text, different operator action) for one
        throttle window."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        first = s._query_recent_threshold_unparsable_tickers()
        self.assertGreater(len(first), 0,
            "Fixture should produce affected tickers")
        # Force throttle to expire then break the next call.
        s._silence_threshold_tickers_last_ts = 0.0
        real_conn = s._state.conn

        class _RaisingConn:
            def execute(self, sql, *args, **kwargs):
                if "GROUP BY ticker" in sql:
                    raise sqlite3.OperationalError(
                        "database is locked")
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        s._state.conn = _RaisingConn()
        second = s._query_recent_threshold_unparsable_tickers()
        self.assertEqual(second, first,
            "Failure must preserve last-good tickers list")

    def test_empty_affected_probe_sql_derives_from_bucket(self):
        """Adversarial round-6 C2: the OPERATOR-FACING fallback SQL
        in the empty-affected probe must derive its `IN (...)` clause
        from `_THRESHOLD_SHAPE_BAIL_REASONS` — not hardcode
        `'threshold_unparsable'`. Otherwise a future bucket extension
        would mask half the affected tickers in the operator's
        copy-paste query (mirror of round-5 C1 for the bot's own SQL).

        Patches BOTH `_BAIL_REJECTION_REASONS` and the bucket together
        to preserve the partition contract at runtime. Wraps the conn
        so breakdown succeeds (gives THRESHOLD routing) but tickers
        fails (forces the empty-affected fallback)."""
        new_thr_bucket = frozenset({
            "threshold_unparsable", "another_threshold_reason"})
        new_bail_reasons = (
            "no_orderbook", "no_best_ask",
            "threshold_unparsable", "another_threshold_reason")
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        real_conn = s._state.conn

        class _TickerOnlyRaisingConn:
            def execute(self, sql, *args, **kwargs):
                if "GROUP BY ticker" in sql:
                    raise sqlite3.OperationalError(
                        "database is locked")
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        s._state.conn = _TickerOnlyRaisingConn()
        with patch.object(
                bot.scanner.OpportunityScanner,
                "_THRESHOLD_SHAPE_BAIL_REASONS", new_thr_bucket), \
             patch.object(
                bot.scanner.OpportunityScanner,
                "_BAIL_REJECTION_REASONS", new_bail_reasons):
            with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
                s._check_15m_silence_alert(_ACTIVE_15M)
                msg = mock_tele.send.call_args.args[0]
                self.assertIn("THRESHOLD UNPARSABLE", msg)
                self.assertIn("(cache empty", msg)
                probe_line = next(
                    (l for l in msg.split("\n")
                     if l.startswith("Probe:")),
                    None)
                self.assertIsNotNone(probe_line)
                # Both bucket reasons must appear in operator SQL.
                self.assertIn("'another_threshold_reason'", probe_line)
                self.assertIn("'threshold_unparsable'", probe_line)
                # IN(...) form, not = '...'.
                self.assertIn("rejection_reason IN (", probe_line)
                self.assertNotIn(
                    "rejection_reason='threshold_unparsable'",
                    probe_line)

    def test_empty_affected_probe_window_derives_from_constant(self):
        """Adversarial round-7 C1: the operator-facing fallback SQL's
        time window must derive from `_SILENCE_AGE_THRESHOLD_SECONDS`
        — not hardcode a literal value. Otherwise tuning the constant
        leaves the operator's copy-paste query referencing a stale
        window, producing tickers from a different incident than the
        bot is alerting on. Mirror of round-5 C1 / round-6 C2 (drift
        prevention via single source of truth).

        Round-8 C2 follow-up: window now rendered in SECONDS (full
        precision), not `// 60` minutes (lossy for non-multiples-of-60).
        Patch to 300s and assert SQL reflects exactly 300 seconds."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        real_conn = s._state.conn

        class _TickerOnlyRaisingConn:
            def execute(self, sql, *args, **kwargs):
                if "GROUP BY ticker" in sql:
                    raise sqlite3.OperationalError(
                        "database is locked")
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        s._state.conn = _TickerOnlyRaisingConn()
        with patch.object(
                bot.scanner.OpportunityScanner,
                "_SILENCE_AGE_THRESHOLD_SECONDS", 300):
            with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
                s._check_15m_silence_alert(_ACTIVE_15M)
                msg = mock_tele.send.call_args.args[0]
                probe_line = next(
                    (l for l in msg.split("\n")
                     if l.startswith("Probe:")),
                    None)
                self.assertIsNotNone(probe_line)
                # Patched 300s renders as `'-300 seconds'` exactly.
                self.assertIn("'-300 seconds'", probe_line)
                # Header line uses same constant for minutes (display
                # convention) — verify they tell consistent stories.
                header_line = msg.split("\n")[1]
                self.assertIn("in last 5 min", header_line)

    def test_empty_affected_probe_window_at_non_multiple_of_60(self):
        """Adversarial round-8 C2: at non-60-multiple values of
        `_SILENCE_AGE_THRESHOLD_SECONDS`, the operator SQL must still
        be precise. Pre-fix, 599s → `// 60 = 9 minutes` → operator
        misses the most-recent 59s of bail rows. Post-fix uses
        seconds form — exact regardless of value."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        real_conn = s._state.conn

        class _TickerOnlyRaisingConn:
            def execute(self, sql, *args, **kwargs):
                if "GROUP BY ticker" in sql:
                    raise sqlite3.OperationalError("locked")
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        s._state.conn = _TickerOnlyRaisingConn()
        with patch.object(
                bot.scanner.OpportunityScanner,
                "_SILENCE_AGE_THRESHOLD_SECONDS", 599):
            with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
                s._check_15m_silence_alert(_ACTIVE_15M)
                msg = mock_tele.send.call_args.args[0]
                probe_line = next(
                    (l for l in msg.split("\n")
                     if l.startswith("Probe:")),
                    None)
                self.assertIn("'-599 seconds'", probe_line)

    def test_threshold_message_when_tickers_empty_omits_placeholder(self):
        """Adversarial round-4 C2/C7: if tickers list is empty (cache
        empty due to first-call failure / cache miss), DO NOT render
        the bare `<ticker>` placeholder URL — that recreates exactly
        the friction the embed was meant to remove. Instead show a
        next-step DB query so the operator has actionable guidance.
        """
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        # Wrap conn so the tickers query fails on first call (no
        # last-good cache to fall back on).
        real_conn = s._state.conn

        class _TickerRaisingConn:
            def execute(self, sql, *args, **kwargs):
                if "GROUP BY ticker" in sql:
                    raise sqlite3.OperationalError(
                        "database is locked")
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        s._state.conn = _TickerRaisingConn()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            # Routing still THRESHOLD (breakdown succeeded).
            self.assertIn("THRESHOLD UNPARSABLE", msg)
            # Crucially: NO bare placeholder URL.
            self.assertNotIn("markets/<ticker>", msg)
            # Affected line should indicate cache empty + next steps.
            self.assertIn("(cache empty", msg)
            # Probe section uses the SQL fallback, not curl <ticker>.
            self.assertIn("rejected_opportunities", msg)

    # ── Markdown-parser safety (round-8 C1) ──────────────────────────

    def test_threshold_message_has_balanced_markdown_entities(self):
        """Adversarial round-8 C1: Telegram sends with parse_mode=
        'Markdown' (legacy). An odd-parity unmatched `_` (italic
        marker) in the message body causes HTTP 400 → alert never
        delivered → operator never sees the diagnostic this
        multi-round project is meant to deliver. Pin: the rendered
        THRESHOLD message must have balanced `*`, `_`, and `` ` ``
        markers (even count after stripping backtick code spans for
        underscores)."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self._assert_markdown_balanced(msg)

    def test_bail_flood_message_has_balanced_markdown_entities(self):
        """Adversarial round-8 C1 sibling: same parse-validity
        guarantee for the BAIL FLOOD path. Breakdown line includes
        `no_best_ask=N` etc. with word-internal underscores; must be
        wrapped in backticks via `_format_bail_breakdown`."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_orderbook",
            n_rejection_rows=200)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self._assert_markdown_balanced(msg)

    def test_threshold_empty_affected_message_has_balanced_markdown(self):
        """Adversarial round-8 C1: third path — empty-affected
        fallback. Operator-facing SQL line contains
        `rejected_opportunities` and `rejection_reason` etc., all
        wrapped in backticks. Pin parse validity."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)
        real_conn = s._state.conn

        class _TickerOnlyRaisingConn:
            def execute(self, sql, *args, **kwargs):
                if "GROUP BY ticker" in sql:
                    raise sqlite3.OperationalError("locked")
                return real_conn.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(real_conn, name)

        s._state.conn = _TickerOnlyRaisingConn()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            msg = mock_tele.send.call_args.args[0]
            self._assert_markdown_balanced(msg)

    def test_markdown_balance_helper_catches_unbalanced_underscore(self):
        """Adversarial round-9: the `_assert_markdown_balanced` helper
        is the sole guard for the round-8 fix. A future regex
        refactor (e.g., word-boundary `\\b_|_\\b` which inverts
        semantics) would silently neuter the helper while all
        positive callers continue to pass — production alerts would
        again fail HTTP 400. Negative tests pin the helper itself."""
        # 1 word-internal underscore (unbalanced) — must raise.
        with self.assertRaises(AssertionError):
            self._assert_markdown_balanced("foo _italic bar baz")

    def test_markdown_balance_helper_catches_unbalanced_backtick(self):
        """Same as above for backticks — unbalanced count must raise."""
        with self.assertRaises(AssertionError):
            self._assert_markdown_balanced("foo `code bar")

    def test_markdown_balance_helper_catches_unbalanced_asterisk(self):
        """Same as above for asterisks."""
        with self.assertRaises(AssertionError):
            self._assert_markdown_balanced("foo *bold bar")

    def test_markdown_balance_helper_strips_backtick_spans_correctly(self):
        """Underscores INSIDE a backtick span are neutralized (legacy
        Markdown spec). Helper must strip backtick spans before
        counting word-internal underscores.

        Asymmetry demo: `threshold_unparsable` (1 word-internal `_`,
        odd) inside a backtick span → 0 after strip → balanced.
        Same identifier raw → 1 word-internal `_` → unbalanced.

        This is the EXACT failure mode the round-8 fix prevents: a
        single `_` in `threshold_unparsable` (used in the THRESHOLD
        message header) was odd-parity until backticks neutralized it."""
        # Inside backticks → stripped → balanced (passes).
        self._assert_markdown_balanced(
            "Breakdown: `threshold_unparsable=4`")
        # Same identifier raw, ODD count of word-internal `_`: raises.
        with self.assertRaises(AssertionError):
            self._assert_markdown_balanced(
                "Breakdown: threshold_unparsable=4")

    @staticmethod
    def _assert_markdown_balanced(msg: str):
        """Helper: assert balanced *, `, and word-internal _ markers
        in a message intended for parse_mode='Markdown'. Strips
        backtick code spans before counting underscores (since `code`
        spans neutralize underscores per the legacy Markdown spec).
        Asterisks and backticks are checked as raw counts (must be
        even — paired)."""
        import re
        backtick_count = msg.count("`")
        if backtick_count % 2 != 0:
            raise AssertionError(
                f"Unbalanced backticks ({backtick_count}) in message:"
                f"\n{msg!r}")
        asterisk_count = msg.count("*")
        if asterisk_count % 2 != 0:
            raise AssertionError(
                f"Unbalanced asterisks ({asterisk_count}) in message:"
                f"\n{msg!r}")
        # Strip backtick code spans before counting underscores.
        stripped = re.sub(r"`[^`]*`", "", msg)
        # Within the stripped text, count word-internal underscores
        # (any `_` adjacent to alphanumeric chars on either side).
        # Word-boundary-only underscores are not parsed as italic by
        # legacy Markdown; only word-internal pairs are at risk.
        word_internal = re.findall(r"(?<=\w)_|_(?=\w)", stripped)
        if len(word_internal) % 2 != 0:
            raise AssertionError(
                "Unbalanced word-internal underscore count "
                f"({len(word_internal)}) outside backtick spans in "
                f"message:\n{msg!r}\n"
                f"Word-internal underscores found: {word_internal}")

    def test_partition_invariant_holds_at_runtime(self):
        """Adversarial round-4 C4: the partition + disjointness
        invariants moved from class-body assertions (which would
        crash bot/_impl.py at import on a developer mistake → systemd
        backoff loop with no Telegram) to test-only enforcement.
        This test owns the contract.

        Adding a new bail reason without bucketing it would: (a)
        fail this test in CI, (b) leave the new reason routed as
        zero in the discriminator, and (c) surface in the Breakdown
        line via `_format_bail_breakdown` (so it's visible to
        operators even if untested)."""
        scanner = bot.scanner.OpportunityScanner
        ws = scanner._WS_SHAPE_BAIL_REASONS
        thr = scanner._THRESHOLD_SHAPE_BAIL_REASONS
        bail = set(scanner._BAIL_REJECTION_REASONS)
        self.assertEqual(
            ws | thr, bail,
            "Shape buckets must cover all _BAIL_REJECTION_REASONS — "
            "any new bail reason MUST be added to either "
            "_WS_SHAPE_BAIL_REASONS or _THRESHOLD_SHAPE_BAIL_REASONS "
            "in the same commit.")
        self.assertEqual(
            ws & thr, set(),
            "Shape buckets must be disjoint — a reason can only be "
            "in one bucket.")

    def test_alert_with_breakdown_unavailable_uses_bail_flood(self):
        """Adversarial round-2 C2/C7/C8: when breakdown query fails
        but the gate-cached `bail_count` admitted us to the alert
        branch, we must (a) route to BAIL FLOOD as the conservative
        default (cannot determine shape), (b) display
        "(breakdown unavailable)" as the breakdown line — not
        zero-everywhere — so the operator knows shape info is
        missing rather than being misled into thinking ALL bail
        reasons happened to be zero, and (c) preserve a non-zero
        header total derived from the gate's count."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_orderbook",
            n_rejection_rows=5)  # gate sees 5
        # Prime the count query so the gate admits us.
        s._query_recent_bail_count()
        # Now break breakdown on the next call by making `execute`
        # raise. The count query already cached its 5; the next gate
        # call will return that cached 5. The breakdown query, when
        # called by the alert path, will hit our raising wrapper and
        # return empty.
        real_conn = s._state.conn

        class _BreakdownRaisingConn:
            def __init__(self, real):
                self._real = real
                self._fail_breakdown = False

            def execute(self, sql, *args, **kwargs):
                # Only fail GROUP BY (the breakdown query)
                if "GROUP BY rejection_reason" in sql:
                    raise sqlite3.OperationalError(
                        "database is locked")
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        s._state.conn = _BreakdownRaisingConn(real_conn)
        # Reset breakdown throttle so the alert path actually queries.
        s._silence_bail_breakdown_last_ts = 0.0

        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()
            call = mock_tele.send.call_args
            msg = call.args[0]
            # Conservative routing: BAIL FLOOD (not threshold).
            self.assertIn("BAIL FLOOD", msg)
            self.assertNotIn("THRESHOLD UNPARSABLE", msg)
            # Explicit annotation, not zero-everywhere.
            self.assertIn("(breakdown unavailable)", msg)
            # Header total preserved from gate count.
            self.assertIn("Primary stale + 5 silent-bail", msg)
            self.assertEqual(
                call.kwargs.get("dedup_key"),
                "silent_15m_bail_flood_alert")


class TestBailReasonConstantContract(unittest.TestCase):
    """AST-walk audit of bot/_impl.py: enumerate every literal-string
    rejection_reason passed to `insert_rejection(...)`, classify each
    as bail or healthy, and require every literal to be classified.

    Catches future PRs that add an unclassified reason — the previous
    "constant equals hardcoded duplicate" test was theater (R3 [A2]).
    This walks the source.

    Maintenance: when adding a new literal rejection_reason in bot/_impl.py,
    add it to either KNOWN_BAIL_REASONS (and to
    `_BAIL_REJECTION_REASONS` in bot/_impl.py — same commit) or
    KNOWN_HEALTHY_REASONS in this test. Dynamic-variable reasons
    (e.g., `reason = prob_result.get("reason")`) are skipped because
    AST cannot resolve their string values; those go through the
    healthy filter by virtue of NOT being in the bail set, which is
    appropriate (probability-engine refusals are healthy).

    RENAME procedure (e.g., `no_orderbook` → `nbbo_unavailable`):
    update _BAIL_REJECTION_REASONS in bot/_impl.py, KNOWN_BAIL_REASONS in
    this test, and the call-site string ALL in the same commit. The
    `unused_known` assertion below catches the partially-applied case.

    NEW DYNAMIC SHAPE: if a future PR adds an f-string or string
    concatenation as the reason arg (e.g., `f"no_{stem}"`), the AST
    walker raises explicitly rather than silently skipping — a
    silent skip would create a classification gap.
    """

    KNOWN_BAIL_REASONS = {
        "no_orderbook",
        "no_best_ask",
        "threshold_unparsable",
    }

    KNOWN_HEALTHY_REASONS = {
        "low_probability_15m",
        "weather_prob_none",
        "price_out_of_range_early",
    }

    def test_bail_constant_matches_curated_set(self):
        """`_BAIL_REJECTION_REASONS` in bot/_impl.py must equal this test's
        KNOWN_BAIL_REASONS. Update both in the same commit."""
        actual = set(bot.scanner.OpportunityScanner._BAIL_REJECTION_REASONS)
        self.assertEqual(
            actual, self.KNOWN_BAIL_REASONS,
            "_BAIL_REJECTION_REASONS drifted from KNOWN_BAIL_REASONS")

    def test_every_literal_reason_in_bot_is_classified(self):
        """Walk every `insert_rejection(...)` call site in bot/_impl.py,
        extract literal-string reason args (positional 4th or kwarg
        'reason'), and assert each is in KNOWN_BAIL_REASONS ∪
        KNOWN_HEALTHY_REASONS.

        SCOPE — bot/_impl.py only: the silence watchdog filters
        `WHERE ticker LIKE 'KX%15M%'`, and 15M-tickered rejection
        writes live exclusively in `bot/_impl.py`'s `OpportunityScanner.scan()`.
        Other engines (weather_engine.py, sports_engine.py, etc.)
        write their own product_type rows under different ticker
        namespaces (KXBTCD-…, KXHIGHNY-…), which the watchdog
        ignores. If a future engine ever writes 'KX*15M%' rejections
        from outside bot/_impl.py, extend this audit to walk those files
        as well. (R8 audit-scope clarification.)
        """
        # Bit 8.1 (2026-05-10): OpportunityScanner extracted to bot/scanner/__init__.py.
        # 15M `insert_rejection()` call sites moved with the class. Walk both
        # files so the audit survives the move.
        # Bit 9.3-iii.c (2026-05-11): bot/_impl.py DELETED — read tolerant of absence.
        bot_path = os.path.join(
            os.path.dirname(__file__), "..", "bot/_impl.py")
        scanner_path = os.path.join(
            os.path.dirname(__file__), "..", "bot/scanner/__init__.py")
        literals = set()
        for src_path in (bot_path, scanner_path):
            if not os.path.exists(src_path):
                continue
            with open(src_path) as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                # Match `something.insert_rejection(...)`
                if not (isinstance(node.func, ast.Attribute)
                        and node.func.attr == "insert_rejection"):
                    continue
                # 4th positional arg is the reason (after ticker,
                # event_ticker, asset). Check for literal string only;
                # dynamic Name/Subscript reasons are skipped.
                reason_node = None
                if len(node.args) >= 4:
                    reason_node = node.args[3]
                for kw in node.keywords:
                    if kw.arg == "reason":
                        reason_node = kw.value
                if reason_node is None:
                    continue
                if (isinstance(reason_node, ast.Constant)
                        and isinstance(reason_node.value, str)):
                    literals.add(reason_node.value)
                elif isinstance(reason_node, (ast.Name, ast.Subscript,
                                              ast.Attribute, ast.Call)):
                    # Dynamic — variable, dict lookup, attr, or function
                    # call (e.g., prob_result.get("reason")). Skipped per
                    # docstring: probability-engine refusals are healthy
                    # by construction.
                    continue
                else:
                    # f-string or BinOp concatenation would silently bypass
                    # classification. Fail loudly so the author either
                    # converts to a literal or extends the audit.
                    self.fail(
                        f"insert_rejection() at line {reason_node.lineno} "
                        f"uses a non-literal, non-variable reason "
                        f"(AST type: {type(reason_node).__name__}). "
                        f"Convert to a literal string or extend the audit "
                        f"to handle this AST shape — silently skipping "
                        f"would create a classification gap.")
        known = self.KNOWN_BAIL_REASONS | self.KNOWN_HEALTHY_REASONS
        unclassified = literals - known
        self.assertEqual(
            unclassified, set(),
            f"Unclassified literal rejection_reason values in bot/_impl.py: "
            f"{sorted(unclassified)}. Add each to either "
            f"KNOWN_BAIL_REASONS (and to _BAIL_REJECTION_REASONS in "
            f"bot/_impl.py — same commit) or KNOWN_HEALTHY_REASONS in this "
            f"test. Bail = scanner could not produce a probability "
            f"(WS cache drift, no orderbook, unparsable strike). "
            f"Healthy = scanner computed a probability and chose to "
            f"filter (low prob, edge too low, price out of range).")
        # Also verify each KNOWN reason IS actually used somewhere —
        # so renames don't leave orphan classifications behind.
        unused_known = known - literals
        self.assertEqual(
            unused_known, set(),
            f"KNOWN reasons not found in any insert_rejection() call "
            f"site: {sorted(unused_known)}. If a reason was removed "
            f"or renamed, drop it from this test (and from "
            f"_BAIL_REJECTION_REASONS in bot/_impl.py if bail). Stale "
            f"entries hide drift.")


class TestSilent15MAlertWatchdogFailureObservability(unittest.TestCase):
    """R2 [A3]: a permanently broken watchdog query must be observable.

    Pre-fix, `except Exception: return` swallowed all errors. If the
    rejected_opportunities schema drifted (no rejection_reason column,
    e.g.) the watchdog would silently return forever. The fix adds
    logging.warning() so the failure is at least visible in logs.
    """

    def test_query_failure_logs_warning(self):
        """When the SQL throws, a warning is logged before returning."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        # Drop the column the new query depends on, simulating schema
        # drift where the watchdog SQL would throw.
        s._state.conn.execute("DROP TABLE rejected_opportunities")
        s._state.conn.commit()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele, \
                patch.object(bot.scanner, "logging") as mock_log:
            s._check_15m_silence_alert(_ACTIVE_15M)
            # Should not alert (we couldn't compute age).
            mock_tele.send.assert_not_called()
            # Should log the failure.
            mock_log.warning.assert_called()
            warn_msg = mock_log.warning.call_args.args[0]
            self.assertIn("silent_15m", warn_msg)

    def test_primary_query_failure_logs_once(self):
        """R3 [A5]: primary query failure must log warning once per
        process lifetime (not every scan tick). Setup: drop the table
        the primary query depends on; primary fails first and short-
        circuits before bail query runs (under R6 new flow), so this
        test exercises only the primary failure path."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._state.conn.execute("DROP TABLE rejected_opportunities")
        s._state.conn.commit()
        with patch.object(bot.notifier, "_TELEGRAM"), \
                patch.object(bot.scanner, "logging") as mock_log:
            s._check_15m_silence_alert(_ACTIVE_15M)
            s._check_15m_silence_alert(_ACTIVE_15M)
            s._check_15m_silence_alert(_ACTIVE_15M)
            warn_msgs = [c.args[0]
                         for c in mock_log.warning.call_args_list]
            primary_warns = [m for m in warn_msgs if "primary" in m]
            self.assertEqual(
                len(primary_warns), 1,
                "primary query failure should log warning only once "
                "per process lifetime (between successes)")

    def test_warning_flag_resets_after_successful_query(self):
        """If a transient failure logs once and then the query
        recovers, a SUBSEQUENT failure should log again — the flag
        is per-failure-burst, not permanent."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        # Manually set the flag to simulate prior failure.
        s._silence_watchdog_warned_primary = True
        with patch.object(bot.notifier, "_TELEGRAM"), \
                patch.object(bot.scanner, "logging"):
            # Healthy query → flag should reset.
            s._check_15m_silence_alert(_ACTIVE_15M)
        self.assertFalse(s._silence_watchdog_warned_primary)

    def test_primary_recovery_does_not_reset_bail_flood_flag(self):
        """R4 [A3]: separate flags. If primary query recovers but the
        bail-flood query is still failing, the bail-flood warning must
        still be rate-limited to one log — the primary recovery must
        NOT clear the bail-flood flag.

        Setup: primary query SUCCEEDS (eval table healthy) but
        bail-flood query FAILS (wrap connection so COUNT(*) throws).
        Expect: primary flag resets, bail flag stays set."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        # Pre-set both flags True to simulate prior dual failure.
        s._silence_watchdog_warned_primary = True
        s._silence_watchdog_warned_bail_flood = True
        # Wrap connection so the bail-flood COUNT(*) query throws but
        # the primary UNION ALL query succeeds.
        real_conn = s._state.conn
        wrapper = MagicMock()

        def selective_execute(sql, params=()):
            if "COUNT(*)" in sql:
                raise sqlite3.OperationalError("simulated bail failure")
            return real_conn.execute(sql, params)
        wrapper.execute.side_effect = selective_execute
        s._state.conn = wrapper
        try:
            # Force a fresh bail query (bypass throttle) by clearing
            # the cached timestamp.
            s._silence_bail_query_last_ts = 0.0
            with patch.object(bot.notifier, "_TELEGRAM"), \
                    patch.object(bot.scanner, "logging"):
                s._check_15m_silence_alert(_ACTIVE_15M)
        finally:
            s._state.conn = real_conn
        self.assertFalse(
            s._silence_watchdog_warned_primary,
            "primary success should reset primary flag")
        self.assertTrue(
            s._silence_watchdog_warned_bail_flood,
            "bail-flood failure should leave bail flag set even when "
            "primary recovers")


class TestSilent15MAlertBailFlood(unittest.TestCase):
    """R3 [A1] CRITICAL: when evaluated_opportunities is empty AND
    rejected_opportunities contains ONLY silent-bail reasons, the
    primary watchdog query returns NULL and the bare `if not row[0]`
    return path silently skips alerting. This is the exact 2026-04-24
    WS-cache-drift signature on a freshly-deployed bot or after a
    state.db rebuild — the watchdog's blind spot is the failure shape
    it was built to catch.

    Fix: a secondary query counts bail-only rows in the recent
    window. If positive AND the primary query returned NULL AND
    Kalshi is publishing 15M windows, fire a SILENT (BAIL FLOOD)
    alert with its own dedup_key.
    """

    def test_bail_flood_with_empty_eval_table_alerts(self):
        """Eval table empty (fresh bot), rejection table has only
        bail rows → must alert."""
        import time as _t
        s = OpportunityScanner.__new__(OpportunityScanner)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.execute(
            "CREATE TABLE evaluated_opportunities "
            "(ticker TEXT, evaluation_time TEXT)")
        conn.execute(
            "CREATE TABLE rejected_opportunities "
            "(ticker TEXT, rejection_time TEXT, rejection_reason TEXT)")
        # Insert 50 bail rejection rows in the last 5 min.
        now = datetime.datetime.now(datetime.timezone.utc)
        for i in range(50):
            ts = (now - datetime.timedelta(minutes=5, seconds=i * 5))
            conn.execute(
                "INSERT INTO rejected_opportunities VALUES (?, ?, ?)",
                (f"KXBTC15M-26APR240000-{i:02d}",
                 ts.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                 "no_orderbook"))
        conn.commit()
        s._state = MagicMock()
        s._state.conn = conn
        s._kalshi_feed = MagicMock()
        s._kalshi_feed.is_connected = True
        s._silence_alert_process_start_ts = _t.time() - (30 * 60)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()
            # Distinct dedup_key from the standard silence alert so
            # operators see this is the bail-flood variant.
            call = mock_tele.send.call_args
            self.assertEqual(
                call.kwargs.get("dedup_key"),
                "silent_15m_bail_flood_alert")
            msg = call.args[0]
            self.assertIn("BAIL FLOOD", msg)

    def test_bail_flood_alert_skipped_when_kalshi_window_gap(self):
        """If Kalshi is publishing zero 15M windows, even a bail-flood
        is more likely upstream sparseness than a bot bug. Don't fire
        the loud Telegram alert in catalog-gap conditions."""
        import time as _t
        s = OpportunityScanner.__new__(OpportunityScanner)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.execute(
            "CREATE TABLE evaluated_opportunities "
            "(ticker TEXT, evaluation_time TEXT)")
        conn.execute(
            "CREATE TABLE rejected_opportunities "
            "(ticker TEXT, rejection_time TEXT, rejection_reason TEXT)")
        now = datetime.datetime.now(datetime.timezone.utc)
        for i in range(10):
            ts = (now - datetime.timedelta(minutes=5, seconds=i * 5))
            conn.execute(
                "INSERT INTO rejected_opportunities VALUES (?, ?, ?)",
                (f"KXBTC15M-26APR240000-{i:02d}",
                 ts.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                 "no_orderbook"))
        conn.commit()
        s._state = MagicMock()
        s._state.conn = conn
        s._kalshi_feed = MagicMock()
        s._kalshi_feed.is_connected = True
        s._silence_alert_process_start_ts = _t.time() - (30 * 60)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert([])  # zero windows
            mock_tele.send.assert_not_called()

    def test_no_data_at_all_no_alert(self):
        """Both tables fully empty → fresh bot pre-data → silent.
        (Uptime guard already passed but we still don't know if
        there's a bug or just a quiet startup.)"""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._state.conn.execute("DELETE FROM evaluated_opportunities")
        s._state.conn.commit()
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_recent_eval_plus_bail_rows_no_alert(self):
        """R7 [A3]: when primary returns a RECENT timestamp (healthy
        scan) AND bail rows are present (single-ticker partial
        failure), the watchdog must NOT fire BAIL FLOOD. The bail
        check is gated behind primary staleness — partial failures
        self-mask via the healthy primary.

        Without this test, a future change that moves the bail
        check before the primary staleness gate would silently
        misclassify partial failures as cache-drift recurrences."""
        s = _make_scanner_with_eval_age(
            age_minutes=2, rejection_age_minutes=2,
            rejection_reason="no_orderbook",
            n_rejection_rows=200)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_unparseable_ts_does_not_crash(self):
        """R7 [A1] CRITICAL: pre-fix, an unparseable `last_ts_str`
        from the primary query would cause the parse-fail branch to
        leave `age_sec`/`last_ts` undefined; the alert message path
        then NameErrored on `age_sec/60` (or AttributeError on
        last_ts.isoformat). The watchdog now treats parse failure as
        equivalent to None and short-circuits before message
        construction."""
        import time as _t
        s = OpportunityScanner.__new__(OpportunityScanner)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.execute(
            "CREATE TABLE evaluated_opportunities "
            "(ticker TEXT, evaluation_time TEXT)")
        conn.execute(
            "CREATE TABLE rejected_opportunities "
            "(ticker TEXT, rejection_time TEXT, rejection_reason TEXT)")
        # Garbage timestamp string that fromisoformat() will reject.
        conn.execute(
            "INSERT INTO evaluated_opportunities VALUES (?, ?)",
            ("KXBTC15M-26APR240000-00", "not-a-real-timestamp"))
        conn.commit()
        s._state = MagicMock()
        s._state.conn = conn
        s._kalshi_feed = MagicMock()
        s._kalshi_feed.is_connected = True
        s._silence_alert_process_start_ts = _t.time() - (30 * 60)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            # Must not raise NameError or AttributeError.
            try:
                s._check_15m_silence_alert(_ACTIVE_15M)
            except (NameError, AttributeError) as e:
                self.fail(
                    f"unparseable ts crashed the watchdog: {e!r}")
            # And must not fire a generic silence alert with bogus
            # message content.
            mock_tele.send.assert_not_called()

    def test_old_bail_rows_excluded_from_window(self):
        """R6 [A9]: bail rows older than _SILENCE_AGE_THRESHOLD_SECONDS
        must NOT count toward the bail signal. Without the time-window
        filter (`rejection_time >= ?`), a one-time historical bail
        burst would fire the alert forever. Setup: 50 bail rows from
        30 min ago + stale eval → no alert (rows are out of window).
        """
        import time as _t
        s = OpportunityScanner.__new__(OpportunityScanner)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.execute(
            "CREATE TABLE evaluated_opportunities "
            "(ticker TEXT, evaluation_time TEXT)")
        conn.execute(
            "CREATE TABLE rejected_opportunities "
            "(ticker TEXT, rejection_time TEXT, rejection_reason TEXT)")
        # Stale eval row from 30 min ago — primary will be stale.
        old_eval = (datetime.datetime.now(datetime.timezone.utc)
                    - datetime.timedelta(minutes=30))
        conn.execute(
            "INSERT INTO evaluated_opportunities VALUES (?, ?)",
            ("KXBTC15M-26APR240000-00",
             old_eval.isoformat(timespec="microseconds").replace("+00:00", "Z")))
        # 50 bail rows from 30 min ago — outside the 10-min window.
        for i in range(50):
            ts = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(minutes=30 + (i / 60.0)))
            conn.execute(
                "INSERT INTO rejected_opportunities VALUES (?, ?, ?)",
                (f"KXBTC15M-26APR240000-{i:02d}",
                 ts.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                 "no_orderbook"))
        conn.commit()
        s._state = MagicMock()
        s._state.conn = conn
        s._kalshi_feed = MagicMock()
        s._kalshi_feed.is_connected = True
        s._silence_alert_process_start_ts = _t.time() - (60 * 60)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            # Bail rows from 30 min ago must NOT trigger BAIL FLOOD.
            # Generic SILENT alert fires on the stale eval.
            mock_tele.send.assert_called_once()
            call = mock_tele.send.call_args
            self.assertEqual(
                call.kwargs.get("dedup_key"),
                "silent_15m_alert",
                "30-min-old bail rows must be excluded from the "
                "10-min bail-flood window — falls back to generic "
                "silence alert")

    def test_stale_eval_plus_bail_flood_prefers_bail_alert(self):
        """R5 [A1+A3] CRITICAL coverage gap: stale eval rows must NOT
        mask an active bail-flood. Operator should get the more
        diagnostic BAIL FLOOD alert (with KB pointer) when the bail
        query crosses the threshold, regardless of whether the eval
        table happens to have stale rows from before the failure
        started.

        Pre-fix: stale eval row → primary path returns ts → fires
        generic silent_15m_alert → bail-flood path never runs.
        Post-fix: bail-flood checked FIRST and independently."""
        import time as _t
        s = OpportunityScanner.__new__(OpportunityScanner)
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        conn = sqlite3.connect(tmp.name)
        conn.execute(
            "CREATE TABLE evaluated_opportunities "
            "(ticker TEXT, evaluation_time TEXT)")
        conn.execute(
            "CREATE TABLE rejected_opportunities "
            "(ticker TEXT, rejection_time TEXT, rejection_reason TEXT)")
        # Stale eval row from 60 min ago — pre-fix this would have
        # been the primary path's return value and the bail-flood
        # detection would have been skipped.
        old = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(minutes=60))
        conn.execute(
            "INSERT INTO evaluated_opportunities VALUES (?, ?)",
            ("KXBTC15M-26APR240000-00",
             old.isoformat(timespec="microseconds").replace("+00:00", "Z")))
        # Active bail flood — 50 no_orderbook rows in the last 5 min.
        now = datetime.datetime.now(datetime.timezone.utc)
        for i in range(50):
            ts = (now - datetime.timedelta(minutes=5, seconds=i * 5))
            conn.execute(
                "INSERT INTO rejected_opportunities VALUES (?, ?, ?)",
                (f"KXBTC15M-26APR240000-{i:02d}",
                 ts.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                 "no_orderbook"))
        conn.commit()
        s._state = MagicMock()
        s._state.conn = conn
        s._kalshi_feed = MagicMock()
        s._kalshi_feed.is_connected = True
        s._silence_alert_process_start_ts = _t.time() - (30 * 60)
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()
            # The BAIL FLOOD alert (not generic silence) should fire.
            call = mock_tele.send.call_args
            self.assertEqual(
                call.kwargs.get("dedup_key"),
                "silent_15m_bail_flood_alert",
                "stale eval + active bail flood should fire the "
                "BAIL FLOOD alert, not the generic silence alert "
                "(R5 coverage gap fix)")

    def test_bail_flood_failure_clears_cached_count(self):
        """R6 [A1/A4]: when the bail query fails, cached count is
        reset to 0 (not retained). Otherwise a stale 'flood' count
        from before the failure would keep firing the BAIL FLOOD
        alert despite the underlying query being broken. Operator
        sees the warning log; the alert state correctly stays clean.
        """
        s = _make_scanner_with_eval_age(age_minutes=30)
        # Pre-populate cached count to simulate a successful prior
        # query that observed a flood. Then fail the next query.
        s._silence_bail_query_last_count = 50
        s._silence_bail_query_last_ts = 0.0  # force re-query
        real_conn = s._state.conn
        wrapper = MagicMock()

        def selective_execute(sql, params=()):
            if "COUNT(*)" in sql:
                raise sqlite3.OperationalError("simulated failure")
            return real_conn.execute(sql, params)
        wrapper.execute.side_effect = selective_execute
        s._state.conn = wrapper
        try:
            count = s._query_recent_bail_count()
        finally:
            s._state.conn = real_conn
        self.assertEqual(
            count, 0,
            "failure must reset cached count to 0 to prevent "
            "spurious alerts from stale state")
        self.assertEqual(
            s._silence_bail_query_last_count, 0,
            "cached count attribute should also be reset")

    def test_bail_flood_failure_advances_throttle(self):
        """R6 [A4]: failure must advance the throttle clock so we
        don't query-storm a broken DB at every scan tick."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._silence_bail_query_last_ts = 0.0
        real_conn = s._state.conn
        wrapper = MagicMock()

        def selective_execute(sql, params=()):
            if "COUNT(*)" in sql:
                raise sqlite3.OperationalError("simulated failure")
            return real_conn.execute(sql, params)
        wrapper.execute.side_effect = selective_execute
        s._state.conn = wrapper
        try:
            before_ts = s._silence_bail_query_last_ts
            s._query_recent_bail_count()
            after_ts = s._silence_bail_query_last_ts
        finally:
            s._state.conn = real_conn
        self.assertGreater(
            after_ts, before_ts,
            "failure must advance _silence_bail_query_last_ts to "
            "throttle subsequent re-queries")

    def test_bail_flood_query_failure_rate_limited(self):
        """R4 [A4]: the bail-flood path has its own try/except with a
        warning log. If the bail-flood SQL throws on every tick (e.g.,
        a column the bail query depends on goes away), the warning
        must rate-limit to once per process lifetime — same contract
        as the primary query. Otherwise permanent failure spams the
        journal."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        # Empty eval table → primary query returns NULL → bail-flood
        # query path is taken.
        s._state.conn.execute("DELETE FROM evaluated_opportunities")
        s._state.conn.commit()
        # Wrap the connection so execute() throws ONLY on the
        # bail-flood query (matched by SQL containing 'COUNT(*)').
        # Primary query (UNION ALL with MAX) must still succeed.
        real_conn = s._state.conn
        wrapper = MagicMock()

        def selective_execute(sql, params=()):
            if "COUNT(*)" in sql:
                raise sqlite3.OperationalError(
                    "simulated bail-flood query failure")
            return real_conn.execute(sql, params)
        wrapper.execute.side_effect = selective_execute
        s._state.conn = wrapper
        try:
            with patch.object(bot.notifier, "_TELEGRAM"), \
                    patch.object(bot.scanner, "logging") as mock_log:
                s._check_15m_silence_alert(_ACTIVE_15M)
                s._check_15m_silence_alert(_ACTIVE_15M)
                s._check_15m_silence_alert(_ACTIVE_15M)
                bail_warnings = [
                    c for c in mock_log.warning.call_args_list
                    if "bail-flood" in c.args[0]
                ]
                self.assertEqual(
                    len(bail_warnings), 1,
                    "bail-flood query failure should log warning only "
                    "once per process lifetime")
        finally:
            s._state.conn = real_conn


class TestBailReasonsConstantNonEmpty(unittest.TestCase):
    """R3 [A3]: an empty `_BAIL_REJECTION_REASONS` produces SQL
    `NOT IN ()` syntax error → except → silent watchdog disable.
    The class-scope assert in bot/_impl.py must catch this at module
    import time. Verify the import doesn't break under normal
    conditions."""

    def test_constant_is_non_empty(self):
        self.assertTrue(
            len(bot.scanner.OpportunityScanner._BAIL_REJECTION_REASONS) > 0)


class TestSilent15MAlertHeartbeatGate(unittest.TestCase):
    """Apr 26 2026 incident #2 (kb/failures/scan-loop-stall-window-rotation-2026-04-26.md):
    after the rapid-resub loop fix shipped, the SILENT_15M alert
    STILL fired at 09:40 UTC despite scan being healthy. Root cause:
    once each (ticker, low_probability_15m) was written at 09:30:45,
    the `_eval_opp_seen` dedup at bot/_impl.py:10274 suppressed all
    subsequent writes. Scan body kept iterating new 0545 windows but
    produced ZERO DB rows for 9.5 min. The DB-only silence watchdog
    saw stale primary timestamps and fired SILENT — exactly the
    dedup-induced false-positive pattern that was fixed for the
    productive (2.5-min) watchdog via `_scan_15m_iter_heartbeat_ts`
    (commit c1c2096 per scan-tick-stall-cluster-2026-04-25.md).

    The silence (10-min) watchdog needs the same heartbeat-based
    aliveness signal. If `_scan_15m_iter_heartbeat_ts` is fresh
    within the staleness threshold, scan IS alive — silent-skip the
    SILENT alert regardless of DB row freshness. Bail-flood detection
    runs first and keeps firing on actual silent-bail recurrences
    (those bypass dedup at bail-rejection insert sites).
    """

    def test_dedup_quiet_market_does_not_fire_silent(self):
        """Primary 30-min stale (dedup blocked subsequent writes)
        BUT heartbeat fresh (scan iterating 15M windows) → silent.
        This is the exact 09:30:45→09:41:09 dedup-quiet window."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        # Heartbeat is fresh — scan body is iterating windows
        # every tick.
        s._scan_15m_iter_heartbeat_ts = time.time() - 1.0  # 1s ago
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_heartbeat_stale_AND_primary_stale_fires_silent(self):
        """Truly stuck scan: primary stale AND heartbeat stale →
        scan body has not iterated a 15M window in 10+ min →
        legitimate silence → fire SILENT."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        # Heartbeat also stale (>10 min).
        s._scan_15m_iter_heartbeat_ts = time.time() - 700.0
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()
            call = mock_tele.send.call_args
            self.assertEqual(
                call.kwargs.get("dedup_key"), "silent_15m_alert")

    def test_heartbeat_fresh_does_NOT_suppress_bail_flood(self):
        """Critical: heartbeat-based suppression must NOT mask a
        real bail flood. If 5 bail rows exist in last 10 min AND
        primary stale, BAIL FLOOD fires regardless of heartbeat
        freshness — the bail-rejection writes are not deduped at
        the same level as healthy rejections."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_orderbook",
            n_rejection_rows=5)
        s._scan_15m_iter_heartbeat_ts = time.time() - 1.0  # fresh
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()
            # Must be the BAIL FLOOD diagnostic, not silent.
            call = mock_tele.send.call_args
            self.assertEqual(
                call.kwargs.get("dedup_key"),
                "silent_15m_bail_flood_alert")

    def test_heartbeat_at_threshold_boundary(self):
        """Heartbeat exactly at threshold (10 min ago) — edge case.
        Code uses `<` so anything >= 600s is stale. Use 601.0 (1s
        past boundary) so the check is deterministic regardless of
        microsecond drift between `time.time()` calls."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._scan_15m_iter_heartbeat_ts = time.time() - 601.0
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_heartbeat_just_under_threshold_no_alert(self):
        """Heartbeat 599s ago (just under 600s threshold) → fresh →
        no alert despite primary 30 min stale."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._scan_15m_iter_heartbeat_ts = time.time() - 599.0
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_heartbeat_zero_treated_as_stale(self):
        """`_scan_15m_iter_heartbeat_ts = 0.0` is the init value
        (scan body never iterated). Must be treated as stale —
        otherwise a fresh-bot watchdog firing post-uptime-guard
        could be wrongly suppressed by an unset heartbeat."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._scan_15m_iter_heartbeat_ts = 0.0  # never set
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_bail_below_threshold_with_fresh_heartbeat_no_alert(self):
        """R2 [A5] edge case: bail_count = 2 (just below threshold of
        3) AND heartbeat fresh AND primary stale → fall through bail
        path → heartbeat suppresses SILENT → no alert. Single transient
        orderbook blip during a dedup-quiet market correctly stays
        silent."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_orderbook",
            n_rejection_rows=2)  # below threshold
        s._scan_15m_iter_heartbeat_ts = time.time() - 1.0
        with patch.object(bot.notifier, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()


class TestHeartbeatSetterContract(unittest.TestCase):
    """R2 [A4]: AST guard that `_scan_15m_iter_heartbeat_ts` is set
    from inside scan body. If a future refactor moves or removes the
    setter, the heartbeat gate becomes dead code (always sees init
    value 0.0 → SILENT fires every dedup-quiet window). Without this
    test, the regression is silent (no test fails until a real
    incident reproduces).
    """

    def test_heartbeat_setter_exists_in_scan_method(self):
        bot_path = os.path.join(
            os.path.dirname(__file__), "..", "bot/scanner/__init__.py")
        if os.path.exists(bot_path):
            with open(bot_path) as f:
                tree = ast.parse(f.read())
        # Qualify by parent class — `def scan` may exist in multiple
        # classes in the future; we want OpportunityScanner.scan
        # specifically.
        scan_func = None
        for cls in ast.walk(tree):
            if not (isinstance(cls, ast.ClassDef)
                    and cls.name == "OpportunityScanner"):
                continue
            for item in cls.body:
                if (isinstance(item, ast.FunctionDef)
                        and item.name == "scan"):
                    scan_func = item
                    break
            if scan_func:
                break
        self.assertIsNotNone(
            scan_func,
            "Could not locate OpportunityScanner.scan in bot/_impl.py")
        # Walk scan body for assignments to
        # _scan_15m_iter_heartbeat_ts.
        found = False
        for sub in ast.walk(scan_func):
            if not isinstance(sub, ast.Assign):
                continue
            for target in sub.targets:
                if (isinstance(target, ast.Attribute)
                        and target.attr == "_scan_15m_iter_heartbeat_ts"):
                    found = True
                    break
            if found:
                break
        self.assertTrue(
            found,
            "scan() must contain an assignment to "
            "self._scan_15m_iter_heartbeat_ts (the silence "
            "watchdog's heartbeat). If it doesn't, the watchdog's "
            "heartbeat gate is dead code and dedup-quiet markets "
            "will fire false-positive SILENT alerts.")


if __name__ == "__main__":
    unittest.main()
