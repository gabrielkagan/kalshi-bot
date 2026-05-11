"""Phase 2.5 — sid-based update_subscription/get_snapshot.

R5 verification on VPS proved Kalshi's WS does NOT honor our prior
get_snapshot send (auto-disabled after 3 timeouts). Root cause: we
were sending the wrong params schema. Per Kalshi docs:

    {"cmd": "update_subscription",
     "params": {"sid": <int>, "action": "get_snapshot"}}

We were sending `params.market_tickers` which is invalid for
`update_subscription` (only valid for `subscribe`). Kalshi was
returning an error frame which `_handle_message` silently dropped
("# Ignore subscription confirmations, errors, etc.").

This module pins:

  1. KalshiFeed maintains `_ticker_to_sid` mapping populated from
     orderbook_snapshot / orderbook_delta envelopes (the top-level
     `sid` field is the subscription ID Kalshi assigns).

  2. `_send_ob_get_snapshot(ws, ticker)` sends the correct schema
     using `params.sid`, NOT `params.market_tickers`.

  3. `force_resubscribe` checks if a sid is known. If not (subscribe
     in flight, or unsubscribed since), it skips the primary path
     and queues unsub+resub directly — same as `_get_snapshot_disabled`.

  4. `_ticker_to_sid` is cleared on `unsubscribe_ticker` and on
     WS reconnect (sids are session-scoped).

  5. `_handle_message` LOGS error frames (type="error") at WARNING
     level so the next time Kalshi rejects a command we'll see it
     instead of silently dropping it.
"""

import ast
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
    """Same fixture as test_ws_force_resubscribe.py — bypass __init__."""
    import bot
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
    # Phase 2.5: ticker -> sid mapping (learned from envelope).
    f._ticker_to_sid = {}
    f._ws_error_frame_seen = set()
    # Phase 2.6: authoritative sid tracking via type=subscribed.
    f._next_msg_id = 100
    f._outstanding_subscribes = {}
    f._outstanding_subscribe_ts = {}
    f._ws_orphan_sid_seen = set()
    f._force_reconnect_requested = False
    f._pending_late_unsubscribes = set()
    f._raw_log_count = 0
    f._raw_log_capped_logged = False
    f._ws_connect_ts = 0.0
    f._lock = threading.Lock()
    return f


# ─────────────────────────────────────────────────────────────────────────────
# 1. _ticker_to_sid mapping populated from incoming envelopes
# ─────────────────────────────────────────────────────────────────────────────

