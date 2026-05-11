"""Phase 2.9 — raw WS frame logging for post-reconnect diagnosis.

Phase 2.8 deploy showed WS_FORCE_RECONNECT firing every ~3 minutes
(7 in 19 min). Pattern: live 15M tickers issue subscribe →
type=subscribed never arrives → 180s watchdog fires → force-
reconnect → next reconnect's subscribes ALSO don't get acked → loop.

We can't diagnose this from current logs alone. Need raw-frame
visibility to see:
  - What did we actually send (full payload)
  - What did Kalshi actually respond with (including non-data
    types like ok/subscribed/error)
  - Are responses matched to the right cmd_ids?

Design:
  1. Always log outgoing frames at INFO with truncation cap (low
     volume — ~100/session, mostly subscribes during startup/reconnect)
  2. Log all incoming frames for first WS_RAW_LOG_DURATION_S after
     connect (captures the initial subscribe burst + acks)
  3. Always log incoming frames with non-data types (subscribed,
     unsubscribed, ok, error) regardless of duration — these are
     responses to our commands, low volume
  4. Truncate payloads to WS_RAW_LOG_TRUNCATE chars to prevent
     log blowup on huge messages
"""

import ast
import asyncio
import json
import logging
import os
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock
import bot.constants  # noqa: F401
import bot.feeds  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bot/feeds/kalshi.py")  # Bit 4.5b: KalshiFeed moved here from bot/_impl.py


def _make_feed():
    import bot
    import bot.feeds  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.feeds.X access)
    f = bot.feeds.KalshiFeed.__new__(bot.feeds.KalshiFeed)
    f._pending_subscribes = []
    f._pending_unsubscribes = []
    f._pending_snapshot_requests = []
    f._subscribed_tickers = set()
    f._orderbooks = {}
    f._snapshot_request_pending = {}
    f._force_resub_cooldown = {}
    f._unsubscribe_blacklist = {}
    f._get_snapshot_consecutive_failed_sweeps = 0
    f._get_snapshot_disabled = False
    f._get_snapshot_disabled_logged = False
    f._force_resub_recovery_deadline = {}
    f._force_resub_recovery_warned = {}
    f._snapshot_schema_probed = True
    f._delta_schema_probed = True
    f._delta_probe_count = 0
    f._delta_probe_max = 0
    f._ws_last_seq = {}
    f._ws_seq_gap_logs = 0
    f._ws_seq_gap_max_logs = 0
    f._ws_last_msg_ts = 0.0
    f._ws_connect_ts = time.time()  # fresh connect — within raw-log window
    f._ticker_to_sid = {}
    f._ws_error_frame_seen = set()
    f._next_msg_id = 100
    f._outstanding_subscribes = {}
    f._outstanding_subscribe_ts = {}
    f._ws_orphan_sid_seen = set()
    f._force_reconnect_requested = False
    f._pending_late_unsubscribes = set()
    # Phase 2.9 R-review A3: per-session raw log cap.
    f._raw_log_count = 0
    f._raw_log_capped_logged = False
    f._lock = threading.Lock()
    return f


# ─────────────────────────────────────────────────────────────────────────────
# 1. Outgoing frames always logged (low volume — diagnostic gold)
# ─────────────────────────────────────────────────────────────────────────────

class TestOutgoingSubscribeLogged(unittest.IsolatedAsyncioTestCase):
    async def test_outgoing_subscribe_logged_at_info(self):
        f = _make_feed()
        ws = AsyncMock()
        with self.assertLogs("root", level="INFO") as cm:
            await f._send_ob_subscribe(ws, "BTC1")
        joined = "\n".join(cm.output)
        self.assertIn(
            "WS_RAW_OUT", joined,
            "Outgoing subscribe MUST log a WS_RAW_OUT line so we "
            "can see what was actually sent during diagnostic "
            "investigations.")
        # The payload should be visible (truncated) in the log.
        self.assertIn(
            "subscribe", joined,
            "Log line should include the cmd ('subscribe') so it's "
            "filterable.")
        self.assertIn(
            "BTC1", joined,
            "Log line should include the ticker for correlation.")


