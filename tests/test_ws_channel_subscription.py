"""Phase 2.10 — channel-subscription redesign.

Phase 2.9's raw-frame logging revealed:
  1. Kalshi sends `type=ok` (NOT `type=subscribed`) for ~99% of
     subscribe commands — only the first 2 establish the channel
     and get `type=subscribed`. Everything after is `type=ok`.
     Pre-2.10 our handler ignored `type=ok` → 230 stuck subscribes
     in 12 min → reconnect loop.
  2. The `sid` in WS envelopes is CHANNEL-level, not per-ticker.
     All orderbook_delta tickers share `sid=2`. Subscribe
     responses (subscribed AND ok) carry that channel sid.
  3. Our `unsubscribe` with `sids=[sid]` would unsubscribe the
     ENTIRE channel (all 100+ tickers). Pre-2.10 this was a
     latent disaster — most tickers had no sid in our map so
     unsubscribe SKIPPED. Once Phase 2.10 binds sids correctly,
     the unsub path MUST switch to `update_subscription` with
     `action: delete_markets` to remove a single ticker without
     touching the rest of the channel.

Phase 2.10 changes:
  A. Add `type=ok` branch in `_handle_message`. Same logic as
     `type=subscribed`: match cmd_id → outstanding, bind sid,
     handle late-unsub.
  B. `_send_ob_unsubscribe` now sends `update_subscription` with
     `action: delete_markets` + `market_tickers: [ticker]`.
     Surgical removal — leaves the rest of the channel alone.
"""

import ast
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock
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
    f._ws_connect_ts = time.time()
    f._ticker_to_sid = {}
    f._ws_error_frame_seen = set()
    f._next_msg_id = 100
    f._outstanding_subscribes = {}
    f._outstanding_subscribe_ts = {}
    f._ws_orphan_sid_seen = set()
    f._force_reconnect_requested = False
    f._pending_late_unsubscribes = set()
    f._raw_log_count = 0
    f._raw_log_capped_logged = False
    f._lock = threading.Lock()
    return f


# ─────────────────────────────────────────────────────────────────────────────
# A. type=ok recognized as ack
# ─────────────────────────────────────────────────────────────────────────────

class TestTypeOkRecognizedAsAck(unittest.TestCase):
    """Phase 2.10 P0: type=ok with matching cmd_id MUST be
    treated as an ack, same as type=subscribed. Pre-fix:
    type=ok was silently ignored → ~99% of subscribes appeared
    stuck → reconnect loop."""

    def test_type_ok_pops_outstanding_and_binds_sid(self):
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._outstanding_subscribes[100] = "BTC1"
        f._outstanding_subscribe_ts[100] = time.monotonic()
        # Real Kalshi type=ok response (cumulative market_tickers).
        f._handle_message(json.dumps({
            "id": 100,
            "type": "ok",
            "sid": 2,
            "seq": 4,
            "msg": {"market_tickers": ["BTC1"]},
        }))
        self.assertEqual(
            f._ticker_to_sid.get("BTC1"), 2,
            "type=ok must bind the channel sid to the ticker — "
            "this is the actual ack for ~99% of subscribes.")
        self.assertNotIn(
            100, f._outstanding_subscribes,
            "type=ok must pop outstanding_subscribes so the "
            "stuck-subscribe watchdog doesn't fire.")
        self.assertNotIn(
            100, f._outstanding_subscribe_ts,
            "type=ok must pop the watchdog timestamp too.")

    def test_type_ok_unknown_id_is_no_op(self):
        """Stale/orphan type=ok (no matching outstanding) must
        not crash."""
        f = _make_feed()
        f._handle_message(json.dumps({
            "id": 999, "type": "ok", "sid": 2, "seq": 1,
            "msg": {"market_tickers": []},
        }))
        # No exception = pass.

    def test_type_ok_late_unsubscribe_path(self):
        """If unsubscribe_ticker was called while subscribe was
        in flight, the type=ok handler must queue a late-unsub
        with the now-known sid (same as type=subscribed path)."""
        f = _make_feed()
        f._outstanding_subscribes[200] = "LATE1"
        f._outstanding_subscribe_ts[200] = time.monotonic()
        f._pending_late_unsubscribes.add("LATE1")
        # Note: ticker NOT in _subscribed_tickers (already unsubbed).
        f._handle_message(json.dumps({
            "id": 200, "type": "ok", "sid": 2, "seq": 1,
            "msg": {"market_tickers": []},
        }))
        self.assertIn(
            "LATE1", f._pending_unsubscribes,
            "Late-unsub branch must queue the unsubscribe via the "
            "drain — same as type=subscribed path.")
        self.assertNotIn(
            "LATE1", f._pending_late_unsubscribes,
            "Late-unsub flag cleared after handling.")
        self.assertEqual(
            f._ticker_to_sid.get("LATE1"), 2,
            "Sid bound (temporarily) so drain can use it for "
            "delete_markets.")


# ─────────────────────────────────────────────────────────────────────────────
# B. Unsubscribe schema: delete_markets, NOT unsubscribe with sids
# ─────────────────────────────────────────────────────────────────────────────

