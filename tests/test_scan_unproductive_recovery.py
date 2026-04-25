"""Phase 3 — scan-productive watchdog auto-recovery.

Detection layer (`_check_scan_productive_15m`) already exists and
fires a Telegram alert at 5 consecutive unproductive ticks. Phase
3 adds the recovery actions:

  R1 (5+ ticks ~2.5 min): force_resubscribe(t, purge_cache=True,
      track_recovery=True) on every active 15M ticker. Throttled
      to once per 60s to avoid hammering the WS during a stuck
      burst. Phase 2 infra — surgical cache reset.

  R2 (10+ ticks ~5 min): set _force_reconnect_requested=True so
      the silence watchdog forces a fresh WS session. One-shot
      per stuck-period.

Reset on first productive tick: counter, throttle, R2-fired flag.

This is the auto-recovery layer. Phase 4 (systemd restart) is the
final escalation if Phase 3 itself can't recover.
"""

import datetime
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _make_main_loop():
    """Minimal OpportunityScanner fixture (the watchdog lives
    there, not on MainLoop)."""
    import bot
    sc = bot.OpportunityScanner.__new__(bot.OpportunityScanner)
    sc._scan_15m_process_start_ts = (
        time.time() - 10 * 60)  # uptime 10 min — past the 7 min gate
    sc._scan_15m_iter_heartbeat_ts = 0.0
    sc._scan_15m_unproductive_count = 0
    # Phase 3 new fields:
    sc._scan_15m_last_recovery_ts = 0.0
    sc._scan_15m_reconnect_triggered = False

    fake_state = MagicMock()
    fake_row = MagicMock()
    fake_row.__getitem__ = MagicMock(return_value=0)
    fake_state.conn.execute = MagicMock(
        return_value=MagicMock(fetchone=MagicMock(return_value=fake_row)))
    sc._state = fake_state

    fake_kf = MagicMock()
    fake_kf.is_connected = True
    fake_kf._force_reconnect_requested = False
    fake_kf._fake_resubbed = []

    def _force_resub(t, *, purge_cache=True, bypass_cooldown=False,
                    track_recovery=True):
        fake_kf._fake_resubbed.append({
            "ticker": t, "purge_cache": purge_cache,
            "bypass_cooldown": bypass_cooldown,
            "track_recovery": track_recovery,
        })

    fake_kf.force_resubscribe = _force_resub
    sc._kalshi_feed = fake_kf
    sc.kalshi_feed = fake_kf

    return sc, fake_kf