class TestOutgoingUnsubscribeLogged(unittest.IsolatedAsyncioTestCase):
    async def test_outgoing_unsubscribe_logged(self):
        """Phase 2.10: unsubscribe is now sent as
        update_subscription/delete_markets, not cmd:unsubscribe."""
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._ticker_to_sid["BTC1"] = 7
        ws = AsyncMock()
        with self.assertLogs("root", level="INFO") as cm:
            await f._send_ob_unsubscribe(ws, "BTC1")
        joined = "\n".join(cm.output)
        self.assertIn("WS_RAW_OUT", joined)
        self.assertIn("delete_markets", joined)


class TestOutgoingGetSnapshotLogged(unittest.IsolatedAsyncioTestCase):
    async def test_outgoing_get_snapshot_logged(self):
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._ticker_to_sid["BTC1"] = 7
        ws = AsyncMock()
        with self.assertLogs("root", level="INFO") as cm:
            await f._send_ob_get_snapshot(ws, "BTC1")
        joined = "\n".join(cm.output)
        self.assertIn("WS_RAW_OUT", joined)
        self.assertIn("update_subscription", joined)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Incoming frames logged within window OR for non-data types
# ─────────────────────────────────────────────────────────────────────────────

class TestIncomingWithinWindowLogged(unittest.TestCase):
    def test_incoming_orderbook_delta_logged_within_window(self):
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._orderbooks["BTC1"] = {"yes": [], "no": [], "ts": time.time()}
        # _ws_connect_ts is fresh — within window
        with self.assertLogs("root", level="INFO") as cm:
            f._handle_message(json.dumps({
                "type": "orderbook_delta",
                "sid": 7,
                "msg": {
                    "market_ticker": "BTC1",
                    "side": "yes",
                    "price_dollars": "0.95",
                    "delta_fp": "10",
                },
            }))
        joined = "\n".join(cm.output)
        self.assertIn(
            "WS_RAW_IN", joined,
            "Incoming frame within WS_RAW_LOG_DURATION_S MUST log "
            "as WS_RAW_IN for raw-trace diagnostic.")


class TestIncomingOutsideWindowDeltaNotLogged(unittest.TestCase):
    def test_orderbook_delta_outside_window_not_logged(self):
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._orderbooks["BTC1"] = {"yes": [], "no": [], "ts": time.time()}
        # Backdate connect ts to past the raw-log window.
        f._ws_connect_ts = (
            time.time() - bot.constants.WS_RAW_LOG_DURATION_S - 5.0)
        # Use a logger context that captures only WS_RAW_IN; if no
        # WS_RAW_IN log fires, we'll need to verify via missing.
        with self.assertLogs("root", level="DEBUG") as cm:
            # Need at least one log inside the with block.
            logging.info("sentinel")
            f._handle_message(json.dumps({
                "type": "orderbook_delta",
                "sid": 7,
                "msg": {
                    "market_ticker": "BTC1",
                    "side": "yes",
                    "price_dollars": "0.95",
                    "delta_fp": "10",
                },
            }))
        joined = "\n".join(cm.output)
        # WS_RAW_IN should NOT appear for orderbook_delta past
        # the window — too noisy.
        self.assertNotIn(
            "WS_RAW_IN", joined,
            "orderbook_delta past the raw-log window MUST NOT be "
            "logged — would flood logs at thousands per minute.")


