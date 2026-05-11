"""Step #3 of architectural rebuild — assert the invariant that
NO LeagueConfig has hardcoded `enabled=False`. All dead-series
detection lives in circuit breakers (commit 36ce0b0), not config.

Expected post-deploy behavior (deploy-success contract):
  - First sports-discovery cycle (~30s after start): ~28 series
    × 2 status-filter calls = ~56 GET /events attempts. Most
    return 400 today (Kalshi-wide). Each series records 1 failure.
  - Cycles 2-3 (~5-10 min): each series accumulates to threshold
    (3) → CIRCUIT_BREAKER_TRIPPED warnings fire.
  - Cycles 4+ (~10 min onwards): tripped breakers short-circuit,
    zero REST traffic for tripped series. One probe per 5 min.

Failure modes to alert on post-deploy:
  - ZERO TRIPPED logs after 15 min uptime → integration broken
  - 4xx error rate doesn't drop after minute 10 → breakers not
    actually short-circuiting

Runtime breaker-wired correctness is verified by tests in
`test_kalshi_client_breakers.py` (per-series isolation, short-
circuit-on-open). If someone removes the @_kalshi_breaker decorator
on get_events, those tests fail.

Sport-API rename failsafe (round-2 Q6) is a known limitation —
no periodic GET /series reconcile yet. See sports_data.py comment.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestNoHardcodedDisabledSeries(unittest.TestCase):
    """Round-1 A3 fix: assert the actual invariant rather than a
    snapshot of 15 names. Once Step #3 ships, NO LeagueConfig
    should have `enabled=False` — every dead-series decision is
    delegated to circuit breakers at runtime. If a 16th series
    needs to be disabled in the future, the answer is to lower
    its breaker threshold or document the breaker behavior, NOT
    to add another hardcoded `enabled=False`."""

    def test_no_league_has_hardcoded_enabled_false(self):
        from bot.engines.sports_data import LEAGUES
        disabled = [
            k for k, cfg in LEAGUES.items() if not cfg.enabled
        ]
        self.assertEqual(
            disabled, [],
            f"After Step #3, NO LeagueConfig should be hardcoded "
            f"`enabled=False`. Dead-series handling is the circuit "
            f"breaker's job (commit 36ce0b0). Found "
            f"{len(disabled)} hardcoded-disabled series: {disabled}. "
            f"If you genuinely need to disable a series, lower its "
            f"breaker threshold to 1 or remove it from LEAGUES "
            f"entirely.")




if __name__ == "__main__":
    unittest.main()
