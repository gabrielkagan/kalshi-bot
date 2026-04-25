"""Phase 2.6 — authoritative sid via type=subscribed + sid-based unsubscribe.

Phase 2.5 attempted to learn ticker→sid from envelope-level `sid` on
incoming orderbook_snapshot/orderbook_delta messages. Kalshi rejected
the resulting `update_subscription` commands with code=7 "Unknown
subscription ID" — proving the envelope sid is NOT the same as the
subscription id Kalshi expects in commands (despite the docs claiming
they are).

Per Kalshi docs the AUTHORITATIVE source of subscription ID is the
`type=subscribed` response to a `subscribe` command:

    {"id": <our_cmd_id>, "type": "subscribed",
     "msg": {"channel": "orderbook_delta", "sid": <int>}}

To match this back to the ticker we just subscribed, we use unique
monotonic command IDs and an `_outstanding_subscribes: Dict[id, ticker]`
map.

Also fixes unsubscribe — Kalshi rejected our pre-2.6 unsubscribe with
code=4 "Subscription IDs required". The correct schema is:

    {"cmd": "unsubscribe", "params": {"sids": [<int>, ...]}}
"""

import ast
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _make_feed():
    """Bypass __init__ for unit testing."""
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
    f._ticker_to_sid = {}
    f._ws_error_frame_seen = set()
    # Phase 2.6 — authoritative sid tracking.
    f._next_msg_id = 100
    f._outstanding_subscribes = {}
    f._outstanding_subscribe_ts = {}
    f._ws_orphan_sid_seen = set()
    # Phase 2.6 R4: force-reconnect flag + late-unsub set.
    f._force_reconnect_requested = False
    f._pending_late_unsubscribes = set()
    # Phase 2.9: raw-log counter + connect ts.
    f._raw_log_count = 0
    f._raw_log_capped_logged = False
    f._ws_connect_ts = 0.0
    f._lock = threading.Lock()
    return f


# ─────────────────────────────────────────────────────────────────────────────
# 1. Subscribe sends a unique command id and registers it for response matching
# ─────────────────────────────────────────────────────────────────────────────

class TestSubscribeUsesUniqueIds(unittest.IsolatedAsyncioTestCase):
    async def test_each_subscribe_uses_distinct_id(self):
        f = _make_feed()
        ws = AsyncMock()
        await f._send_ob_subscribe(ws, "BTC1")
        await f._send_ob_subscribe(ws, "ETH1")
        sent_a = json.loads(ws.send.await_args_list[0].args[0])
        sent_b = json.loads(ws.send.await_args_list[1].args[0])
        self.assertNotEqual(
            sent_a["id"], sent_b["id"],
            "Each subscribe must use a UNIQUE id so we can match "
            "the type=subscribed response back to the right ticker. "
            "Pre-2.6 we used static id=2 for ALL subscribes, making "
            "response-matching impossible.")

    async def test_subscribe_registers_outstanding(self):
        f = _make_feed()
        ws = AsyncMock()
        await f._send_ob_subscribe(ws, "BTC1")
        sent = json.loads(ws.send.await_args.args[0])
        self.assertIn(
            sent["id"], f._outstanding_subscribes,
            "Subscribe must register its id in _outstanding_subscribes "
            "so the type=subscribed response handler can look up "
            "which ticker the sid belongs to.")
        self.assertEqual(
            f._outstanding_subscribes[sent["id"]], "BTC1",
            "Outstanding map must record the ticker for this id.")


# ─────────────────────────────────────────────────────────────────────────────
# 2. type=subscribed response captures sid into _ticker_to_sid
# ─────────────────────────────────────────────────────────────────────────────

class TestSubscribedResponseCapturesSid(unittest.TestCase):
    def test_subscribed_response_records_ticker_to_sid(self):
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._outstanding_subscribes[101] = "BTC1"
        f._handle_message(json.dumps({
            "id": 101,
            "type": "subscribed",
            "msg": {"channel": "orderbook_delta", "sid": 7},
        }))
        self.assertEqual(
            f._ticker_to_sid.get("BTC1"), 7,
            "type=subscribed response must populate _ticker_to_sid "
            "with the sid from msg.sid (authoritative source).")
        self.assertNotIn(
            101, f._outstanding_subscribes,
            "Outstanding entry must be popped after response.")

    def test_subscribed_response_unknown_id_no_crash(self):
        """Stale / orphan response (e.g., after reconnect) must not
        crash — just skip."""
        f = _make_feed()
        f._handle_message(json.dumps({
            "id": 999,
            "type": "subscribed",
            "msg": {"channel": "orderbook_delta", "sid": 5},
        }))
        # No entry in outstanding for id=999 — should be no-op.
        self.assertEqual(f._ticker_to_sid, {})