class TestNonDataTypesAlwaysLogged(unittest.TestCase):
    """Non-data types (subscribed, unsubscribed, ok, error) are
    responses to our commands — low volume, always log even past
    the time window for correlation diagnostics."""

    def test_subscribed_logged_outside_window(self):
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        f._ws_connect_ts = (
            time.time() - bot.constants.WS_RAW_LOG_DURATION_S - 5.0)
        with self.assertLogs("root", level="INFO") as cm:
            f._handle_message(json.dumps({
                "id": 100,
                "type": "subscribed",
                "msg": {"channel": "orderbook_delta", "sid": 7},
            }))
        joined = "\n".join(cm.output)
        self.assertIn(
            "WS_RAW_IN", joined,
            "type=subscribed MUST always log raw — it's a low-"
            "volume command response and the ack-reliability "
            "diagnostic depends on seeing every one.")

    def test_unsubscribed_logged_outside_window(self):
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        f._ws_connect_ts = (
            time.time() - bot.constants.WS_RAW_LOG_DURATION_S - 5.0)
        with self.assertLogs("root", level="INFO") as cm:
            f._handle_message(json.dumps({
                "id": 200, "type": "unsubscribed",
                "sid": 7, "seq": 1,
            }))
        joined = "\n".join(cm.output)
        self.assertIn("WS_RAW_IN", joined)

    def test_ok_logged_outside_window(self):
        """type=ok is the generic command-success response."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        f._ws_connect_ts = (
            time.time() - bot.constants.WS_RAW_LOG_DURATION_S - 5.0)
        with self.assertLogs("root", level="INFO") as cm:
            f._handle_message(json.dumps({
                "id": 3, "type": "ok",
                "msg": [{"channel": "orderbook_delta", "sid": 1}],
            }))
        joined = "\n".join(cm.output)
        self.assertIn("WS_RAW_IN", joined)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Payload truncation (avoid log blowup)
# ─────────────────────────────────────────────────────────────────────────────

class TestPayloadTruncation(unittest.TestCase):
    def test_huge_incoming_payload_truncated(self):
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        # Build a giant payload way over the truncate cap.
        huge = "x" * (bot.constants.WS_RAW_LOG_TRUNCATE * 5)
        with self.assertLogs("root", level="INFO") as cm:
            f._handle_message(json.dumps({
                "id": 1, "type": "ok",
                "msg": {"junk": huge},
            }))
        joined = "\n".join(cm.output)
        # The full huge string must NOT appear in the log.
        self.assertNotIn(
            huge, joined,
            "Huge payload MUST be truncated to prevent log blowup.")
        # Truncation indicator should be present.
        self.assertIn(
            "truncated", joined.lower(),
            "Truncated payload should be marked so reader knows "
            "the log is partial.")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Module constants
# ─────────────────────────────────────────────────────────────────────────────

class TestConstantsDefined(unittest.TestCase):
    def test_ws_raw_log_duration_constant_defined(self):
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        self.assertTrue(
            hasattr(bot.constants, "WS_RAW_LOG_DURATION_S"),
            "Must define WS_RAW_LOG_DURATION_S module-level "
            "constant.")
        val = bot.constants.WS_RAW_LOG_DURATION_S
        # Sane range: at least 30s (capture initial burst), no
        # more than 5min (avoid sustained log spam).
        self.assertGreaterEqual(val, 30.0)
        self.assertLessEqual(val, 300.0)

    def test_ws_raw_log_truncate_constant_defined(self):
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        self.assertTrue(hasattr(bot.constants, "WS_RAW_LOG_TRUNCATE"))
        val = bot.constants.WS_RAW_LOG_TRUNCATE
        self.assertGreaterEqual(val, 100)
        self.assertLessEqual(val, 5000)


class TestRawLogCap(unittest.TestCase):
    """R-review A3: hard per-session cap on raw log lines so
    reconnect-every-3min storms don't blow out journalctl. Cap
    is enforced; one-shot WS_RAW_CAPPED notice; resets on
    reconnect."""

    def test_cap_enforced_for_data_frames(self):
        """R2 / A1: cap applies ONLY to bulk data frames; outgoing
        and command-response types are exempt. This test exercises
        the bulk-data-frame path via _log_raw_in with a data
        msg_type."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        cap = bot.constants.WS_RAW_LOG_MAX_PER_SESSION
        f._raw_log_count = cap - 1
        # First log fires (consumes last budget slot).
        f._log_raw_in(
            '{"type":"orderbook_delta"}',
            msg_type="orderbook_delta",
        )
        self.assertEqual(f._raw_log_count, cap)
        # Next data-frame log should be suppressed.
        with self.assertLogs("root", level="INFO") as cm:
            f._log_raw_in(
                '{"type":"orderbook_delta","note":"should-be-capped"}',
                msg_type="orderbook_delta",
            )
        joined = "\n".join(cm.output)
        self.assertIn(
            "WS_RAW_CAPPED", joined,
            "Hitting cap MUST emit a one-shot WS_RAW_CAPPED log.")
        self.assertNotIn(
            "should-be-capped", joined,
            "Suppressed payload must NOT appear in logs.")

    def test_cap_one_shot(self):
        """Repeat hits past the cap don't produce more
        WS_RAW_CAPPED lines."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        f._raw_log_count = bot.constants.WS_RAW_LOG_MAX_PER_SESSION
        f._raw_log_capped_logged = True  # already noticed
        with self.assertLogs("root", level="DEBUG") as cm:
            logging.info("sentinel")
            for _ in range(5):
                f._log_raw_in(
                    '{"type":"orderbook_delta"}',
                    msg_type="orderbook_delta",
                )
        joined = "\n".join(cm.output)
        self.assertEqual(
            joined.count("WS_RAW_CAPPED"), 0,
            "Repeat over-cap calls must NOT log WS_RAW_CAPPED again.")

    def test_session_cleanup_resets_cap(self):
        """_cleanup_session_state resets the counter so the next
        session gets a fresh diagnostic budget."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        f._connected = True
        f._raw_log_count = bot.constants.WS_RAW_LOG_MAX_PER_SESSION + 100
        f._raw_log_capped_logged = True
        f._cleanup_session_state()
        self.assertEqual(f._raw_log_count, 0)
        self.assertFalse(f._raw_log_capped_logged)