def _tick_ts() -> str:
    """Generate a tick_start_ts in the format _check expects."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _active_windows_with_n_15m(n: int):
    """Build active_windows with N 15m markets."""
    return [{
        "product_type": "15m",
        "markets": [{"ticker": f"KX15M-T{i}"} for i in range(n)],
    }]


# ─────────────────────────────────────────────────────────────────────────────
# R1: force_resubscribe at threshold
# ─────────────────────────────────────────────────────────────────────────────

class TestR1ForceResubscribeAt5Ticks(unittest.TestCase):
    """At 5 consecutive unproductive ticks, force_resubscribe each
    active 15M ticker with purge_cache=True (real cache reset)
    and track_recovery=True (B2 watchdog visibility)."""

    def test_resubscribes_all_15m_tickers_at_threshold(self):
        ml, kf = _make_main_loop()
        active = _active_windows_with_n_15m(4)
        # Backdate counter so this tick crosses threshold (5).
        ml._scan_15m_unproductive_count = 4
        ml._check_scan_productive_15m(active, _tick_ts())
        # Should have fired force_resubscribe on all 4 tickers.
        self.assertEqual(
            len(kf._fake_resubbed), 4,
            "At 5 consecutive unproductive ticks, MUST "
            "force_resubscribe all active 15M tickers.")
        for entry in kf._fake_resubbed:
            self.assertTrue(
                entry["purge_cache"],
                "Recovery resub MUST purge cache (real reset, "
                "not periodic insurance).")
            self.assertTrue(
                entry["track_recovery"],
                "Recovery resub MUST track_recovery so B2 "
                "watchdog can surface stuck tickers.")

    def test_no_resubscribe_below_threshold(self):
        ml, kf = _make_main_loop()
        active = _active_windows_with_n_15m(4)
        ml._scan_15m_unproductive_count = 3
        ml._check_scan_productive_15m(active, _tick_ts())
        # Counter increments to 4 — below threshold of 5.
        self.assertEqual(len(kf._fake_resubbed), 0)


class TestR1ThrottleRefires(unittest.TestCase):
    """Throttle: once force_resubscribe fires, don't re-fire for
    60s even if scan stays unproductive. Otherwise we hammer the
    WS during a stuck burst."""

    def test_recovery_throttled_within_60s(self):
        ml, kf = _make_main_loop()
        active = _active_windows_with_n_15m(4)
        # First fire at threshold.
        ml._scan_15m_unproductive_count = 4
        ml._check_scan_productive_15m(active, _tick_ts())
        first_count = len(kf._fake_resubbed)
        self.assertEqual(first_count, 4)
        # Immediate next tick (within 60s throttle window) — should NOT re-fire.
        ml._scan_15m_unproductive_count = 5  # already past threshold
        ml._check_scan_productive_15m(active, _tick_ts())
        self.assertEqual(
            len(kf._fake_resubbed), first_count,
            "Recovery MUST NOT re-fire within 60s throttle.")

    def test_recovery_refires_after_throttle_expires(self):
        ml, kf = _make_main_loop()
        active = _active_windows_with_n_15m(4)
        # Set last-recovery timestamp to >60s ago.
        ml._scan_15m_last_recovery_ts = time.time() - 65.0
        ml._scan_15m_unproductive_count = 5  # past threshold
        ml._check_scan_productive_15m(active, _tick_ts())
        self.assertEqual(
            len(kf._fake_resubbed), 4,
            "After 60s throttle expires, recovery MUST re-fire.")


# ─────────────────────────────────────────────────────────────────────────────
# R2: force WS reconnect at higher threshold
# ─────────────────────────────────────────────────────────────────────────────

class TestR2ForceReconnectAt10Ticks(unittest.TestCase):
    """At 10 consecutive unproductive ticks, set
    _force_reconnect_requested=True. The silence watchdog (Phase
    2) observes the flag and triggers a fresh WS session."""

    def test_force_reconnect_at_10_ticks(self):
        ml, kf = _make_main_loop()
        active = _active_windows_with_n_15m(4)
        ml._scan_15m_unproductive_count = 9
        ml._check_scan_productive_15m(active, _tick_ts())
        self.assertTrue(
            kf._force_reconnect_requested,
            "At 10+ consecutive unproductive ticks, "
            "_force_reconnect_requested MUST be set so silence "
            "watchdog triggers fresh WS session.")
        self.assertTrue(
            ml._scan_15m_reconnect_triggered,
            "One-shot flag prevents repeat reconnect requests "
            "until next stuck period.")

    def test_no_reconnect_below_10_ticks(self):
        ml, kf = _make_main_loop()
        active = _active_windows_with_n_15m(4)
        ml._scan_15m_unproductive_count = 8
        ml._check_scan_productive_15m(active, _tick_ts())
        self.assertFalse(
            kf._force_reconnect_requested,
            "Below 10 ticks, must NOT request reconnect "
            "(let R1 force_resubscribe try first).")

    def test_reconnect_one_shot_per_stuck_period(self):
        """Repeat ticks past 10 do NOT keep setting the flag.
        Once requested, wait for the recovery loop / next stuck."""
        ml, kf = _make_main_loop()
        active = _active_windows_with_n_15m(4)
        ml._scan_15m_unproductive_count = 9
        ml._check_scan_productive_15m(active, _tick_ts())
        # Reset the flag (simulating silence watchdog consumed it).
        kf._force_reconnect_requested = False
        # Tick again past threshold.
        ml._scan_15m_unproductive_count = 10
        ml._check_scan_productive_15m(active, _tick_ts())
        # Reconnect should NOT be re-requested (one-shot flag).
        self.assertFalse(
            kf._force_reconnect_requested,
            "Reconnect is one-shot per stuck period — must not "
            "re-request until next productive→unproductive cycle.")


# ─────────────────────────────────────────────────────────────────────────────
# Reset on productive tick
# ─────────────────────────────────────────────────────────────────────────────

class TestResetOnProductiveTick(unittest.TestCase):
    """When a productive tick occurs (heartbeat_recent or
    rows_written > 0), all Phase 3 state MUST reset: counter,
    throttle, reconnect flag."""

    def test_productive_tick_resets_all_state(self):
        ml, kf = _make_main_loop()
        active = _active_windows_with_n_15m(4)
        # Simulate: stuck for a while, then heartbeat updates.
        ml._scan_15m_unproductive_count = 8
        ml._scan_15m_last_recovery_ts = time.time()
        ml._scan_15m_reconnect_triggered = True
        # Set heartbeat to "right now" so it's > tick_start_ts.
        ml._scan_15m_iter_heartbeat_ts = time.time() + 1
        ml._check_scan_productive_15m(active, _tick_ts())
        self.assertEqual(
            ml._scan_15m_unproductive_count, 0,
            "Productive tick MUST reset counter.")
        self.assertEqual(
            ml._scan_15m_last_recovery_ts, 0.0,
            "Productive tick MUST reset throttle so next stuck "
            "period gets fresh recovery cadence.")
        self.assertFalse(
            ml._scan_15m_reconnect_triggered,
            "Productive tick MUST reset one-shot flag so next "
            "stuck period can re-request reconnect.")


class TestR2A6PostReconnectNormalOperation(unittest.TestCase):
    """R2 / A6: after a disconnect→reconnect transition, normal
    operation must resume — counter walks back up on continued
    unproductive ticks, R1 fires at 5, R2 fires at 10. Verifies
    the transition reset doesn't break the post-recovery cadence."""

    def test_post_reconnect_walks_back_up_and_fires_r1_r2(self):
        sc, kf = _make_main_loop()
        active = _active_windows_with_n_15m(4)
        # Disconnect for a while.
        kf.is_connected = False
        for _ in range(15):
            sc._check_scan_productive_15m(active, _tick_ts())
        # Reconnect — transition resets counter.
        kf.is_connected = True
        sc._check_scan_productive_15m(active, _tick_ts())
        # Now scan stays unproductive. Counter walks back up.
        # 4 more ticks → counter at 5 → R1 fires.
        for _ in range(4):
            sc._check_scan_productive_15m(active, _tick_ts())
        self.assertEqual(
            len(kf._fake_resubbed), 4,
            "Post-reconnect: counter walks back up; R1 fires "
            "at threshold of 5 unproductive ticks. Transition "
            "reset must not break normal recovery cadence.")
        # Backdate throttle so R1 can re-fire.
        sc._scan_15m_last_recovery_ts = time.time() - 65.0
        # 5 more ticks → counter at 10 → R2 fires.
        for _ in range(5):
            sc._check_scan_productive_15m(active, _tick_ts())
        self.assertTrue(
            kf._force_reconnect_requested,
            "Post-reconnect: R2 fires at counter 10. Transition "
            "reset doesn't break long-term escalation.")