class TestEnvelopeSidNoLongerLearned(unittest.TestCase):
    """Phase 2.6: envelope-sid learning was REMOVED. The envelope
    sid in orderbook_snapshot/orderbook_delta is NOT the sid Kalshi
    expects in commands (R5 finding: code=7 'Unknown subscription
    ID' when we used envelope sid). Sids are now learned from
    `type=subscribed` responses instead (test below)."""

    def test_snapshot_envelope_sid_does_not_populate_map(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f._handle_ob_snapshot({
            "sid": 456,  # envelope sid — IGNORED in 2.6
            "type": "orderbook_snapshot",
            "msg": {
                "market_ticker": "KXBTC15M-FOO",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            },
        })
        self.assertNotIn(
            "KXBTC15M-FOO", f._ticker_to_sid,
            "Phase 2.6: envelope sid must NOT populate the map. "
            "The authoritative sid comes from the type=subscribed "
            "response, captured via _handle_message.")

    def test_delta_envelope_sid_does_not_populate_map(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f._orderbooks["KXBTC15M-FOO"] = {
            "yes": [], "no": [], "ts": time.time()}
        f._handle_ob_delta({
            "sid": 789,
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": "KXBTC15M-FOO",
                "side": "yes",
                "price_dollars": "0.95",
                "delta_fp": "10",
            },
        })
        self.assertNotIn(
            "KXBTC15M-FOO", f._ticker_to_sid,
            "Phase 2.6: envelope sid in deltas must NOT populate "
            "the map either.")


# ─────────────────────────────────────────────────────────────────────────────
# 2. _send_ob_get_snapshot uses sid, not market_tickers
# ─────────────────────────────────────────────────────────────────────────────

class TestSendUsesSidAndMarketTickers(unittest.IsolatedAsyncioTestCase):
    """Phase 2.7: Kalshi's update_subscription requires BOTH:
      - sid (or sids)
      - At least one market identifier (market_tickers / market_ticker
        / market_id / market_ids)

    Phase 2.5 sent only market_tickers → code=7 'Unknown subscription
    ID'. Phase 2.6 sent only sid → code=14 'Market Ticker required'.
    Phase 2.7 sends BOTH."""

    async def test_send_ob_get_snapshot_includes_sid_and_market_tickers(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f._ticker_to_sid["KXBTC15M-FOO"] = 456
        ws = AsyncMock()
        await f._send_ob_get_snapshot(ws, "KXBTC15M-FOO")
        ws.send.assert_awaited_once()
        sent = json.loads(ws.send.await_args.args[0])
        self.assertEqual(sent["cmd"], "update_subscription")
        self.assertEqual(sent["params"]["action"], "get_snapshot")
        self.assertEqual(
            sent["params"]["sid"], 456,
            "Phase 2.7: sid required (else code=7).")
        self.assertEqual(
            sent["params"]["market_tickers"], ["KXBTC15M-FOO"],
            "Phase 2.7: market_tickers ALSO required (else code=14 "
            "'Market Ticker required'). Phase 2.6 sent only sid; "
            "Kalshi rejected on every restart.")


# ─────────────────────────────────────────────────────────────────────────────
# 3. force_resubscribe falls back when sid unknown
# ─────────────────────────────────────────────────────────────────────────────

class TestForceResubFallbackWhenSidUnknown(unittest.TestCase):
    def setUp(self):
        self.f = _make_feed()
        self.f._subscribed_tickers.add("KXBTC15M-NEW")
        # No entry in _ticker_to_sid yet — subscribe is in flight.

    def test_no_sid_is_true_no_op(self):
        """Phase 2.6 R-review A1: with no known sid we cannot
        unsubscribe (no sid to send) and cannot resub-without-unsub
        (creates duplicate Kalshi subscription). True no-op until
        sid lands via type=subscribed response."""
        self.f.force_resubscribe("KXBTC15M-NEW")
        self.assertNotIn(
            "KXBTC15M-NEW", self.f._pending_snapshot_requests)
        self.assertNotIn(
            "KXBTC15M-NEW", self.f._pending_unsubscribes,
            "No-sid case must NOT queue unsub (would skip → "
            "leaving sub to create duplicate subscription).")
        self.assertNotIn(
            "KXBTC15M-NEW", self.f._pending_subscribes,
            "No-sid case must NOT queue resub (would create a "
            "duplicate subscription on Kalshi side, leaking the "
            "in-flight one forever).")

    def test_with_sid_uses_primary_path(self):
        self.f._ticker_to_sid["KXBTC15M-NEW"] = 456
        self.f.force_resubscribe("KXBTC15M-NEW")
        self.assertIn(
            "KXBTC15M-NEW", self.f._pending_snapshot_requests,
            "With a known sid, the primary path is queued.")


# ─────────────────────────────────────────────────────────────────────────────
# 4. Cleanup of _ticker_to_sid
# ─────────────────────────────────────────────────────────────────────────────

class TestSidMapCleanup(unittest.TestCase):
    def test_unsubscribe_keeps_sid_until_drain_sends(self):
        """Phase 2.6 R5 / P1: unsubscribe_ticker does NOT pop sid.
        The drain's `_send_ob_unsubscribe` needs the sid to actually
        send the unsubscribe to Kalshi; it pops on successful send.
        Pre-fix, popping in unsubscribe_ticker would silently drop
        the sid → drain SKIPs → Kalshi-side subscription leaks."""
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f._ticker_to_sid["KXBTC15M-FOO"] = 456
        f.unsubscribe_ticker("KXBTC15M-FOO")
        self.assertEqual(
            f._ticker_to_sid.get("KXBTC15M-FOO"), 456,
            "unsubscribe_ticker MUST keep sid in map so drain can "
            "send the unsubscribe with it. _send_ob_unsubscribe "
            "pops on successful send.")
        self.assertIn(
            "KXBTC15M-FOO", f._pending_unsubscribes,
            "Sanity: unsubscribe IS queued for drain.")

    def test_session_cleanup_clears_sid_map(self):
        """sids are session-scoped — Kalshi assigns new ones on
        reconnect. _cleanup_session_state must clear the map."""
        f = _make_feed()
        f._connected = True
        f._ticker_to_sid["A"] = 1
        f._ticker_to_sid["B"] = 2
        f._cleanup_session_state()
        self.assertEqual(
            f._ticker_to_sid, {},
            "_cleanup_session_state must clear _ticker_to_sid "
            "(sids don't survive across WS reconnects).")


# ─────────────────────────────────────────────────────────────────────────────
# 5. _handle_message logs error frames
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorFrameLogging(unittest.TestCase):
    def test_error_frame_emits_warning_log(self):
        f = _make_feed()
        with self.assertLogs("root", level="WARNING") as cm:
            f._handle_message(json.dumps({
                "type": "error",
                "id": 4,
                "msg": {"code": "invalid_params",
                        "details": "malformed update_subscription"},
            }))
        joined = "\n".join(cm.output)
        self.assertIn("WS_ERROR_FRAME", joined,
                      "Pre-fix: error frames were silently dropped, "
                      "hiding R5's discovery that update_subscription/"
                      "get_snapshot was rejected. Now they MUST log at "
                      "WARNING with a parseable prefix.")
        self.assertIn("invalid_params", joined,
                      "Logged error must include the Kalshi error "
                      "code/details so we can debug.")


class TestAstMainHandlerLogsErrors(unittest.TestCase):
    """AST sanity: _handle_message must reference 'error' as a type
    branch and call logging.warning with WS_ERROR_FRAME."""

    def test_handle_message_branches_on_error_type(self):
        with open(BOT_PY) as fh:
            src = fh.read()
        tree = ast.parse(src)
        target = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "KalshiFeed"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_handle_message"):
                    target = fn
                    break
        self.assertIsNotNone(target, "_handle_message not found")
        body_src = ast.unparse(target)
        # ast.unparse uses single quotes for string literals.
        self.assertIn(
            "'error'", body_src,
            "_handle_message must explicitly branch on type=='error' "
            "to log error frames (closes the silent-drop bug).")
        self.assertIn(
            "WS_ERROR_FRAME", body_src,
            "_handle_message must use WS_ERROR_FRAME as the log "
            "prefix so it's greppable.")


# Phase 2.6: envelope-sid monotonic guard and malformed-delta sid
# protection were both REMOVED — sids are now learned exclusively
# from `type=subscribed` responses, where these concerns don't
# apply (each response has explicit sid + matched ticker via
# command id, no race or malformed-stream concerns).


class TestTimeoutFallbackKeepsSidForDrain(unittest.TestCase):
    """Phase 2.6 R6: timeout fallback MUST KEEP _ticker_to_sid so
    the drain's _send_ob_unsubscribe can use it. Pre-fix it
    pre-popped sid (defensive against monotonic-guard collisions
    that don't apply in 2.6) → drain SKIPs → Kalshi-side
    subscription leaks. Symmetric to the unsubscribe_ticker R5 fix."""

    def test_timeout_fallback_keeps_sid_for_drain(self):
        import bot
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-LOOP")
        f._ticker_to_sid["KXBTC15M-LOOP"] = 500
        # Backdate pending request past timeout.
        f._snapshot_request_pending["KXBTC15M-LOOP"] = (
            time.monotonic() - bot.constants.WS_SNAPSHOT_REQUEST_TIMEOUT_S - 1.0)
        f._check_snapshot_timeouts()
        self.assertEqual(
            f._ticker_to_sid.get("KXBTC15M-LOOP"), 500,
            "Phase 2.6 R6: sid MUST stay in map. Drain's "
            "_send_ob_unsubscribe pops on successful send; "
            "pre-popping here = silent leak.")
        # Fallback unsub+resub IS queued.
        self.assertIn("KXBTC15M-LOOP", f._pending_unsubscribes)
        self.assertIn("KXBTC15M-LOOP", f._pending_subscribes)


class TestErrorFrameDedup(unittest.TestCase):
    """Adversarial-review P1: a broken Kalshi contract would cause
    error frames every time we send the failing command. Without
    dedup, that's 4 tickers × periodic-sweeps × forever WARNING-
    level spam — same noise pattern that hid R5. Dedup by (id,code)."""

    def test_repeat_error_frame_logged_once(self):
        f = _make_feed()
        with self.assertLogs("root", level="WARNING") as cm1:
            f._handle_message(json.dumps({
                "type": "error",
                "id": 4,
                "msg": {"code": "invalid_params", "msg": "bad"},
            }))
        first = "\n".join(cm1.output)
        self.assertIn("WS_ERROR_FRAME", first)

        # Repeat the SAME (id, code) — should not add a new WARNING.
        # Use assertNoLogs equivalent: capture and check no WARNING+.
        # Python 3.9 doesn't have assertNoLogs; use logger filter.
        before_count = first.count("WS_ERROR_FRAME")
        try:
            with self.assertLogs("root", level="WARNING") as cm2:
                f._handle_message(json.dumps({
                    "type": "error",
                    "id": 4,
                    "msg": {"code": "invalid_params", "msg": "bad"},
                }))
        except AssertionError:
            # No WARNING-level logs emitted — that's the desired
            # outcome (dedup works).
            return
        # If we get here, a WARNING was emitted on the repeat.
        repeat = "\n".join(cm2.output)
        self.assertNotIn(
            "WS_ERROR_FRAME id=4 code=invalid_params msg=bad",
            repeat,
            "Repeat WS_ERROR_FRAME with same (id,code) must be "
            "suppressed to avoid log spam from broken contracts.")

    def test_different_error_codes_log_separately(self):
        f = _make_feed()
        with self.assertLogs("root", level="WARNING") as cm:
            f._handle_message(json.dumps({
                "type": "error", "id": 4,
                "msg": {"code": "invalid_params", "msg": "a"},
            }))
            f._handle_message(json.dumps({
                "type": "error", "id": 4,
                "msg": {"code": "subscription_not_found", "msg": "b"},
            }))
        joined = "\n".join(cm.output)
        # Both codes should produce a WARNING line.
        self.assertEqual(
            joined.count("WS_ERROR_FRAME id=4 code=invalid_params"), 1,
            "First error code logs once.")
        self.assertEqual(
            joined.count(
                "WS_ERROR_FRAME id=4 code=subscription_not_found"),
            1,
            "Different error code logs separately (not deduped).")

    def test_session_cleanup_clears_error_dedup(self):
        f = _make_feed()
        f._connected = True
        f._ws_error_frame_seen.add((4, "invalid_params"))
        f._cleanup_session_state()
        self.assertEqual(
            f._ws_error_frame_seen, set(),
            "Session cleanup must clear error-frame dedup so a new "
            "WS session retains fresh observability for retried "
            "commands.")


if __name__ == "__main__":
    unittest.main()