class TestR2A1NonDataExemptFromCap(unittest.TestCase):
    """R2 / A1: command-response types (subscribed/unsubscribed/
    ok/error) MUST NOT be cap-suppressed. Pre-fix the shared cap
    meant chatty deltas could exhaust the budget in ~6s, silencing
    the very `type=subscribed` ack the diagnostic depends on."""

    def test_subscribed_logged_even_after_cap(self):
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        # Pretend the data-frame cap is fully exhausted.
        f._raw_log_count = bot.constants.WS_RAW_LOG_MAX_PER_SESSION
        with self.assertLogs("root", level="INFO") as cm:
            f._handle_message(json.dumps({
                "id": 100,
                "type": "subscribed",
                "msg": {"channel": "orderbook_delta", "sid": 7},
            }))
        joined = "\n".join(cm.output)
        self.assertIn(
            "WS_RAW_IN", joined,
            "type=subscribed MUST log even with the data-frame "
            "cap exhausted — it's the actual diagnostic signal.")

    def test_error_logged_even_after_cap(self):
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        f._raw_log_count = bot.constants.WS_RAW_LOG_MAX_PER_SESSION
        with self.assertLogs("root", level="INFO") as cm:
            f._handle_message(json.dumps({
                "id": 4, "type": "error",
                "msg": {"code": 7, "msg": "Unknown subscription ID"},
            }))
        joined = "\n".join(cm.output)
        self.assertIn(
            "WS_RAW_IN", joined,
            "type=error MUST log even at cap.")

    def test_outgoing_logged_even_after_cap(self):
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        f._raw_log_count = bot.constants.WS_RAW_LOG_MAX_PER_SESSION
        with self.assertLogs("root", level="INFO") as cm:
            f._log_raw_out({"id": 200, "cmd": "subscribe"})
        joined = "\n".join(cm.output)
        self.assertIn(
            "WS_RAW_OUT", joined,
            "Outgoing frames MUST log even at cap — bounded volume "
            "and constitute the OUT side of the diagnostic.")

    def test_data_frame_still_capped(self):
        """Sanity: bulk types still respect the cap."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        f = _make_feed()
        f._raw_log_count = bot.constants.WS_RAW_LOG_MAX_PER_SESSION
        # Mark the capped-notice as already emitted to prevent it
        # from firing in this test (we only want to test suppression).
        f._raw_log_capped_logged = True
        with self.assertLogs("root", level="DEBUG") as cm:
            logging.info("sentinel")
            f._log_raw_in(
                '{"type": "orderbook_delta", "sid": 7}',
                msg_type="orderbook_delta",
            )
        joined = "\n".join(cm.output)
        self.assertNotIn(
            "WS_RAW_IN", joined,
            "Bulk orderbook_delta MUST be cap-suppressed.")


class TestAstReconnectResetsConnectTs(unittest.TestCase):
    """R-review A2: production reconnect path MUST set
    self._ws_connect_ts = ... so the post-connect raw-log window
    actually reopens on each reconnect (not just at process start)."""

    def test_ws_loop_sets_connect_ts(self):
        src = ""
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as fh:
                src = fh.read()
        tree = ast.parse(src)
        target = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "KalshiFeed"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.AsyncFunctionDef)
                        and fn.name == "_ws_loop"):
                    target = fn
                    break
        self.assertIsNotNone(target, "_ws_loop not found")
        body_src = ast.unparse(target)
        self.assertIn(
            "self._ws_connect_ts =", body_src,
            "Reconnect path must reset _ws_connect_ts so the "
            "post-connect raw-log window reopens for each new "
            "session — otherwise we only get raw frames once "
            "per process start.")


if __name__ == "__main__":
    unittest.main()
