"""Phase 2 — WS cache reset via force_resubscribe.

The Apr 24 fixes (#1a/#1b/#3) detected WS drift and bypassed the cache
for 60s, but never RESET the cache state. When bypass expired, the
phantom state was still there. This file pins the new behavior:

  1. KalshiFeed.force_resubscribe(ticker) queues an unsubscribe + a
     re-subscribe in `_pending_unsubscribes` / `_pending_subscribes`
     so the WS thread sends both commands. The server responds with
     a fresh snapshot, replacing the corrupted cache atomically.

  2. flag_ticker_drifted (existing detector) now ALSO calls
     force_resubscribe. Replaces the 60s symptom-bandaid with a real
     reset.

  3. Periodic 5-min full re-snapshot of all active 15M tickers
     (insurance against undetected drift — H3' open hypothesis from
     ws-cache-drift-investigation.md).

  4. Rate-limit per ticker: don't force-resub the same ticker more
     than once per ~30s, even if multiple triggers fire (avoids
     re-sub loops on flapping connections).

Why this is the durable fix:
  - Doesn't depend on seq gaps (DISPROVEN by 20-min observation).
  - Doesn't depend on stable REST (DISPROVEN by stability probe).
  - Forces a fresh server snapshot — the only thing that DEFINITIVELY
    clears accumulated state.
  - 5-min periodic + drift-triggered + rate-limited = belt-and-
    suspenders defense.
"""

import ast
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/_impl.py")


def _make_feed():
    """Construct a minimal KalshiFeed with the attrs force_resubscribe
    needs. Bypasses __init__ to avoid touching network/asyncio."""
    import bot
    f = bot.KalshiFeed.__new__(bot.KalshiFeed)
    f._pending_subscribes = []
    f._pending_unsubscribes = []
    f._pending_snapshot_requests = []
    f._subscribed_tickers = set()
    f._orderbooks = {}
    f._snapshot_request_pending = {}
    f._force_resub_cooldown = {}
    f._unsubscribe_blacklist = {}
    # R1 / A1 [P0] + R2 / P0-2: get_snapshot disable tracking.
    f._get_snapshot_consecutive_failed_sweeps = 0
    f._get_snapshot_disabled = False
    f._get_snapshot_disabled_logged = False
    # R1 / A5 [P1]: post-resub recovery watchdog state.
    f._force_resub_recovery_deadline = {}
    f._force_resub_recovery_warned = {}
    # Phase 2.5: ticker→sid map. Tests that exercise the primary
    # get_snapshot path must pre-populate this in setUp; tests that
    # specifically test the no-sid-known fallback leave it empty.
    f._ticker_to_sid = {}
    f._ws_error_frame_seen = set()
    # Phase 2.6: authoritative sid tracking.
    f._next_msg_id = 100
    f._outstanding_subscribes = {}
    f._outstanding_subscribe_ts = {}
    f._ws_orphan_sid_seen = set()
    f._force_reconnect_requested = False
    f._pending_late_unsubscribes = set()
    f._raw_log_count = 0
    f._raw_log_capped_logged = False
    f._ws_connect_ts = 0.0
    f._snapshot_schema_probed = True
    f._delta_probe_count = 0
    f._delta_probe_max = 0
    import threading
    f._lock = threading.Lock()
    return f


class TestForceResubscribeHelper(unittest.TestCase):
    """KalshiFeed.force_resubscribe(ticker) — hybrid design:
    primary path is `update_subscription` with `action: get_snapshot`
    (queued in _pending_snapshot_requests). If no snapshot arrives
    within WS_SNAPSHOT_REQUEST_TIMEOUT_S, the timeout checker falls
    back to unsubscribe + resubscribe."""

    def setUp(self):
        self.f = _make_feed()
        self.f._subscribed_tickers.add("KXBTC15M-FOO")
        # Phase 2.5: pre-populate sid so primary path is eligible.
        # Without a sid, force_resubscribe routes to fallback.
        self.f._ticker_to_sid["KXBTC15M-FOO"] = 100

    def test_force_resub_queues_snapshot_request_first(self):
        """Primary path: queue a get_snapshot request. unsub+resub
        is the FALLBACK that fires only on timeout."""
        self.f.force_resubscribe("KXBTC15M-FOO")
        self.assertIn(
            "KXBTC15M-FOO", self.f._pending_snapshot_requests,
            "force_resubscribe must queue a get_snapshot request "
            "as the primary cache-reset path (per Kalshi docs — "
            "preserves subscription, no gap).")
        # And it must have set the pending tracker for timeout
        # detection.
        self.assertIn(
            "KXBTC15M-FOO", self.f._snapshot_request_pending,
            "force_resubscribe must record the request timestamp "
            "so _check_snapshot_timeouts can fire the fallback if "
            "no snapshot arrives.")
        # NOT in unsub/resub queues yet — those are fallback only.
        self.assertNotIn(
            "KXBTC15M-FOO", self.f._pending_unsubscribes,
            "Unsub+resub fallback must NOT fire on the primary "
            "path — only on timeout via _check_snapshot_timeouts.")

    def test_force_resub_purges_orderbook_cache(self):
        """Stale cache must be cleared so scan doesn't read from
        the corrupt cache during the brief snapshot-request window.
        Phase 1's silent-continue wiring makes the brief gap
        observable (no_orderbook rejection)."""
        self.f._orderbooks["KXBTC15M-FOO"] = {"yes": [], "no": []}
        self.f.force_resubscribe("KXBTC15M-FOO")
        self.assertNotIn(
            "KXBTC15M-FOO", self.f._orderbooks,
            "force_resubscribe must purge the corrupted cache.")

    def test_force_resub_keeps_ticker_in_subscribed_set(self):
        """The ticker stays subscribed — we want a snapshot, not
        a full unsubscription."""
        self.f.force_resubscribe("KXBTC15M-FOO")
        self.assertIn(
            "KXBTC15M-FOO", self.f._subscribed_tickers,
            "force_resubscribe must NOT remove the ticker from the "
            "subscribed set — it's a state reset, not unsubscription.")

    def test_force_resub_rate_limited_per_ticker(self):
        """Repeat call within cooldown is no-op."""
        self.f.force_resubscribe("KXBTC15M-FOO")
        self.f._pending_snapshot_requests.clear()
        # Second call within cooldown window — must no-op.
        self.f.force_resubscribe("KXBTC15M-FOO")
        self.assertNotIn(
            "KXBTC15M-FOO", self.f._pending_snapshot_requests,
            "Second force_resubscribe within cooldown must no-op.")

    def test_force_resub_after_cooldown_expires_works(self):
        """After cooldown expires, force_resub fires again."""
        self.f.force_resubscribe("KXBTC15M-FOO")
        # Backdate cooldown entry past expiry.
        import bot
        self.f._force_resub_cooldown["KXBTC15M-FOO"] = (
            time.monotonic() - bot.WS_FORCE_RESUB_COOLDOWN_S - 1.0)
        self.f._pending_snapshot_requests.clear()
        self.f.force_resubscribe("KXBTC15M-FOO")
        self.assertIn(
            "KXBTC15M-FOO", self.f._pending_snapshot_requests,
            "After cooldown expiry, force_resubscribe must work again.")

    def test_unsubscribed_ticker_resub_no_op(self):
        """If the ticker isn't currently subscribed, force_resubscribe
        is a no-op (nothing to reset)."""
        self.f._subscribed_tickers.discard("KXBTC15M-FOO")
        self.f.force_resubscribe("KXBTC15M-FOO")
        self.assertNotIn(
            "KXBTC15M-FOO", self.f._pending_snapshot_requests)
        self.assertNotIn(
            "KXBTC15M-FOO", self.f._pending_unsubscribes)