class TestR_ReviewA1NoReconnectBomb(unittest.TestCase):
    """R-review A1: during a sustained WS disconnect the counter
    accumulates (operator still gets Telegram). The moment WS
    reconnects, counter MUST reset so R2 doesn't immediately fire
    _force_reconnect_requested → tear down the freshly-reconnected
    WS → reconnect-bomb loop."""

    def test_disconnect_then_reconnect_resets_counter(self):
        sc, kf = _make_main_loop()
        active = _active_windows_with_n_15m(4)
        # Simulate disconnect (kf.is_connected=False) for many ticks.
        kf.is_connected = False
        for _ in range(20):
            sc._check_scan_productive_15m(active, _tick_ts())
        # Counter accumulated during disconnect.
        self.assertGreater(sc._scan_15m_unproductive_count, 10)
        # No recovery actions fired (WS was down).
        self.assertEqual(len(kf._fake_resubbed), 0)
        self.assertFalse(kf._force_reconnect_requested)
        # Now reconnect.
        kf.is_connected = True
        sc._check_scan_productive_15m(active, _tick_ts())
        # Counter MUST reset on disconnect→reconnect transition.
        # (Either to 0 from transition, or to 1 if it incremented
        # this tick — must NOT be the accumulated value.)
        self.assertLessEqual(
            sc._scan_15m_unproductive_count, 1,
            "On disconnect→reconnect transition, counter MUST "
            "reset so R2 doesn't immediately fire reconnect-bomb.")
        self.assertFalse(
            kf._force_reconnect_requested,
            "R2 reconnect MUST NOT fire on the transition tick — "
            "would tear down the freshly-reconnected WS.")


if __name__ == "__main__":
    unittest.main()
