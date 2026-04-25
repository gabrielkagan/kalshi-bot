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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _make_feed():
    """Same fixture as test_ws_force_resubscribe.py — bypass __init__."""
    import bot
    f = bot.KalshiFeed.__new__(bot.KalshiFeed)
    f._pending_subscribes = []
    f._pending_unsubscribes = []
    f._pending_snapshot_requests = []
    f._subscribed_tickers = set()
    f._orderbooks = {}
    f._snapshot_request_pending = {}
    f._force_resub_cooldown = {}
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
    f._lock = threading.Lock()
    return f


# ─────────────────────────────────────────────────────────────────────────────
# 1. _ticker_to_sid mapping populated from incoming envelopes
# ─────────────────────────────────────────────────────────────────────────────

class TestSidMapPopulatedFromSnapshot(unittest.TestCase):
    def test_snapshot_with_sid_records_mapping(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f._handle_ob_snapshot({
            "sid": 456,
            "type": "orderbook_snapshot",
            "msg": {
                "market_ticker": "KXBTC15M-FOO",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            },
        })
        self.assertEqual(
            f._ticker_to_sid.get("KXBTC15M-FOO"), 456,
            "_handle_ob_snapshot must capture the envelope-level "
            "`sid` into _ticker_to_sid for this ticker.")

    def test_snapshot_without_sid_skips_mapping(self):
        """Defensive: pre-2026 fixture data may lack envelope sid;
        absence must not crash, just skip the mapping."""
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f._handle_ob_snapshot({
            "type": "orderbook_snapshot",
            "msg": {
                "market_ticker": "KXBTC15M-FOO",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            },
        })
        self.assertNotIn("KXBTC15M-FOO", f._ticker_to_sid)

    def test_resubscribe_updates_sid_mapping(self):
        """When unsub+resub fires, Kalshi assigns a NEW sid. The
        next snapshot for the same ticker must overwrite the old
        sid in the map."""
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f._ticker_to_sid["KXBTC15M-FOO"] = 100  # stale sid
        f._handle_ob_snapshot({
            "sid": 200,  # new sid from resubscribe
            "msg": {
                "market_ticker": "KXBTC15M-FOO",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            },
        })
        self.assertEqual(
            f._ticker_to_sid["KXBTC15M-FOO"], 200,
            "New snapshot for resubscribed ticker must overwrite "
            "the stale sid.")


class TestSidMapPopulatedFromDelta(unittest.TestCase):
    def test_delta_with_sid_records_mapping(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-FOO")
        # Seed with a snapshot first (so delta has something to apply).
        f._orderbooks["KXBTC15M-FOO"] = {
            "yes": [], "no": [], "ts": time.time()}
        f._apply_fp_delta(
            "KXBTC15M-FOO",
            {
                "side": "yes",
                "price_dollars": "0.95",
                "delta_fp": "10",
            },
            envelope_sid=789,  # NEW kwarg if implemented; or attr passing
        ) if False else None  # placeholder — see _handle_ob_delta path below
        # Actual exercise: send full delta envelope through _handle_ob_delta
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
        self.assertEqual(
            f._ticker_to_sid.get("KXBTC15M-FOO"), 789,
            "Delta envelope's sid must populate _ticker_to_sid so "
            "tickers can be queried for sid even before their first "
            "snapshot has arrived (snapshot may be lost or late).")


# ─────────────────────────────────────────────────────────────────────────────
# 2. _send_ob_get_snapshot uses sid, not market_tickers
# ─────────────────────────────────────────────────────────────────────────────

class TestSendUsesSid(unittest.IsolatedAsyncioTestCase):
    async def test_send_ob_get_snapshot_uses_sid_param(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f._ticker_to_sid["KXBTC15M-FOO"] = 456
        ws = AsyncMock()
        await f._send_ob_get_snapshot(ws, "KXBTC15M-FOO")
        # Reconstruct what was sent.
        ws.send.assert_awaited_once()
        sent_raw = ws.send.await_args.args[0]
        sent = json.loads(sent_raw)
        self.assertEqual(sent["cmd"], "update_subscription")
        self.assertEqual(sent["params"]["action"], "get_snapshot")
        self.assertEqual(
            sent["params"]["sid"], 456,
            "Per Kalshi docs, params.sid is required for "
            "update_subscription. Pre-fix we sent params.market_tickers "
            "which Kalshi rejected with an error frame we silently "
            "dropped.")
        self.assertNotIn(
            "market_tickers", sent["params"],
            "Pre-fix bug: we sent params.market_tickers (invalid for "
            "update_subscription). Kalshi only accepts sid/sids.")


# ─────────────────────────────────────────────────────────────────────────────
# 3. force_resubscribe falls back when sid unknown
# ─────────────────────────────────────────────────────────────────────────────

class TestForceResubFallbackWhenSidUnknown(unittest.TestCase):
    def setUp(self):
        self.f = _make_feed()
        self.f._subscribed_tickers.add("KXBTC15M-NEW")
        # No entry in _ticker_to_sid yet — subscribe is in flight.

    def test_no_sid_skips_primary_queues_unsub_resub(self):
        self.f.force_resubscribe("KXBTC15M-NEW")
        self.assertNotIn(
            "KXBTC15M-NEW", self.f._pending_snapshot_requests,
            "Without a known sid, primary path is impossible — "
            "force_resubscribe must skip _pending_snapshot_requests "
            "and go straight to the unsub+resub fallback.")
        self.assertIn(
            "KXBTC15M-NEW", self.f._pending_unsubscribes)
        self.assertIn(
            "KXBTC15M-NEW", self.f._pending_subscribes)

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
    def test_unsubscribe_pops_sid(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f._ticker_to_sid["KXBTC15M-FOO"] = 456
        f.unsubscribe_ticker("KXBTC15M-FOO")
        self.assertNotIn(
            "KXBTC15M-FOO", f._ticker_to_sid,
            "unsubscribe_ticker must clear _ticker_to_sid — sids "
            "are subscription-scoped and meaningless after unsub.")

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


class TestMonotonicSidGuard(unittest.TestCase):
    """R2-review Phase 2.5: a stale in-flight message from a prior
    subscription (smaller sid) MUST NOT overwrite a newer cached
    sid. Otherwise force_resubscribe would send a dead sid in
    update_subscription, silently failing until fallback kicks in."""

    def test_smaller_envelope_sid_does_not_overwrite_snapshot(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-MONO")
        # Cached newer sid first.
        f._ticker_to_sid["KXBTC15M-MONO"] = 200
        # Stale snapshot arrives with smaller sid (in-flight from
        # prior subscription).
        f._handle_ob_snapshot({
            "sid": 100,  # stale!
            "msg": {
                "market_ticker": "KXBTC15M-MONO",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            },
        })
        self.assertEqual(
            f._ticker_to_sid["KXBTC15M-MONO"], 200,
            "Stale (smaller) sid must NOT overwrite the cached "
            "newer sid. Otherwise the next update_subscription "
            "would send a dead sid → silent failure.")

    def test_smaller_envelope_sid_does_not_overwrite_delta(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-MONO")
        f._orderbooks["KXBTC15M-MONO"] = {
            "yes": [], "no": [], "ts": time.time()}
        f._ticker_to_sid["KXBTC15M-MONO"] = 200
        f._handle_ob_delta({
            "sid": 100,  # stale!
            "msg": {
                "market_ticker": "KXBTC15M-MONO",
                "side": "yes",
                "price_dollars": "0.95",
                "delta_fp": "10",
            },
        })
        self.assertEqual(
            f._ticker_to_sid["KXBTC15M-MONO"], 200,
            "Stale delta sid must NOT overwrite cached newer sid.")

    def test_equal_or_greater_sid_does_overwrite(self):
        """Same sid (no-op) and greater sid (legit resubscribe)
        both update the map."""
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-MONO")
        f._ticker_to_sid["KXBTC15M-MONO"] = 200
        # Equal — fine, no-op overwrite.
        f._handle_ob_snapshot({
            "sid": 200,
            "msg": {
                "market_ticker": "KXBTC15M-MONO",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            },
        })
        self.assertEqual(f._ticker_to_sid["KXBTC15M-MONO"], 200)
        # Greater — legitimate resubscribe.
        f._handle_ob_snapshot({
            "sid": 300,
            "msg": {
                "market_ticker": "KXBTC15M-MONO",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            },
        })
        self.assertEqual(f._ticker_to_sid["KXBTC15M-MONO"], 300)


class TestSidNotCapturedOnMalformedDelta(unittest.TestCase):
    """Adversarial-review P1: when a delta arrives with a valid
    envelope sid but malformed msg body (no price_dollars AND no
    yes/no), we MUST NOT cache the sid. Otherwise force_resubscribe
    would take the primary path on a broken stream when the safer
    unsub+resub fallback is what we actually want."""

    def test_malformed_delta_does_not_set_sid(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-BAD")
        # Envelope is valid but msg has none of the expected schema
        # fields → OrderbookSchemaError raised.
        f._handle_ob_delta({
            "sid": 999,
            "msg": {
                "market_ticker": "KXBTC15M-BAD",
                "garbage_field": True,
            },
        })
        self.assertNotIn(
            "KXBTC15M-BAD", f._ticker_to_sid,
            "Malformed delta must NOT cache sid — caching it would "
            "let force_resubscribe use the primary path on a stream "
            "we can't actually parse.")


class TestTimeoutFallbackClearsSid(unittest.TestCase):
    """R3-review Phase 2.5: when the snapshot-timeout fallback
    queues unsub+resub, it MUST also pop _ticker_to_sid for the
    affected ticker. Without this, a Kalshi sid-recycling scenario
    (new sid < old) would be rejected by the monotonic guard,
    leaving the bot in a stuck-stale-sid loop."""

    def test_timeout_fallback_pops_ticker_to_sid(self):
        import bot
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-LOOP")
        f._ticker_to_sid["KXBTC15M-LOOP"] = 500
        # Backdate pending request past timeout.
        f._snapshot_request_pending["KXBTC15M-LOOP"] = (
            time.monotonic() - bot.WS_SNAPSHOT_REQUEST_TIMEOUT_S - 1.0)
        f._check_snapshot_timeouts()
        self.assertNotIn(
            "KXBTC15M-LOOP", f._ticker_to_sid,
            "Timeout fallback must clear _ticker_to_sid — the sid "
            "is about to be invalidated by the queued unsubscribe, "
            "and stale sid blocks fresh snapshot acceptance under "
            "the monotonic-sid guard.")
        # Sanity: fallback unsub+resub IS queued.
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