class TestSnapshotTimeoutFallback(unittest.TestCase):
    """If the get_snapshot request doesn't yield a snapshot within
    WS_SNAPSHOT_REQUEST_TIMEOUT_S, _check_snapshot_timeouts queues
    the unsub+resub fallback."""

    def setUp(self):
        self.f = _make_feed()
        self.f._subscribed_tickers.add("KXBTC15M-BAR")

    def test_timeout_triggers_unsub_resub_fallback(self):
        """Backdate the pending timestamp past WS_SNAPSHOT_REQUEST_TIMEOUT_S;
        _check_snapshot_timeouts should queue both unsub and sub."""
        import bot
        self.f._snapshot_request_pending["KXBTC15M-BAR"] = (
            time.monotonic() - bot.WS_SNAPSHOT_REQUEST_TIMEOUT_S - 1.0)
        timed_out = self.f._check_snapshot_timeouts()
        self.assertEqual(timed_out, ["KXBTC15M-BAR"])
        self.assertIn(
            "KXBTC15M-BAR", self.f._pending_unsubscribes,
            "Timeout fallback must queue unsubscribe.")
        self.assertIn(
            "KXBTC15M-BAR", self.f._pending_subscribes,
            "Timeout fallback must queue resubscribe.")
        self.assertNotIn(
            "KXBTC15M-BAR", self.f._snapshot_request_pending,
            "Pending tracker must be cleared after timeout fires.")

    def test_no_timeout_yet_no_fallback(self):
        """Pending request within the timeout window — no fallback
        yet."""
        self.f._snapshot_request_pending["KXBTC15M-BAR"] = (
            time.monotonic() - 0.5)  # only 0.5s elapsed
        timed_out = self.f._check_snapshot_timeouts()
        self.assertEqual(timed_out, [])
        self.assertNotIn(
            "KXBTC15M-BAR", self.f._pending_unsubscribes)

    def test_snapshot_arrival_clears_pending(self):
        """When _handle_ob_snapshot processes a snapshot for a
        pending ticker, the pending entry must be cleared so the
        timeout fallback doesn't fire after the snapshot already
        arrived."""
        self.f._snapshot_request_pending["KXBTC15M-BAR"] = (
            time.monotonic())
        # Simulate snapshot arrival via _handle_ob_snapshot. We
        # construct a minimal envelope.
        msg = {
            "msg": {
                "market_ticker": "KXBTC15M-BAR",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            }
        }
        # _handle_ob_snapshot doesn't touch network; safe to call.
        self.f._handle_ob_snapshot(msg)
        self.assertNotIn(
            "KXBTC15M-BAR", self.f._snapshot_request_pending,
            "_handle_ob_snapshot must clear pending entry on receipt.")


class TestForceResubCooldownConstant(unittest.TestCase):
    """A module-level constant defines the rate-limit window."""

    def test_constant_defined(self):
        import bot
        self.assertTrue(
            hasattr(bot, "WS_FORCE_RESUB_COOLDOWN_S"),
            "Must define WS_FORCE_RESUB_COOLDOWN_S as a module-level "
            "constant.")
        # Reasonable range — not so short we re-sub mid-snapshot,
        # not so long that legitimate repeat triggers wait too long.
        val = bot.WS_FORCE_RESUB_COOLDOWN_S
        self.assertGreaterEqual(val, 5.0)
        self.assertLessEqual(val, 120.0)


