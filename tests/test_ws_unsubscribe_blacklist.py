"""Post-unsubscribe blacklist — kills the window-rotation rapid resubscribe loop.

Apr 26 2026 incident (kb/failures/scan-loop-stall-window-rotation-2026-04-26.md):
at the 09:30 UTC 15M settlement boundary, the bot looped subscribe → delete →
subscribe → delete on KXSOL15M-26APR260530-30 every second for 7 seconds
(WS_RAW_OUT ids 260-273), then the entire 15M scan path went dark for 10
minutes while snapshots failed to apply. Productive watchdog R1 fired at
09:30:10; silence watchdog SILENT fired at 09:40:46.

Race mechanism (evidence-backed):
  1. Worker thread updates `_active_windows` to NEW (0545) windows.
  2. Worker calls `discovery_ob_subscribe`: unsubscribes OLD (0530) tickers
     via `unsubscribe_ticker(t)` — which removes them from
     `_subscribed_tickers` AND pops `_force_resub_cooldown[t]` (line 4812).
  3. Main thread is mid-scan with a STALE `_local_windows` snapshot still
     containing the OLD tickers. `_get_orderbook_cached(OLD_t)` calls
     `subscribe_ticker(OLD_t)` (line 15808/15812) — re-adding to
     `_subscribed_tickers` and queueing a fresh subscribe. The cooldown
     was popped, so the rate-limit doesn't catch this re-add.
  4. Repeat per scan tick → 1 Hz loop until `_local_windows` reflects NEW.

Fix: a per-ticker post-unsubscribe blacklist. `unsubscribe_ticker(t)` records
`t -> monotonic() + UNSUBSCRIBE_BLACKLIST_S`. Both `subscribe_ticker` and
`force_resubscribe` consult this map and silent-skip while the entry is
fresh. Held-position tickers are never unsubscribed in the first place
(existing `_held_tickers` exclusion in `discovery_ob_subscribe`), so the
blacklist cannot starve a position. TTL of 30s is well below the 15-min
window lifecycle so legitimate next-window re-subscriptions aren't blocked.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock
import bot.feeds  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_feed():
    """Mirror of test_ws_force_resubscribe._make_feed — minimal KalshiFeed
    with attrs the subscribe/unsubscribe paths need. Kept local so future
    blacklist additions land in lockstep with the SUT."""
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
    f._ws_connect_ts = 0.0
    f._snapshot_schema_probed = True
    f._delta_probe_count = 0
    f._delta_probe_max = 0
    import threading
    f._lock = threading.Lock()
    return f


class TestUnsubscribeBlacklistBlocksSubscribe(unittest.TestCase):
    """`subscribe_ticker(t)` must silent-skip while `t` is on the
    post-unsubscribe blacklist."""

    def setUp(self):
        self.f = _make_feed()
        self.t = "KXSOL15M-26APR260530-30"
        self.f._subscribed_tickers.add(self.t)

    def test_subscribe_within_grace_is_skipped(self):
        """Worker unsubscribes ticker (window settled). Main-thread
        lazy `_get_orderbook_cached` calls `subscribe_ticker` with the
        SAME ticker (stale `_local_windows`). The blacklist must
        prevent re-add."""
        self.f.unsubscribe_ticker(self.t)
        # _pending_unsubscribes contains the ticker (queued for drain).
        self.assertIn(self.t, self.f._pending_unsubscribes)
        # _subscribed_tickers no longer contains it.
        self.assertNotIn(self.t, self.f._subscribed_tickers)
        # Now the lazy re-subscribe attempt:
        self.f.subscribe_ticker(self.t)
        # The blacklist must reject it: still NOT in subscribed,
        # no fresh subscribe queued.
        self.assertNotIn(
            self.t, self.f._subscribed_tickers,
            "subscribe_ticker within grace must NOT re-add the "
            "ticker to _subscribed_tickers (kills the loop where "
            "scan body's lazy re-subscribe undoes worker cleanup)")
        self.assertNotIn(
            self.t, self.f._pending_subscribes,
            "subscribe_ticker within grace must NOT queue a fresh "
            "subscribe to Kalshi")

    def test_subscribe_after_grace_is_allowed(self):
        """Once grace expires, subscribe_ticker resumes normal
        behavior. Use synthetic monotonic-ts manipulation to skip the
        TTL wait (real-time sleep would flake CI)."""
        import bot
        self.f.unsubscribe_ticker(self.t)
        # Force the blacklist entry into the past.
        for k in list(self.f._unsubscribe_blacklist.keys()):
            self.f._unsubscribe_blacklist[k] = (
                time.monotonic() - 1.0)
        self.f.subscribe_ticker(self.t)
        self.assertIn(
            self.t, self.f._subscribed_tickers,
            "subscribe_ticker after blacklist expiry must re-add "
            "the ticker normally")
        self.assertIn(
            self.t, self.f._pending_subscribes,
            "subscribe_ticker after blacklist expiry must queue "
            "the subscribe to Kalshi")

    def test_blacklist_entry_set_on_unsubscribe(self):
        """`unsubscribe_ticker` must record the blacklist entry —
        the contract this test pins. Defensive against a future
        commit dropping the side-effect."""
        self.f.unsubscribe_ticker(self.t)
        self.assertIn(
            self.t, self.f._unsubscribe_blacklist,
            "unsubscribe_ticker must add `t` to "
            "_unsubscribe_blacklist with a future unblock_ts")
        unblock = self.f._unsubscribe_blacklist[self.t]
        # Must be in the future relative to monotonic now.
        self.assertGreater(unblock, time.monotonic())
        # And not absurdly far (sanity — under 5 min).
        self.assertLess(unblock, time.monotonic() + 300.0)


class TestUnsubscribeBlacklistBlocksForceResubscribe(unittest.TestCase):
    """`force_resubscribe(t)` must silent-skip while `t` is blacklisted.
    Otherwise the R1 productive-recovery path re-adds the OLD ticker
    immediately after the worker unsubscribes — exact incident shape."""

    def setUp(self):
        self.f = _make_feed()
        self.t = "KXSOL15M-26APR260530-30"
        self.f._subscribed_tickers.add(self.t)
        self.f._ticker_to_sid[self.t] = 200

    def test_force_resub_within_grace_is_skipped(self):
        """Defense-in-depth test. Setup:
            1. unsubscribe_ticker (blacklist + remove from subscribed)
            2. SIMULATE A BYPASS PATH: directly re-add ticker to
               `_subscribed_tickers` (no real code path does this
               today, but this verifies the blacklist gate in
               force_resubscribe holds even if a future commit
               introduces such a path).
            3. force_resubscribe — must silent-skip via blacklist.
        Without manual step 2, the existing `if t not in
        _subscribed_tickers: return` guard would handle this case
        anyway; the test specifically verifies the new blacklist
        layer adds defense regardless of subscribed-state state."""
        self.f.unsubscribe_ticker(self.t)
        # Simulate a defect: ticker gets re-added to _subscribed_tickers
        # via some path that does NOT consult the blacklist. The
        # blacklist on force_resubscribe is the secondary defense.
        self.f._subscribed_tickers.add(self.t)
        self.f.force_resubscribe(
            self.t, purge_cache=True, track_recovery=True)
        # The primary guard (existing 'not in _subscribed_tickers')
        # would NOT block this call (we just re-added). The blacklist
        # is what stops it.
        self.assertNotIn(
            self.t, self.f._pending_subscribes,
            "force_resubscribe within blacklist grace must NOT "
            "queue a subscribe (defense-in-depth: even if "
            "_subscribed_tickers is dirty)")
        self.assertNotIn(
            self.t, self.f._pending_snapshot_requests,
            "force_resubscribe within blacklist grace must NOT "
            "queue a snapshot request")

    def test_force_resub_after_grace_is_allowed(self):
        self.f.unsubscribe_ticker(self.t)
        # Force expiry of blacklist + re-add to _subscribed_tickers
        # (e.g., the ticker came back as a NEW window or is held
        # via a position).
        for k in list(self.f._unsubscribe_blacklist.keys()):
            self.f._unsubscribe_blacklist[k] = (
                time.monotonic() - 1.0)
        self.f._subscribed_tickers.add(self.t)
        self.f.force_resubscribe(
            self.t, purge_cache=True, track_recovery=True)
        # Primary path queued the snapshot request (sid_known=True).
        self.assertIn(
            self.t, self.f._pending_snapshot_requests,
            "force_resubscribe after blacklist expiry resumes "
            "normal primary path")


class TestRapidResubscribeLoopRegression(unittest.TestCase):
    """End-to-end regression for the 09:30:11-09:30:19 WS_RAW_OUT
    rapid loop. Simulates the FULL race shape from the Apr 26 PM:
    worker unsubscribes once, then both lazy `subscribe_ticker`
    (from `_get_orderbook_cached` in scan body) AND R1 watchdog
    `force_resubscribe` (from `_check_scan_productive_15m`) fire
    repeatedly. Pre-fix, this produced subscribe→delete→subscribe
    →delete every 1Hz. Post-fix, all calls are suppressed."""

    def test_lazy_resubscribe_loop_blocked(self):
        """7 lazy subscribe_ticker calls (the scan-body
        `_get_orderbook_cached` path) post-unsubscribe → 0 fresh
        subscribes queued."""
        f = _make_feed()
        t = "KXSOL15M-26APR260530-30"
        f._subscribed_tickers.add(t)
        f.unsubscribe_ticker(t)
        for _ in range(7):
            f.subscribe_ticker(t)
        unsub_count = sum(1 for x in f._pending_unsubscribes if x == t)
        sub_count = sum(1 for x in f._pending_subscribes if x == t)
        self.assertEqual(unsub_count, 1)
        self.assertEqual(
            sub_count, 0,
            "Pre-fix: 7 enqueues here drove the WS_RAW_OUT loop")

    def test_force_resub_loop_blocked(self):
        """R1 watchdog calls force_resubscribe with stale active_windows.
        After unsubscribe, those calls must NOT queue any work — even
        if some other path puts the ticker back in _subscribed_tickers.
        Defense-in-depth at the WS layer."""
        f = _make_feed()
        t = "KXSOL15M-26APR260530-30"
        f._subscribed_tickers.add(t)
        f._ticker_to_sid[t] = 123
        f.unsubscribe_ticker(t)
        # Simulate concurrent re-add via some other path bypassing
        # blacklist (e.g., a future code change). Blacklist on
        # force_resubscribe is the secondary defense.
        f._subscribed_tickers.add(t)
        for _ in range(5):
            f.force_resubscribe(t, purge_cache=True, track_recovery=True)
        snap_count = sum(
            1 for x in f._pending_snapshot_requests if x == t)
        unsub_count = sum(1 for x in f._pending_unsubscribes if x == t)
        sub_count = sum(1 for x in f._pending_subscribes if x == t)
        self.assertEqual(
            snap_count, 0,
            "force_resubscribe within grace must NOT queue snapshot "
            "requests")
        # The original unsubscribe queued one delete; any beyond that
        # would mean blacklist failed.
        self.assertEqual(unsub_count, 1)
        self.assertEqual(sub_count, 0)

    def test_combined_lazy_and_force_resub_loop_blocked(self):
        """The exact incident shape: lazy subscribe_ticker AND
        force_resubscribe both fire post-unsubscribe across multiple
        ticks. Pre-fix produced subscribe → delete → subscribe →
        delete at 1Hz. Post-fix: 1 unsubscribe, 0 of anything else."""
        f = _make_feed()
        t = "KXSOL15M-26APR260530-30"
        f._subscribed_tickers.add(t)
        f._ticker_to_sid[t] = 123
        # Worker cleanup.
        f.unsubscribe_ticker(t)
        # 7 ticks of mixed lazy + force_resub (mimics scan body +
        # R1 watchdog interleaving).
        for _ in range(7):
            f.subscribe_ticker(t)
            # Simulate concurrent re-add (the scenario force_resub's
            # blacklist guards against — a bypass path landing
            # the ticker back in _subscribed_tickers).
            f._subscribed_tickers.add(t)
            f.force_resubscribe(t, purge_cache=True)
        unsub_count = sum(1 for x in f._pending_unsubscribes if x == t)
        sub_count = sum(1 for x in f._pending_subscribes if x == t)
        snap_count = sum(
            1 for x in f._pending_snapshot_requests if x == t)
        self.assertEqual(unsub_count, 1, "exactly 1 unsubscribe")
        self.assertEqual(sub_count, 0, "0 subscribes within grace")
        self.assertEqual(snap_count, 0, "0 snapshots within grace")


class TestBlacklistDoesNotStarveNewWindow(unittest.TestCase):
    """Critical safety: blacklisting OLD ticker `KXSOL15M-26APR260530-30`
    must NOT block subscription to NEW ticker `KXSOL15M-26APR260545-45`.
    They are distinct strings; blacklist is per-ticker."""

    def test_new_ticker_subscribes_normally(self):
        f = _make_feed()
        old_t = "KXSOL15M-26APR260530-30"
        new_t = "KXSOL15M-26APR260545-45"
        f._subscribed_tickers.add(old_t)
        f.unsubscribe_ticker(old_t)
        # Worker subscribes new ticker immediately after.
        f.subscribe_ticker(new_t)
        self.assertIn(
            new_t, f._subscribed_tickers,
            "NEW window's ticker must subscribe normally — "
            "blacklist is per-ticker, not per-asset")
        self.assertIn(
            new_t, f._pending_subscribes,
            "NEW window's subscribe must be queued to Kalshi")


class TestBlacklistTTLExpiry(unittest.TestCase):
    """Blacklist entries must expire and not accumulate. Long-running
    bot must not grow `_unsubscribe_blacklist` unboundedly. R1 [A4]:
    settled 15M tickers (~384/day) and weather/SPX/sports tickers are
    unique strings — without sweeping, they accumulate forever after
    settlement.
    """

    def test_expired_entries_pruned_on_subscribe(self):
        f = _make_feed()
        t = "KXSOL15M-26APR260530-30"
        f._subscribed_tickers.add(t)
        f.unsubscribe_ticker(t)
        self.assertIn(t, f._unsubscribe_blacklist)
        # Force expiry.
        f._unsubscribe_blacklist[t] = time.monotonic() - 1.0
        f.subscribe_ticker(t)
        self.assertNotIn(t, f._unsubscribe_blacklist)

    def test_sweep_removes_expired_for_OTHER_tickers(self):
        """R1 [A4] memory-leak fix: sweep prunes ALL expired entries,
        not just the requested ticker. This bounds dict size at the
        active universe (tickers unsubscribed in the last 30s),
        not the historical universe (every ticker ever unsubscribed)."""
        f = _make_feed()
        # Seed blacklist with 100 expired entries from settled
        # tickers across many days (the R1 [A4] failure mode —
        # forever-dead tickers accumulating).
        past = time.monotonic() - 100.0
        for i in range(100):
            f._unsubscribe_blacklist[f"KXBTC15M-26APR{i:02d}-00"] = past
        self.assertEqual(len(f._unsubscribe_blacklist), 100)
        # ANY subscribe call must sweep them all. Use a ticker NOT
        # already in _subscribed_tickers so we can verify the
        # subscribe was actually applied (post-sweep ordering).
        new_t = "KXSOL15M-26APR270000-00"
        f.subscribe_ticker(new_t)
        self.assertEqual(
            len(f._unsubscribe_blacklist), 0,
            "subscribe_ticker must sweep ALL expired blacklist "
            "entries, not just the requested ticker — otherwise "
            "settled-ticker strings (unique forever) accumulate "
            "unbounded over the bot's lifetime (R1 [A4])")
        self.assertIn(
            new_t, f._subscribed_tickers,
            "After sweeping expired entries, the new (non-blacklisted) "
            "subscribe must succeed normally")
        self.assertIn(
            new_t, f._pending_subscribes,
            "Post-sweep subscribe must queue the subscribe to Kalshi")

    def test_sweep_runs_on_force_resubscribe(self):
        """R1 [A6] test gap: force_resubscribe must also sweep.
        If a future refactor moves the sweep call out of
        force_resubscribe, this test fails."""
        f = _make_feed()
        past = time.monotonic() - 100.0
        for i in range(50):
            f._unsubscribe_blacklist[f"X-{i}"] = past
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f._ticker_to_sid["KXBTC15M-FOO"] = 1
        f.force_resubscribe("KXBTC15M-FOO")
        self.assertEqual(
            len(f._unsubscribe_blacklist), 0,
            "force_resubscribe must also prune expired entries")

    def test_sweep_runs_on_unsubscribe(self):
        """Unsubscribe is the path that ADDS entries; it should also
        prune expired ones in the same call so a long quiet period
        followed by a settlement doesn't lazy-add to a growing dict."""
        f = _make_feed()
        past = time.monotonic() - 100.0
        for i in range(50):
            f._unsubscribe_blacklist[f"X-{i}"] = past
        f._subscribed_tickers.add("KXBTC15M-FOO")
        f.unsubscribe_ticker("KXBTC15M-FOO")
        # All expired entries gone; only the new entry remains.
        self.assertEqual(
            list(f._unsubscribe_blacklist.keys()),
            ["KXBTC15M-FOO"],
            "unsubscribe_ticker must sweep before adding the new "
            "entry, leaving only the fresh entry in the dict")


if __name__ == "__main__":
    unittest.main()