# ─────────────────────────────────────────────────────────────────────────────
# 3. Unsubscribe uses sids array, not channels/market_tickers
# ─────────────────────────────────────────────────────────────────────────────

class TestUnsubscribeUsesSidsArray(unittest.IsolatedAsyncioTestCase):
    async def test_unsub_sends_sids_when_known(self):
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._ticker_to_sid["BTC1"] = 7
        ws = AsyncMock()
        await f._send_ob_unsubscribe(ws, "BTC1")
        sent = json.loads(ws.send.await_args.args[0])
        self.assertEqual(sent["cmd"], "unsubscribe")
        self.assertEqual(
            sent["params"].get("sids"), [7],
            "unsubscribe must send params.sids array per Kalshi "
            "docs. Pre-2.6 we sent params.market_tickers which "
            "Kalshi rejected with code=4 'Subscription IDs required'.")
        self.assertNotIn(
            "market_tickers", sent["params"],
            "market_tickers is not a valid unsubscribe param.")
        self.assertNotIn(
            "channels", sent["params"],
            "channels is not a valid unsubscribe param.")

    async def test_unsub_skips_when_sid_unknown(self):
        """Subscribe in flight (no sid yet) — unsubscribe can't be
        sent. Skip it; the subscribe will eventually succeed and a
        future unsub will work."""
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        # No entry in _ticker_to_sid.
        ws = AsyncMock()
        await f._send_ob_unsubscribe(ws, "BTC1")
        # Either sent nothing (skip) OR sent something safe — but
        # MUST NOT send the broken pre-2.6 schema.
        if ws.send.await_count > 0:
            sent = json.loads(ws.send.await_args.args[0])
            self.assertNotIn(
                "market_tickers", sent.get("params", {}),
                "Unsubscribe with unknown sid must NOT fall back to "
                "the broken pre-2.6 schema — Kalshi rejects it.")


# ─────────────────────────────────────────────────────────────────────────────
# 4. type=unsubscribed response cleans up state
# ─────────────────────────────────────────────────────────────────────────────

class TestUnsubscribedResponseCleanup(unittest.TestCase):
    """R-review A2: type=unsubscribed is now a confirmation-only log;
    sid is popped at SEND TIME in _send_ob_unsubscribe (see
    TestR1A2UnsubscribePopsSidAtSendTime). This test pins that the
    response is handled without crashing."""

    def test_unsubscribed_response_handled_without_crash(self):
        f = _make_feed()
        f._handle_message(json.dumps({
            "id": 200,
            "type": "unsubscribed",
            "sid": 7,
        }))
        # No exception = pass. The handler is now confirmation-only.


# ─────────────────────────────────────────────────────────────────────────────
# 5. AST checks — verify implementation surface
# ─────────────────────────────────────────────────────────────────────────────

class TestAstSchemaCorrectness(unittest.TestCase):
    def test_send_ob_unsubscribe_uses_sids(self):
        with open(BOT_PY) as fh:
            src = fh.read()
        tree = ast.parse(src)
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "KalshiFeed"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.AsyncFunctionDef)
                        and fn.name == "_send_ob_unsubscribe"):
                    body_src = ast.unparse(fn)
                    self.assertIn(
                        "'sids'", body_src,
                        "_send_ob_unsubscribe must use 'sids' "
                        "param per Kalshi docs.")
                    self.assertNotIn(
                        "'market_tickers'", body_src,
                        "Pre-2.6 'market_tickers' is rejected by "
                        "Kalshi (code=4).")
                    return
        self.fail("_send_ob_unsubscribe not found")

    def test_handle_message_branches_on_subscribed(self):
        with open(BOT_PY) as fh:
            src = fh.read()
        tree = ast.parse(src)
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "KalshiFeed"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_handle_message"):
                    body_src = ast.unparse(fn)
                    self.assertIn(
                        "'subscribed'", body_src,
                        "_handle_message must branch on type=="
                        "'subscribed' to capture authoritative sid.")
                    return
        self.fail("_handle_message not found")