class TestFlagDriftedTriggersForceResub(unittest.TestCase):
    """flag_ticker_drifted (the existing severity detector) must
    ALSO call force_resubscribe — replace the 60s symptom-bandaid
    with a real cache reset."""

    def test_ast_flag_drifted_calls_force_resubscribe(self):
        """AST: walk OpportunityScanner.flag_ticker_drifted; verify
        it calls force_resubscribe on self._kalshi_feed (or
        equivalent)."""
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        target_fn = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OpportunityScanner"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "flag_ticker_drifted"):
                    target_fn = fn
                    break
        self.assertIsNotNone(
            target_fn,
            "OpportunityScanner.flag_ticker_drifted not found")
        fn_src = ast.unparse(target_fn)
        self.assertIn(
            "force_resubscribe", fn_src,
            "flag_ticker_drifted must call force_resubscribe — "
            "replaces the 60s WS bypass bandaid with a real cache "
            "reset. Phase 2 fix.")


class TestPeriodicResnapshot(unittest.TestCase):
    """Every WS_PERIODIC_RESNAPSHOT_INTERVAL_S seconds, MainLoop
    triggers a force_resubscribe on all currently-subscribed 15M
    tickers. Insurance against H3' (msg loss without seq increment
    on server) — the open hypothesis from the cache-drift
    investigation."""

    def test_periodic_resnap_constant_defined(self):
        import bot
        self.assertTrue(
            hasattr(bot, "WS_PERIODIC_RESNAPSHOT_INTERVAL_S"),
            "Must define WS_PERIODIC_RESNAPSHOT_INTERVAL_S "
            "module-level.")
        val = bot.WS_PERIODIC_RESNAPSHOT_INTERVAL_S
        # Reasonable range: 1-30 min.
        self.assertGreaterEqual(val, 60.0)
        self.assertLessEqual(val, 1800.0)

    def test_main_loop_tick_calls_periodic_resnap(self):
        """AST: MainLoop._tick references WS_PERIODIC_RESNAPSHOT_INTERVAL_S
        and calls force_resubscribe in some path."""
        with open(BOT_PY) as f:
            src = f.read()
        # The constant must be referenced from inside MainLoop._tick.
        # We don't verify call ordering — just that the wiring exists.
        tree = ast.parse(src)
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "MainLoop"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_tick"):
                    body_src = ast.unparse(fn)
                    self.assertIn(
                        "WS_PERIODIC_RESNAPSHOT_INTERVAL_S", body_src,
                        "MainLoop._tick must reference "
                        "WS_PERIODIC_RESNAPSHOT_INTERVAL_S to "
                        "schedule the periodic re-snapshot.")
                    self.assertIn(
                        "force_resubscribe", body_src,
                        "MainLoop._tick must call force_resubscribe "
                        "as part of the periodic re-snapshot.")
                    return
        self.fail("MainLoop._tick not found")


class TestR1A4UnsubBeforeSubOrdering(unittest.TestCase):
    """R1 / A4 [P0]: in _process_pending_subs, unsubscribes MUST
    drain before subscribes. If subs went first, the timeout
    fallback (which queues a ticker into BOTH unsubs and subs)
    would see subscribe-then-unsubscribe — leaving the ticker
    permanently unsubscribed."""

    def test_ast_unsubs_loop_before_subs_loop(self):
        """Walk _process_pending_subs body and verify the for-loop
        iterating `unsubs` appears textually BEFORE the for-loop
        iterating `subs`."""
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        target = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "KalshiFeed"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.AsyncFunctionDef)
                        and fn.name == "_process_pending_subs"):
                    target = fn
                    break
        self.assertIsNotNone(
            target, "_process_pending_subs not found")
        unsub_idx, sub_idx = None, None
        for i, node in enumerate(target.body):
            if isinstance(node, ast.For):
                # Match `for ticker in unsubs:` and `... in subs:`.
                iter_src = ast.unparse(node.iter)
                if iter_src == "unsubs" and unsub_idx is None:
                    unsub_idx = i
                elif iter_src == "subs" and sub_idx is None:
                    sub_idx = i
        self.assertIsNotNone(
            unsub_idx, "for ticker in unsubs: loop missing")
        self.assertIsNotNone(
            sub_idx, "for ticker in subs: loop missing")
        self.assertLess(
            unsub_idx, sub_idx,
            "R1 / A4 [P0]: `for ticker in unsubs:` must drain "
            "BEFORE `for ticker in subs:`. The timeout fallback "
            "queues a ticker into both lists; if subs run first, "
            "the resubscribe fires before the unsubscribe, leaving "
            "the ticker unsubscribed.")


class TestR1A2PurgeCacheParam(unittest.TestCase):
    """R1 / A2 [P0]: force_resubscribe(purge_cache=False) leaves
    the cached orderbook in place. Periodic insurance uses this
    so the 5-min sweep doesn't simultaneously evict caches for
    every 15M ticker."""

    def setUp(self):
        self.f = _make_feed()
        self.f._subscribed_tickers.add("KXBTC15M-PERIODIC")
        self.f._orderbooks["KXBTC15M-PERIODIC"] = {"yes": [[91, 50]], "no": []}
        self.f._ticker_to_sid["KXBTC15M-PERIODIC"] = 200

    def test_purge_cache_false_keeps_cache(self):
        self.f.force_resubscribe(
            "KXBTC15M-PERIODIC", purge_cache=False)
        self.assertIn(
            "KXBTC15M-PERIODIC", self.f._orderbooks,
            "purge_cache=False MUST leave the orderbook cache in "
            "place. Periodic insurance must not create a system-"
            "wide gap by purging all 15M caches simultaneously.")
        # Snapshot still requested.
        self.assertIn(
            "KXBTC15M-PERIODIC", self.f._pending_snapshot_requests)

    def test_purge_cache_default_true_evicts(self):
        """Default (detected-drift path) still purges."""
        self.f.force_resubscribe("KXBTC15M-PERIODIC")
        self.assertNotIn("KXBTC15M-PERIODIC", self.f._orderbooks)


