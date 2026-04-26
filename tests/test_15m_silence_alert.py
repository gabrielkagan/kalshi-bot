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
from bot import OpportunityScanner

# Default active_windows for tests that exercise the 15M-present branch.
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
    # in the per-window loop body (~bot.py:9909). Initialized to 0.0
    # in `OpportunityScanner.__init__` (~bot.py:9087). Default 0.0
    # here matches "scan body has not iterated a 15M window since
    # process start." Tests that need a fresh heartbeat set it
    # explicitly.
    s._scan_15m_iter_heartbeat_ts = 0.0
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_both_eval_and_rejection_stale_alert_fires(self):
        """Eval 30 min stale AND rejection 30 min stale → genuinely
        silent → alert."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=30)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_recent_eval_stale_rejection_no_alert(self):
        """Eval 2 min ago, rejection 30 min ago → recent activity via
        evals → no alert (preserves pre-fix behavior for the
        eval-dominant case)."""
        s = _make_scanner_with_eval_age(
            age_minutes=2, rejection_age_minutes=30)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_rejection_at_threshold_boundary(self):
        """Eval 30 min stale, rejection 9 min ago (just under threshold)
        → no alert — rejection still counts as productive scan."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=9)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_rejection_just_past_threshold_alert_fires(self):
        """Eval 30 min stale, rejection 11 min stale → both past 10-min
        threshold → alert fires."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=11)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_no_best_ask_flood_still_alerts(self):
        """Same shape with the other silent-bail reason."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="no_best_ask",
            n_rejection_rows=3)
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_healthy_reason_suppresses_alert(self):
        """Stale eval + recent rejection with healthy reason
        (low_probability_15m) → quiet market, scan alive → no alert.
        This is the 2026-04-26 false-positive shape."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="low_probability_15m")
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_threshold_unparsable_flood_still_alerts(self):
        """`threshold_unparsable` is a third silent-bail reason
        (bot.py:9997). It indicates the scanner couldn't parse the
        strike from the ticker — bail-shaped, not healthy. If Kalshi
        renames the ticker format, all 4 active 15M tickers hit this
        before dedup caps each one (`_eval_opp_seen`); 3 rows is the
        smallest count that proves multi-ticker spread (per R7 [A2]
        threshold rationale)."""
        s = _make_scanner_with_eval_age(
            age_minutes=30, rejection_age_minutes=2,
            rejection_reason="threshold_unparsable",
            n_rejection_rows=4)  # all 4 15M tickers hit
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()


class TestBailReasonConstantContract(unittest.TestCase):
    """AST-walk audit of bot.py: enumerate every literal-string
    rejection_reason passed to `insert_rejection(...)`, classify each
    as bail or healthy, and require every literal to be classified.

    Catches future PRs that add an unclassified reason — the previous
    "constant equals hardcoded duplicate" test was theater (R3 [A2]).
    This walks the source.

    Maintenance: when adding a new literal rejection_reason in bot.py,
    add it to either KNOWN_BAIL_REASONS (and to
    `_BAIL_REJECTION_REASONS` in bot.py — same commit) or
    KNOWN_HEALTHY_REASONS in this test. Dynamic-variable reasons
    (e.g., `reason = prob_result.get("reason")`) are skipped because
    AST cannot resolve their string values; those go through the
    healthy filter by virtue of NOT being in the bail set, which is
    appropriate (probability-engine refusals are healthy).

    RENAME procedure (e.g., `no_orderbook` → `nbbo_unavailable`):
    update _BAIL_REJECTION_REASONS in bot.py, KNOWN_BAIL_REASONS in
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
        """`_BAIL_REJECTION_REASONS` in bot.py must equal this test's
        KNOWN_BAIL_REASONS. Update both in the same commit."""
        actual = set(bot.OpportunityScanner._BAIL_REJECTION_REASONS)
        self.assertEqual(
            actual, self.KNOWN_BAIL_REASONS,
            "_BAIL_REJECTION_REASONS drifted from KNOWN_BAIL_REASONS")

    def test_every_literal_reason_in_bot_is_classified(self):
        """Walk every `insert_rejection(...)` call site in bot.py,
        extract literal-string reason args (positional 4th or kwarg
        'reason'), and assert each is in KNOWN_BAIL_REASONS ∪
        KNOWN_HEALTHY_REASONS.

        SCOPE — bot.py only: the silence watchdog filters
        `WHERE ticker LIKE 'KX%15M%'`, and 15M-tickered rejection
        writes live exclusively in `bot.py`'s `OpportunityScanner.scan()`.
        Other engines (weather_engine.py, sports_engine.py, etc.)
        write their own product_type rows under different ticker
        namespaces (KXBTCD-…, KXHIGHNY-…), which the watchdog
        ignores. If a future engine ever writes 'KX*15M%' rejections
        from outside bot.py, extend this audit to walk those files
        as well. (R8 audit-scope clarification.)
        """
        bot_path = os.path.join(
            os.path.dirname(__file__), "..", "bot.py")
        with open(bot_path) as f:
            tree = ast.parse(f.read())
        literals = set()
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
            f"Unclassified literal rejection_reason values in bot.py: "
            f"{sorted(unclassified)}. Add each to either "
            f"KNOWN_BAIL_REASONS (and to _BAIL_REJECTION_REASONS in "
            f"bot.py — same commit) or KNOWN_HEALTHY_REASONS in this "
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
            f"_BAIL_REJECTION_REASONS in bot.py if bail). Stale "
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
        with patch.object(bot, "_TELEGRAM") as mock_tele, \
                patch.object(bot, "logging") as mock_log:
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
        with patch.object(bot, "_TELEGRAM"), \
                patch.object(bot, "logging") as mock_log:
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
        with patch.object(bot, "_TELEGRAM"), \
                patch.object(bot, "logging"):
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
            with patch.object(bot, "_TELEGRAM"), \
                    patch.object(bot, "logging"):
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert([])  # zero windows
            mock_tele.send.assert_not_called()

    def test_no_data_at_all_no_alert(self):
        """Both tables fully empty → fresh bot pre-data → silent.
        (Uptime guard already passed but we still don't know if
        there's a bug or just a quiet startup.)"""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._state.conn.execute("DELETE FROM evaluated_opportunities")
        s._state.conn.commit()
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
            with patch.object(bot, "_TELEGRAM"), \
                    patch.object(bot, "logging") as mock_log:
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
    The class-scope assert in bot.py must catch this at module
    import time. Verify the import doesn't break under normal
    conditions."""

    def test_constant_is_non_empty(self):
        self.assertTrue(
            len(bot.OpportunityScanner._BAIL_REJECTION_REASONS) > 0)


class TestSilent15MAlertHeartbeatGate(unittest.TestCase):
    """Apr 26 2026 incident #2 (kb/failures/scan-loop-stall-window-rotation-2026-04-26.md):
    after the rapid-resub loop fix shipped, the SILENT_15M alert
    STILL fired at 09:40 UTC despite scan being healthy. Root cause:
    once each (ticker, low_probability_15m) was written at 09:30:45,
    the `_eval_opp_seen` dedup at bot.py:10274 suppressed all
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_heartbeat_stale_AND_primary_stale_fires_silent(self):
        """Truly stuck scan: primary stale AND heartbeat stale →
        scan body has not iterated a 15M window in 10+ min →
        legitimate silence → fire SILENT."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        # Heartbeat also stale (>10 min).
        s._scan_15m_iter_heartbeat_ts = time.time() - 700.0
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_called_once()

    def test_heartbeat_just_under_threshold_no_alert(self):
        """Heartbeat 599s ago (just under 600s threshold) → fresh →
        no alert despite primary 30 min stale."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._scan_15m_iter_heartbeat_ts = time.time() - 599.0
        with patch.object(bot, "_TELEGRAM") as mock_tele:
            s._check_15m_silence_alert(_ACTIVE_15M)
            mock_tele.send.assert_not_called()

    def test_heartbeat_zero_treated_as_stale(self):
        """`_scan_15m_iter_heartbeat_ts = 0.0` is the init value
        (scan body never iterated). Must be treated as stale —
        otherwise a fresh-bot watchdog firing post-uptime-guard
        could be wrongly suppressed by an unset heartbeat."""
        s = _make_scanner_with_eval_age(age_minutes=30)
        s._scan_15m_iter_heartbeat_ts = 0.0  # never set
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
        with patch.object(bot, "_TELEGRAM") as mock_tele:
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
            os.path.dirname(__file__), "..", "bot.py")
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
            "Could not locate OpportunityScanner.scan in bot.py")
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