class TestR1A1ForceResubSkipsWhenSidUnknown(unittest.TestCase):
    """R-review A1 [P0]: force_resubscribe with no known sid MUST
    be a true no-op. Pre-fix it queued unsub+resub, but the unsub
    SKIPS (no sid) → resub creates DUPLICATE subscription on Kalshi
    side → original subscription leaks forever (we never learn its
    sid)."""

    def test_no_sid_skips_force_resubscribe(self):
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-NEW")
        # No entry in _ticker_to_sid (subscribe in flight).
        f.force_resubscribe("KXBTC15M-NEW")
        self.assertNotIn(
            "KXBTC15M-NEW", f._pending_snapshot_requests,
            "Phase 2.6 R-review: no sid → no primary path queued.")
        self.assertNotIn(
            "KXBTC15M-NEW", f._pending_unsubscribes,
            "Phase 2.6 R-review A1: no sid → no fallback unsub "
            "queued (would create zombie subscription).")
        self.assertNotIn(
            "KXBTC15M-NEW", f._pending_subscribes,
            "Phase 2.6 R-review A1: no sid → no fallback resub "
            "queued (would create duplicate subscription).")


class TestR1A2UnsubscribePopsSidAtSendTime(unittest.IsolatedAsyncioTestCase):
    """R-review A2: pop _ticker_to_sid in _send_ob_unsubscribe
    after successful send, NOT via search-by-value in
    type=unsubscribed handler."""

    async def test_unsubscribe_pops_sid_after_send(self):
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._ticker_to_sid["BTC1"] = 7
        ws = AsyncMock()
        await f._send_ob_unsubscribe(ws, "BTC1")
        self.assertNotIn(
            "BTC1", f._ticker_to_sid,
            "After _send_ob_unsubscribe, the sid mapping must be "
            "popped at send time (not via search-by-value on "
            "type=unsubscribed response).")

    async def test_unsubscribe_send_failure_keeps_sid(self):
        """If ws.send raises, the sid should NOT be popped — caller
        may retry."""
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._ticker_to_sid["BTC1"] = 7
        ws = AsyncMock()
        ws.send.side_effect = ConnectionError("boom")
        with self.assertRaises(ConnectionError):
            await f._send_ob_unsubscribe(ws, "BTC1")
        self.assertEqual(
            f._ticker_to_sid.get("BTC1"), 7,
            "Send failure must NOT pop the sid (caller may retry).")


class TestR1A3SubscribeOrphanCleanupOnSendFailure(
        unittest.IsolatedAsyncioTestCase):
    """R-review A3: if _send_ob_subscribe raises after registering
    in _outstanding_subscribes, the entry orphans. Pop it on
    failure so retries can register a fresh cmd_id."""

    async def test_send_failure_pops_outstanding_entry(self):
        f = _make_feed()
        ws = AsyncMock()
        ws.send.side_effect = ConnectionError("boom")
        with self.assertRaises(ConnectionError):
            await f._send_ob_subscribe(ws, "BTC1")
        self.assertEqual(
            f._outstanding_subscribes, {},
            "Send failure must pop the orphan cmd_id from "
            "_outstanding_subscribes.")


class TestR1A4SubscribeErrorPopsOutstanding(unittest.TestCase):
    """R-review A4: when Kalshi sends an error response with the
    cmd_id of a subscribe, pop _outstanding_subscribes[id] so we
    don't leak an entry waiting for a response that won't come."""

    def test_error_response_pops_matching_subscribe(self):
        f = _make_feed()
        f._outstanding_subscribes[101] = "BTC1"
        f._handle_message(json.dumps({
            "type": "error",
            "id": 101,
            "msg": {"code": "invalid_market", "msg": "no such ticker"},
        }))
        self.assertEqual(
            f._outstanding_subscribes, {},
            "Error frame for a subscribe cmd_id must pop the "
            "orphan entry.")

    def test_error_response_unrelated_id_no_op(self):
        """Error frame whose id doesn't match any outstanding
        subscribe must not crash or affect unrelated entries."""
        f = _make_feed()
        f._outstanding_subscribes[101] = "BTC1"
        f._handle_message(json.dumps({
            "type": "error",
            "id": 999,
            "msg": {"code": "rate_limited"},
        }))
        # Outstanding for 101 is intact.
        self.assertEqual(f._outstanding_subscribes.get(101), "BTC1")