class TestR1A7BypassCooldown(unittest.TestCase):
    """R1 / A7 [P1]: bypass_cooldown=True ignores the per-ticker
    rate limit. Periodic insurance uses this so a recent
    flag_ticker_drifted firing doesn't starve the 5-min sweep."""

    def setUp(self):
        self.f = _make_feed()
        self.f._subscribed_tickers.add("KXBTC15M-RATE")
        self.f._ticker_to_sid["KXBTC15M-RATE"] = 300

    def test_bypass_cooldown_overrides_rate_limit(self):
        # First call sets cooldown.
        self.f.force_resubscribe("KXBTC15M-RATE")
        self.f._pending_snapshot_requests.clear()
        # Without bypass: would be a no-op (within cooldown).
        self.f.force_resubscribe("KXBTC15M-RATE")
        self.assertNotIn(
            "KXBTC15M-RATE", self.f._pending_snapshot_requests,
            "Sanity: cooldown rate-limit still works without bypass.")
        # WITH bypass: fires regardless.
        self.f.force_resubscribe(
            "KXBTC15M-RATE", bypass_cooldown=True)
        self.assertIn(
            "KXBTC15M-RATE", self.f._pending_snapshot_requests,
            "bypass_cooldown=True MUST override the per-ticker "
            "rate limit (periodic insurance must always run).")


class TestR1A1AutoDisablePrimary(unittest.TestCase):
    """R1 / A1 [P0]: after WS_GET_SNAPSHOT_DISABLE_AFTER consecutive
    timeouts on the get_snapshot path, force_resubscribe stops
    using the primary path and queues unsub+resub directly. One
    LOUD log line on the transition."""

    def setUp(self):
        self.f = _make_feed()
        self.f._subscribed_tickers.add("KXBTC15M-DEAD")
        # Phase 2.6: sid required for force_resubscribe to take any
        # action other than no-op.
        self.f._ticker_to_sid["KXBTC15M-DEAD"] = 500

    def test_single_bad_sweep_does_not_disable(self):
        """R2 / P0-2: a single sweep with multiple ticker timeouts
        must NOT disable the primary path. Only consecutive
        FULLY-FAILED SWEEPS count, not per-ticker timeouts."""
        import bot
        # 4 tickers all timed out in ONE sweep — represents a
        # transient WS hiccup, not a contract failure.
        for i in range(4):
            t = f"KXBTC15M-Q{i}"
            self.f._subscribed_tickers.add(t)
            self.f._snapshot_request_pending[t] = (
                time.monotonic() - bot.WS_SNAPSHOT_REQUEST_TIMEOUT_S - 1.0)
        self.f._check_snapshot_timeouts()
        self.assertEqual(
            self.f._get_snapshot_consecutive_failed_sweeps, 1,
            "One sweep with 4 timeouts must increment counter "
            "by 1 (not 4). Per-ticker count was the R1 bug.")
        self.assertFalse(
            self.f._get_snapshot_disabled,
            "Single bad sweep must NOT disable primary path.")

    def test_disable_after_threshold_consecutive_failed_sweeps(self):
        """R2 / P0-2: only after WS_GET_SNAPSHOT_DISABLE_AFTER
        separate sweeps each producing at least one timeout, with
        zero successful snapshots in between, does the primary
        path auto-disable."""
        import bot
        threshold = bot.WS_GET_SNAPSHOT_DISABLE_AFTER
        for sweep in range(threshold):
            t = f"KXBTC15M-S{sweep}"
            self.f._subscribed_tickers.add(t)
            self.f._snapshot_request_pending[t] = (
                time.monotonic() - bot.WS_SNAPSHOT_REQUEST_TIMEOUT_S - 1.0)
            self.f._check_snapshot_timeouts()
        self.assertTrue(
            self.f._get_snapshot_disabled,
            "After WS_GET_SNAPSHOT_DISABLE_AFTER consecutive "
            "failed sweeps, _get_snapshot_disabled must be True.")

    def test_successful_snapshot_resets_failed_sweep_counter(self):
        # One failed sweep.
        import bot
        self.f._subscribed_tickers.add("KXBTC15M-T0")
        self.f._snapshot_request_pending["KXBTC15M-T0"] = (
            time.monotonic() - bot.WS_SNAPSHOT_REQUEST_TIMEOUT_S - 1.0)
        self.f._check_snapshot_timeouts()
        self.assertEqual(
            self.f._get_snapshot_consecutive_failed_sweeps, 1)
        # Successful snapshot.
        self.f._snapshot_request_pending["KXBTC15M-OK"] = time.monotonic()
        self.f._subscribed_tickers.add("KXBTC15M-OK")
        self.f._handle_ob_snapshot({
            "msg": {
                "market_ticker": "KXBTC15M-OK",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            }
        })
        self.assertEqual(
            self.f._get_snapshot_consecutive_failed_sweeps, 0,
            "Successful snapshot must reset the failed-sweep "
            "counter (proves primary path is healthy).")

    def test_disabled_path_skips_to_unsub_resub(self):
        """When the primary path is disabled, force_resubscribe
        queues directly into _pending_unsubscribes + _pending_subscribes,
        skipping _pending_snapshot_requests entirely."""
        self.f._get_snapshot_disabled = True
        self.f.force_resubscribe("KXBTC15M-DEAD")
        self.assertNotIn(
            "KXBTC15M-DEAD", self.f._pending_snapshot_requests,
            "Disabled-primary path must NOT use get_snapshot.")
        self.assertIn(
            "KXBTC15M-DEAD", self.f._pending_unsubscribes,
            "Disabled-primary path must queue unsubscribe.")
        self.assertIn(
            "KXBTC15M-DEAD", self.f._pending_subscribes,
            "Disabled-primary path must queue resubscribe.")


