"""Regression tests for the dead `_active_taker_count` cap removal.

May 4 2026: `OrderExecutor._active_taker_count` was a Dict[str, int]
that two execution paths (`_execute_hourly_taker`,
`_execute_hourly_no_taker`) read against `MAX_CONCURRENT_TAKER_PER_ASSET`
to gate concurrent IOC submissions. **No code ever wrote to the dict**,
so the gate's right-hand side was always 0; the cap never fired.

RCA finding:
- `executor.execute()` is called sequentially in a `for` loop
  (bot/_impl.py:27231 — single scan thread).
- `_submit_taker` is synchronous; IOCs resolve in <1s within a single
  call.
- Every `_submit_taker` call is reached synchronously from the
  sequential scan loop or its in-tick sub-paths (hourly/weather IOC
  entry, escalation, ladder retry, DC overlay, addon, maker-tail).
- Therefore the cap couldn't fire in current architecture even if
  the dict were maintained correctly: by the time candidate N is
  processed, candidates 1..N-1 have already returned.

Decision: Option B (delete the dead cap), not Option A (wire
increment/decrement). Option A would solve nothing under the
single-thread architecture; if parallelism is ever introduced, real
concurrency control (locks, async-safe counters) needs to be designed,
not retrofitted on a counter pattern.

These tests pin:
1. The dead-cap symbol set is gone (no init, no constant, no reads).
2. The hourly_taker / hourly_no_taker execution paths still gate
   correctly on the OTHER (real) controls — ticker cooldown +
   downstream `_submit_taker` calls.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
import bot._impl  # noqa: F401
import bot.executor  # noqa: F401
import bot.constants  # noqa: F401


# ─── Symbol-removal pins ─────────────────────────────────────────────────

class TestDeadCapSymbolsRemoved(unittest.TestCase):
    """Pin that the dead-cap surface area is gone. If a future commit
    re-adds any of these without designing real concurrency control,
    these tests fail loudly."""

    def test_max_concurrent_taker_constant_removed(self):
        self.assertFalse(
            hasattr(bot.constants, "MAX_CONCURRENT_TAKER_PER_ASSET"),
            "MAX_CONCURRENT_TAKER_PER_ASSET was a dead-cap constant. "
            "Re-introducing it without wiring real per-asset "
            "concurrency control (locks + atomic counter, or async-safe "
            "dict) would silently re-create the dead-defense pattern. "
            "See kb/failures/active-taker-count-dead-cap-may04.md.")

    def test_active_taker_count_init_removed(self):
        # Construct an OrderExecutor without going through __init__,
        # then trigger the constant-init path that __init__ would have
        # taken. We can't easily exercise __init__ without the full
        # client/state setup, so we grep the source instead — same
        # intent as a structural test.
        with open(bot._impl.__file__) as f:
            src = f.read()
        self.assertNotIn(
            "_active_taker_count", src,
            "_active_taker_count dict was the dead-cap state. "
            "All references (init + 2 reads) must be removed together. "
            "Partial removal would leave a write-only or read-only "
            "ghost in the codebase.")


# ─── Behavioral pins: hourly_taker / hourly_no_taker still gate correctly ──

def _make_executor():
    """Stub OrderExecutor for behavioral tests."""
    e = bot.executor.OrderExecutor.__new__(bot.executor.OrderExecutor)
    e._recent_taker_tickers = {}
    e._submit_taker = MagicMock(return_value={"ok": True})
    e._logger = MagicMock()
    return e


def _make_candidate(asset="BTC",
                    ticker="KXBTCD-26MAY04H1300-T75999",
                    best_yes_ask=55,
                    position_size=10,
                    seconds_to_close=900,
                    calibrated_prob=0.85,
                    fee_adjusted_edge=0.20,
                    side="yes"):
    return {
        "asset": asset,
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "best_yes_ask": best_yes_ask,
        "position_size": position_size,
        "seconds_to_close": seconds_to_close,
        "calibrated_prob": calibrated_prob,
        "fee_adjusted_edge": fee_adjusted_edge,
        "edge": fee_adjusted_edge,
        "side": side,
        "balance_at_scan": 50000,
        "kelly_f": 0.05,
        "vol_regime": "normal",
        "z_score": 1.0,
        "ob_snapshot": {},
    }


class TestHourlyTakerPaths(unittest.TestCase):
    """Behavioral pin: hourly_taker / hourly_no_taker still gate on
    cooldown + still call _submit_taker. Ensures the deletion didn't
    accidentally remove other guards."""

    def test_hourly_taker_calls_submit_when_cooldown_clear(self):
        e = _make_executor()
        cand = _make_candidate(asset="BTC")

        e._execute_hourly_taker(cand)

        e._submit_taker.assert_called_once()

    def test_hourly_taker_skips_when_in_cooldown(self):
        e = _make_executor()
        cand = _make_candidate(asset="BTC")
        e._recent_taker_tickers[cand["ticker"]] = time.time()

        e._execute_hourly_taker(cand)

        e._submit_taker.assert_not_called()

    def test_hourly_no_taker_calls_submit_when_cooldown_clear(self):
        e = _make_executor()
        cand = _make_candidate(asset="ETH", side="no")

        e._execute_hourly_no_taker(cand)

        e._submit_taker.assert_called_once()

    def test_hourly_no_taker_skips_when_in_cooldown(self):
        e = _make_executor()
        cand = _make_candidate(asset="ETH", side="no")
        e._recent_taker_tickers[cand["ticker"]] = time.time()

        e._execute_hourly_no_taker(cand)

        e._submit_taker.assert_not_called()


# ─── Pin: many sequential candidates same asset all reach _submit_taker ──

class TestNoCapBlocksSequentialSubmissions(unittest.TestCase):
    """Pin that the deletion didn't silently introduce any other
    per-asset cap. Sequential candidates on the same asset (different
    tickers, no cooldown overlap) must all reach _submit_taker.
    Pre-deletion the dead cap would have allowed this anyway (it never
    fired); post-deletion the same behavior must hold."""

    def test_four_sequential_btc_candidates_all_submit(self):
        e = _make_executor()
        for i in range(4):
            cand = _make_candidate(
                asset="BTC",
                ticker=f"KXBTCD-26MAY04H1300-T7{i}999")
            e._execute_hourly_taker(cand)

        self.assertEqual(
            e._submit_taker.call_count, 4,
            "4 sequential same-asset candidates must all reach "
            "_submit_taker. If only 3 fire, the deletion missed a cap "
            "or a new cap was introduced.")

    def test_four_sequential_btc_no_candidates_all_submit(self):
        e = _make_executor()
        for i in range(4):
            cand = _make_candidate(
                asset="BTC",
                ticker=f"KXBTCD-26MAY04H1300-T7{i}999",
                side="no")
            e._execute_hourly_no_taker(cand)

        self.assertEqual(e._submit_taker.call_count, 4)


if __name__ == "__main__":
    unittest.main()