class TestR2B1OrphanSidDetection(unittest.TestCase):
    """R2 / B1: detect when Kalshi streams data on a sid we
    previously thought we'd unsubscribed (pop-at-send-time can
    leak if Kalshi never processes the unsubscribe). Surface as
    WARNING; will self-heal on next WS reconnect."""

    def test_envelope_sid_mismatch_logs_warning(self):
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._ticker_to_sid["BTC1"] = 10  # current known sid
        with self.assertLogs("root", level="WARNING") as cm:
            f._handle_ob_snapshot({
                "type": "orderbook_snapshot",
                "sid": 7,  # stale — different from 10
                "msg": {
                    "market_ticker": "BTC1",
                    "yes_dollars_fp": [],
                    "no_dollars_fp": [],
                },
            })
        joined = "\n".join(cm.output)
        self.assertIn("WS_ORPHAN_SID", joined,
                      "Mismatched envelope sid must trigger "
                      "WS_ORPHAN_SID warning.")
        self.assertIn("BTC1", joined)
        self.assertIn("envelope_sid=7", joined)
        self.assertIn("expected_sid=10", joined)


class TestR2B2OutstandingSubscribeWatchdog(unittest.TestCase):
    """R2 / B2: watchdog for stuck outstanding subscribes. If
    type=subscribed never arrives within
    WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S, pop the entry so
    force_resubscribe doesn't SKIP forever."""

    def test_stuck_outstanding_subscribe_popped(self):
        import bot
        f = _make_feed()
        f._subscribed_tickers.add("STUCK1")
        # Register stale outstanding subscribe.
        f._outstanding_subscribes[42] = "STUCK1"
        f._outstanding_subscribe_ts[42] = (
            time.monotonic()
            - bot.WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S - 1.0)
        with self.assertLogs("root", level="WARNING") as cm:
            f._check_snapshot_timeouts()
        self.assertNotIn(
            42, f._outstanding_subscribes,
            "Stuck outstanding subscribe must be popped after "
            "watchdog timeout.")
        self.assertNotIn(42, f._outstanding_subscribe_ts)
        joined = "\n".join(cm.output)
        self.assertIn(
            "WS_SUBSCRIBE_STUCK", joined,
            "Watchdog must log WARNING for stuck subscribe.")


class TestR4StuckSubscribeRequestsForceReconnect(unittest.TestCase):
    """R4 / A1+A2+A3: when B2 watchdog finds a stuck ticker still
    in _subscribed_tickers, set _force_reconnect_requested so the
    silence watchdog can trigger a fresh WS session. Without this,
    one stuck subscribe = permanent stale cache for that ticker
    (silence watchdog won't fire because other tickers keep
    _ws_last_msg_ts fresh)."""

    def test_stuck_subscribe_for_subscribed_ticker_requests_reconnect(self):
        import bot
        f = _make_feed()
        f._subscribed_tickers.add("STUCK1")
        f._outstanding_subscribes[42] = "STUCK1"
        f._outstanding_subscribe_ts[42] = (
            time.monotonic()
            - bot.WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S - 1.0)
        f._check_snapshot_timeouts()
        self.assertTrue(
            f._force_reconnect_requested,
            "Stuck subscribe for a still-subscribed ticker MUST "
            "set _force_reconnect_requested. Otherwise drift "
            "recovery is permanently dead until natural "
            "disconnect.")

    def test_stuck_subscribe_for_unsubscribed_ticker_no_reconnect(self):
        """If the stuck ticker has been unsubscribed since, no
        reconnect is needed."""
        import bot
        f = _make_feed()
        # Note: NOT in _subscribed_tickers.
        f._outstanding_subscribes[42] = "GONE1"
        f._outstanding_subscribe_ts[42] = (
            time.monotonic()
            - bot.WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S - 1.0)
        f._check_snapshot_timeouts()
        self.assertFalse(
            f._force_reconnect_requested,
            "Stuck subscribe for an unsubscribed ticker should "
            "not trigger reconnect — nobody needs the data.")