class TestR1A3LockedSubscribedAccessor(unittest.TestCase):
    """R1 / A3 [P1]: get_subscribed_tickers() returns a thread-safe
    snapshot. Periodic loop in MainLoop reads via this accessor
    instead of touching `_subscribed_tickers` directly."""

    def test_get_subscribed_tickers_returns_list_copy(self):
        f = _make_feed()
        f._subscribed_tickers.update({"A", "B", "C"})
        out = f.get_subscribed_tickers()
        self.assertEqual(set(out), {"A", "B", "C"})
        # Mutating the returned list must NOT affect the underlying set.
        out.append("D")
        self.assertNotIn("D", f._subscribed_tickers)

    def test_main_loop_uses_accessor_not_direct_attr(self):
        """AST: MainLoop._tick references get_subscribed_tickers."""
        with open(BOT_PY) as f:
            src = f.read()
        tree = ast.parse(src)
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "MainLoop"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_tick"):
                    body_src = ast.unparse(fn)
                    self.assertIn(
                        "get_subscribed_tickers", body_src,
                        "MainLoop._tick must call "
                        "get_subscribed_tickers() — direct "
                        "_subscribed_tickers access is racy.")
                    return
        self.fail("MainLoop._tick not found")


class TestR1A5RecoveryWatchdog(unittest.TestCase):
    """R1 / A5 [P1]: after force_resubscribe, if the cache stays
    empty past WS_FORCE_RESUB_RECOVERY_TIMEOUT_S, log a WARNING
    so silent-stuck tickers surface."""

    def setUp(self):
        self.f = _make_feed()
        self.f._subscribed_tickers.add("KXBTC15M-STUCK")
        self.f._ticker_to_sid["KXBTC15M-STUCK"] = 600

    def test_recovery_deadline_set_on_force_resub(self):
        self.f.force_resubscribe("KXBTC15M-STUCK")
        self.assertIn(
            "KXBTC15M-STUCK", self.f._force_resub_recovery_deadline,
            "force_resubscribe must set a recovery deadline so the "
            "watchdog can surface stuck tickers.")

    def test_snapshot_arrival_clears_recovery_deadline(self):
        self.f.force_resubscribe("KXBTC15M-STUCK")
        self.f._handle_ob_snapshot({
            "msg": {
                "market_ticker": "KXBTC15M-STUCK",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            }
        })
        self.assertNotIn(
            "KXBTC15M-STUCK", self.f._force_resub_recovery_deadline,
            "Successful snapshot must clear recovery state.")


class TestR2P01UnsubscribeCleansPhase2State(unittest.TestCase):
    """R2 / P0-1: unsubscribe_ticker MUST clean up all Phase 2
    state for the ticker. Without this, settled-window churn
    poisons the disable counter and emits false WS_RESUB_STUCK
    warnings."""

    def setUp(self):
        self.f = _make_feed()
        self.f._subscribed_tickers.add("KXBTC15M-LIFE")
        self.f._ticker_to_sid["KXBTC15M-LIFE"] = 400

    def test_unsubscribe_clears_snapshot_request_pending(self):
        self.f.force_resubscribe("KXBTC15M-LIFE")
        self.assertIn(
            "KXBTC15M-LIFE", self.f._snapshot_request_pending)
        self.f.unsubscribe_ticker("KXBTC15M-LIFE")
        self.assertNotIn(
            "KXBTC15M-LIFE", self.f._snapshot_request_pending,
            "unsubscribe_ticker must clear _snapshot_request_pending "
            "to prevent stale entries from contributing fake "
            "timeouts to the disable counter.")

    def test_unsubscribe_clears_force_resub_cooldown(self):
        self.f.force_resubscribe("KXBTC15M-LIFE")
        self.assertIn(
            "KXBTC15M-LIFE", self.f._force_resub_cooldown)
        self.f.unsubscribe_ticker("KXBTC15M-LIFE")
        self.assertNotIn(
            "KXBTC15M-LIFE", self.f._force_resub_cooldown,
            "unsubscribe_ticker must clear _force_resub_cooldown "
            "to prevent unbounded dict growth.")

    def test_unsubscribe_clears_recovery_deadline_and_warned(self):
        # Use default purge_cache=True to set the deadline.
        self.f.force_resubscribe("KXBTC15M-LIFE")
        self.assertIn(
            "KXBTC15M-LIFE",
            self.f._force_resub_recovery_deadline)
        self.f._force_resub_recovery_warned["KXBTC15M-LIFE"] = True
        self.f.unsubscribe_ticker("KXBTC15M-LIFE")
        self.assertNotIn(
            "KXBTC15M-LIFE",
            self.f._force_resub_recovery_deadline,
            "Stale recovery_deadline → false WS_RESUB_STUCK warnings "
            "30s after unsubscribe.")
        self.assertNotIn(
            "KXBTC15M-LIFE",
            self.f._force_resub_recovery_warned)

    def test_unsubscribe_clears_pending_snapshot_request_list(self):
        self.f.force_resubscribe("KXBTC15M-LIFE")
        self.assertIn(
            "KXBTC15M-LIFE", self.f._pending_snapshot_requests)
        self.f.unsubscribe_ticker("KXBTC15M-LIFE")
        self.assertNotIn(
            "KXBTC15M-LIFE", self.f._pending_snapshot_requests,
            "Stale entry would otherwise be sent as a "
            "get_snapshot for a dead ticker on next drain.")