class TestUnsubscribeUsesDeleteMarkets(unittest.IsolatedAsyncioTestCase):
    """Phase 2.10 P0: unsubscribe ONE ticker MUST use
    `update_subscription` with `action: delete_markets`. Pre-fix
    we sent `cmd: unsubscribe` with `sids: [sid]`, which Kalshi
    interprets as "cancel the entire subscription" — i.e., remove
    ALL ~100 tickers, not just one. Latent disaster that happened
    to be masked by Phase 2.6's per-ticker sid model not
    populating most tickers."""

    async def test_unsub_sends_update_subscription_delete_markets(self):
        f = _make_feed()
        f._subscribed_tickers.add("BTC1")
        f._ticker_to_sid["BTC1"] = 2  # channel sid
        ws = AsyncMock()
        await f._send_ob_unsubscribe(ws, "BTC1")
        ws.send.assert_awaited_once()
        sent = json.loads(ws.send.await_args.args[0])
        self.assertEqual(sent["cmd"], "update_subscription")
        self.assertEqual(
            sent["params"]["action"], "delete_markets",
            "Single-ticker removal MUST use delete_markets — "
            "NOT the whole-channel `cmd: unsubscribe` which would "
            "drop ALL tickers on that sid.")
        self.assertEqual(
            sent["params"]["sid"], 2,
            "channel sid required for update_subscription.")
        self.assertEqual(
            sent["params"]["market_tickers"], ["BTC1"],
            "Only the specific ticker — surgical removal.")
        # Crucially: sids array NOT used (would nuke channel).
        self.assertNotIn(
            "sids", sent["params"],
            "sids array must NOT be present — that's the "
            "cancel-whole-subscription mode.")

    async def test_unsub_skips_when_sid_unknown(self):
        """Subscribe in flight (no sid yet) — can't send
        delete_markets without sid. Skip; let resub recover."""
        f = _make_feed()
        f._subscribed_tickers.add("NEW1")
        # No entry in _ticker_to_sid.
        ws = AsyncMock()
        await f._send_ob_unsubscribe(ws, "NEW1")
        # No send (or skip — must NOT use the dangerous sids API).
        if ws.send.await_count > 0:
            sent = json.loads(ws.send.await_args.args[0])
            self.assertNotIn(
                "sids", sent.get("params", {}),
                "Even in fallback, MUST NOT use sids — would nuke "
                "the entire channel.")


# ─────────────────────────────────────────────────────────────────────────────
# C. AST guards
# ─────────────────────────────────────────────────────────────────────────────

class TestAstHandleMessageBranchesOnOk(unittest.TestCase):
    def test_handle_message_branches_on_ok(self):
        src = ""
        if os.path.exists(BOT_PY):
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
                        "'ok'", body_src,
                        "_handle_message MUST branch on type=='ok' "
                        "to recognize Kalshi's actual subscribe ack.")
                    return
        self.fail("_handle_message not found")


class TestAstUnsubscribeUsesDeleteMarkets(unittest.TestCase):
    def test_send_ob_unsubscribe_uses_delete_markets(self):
        src = ""
        if os.path.exists(BOT_PY):
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
                        "'delete_markets'", body_src,
                        "_send_ob_unsubscribe MUST use action="
                        "delete_markets to remove a single ticker.")
                    self.assertIn(
                        "'update_subscription'", body_src,
                        "Must use update_subscription cmd, not "
                        "the channel-killing unsubscribe cmd.")
                    return
        self.fail("_send_ob_unsubscribe not found")


class TestDeleteMarketsLeavesOtherTickersIntact(unittest.IsolatedAsyncioTestCase):
    """R-review A3: removing one ticker via delete_markets MUST
    leave other tickers on the same channel sid functional. Phase
    2.10's premise is that all tickers share one channel sid;
    if delete_markets accidentally invalidated the channel for
    OTHER tickers, that would be the latent disaster Phase 2.10
    was meant to prevent."""

    async def test_other_tickers_keep_their_sid(self):
        f = _make_feed()
        # Multiple tickers on shared channel sid=2.
        f._subscribed_tickers.update({"BTC1", "ETH1", "SOL1"})
        f._ticker_to_sid["BTC1"] = 2
        f._ticker_to_sid["ETH1"] = 2
        f._ticker_to_sid["SOL1"] = 2
        ws = AsyncMock()
        # Remove just BTC1.
        await f._send_ob_unsubscribe(ws, "BTC1")
        sent = json.loads(ws.send.await_args.args[0])
        self.assertEqual(sent["params"]["action"], "delete_markets")
        self.assertEqual(sent["params"]["market_tickers"], ["BTC1"])
        # BTC1 popped from local map (matches what Kalshi did).
        self.assertNotIn("BTC1", f._ticker_to_sid)
        # ETH1 and SOL1 STILL have their sid bound — Phase 2.10
        # must not yank the rest of the channel.
        self.assertEqual(f._ticker_to_sid.get("ETH1"), 2,
                         "Other tickers on shared channel sid MUST "
                         "retain their mapping after one ticker is "
                         "deleted via delete_markets.")
        self.assertEqual(f._ticker_to_sid.get("SOL1"), 2)


if __name__ == "__main__":
    unittest.main()