class TestR5BenignRaceUnsubAfterSubscribed(unittest.IsolatedAsyncioTestCase):
    """Phase 2.6 R5 / P1: the BENIGN ordering race.
    type=subscribed lands first (sid bound), THEN unsubscribe_ticker
    is called. Pre-fix, unsubscribe_ticker would pop _ticker_to_sid,
    leaving the drain's _send_ob_unsubscribe with no sid to use →
    SKIP → Kalshi-side subscription leaks. Fix: unsubscribe_ticker
    does NOT pop sid; drain consumes it via _send_ob_unsubscribe."""

    async def test_drain_can_send_unsub_after_subscribed_then_unsub(self):
        f = _make_feed()
        # 1. Subscribe in flight, response arrives.
        f._subscribed_tickers.add("RACE1")
        f._outstanding_subscribes[100] = "RACE1"
        f._handle_message(json.dumps({
            "id": 100,
            "type": "subscribed",
            "msg": {"channel": "orderbook_delta", "sid": 50},
        }))
        # Sanity: sid bound.
        self.assertEqual(f._ticker_to_sid.get("RACE1"), 50)
        # 2. unsubscribe_ticker called.
        f.unsubscribe_ticker("RACE1")
        # 3. Sid MUST still be present (drain needs it).
        self.assertEqual(
            f._ticker_to_sid.get("RACE1"), 50,
            "Phase 2.6 R5: sid must survive unsubscribe_ticker so "
            "the drain can send unsubscribe with it. Pre-fix, "
            "this was popped → silent leak.")
        # 4. Drain calls _send_ob_unsubscribe — should send valid sids array.
        ws = AsyncMock()
        await f._send_ob_unsubscribe(ws, "RACE1")
        ws.send.assert_awaited_once()
        sent = json.loads(ws.send.await_args.args[0])
        self.assertEqual(
            sent["params"]["sids"], [50],
            "Drain must use the bound sid to actually unsubscribe "
            "the live Kalshi-side subscription.")
        # 5. After successful send, sid pop happens.
        self.assertNotIn(
            "RACE1", f._ticker_to_sid,
            "_send_ob_unsubscribe pops sid AFTER successful send.")


class TestR4LateUnsubscribe(unittest.TestCase):
    """R4 / A8: unsubscribe_ticker called while subscribe is in
    flight must NOT leak the Kalshi-side subscription. Use the
    _pending_late_unsubscribes set + late-arriving subscribed
    handler to send a sid-based unsubscribe with the now-known sid."""

    def test_unsubscribe_during_inflight_subscribe_marks_late(self):
        f = _make_feed()
        f._subscribed_tickers.add("LATE1")
        # Simulate a subscribe in flight.
        f._outstanding_subscribes[100] = "LATE1"
        f._outstanding_subscribe_ts[100] = time.monotonic()
        f.unsubscribe_ticker("LATE1")
        self.assertIn(
            "LATE1", f._pending_late_unsubscribes,
            "unsubscribe_ticker on an in-flight subscribe MUST "
            "mark the ticker for late-unsubscribe via "
            "_pending_late_unsubscribes.")
        self.assertNotIn(
            "LATE1", f._subscribed_tickers,
            "Unsub still discards from _subscribed_tickers.")

    def test_late_subscribed_response_queues_unsub(self):
        """When type=subscribed arrives for a late-unsub ticker,
        we capture sid and immediately queue an unsubscribe so
        Kalshi doesn't keep streaming forever."""
        f = _make_feed()
        # Setup: ticker was unsubbed while subscribe in flight.
        f._outstanding_subscribes[100] = "LATE1"
        f._pending_late_unsubscribes.add("LATE1")
        # Note: ticker NOT in _subscribed_tickers (already unsub'd).
        with self.assertLogs("root", level="WARNING") as cm:
            f._handle_message(json.dumps({
                "id": 100,
                "type": "subscribed",
                "msg": {"channel": "orderbook_delta", "sid": 50},
            }))
        self.assertEqual(
            f._ticker_to_sid.get("LATE1"), 50,
            "Sid must be bound (temporarily) so _send_ob_unsubscribe "
            "can look it up.")
        self.assertIn(
            "LATE1", f._pending_unsubscribes,
            "Late-unsub ticker must be queued for unsubscribe drain.")
        self.assertNotIn(
            "LATE1", f._pending_late_unsubscribes,
            "Late-unsub flag must be cleared after handling.")
        joined = "\n".join(cm.output)
        self.assertIn("WS_LATE_UNSUBSCRIBE", joined)


if __name__ == "__main__":
    unittest.main()