class TestR3TrackRecoveryParam(unittest.TestCase):
    """R3 / P0-B + P1-D + P1-F: deadline tracking is decoupled
    from purge_cache. `track_recovery=False` (periodic) skips
    deadline; `track_recovery=True` (drift detector, default)
    always refreshes deadline AND clears any prior warned flag
    so re-detected drift produces a fresh warning."""

    def setUp(self):
        self.f = _make_feed()
        self.f._subscribed_tickers.add("KXBTC15M-DRIFT")
        self.f._ticker_to_sid["KXBTC15M-DRIFT"] = 700

    def test_track_recovery_false_skips_deadline(self):
        """Periodic (track_recovery=False) must NOT set a
        deadline — periodic is not a detected-drift event, so its
        watchdog timer would only generate noise."""
        self.f.force_resubscribe(
            "KXBTC15M-DRIFT",
            purge_cache=False,
            track_recovery=False,
        )
        self.assertNotIn(
            "KXBTC15M-DRIFT",
            self.f._force_resub_recovery_deadline,
            "track_recovery=False (periodic) MUST NOT set a "
            "deadline.")

    def test_track_recovery_true_sets_deadline_even_purge_false(self):
        """R3 / P0-B + P1-D: drift detector now uses purge=False
        BUT must still set deadline. Otherwise drift-detected
        failures are silent (the entire reason A5 watchdog
        exists)."""
        self.f.force_resubscribe(
            "KXBTC15M-DRIFT",
            purge_cache=False,
            track_recovery=True,
        )
        self.assertIn(
            "KXBTC15M-DRIFT",
            self.f._force_resub_recovery_deadline,
            "track_recovery=True MUST set deadline regardless of "
            "purge_cache. Drift was DETECTED — silent failure must "
            "be observable.")

    def test_track_recovery_true_refreshes_deadline_and_clears_warned(self):
        """R3 / P1-F: each tracked call refreshes the deadline AND
        clears the warned flag. Repeated drift firings on a stuck
        ticker should produce repeated warnings, not silence."""
        # First firing.
        self.f.force_resubscribe(
            "KXBTC15M-DRIFT", track_recovery=True)
        first_deadline = (
            self.f._force_resub_recovery_deadline["KXBTC15M-DRIFT"])
        # Simulate "watchdog fired warning."
        self.f._force_resub_recovery_warned["KXBTC15M-DRIFT"] = True
        time.sleep(0.001)  # ensure monotonic clock advances
        # Second firing.
        self.f.force_resubscribe(
            "KXBTC15M-DRIFT",
            track_recovery=True,
            bypass_cooldown=True,
        )
        second_deadline = (
            self.f._force_resub_recovery_deadline["KXBTC15M-DRIFT"])
        self.assertGreater(
            second_deadline, first_deadline,
            "Each tracked force_resubscribe must REFRESH the "
            "deadline so the watchdog measures time since most "
            "recent firing.")
        self.assertNotIn(
            "KXBTC15M-DRIFT",
            self.f._force_resub_recovery_warned,
            "Each tracked force_resubscribe must CLEAR the warned "
            "flag so re-detected drift produces a fresh warning "
            "(prevents silent ongoing degradation).")


class TestR2P13SnapshotForUnsubscribedTicker(unittest.TestCase):
    """R2 / P1-3: _handle_ob_snapshot must drop snapshots for
    tickers that have already been unsubscribed. Otherwise an
    in-flight snapshot leaks a zombie entry into _orderbooks
    forever."""

    def test_snapshot_for_unsubscribed_ticker_dropped(self):
        f = _make_feed()
        # Ticker NOT in _subscribed_tickers (e.g. expired during drain).
        f._handle_ob_snapshot({
            "msg": {
                "market_ticker": "KXBTC15M-ZOMBIE",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            }
        })
        self.assertNotIn(
            "KXBTC15M-ZOMBIE", f._orderbooks,
            "Snapshot for an unsubscribed ticker must NOT write "
            "to _orderbooks (cache leak).")

    def test_snapshot_for_subscribed_ticker_writes(self):
        """Sanity: the guard doesn't break the normal path."""
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-LIVE")
        f._handle_ob_snapshot({
            "msg": {
                "market_ticker": "KXBTC15M-LIVE",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            }
        })
        self.assertIn("KXBTC15M-LIVE", f._orderbooks)


class TestR2P15ResetDisableOnReconnect(unittest.TestCase):
    """R2 / P1-5: WS reconnect must reset _get_snapshot_disabled
    and the failed-sweep counter. Sticky-across-reconnect is a
    bug — fresh session = fresh sids = let primary re-prove."""

    def test_ast_reconnect_resets_disable_state(self):
        with open(BOT_PY) as fh:
            src = fh.read()
        # The reset must happen inside _ws_loop after
        # `self._connected = True`. We verify the lines exist
        # textually inside the KalshiFeed._ws_loop method.
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
            "_get_snapshot_disabled = False", body_src,
            "_ws_loop must reset _get_snapshot_disabled on "
            "reconnect — sticky-across-reconnect is a bug.")
        self.assertIn(
            "_get_snapshot_consecutive_failed_sweeps = 0", body_src,
            "_ws_loop must reset failed-sweep counter on reconnect.")


