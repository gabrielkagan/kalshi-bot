"""Regression — Apr 25 09:34 UTC sports-400 cascade.

The chronic noise we deferred last night ('harmless') compounded
into a real outage this morning: ~15 dead soccer series each
making 2 REST calls per discovery cycle (open + active status),
each returning 400 in ~1 second. With Kalshi's 400-response time
slowing on Saturday morning, that became ~30 seconds of synchronous
REST blocking on the sports discovery thread per cycle, and the
cascade contributed to scan throughput collapse (multi-minute
silent gaps in 15M evaluation, watchdog firing at uptime 468 min).

Fix: set `enabled=False` on the 15 confirmed dead series in
sports_data.py LEAGUES. The existing
`refresh_if_needed()` already has `if not league_cfg.enabled:
continue` (sports_engine.py line 483) — so disabling them stops
the REST calls at the source with zero code change.

This test asserts the 15 dead series have `enabled=False` so that
a future refactor can't silently re-enable them and reintroduce
the 400 spam.

See kb/failures/scan-tick-stall-cluster-2026-04-25.md item #4
in the Tomorrow's checklist (now completed).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Series confirmed returning HTTP 400 from Kalshi /events on Apr 25
# 09:34 UTC. Verified by journalctl grep `series_ticker=KX.*GAME`.
DEAD_SERIES = {
    "KXEPLGAME",
    "KXBUNDESLIGAGAME",
    "KXLALIGAGAME",
    "KXSERIEAGAME",
    "KXUCLGAME",
    "KXLIGUE1GAME",
    "KXSUPERLIGGAME",
    "KXMLSGAME",
    "KXUELGAME",
    "KXUECLGAME",
    "KXLIGAMXGAME",
    "KXWCGAME",
    "KXFIFAGAME",
    "KXAFCONGAME",
    "KXEREDIVISIEGAME",
}


class TestSportsDeadSeriesDisabled(unittest.TestCase):
    """Each known-dead soccer series must have `enabled=False` so
    `KalshiSportsDiscovery.refresh_if_needed()` skips them and the
    main thread doesn't burn ~1s per series on a 400 response."""

    def test_dead_series_are_disabled(self):
        from sports_data import LEAGUES
        still_enabled = []
        for series_ticker in DEAD_SERIES:
            cfg = LEAGUES.get(series_ticker)
            if cfg is None:
                # If a dead series is removed entirely, that's also fine.
                continue
            if cfg.enabled:
                still_enabled.append(series_ticker)
        self.assertEqual(
            still_enabled, [],
            f"These known-dead series are still enabled and will "
            f"cause 400-cascade REST spam: {still_enabled}. "
            f"Set enabled=False in sports_data.py LEAGUES.")

    def test_active_series_still_enabled(self):
        """Sanity — the live sports we DO trade must still be
        enabled. Catches a regression where someone disables every
        soccer series and accidentally disables NBA/NHL/MLB too."""
        from sports_data import LEAGUES
        for active_series in ("KXNBAGAME", "KXNHLGAME", "KXMLBGAME",
                              "KXNCAABBGAME", "KXNCAAFGAME",
                              "KXNFLGAME", "KXWNBAGAME"):
            cfg = LEAGUES.get(active_series)
            self.assertIsNotNone(
                cfg, f"Expected {active_series} in LEAGUES")
            self.assertTrue(
                cfg.enabled,
                f"{active_series} must remain enabled (it's a live "
                f"sport we trade).")


if __name__ == "__main__":
    unittest.main()