class TestR2P16DriftDetectorPurgeFalse(unittest.TestCase):
    """R2 / P1-6: flag_ticker_drifted must call force_resubscribe
    with purge_cache=False. Atomic replace via _handle_ob_snapshot
    is strictly better than wipe-then-wait — wipe creates a 7-8s
    `no_orderbook` rejection window for every drift firing."""

    def test_ast_drift_detector_passes_purge_false(self):
        with open(BOT_PY) as fh:
            src = fh.read()
        tree = ast.parse(src)
        target = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OpportunityScanner"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "flag_ticker_drifted"):
                    target = fn
                    break
        self.assertIsNotNone(
            target, "flag_ticker_drifted not found")
        body_src = ast.unparse(target)
        self.assertIn(
            "purge_cache=False", body_src,
            "flag_ticker_drifted must call force_resubscribe with "
            "purge_cache=False. Wipe-then-wait creates a multi-"
            "second `no_orderbook` rejection window during drift "
            "events — exactly when we DON'T want to lose data.")


class TestR4F2WatchdogFiresWithStaleCache(unittest.TestCase):
    """R4 / F2: the recovery watchdog must warn when the deadline
    expires, regardless of whether _orderbooks is populated. The
    drift-detector path (purge_cache=False) keeps the cache
    populated even when no fresh snapshot ever arrives — pre-R4
    the watchdog used `if t not in self._orderbooks`, which made
    drift-detected silent failures truly silent."""

    def test_watchdog_warns_when_cache_populated_but_snapshot_missing(self):
        """purge_cache=False keeps the cache populated. If the
        snapshot doesn't arrive (deadline still in dict past
        expiry), watchdog must still warn — that's the entire
        point of the deadline."""
        import bot
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-DRIFT")
        # Simulate populated stale cache.
        f._orderbooks["KXBTC15M-DRIFT"] = {
            "yes": [[91, 50]], "no": [], "ts": time.time()}
        # Backdate deadline past expiry.
        f._force_resub_recovery_deadline["KXBTC15M-DRIFT"] = (
            time.monotonic() - 1.0)
        # Run the watchdog (it's inline in _check_snapshot_timeouts).
        f._check_snapshot_timeouts()
        self.assertTrue(
            f._force_resub_recovery_warned.get("KXBTC15M-DRIFT"),
            "Watchdog must warn even when _orderbooks contains a "
            "(stale) entry. Pre-R4 it silently skipped, making "
            "drift-detected snapshot failures invisible.")

    def test_watchdog_warning_one_shot_per_stuck_period(self):
        """One warning per stuck period; cleared on next
        force_resubscribe(track_recovery=True) so a refreshed
        deadline can produce a fresh warning."""
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-DRIFT")
        f._orderbooks["KXBTC15M-DRIFT"] = {
            "yes": [], "no": [], "ts": time.time()}
        f._force_resub_recovery_deadline["KXBTC15M-DRIFT"] = (
            time.monotonic() - 1.0)
        # First sweep: warns.
        f._check_snapshot_timeouts()
        self.assertTrue(
            f._force_resub_recovery_warned.get("KXBTC15M-DRIFT"))
        # Re-run — must NOT warn again (one-shot per stuck-period).
        f._force_resub_recovery_warned["KXBTC15M-DRIFT"] = True
        # Second sweep: still inside the same stuck period.
        # The warned flag is already True, so no re-warn.
        f._check_snapshot_timeouts()
        # Verify deadline still set (i.e. watchdog didn't pop it).
        self.assertIn(
            "KXBTC15M-DRIFT",
            f._force_resub_recovery_deadline,
            "Deadline should NOT be popped by the watchdog — only "
            "by snapshot arrival or by a new force_resubscribe.")

    def test_snapshot_arrival_clears_both_deadline_and_warned(self):
        """Recovery signal: snapshot arrives → deadline popped →
        warned flag cleared. Next stuck-period gets fresh warning."""
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-RECOVER")
        f._force_resub_recovery_deadline["KXBTC15M-RECOVER"] = (
            time.monotonic() + 60.0)
        f._force_resub_recovery_warned["KXBTC15M-RECOVER"] = True
        f._handle_ob_snapshot({
            "msg": {
                "market_ticker": "KXBTC15M-RECOVER",
                "yes_dollars_fp": [],
                "no_dollars_fp": [],
            }
        })
        self.assertNotIn(
            "KXBTC15M-RECOVER",
            f._force_resub_recovery_deadline,
            "Snapshot arrival must pop the deadline (recovery).")
        self.assertNotIn(
            "KXBTC15M-RECOVER",
            f._force_resub_recovery_warned,
            "Snapshot arrival must clear the warned flag so the "
            "next stuck-period emits a fresh warning.")


class TestR3P0AAndR4F1SessionCleanup(unittest.TestCase):
    """R3 / P0-A + R4 / F1: WS-session-bound state must be
    cleared on BOTH the exception path (before backoff sleep —
    so is_connected reads False during reconnect wait) AND the
    graceful-close path (silence watchdog ws.close(), server
    close). _cleanup_session_state() is the helper; it must be
    invoked from both branches."""

    def test_ast_cleanup_helper_exists_and_clears_orderbooks(self):
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
                        and fn.name == "_cleanup_session_state"):
                    target = fn
                    break
        self.assertIsNotNone(
            target,
            "_cleanup_session_state helper must exist — it's the "
            "single source of truth for WS-session-bound cleanup.")
        body_src = ast.unparse(target)
        self.assertIn(
            "_orderbooks.clear()", body_src,
            "Cleanup helper must clear _orderbooks (the entire "
            "Phase 2 raison d'être is preventing stale-cache "
            "leakage across sessions).")
        self.assertIn(
            "self._connected = False", body_src,
            "Cleanup helper must reset _connected so is_connected "
            "reads False during reconnect wait.")

    def test_ast_ws_loop_invokes_cleanup_in_except_before_sleep(self):
        """R4 / F1: cleanup must fire BEFORE the backoff sleep,
        otherwise during the up-to-60s wait, is_connected returns
        True and stale cache from the prior session is served."""
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
        # Walk the inner Try with multiple handlers; find the
        # generic Exception handler and verify _cleanup_session_state
        # appears textually BEFORE asyncio.wait_for/sleep.
        for node in ast.walk(target):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                if (handler.type is not None
                        and isinstance(handler.type, ast.Name)
                        and handler.type.id == "Exception"):
                    handler_src = ast.unparse(handler)
                    cleanup_idx = handler_src.find(
                        "_cleanup_session_state")
                    sleep_idx = handler_src.find("asyncio.wait_for")
                    if cleanup_idx == -1 or sleep_idx == -1:
                        continue
                    self.assertLess(
                        cleanup_idx, sleep_idx,
                        "_cleanup_session_state MUST be called "
                        "BEFORE the backoff sleep in the except "
                        "Exception block. R3 mistakenly moved "
                        "cleanup to a `finally:` that ran AFTER "
                        "the sleep — during which is_connected "
                        "returned True and stale cache was served.")
                    return
        self.fail(
            "Could not locate the except Exception block calling "
            "_cleanup_session_state in _ws_loop.")

    def test_ast_ws_loop_invokes_cleanup_on_graceful_close(self):
        """Graceful-close path (async-with normal exit) must also
        clear cache. Either via `else:` clause on the try, or
        equivalent placement after the try."""
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
        # We require AT LEAST 2 invocations of
        # _cleanup_session_state inside _ws_loop (except path +
        # graceful path). CancelledError handler also calls it
        # but we only require 2 minimum.
        invocations = body_src.count(
            "self._cleanup_session_state()")
        self.assertGreaterEqual(
            invocations, 2,
            "_ws_loop must invoke _cleanup_session_state from "
            "BOTH the exception path AND the graceful-close path. "
            "Found %d invocations." % invocations)


class TestR3P1ABReconnectClearsPhase2Dicts(unittest.TestCase):
    """R3 / P1-A + P1-B: WS reconnect must clear stale Phase 2
    pending state. Otherwise old-session entries time out 5s into
    new session, falsely incrementing the failed-sweep counter,
    and old recovery deadlines fire spurious warnings."""

    def test_ast_reconnect_block_clears_pending_dicts(self):
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
        for needle in [
            "_snapshot_request_pending.clear()",
            "_pending_snapshot_requests.clear()",
            "_force_resub_recovery_deadline.clear()",
            "_force_resub_recovery_warned.clear()",
        ]:
            self.assertIn(
                needle, body_src,
                f"_ws_loop reconnect block must clear `{needle}` "
                "to prevent stale Phase 2 state from poisoning the "
                "new session.")


class TestR3P1CSendFailurePopsPending(unittest.TestCase):
    """R3 / P1-C: when _send_ob_get_snapshot raises, the pending
    tracker must be popped — otherwise transport-layer failures
    inflate the consecutive-failed-sweep counter and trip the
    disable for non-Kalshi-contract reasons."""

    def test_ast_snap_req_send_failure_pops_pending(self):
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
                        and fn.name == "_process_pending_subs"):
                    target = fn
                    break
        self.assertIsNotNone(
            target, "_process_pending_subs not found")
        body_src = ast.unparse(target)
        # The except branch around the `_send_ob_get_snapshot`
        # call must pop _snapshot_request_pending so the timeout
        # doesn't fire for a request that never went out.
        self.assertIn(
            "_snapshot_request_pending.pop(ticker, None)",
            body_src,
            "Send-failure handler for _send_ob_get_snapshot must "
            "pop the pending tracker — otherwise transport "
            "failures get counted as Kalshi-contract failures.")


class TestR3P1EDeltaZombieGuard(unittest.TestCase):
    """R3 / P1-E: _apply_fp_delta and _apply_legacy_delta must
    drop deltas for unsubscribed tickers. Without this, an
    in-flight delta from a prior subscription creates a NEW
    _orderbooks entry for an unsubscribed ticker, leaking zombie
    state forever. Deltas have a much larger race window than
    snapshots (they arrive constantly)."""

    def test_apply_fp_delta_drops_unsubscribed_ticker(self):
        f = _make_feed()
        # Ticker NOT in _subscribed_tickers.
        f._apply_fp_delta("KXBTC15M-ZOMBIE", {
            "side": "yes",
            "price_dollars": "0.95",
            "delta_fp": "10",
        })
        self.assertNotIn(
            "KXBTC15M-ZOMBIE", f._orderbooks,
            "Delta for unsubscribed ticker must NOT create an "
            "_orderbooks entry (zombie cache leak).")

    def test_apply_legacy_delta_drops_unsubscribed_ticker(self):
        f = _make_feed()
        f._apply_legacy_delta("KXBTC15M-ZOMBIE", {
            "yes": [[95, 50]],
        })
        self.assertNotIn(
            "KXBTC15M-ZOMBIE", f._orderbooks,
            "Legacy delta for unsubscribed ticker must NOT create "
            "an _orderbooks entry.")

    def test_apply_fp_delta_subscribed_ticker_works(self):
        """Sanity: guard doesn't break the normal path."""
        f = _make_feed()
        f._subscribed_tickers.add("KXBTC15M-LIVE")
        f._apply_fp_delta("KXBTC15M-LIVE", {
            "side": "yes",
            "price_dollars": "0.95",
            "delta_fp": "10",
        })
        self.assertIn("KXBTC15M-LIVE", f._orderbooks)
        ob = f._orderbooks["KXBTC15M-LIVE"]
        self.assertEqual(ob["yes"], [[95, 10]])


if __name__ == "__main__":
    unittest.main()
