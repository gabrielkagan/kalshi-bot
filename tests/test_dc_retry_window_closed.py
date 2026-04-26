"""DC retry queue must drop entries when the window has settled (STC ≤ 0).

Apr 26 11:15 UTC incident
(kb/failures/dc-retry-post-settlement-burn-2026-04-26.md):
BTC DC T1 candidate fired at 11:14:55 with STC=5s, hit phantom depth
(IOC_ABORT_PHANTOM), got queued for retry. The retry queue (max
DC_IOC_MAX_RETRIES + 1 = 11 attempts) burned attempts at ~1s intervals
from 11:15:03 onward — well AFTER the 11:15:00 window close.

Each retry: `_dc_get_ask_with_depth` returns a (stale) NBBO ask price
+ depth=0, so retry submits anyway, `_submit_taker` aborts via
IOC_ABORT_PHANTOM, returns None, retry re-queues with adaptive_delay
(=1.0 since STC<30). 11 × ~1s = ~11s of scan-tick burn → 5 consecutive
empty 15M ticks → SCAN_UNPRODUCTIVE_15M alert.

The window has settled. There is no scenario where IOC at a closed
window will fill. The retry must drop the entry, log the outcome,
and stop wasting scan ticks.

This test pins the behavior:
  - STC ≤ 0 → drop entry (no retry, no _submit_taker call)
  - STC > 0 → behave as before
  - Outcome update reflects partial_filled vs unfilled_window_closed
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


def _make_executor():
    """Stub OrderExecutor with the attrs process_dc_retries needs.
    Bypass __init__ to skip Kalshi client + state init."""
    e = bot.OrderExecutor.__new__(bot.OrderExecutor)
    e._dc_retry_queue = []
    e._session_dc_retries = 0
    e._session_dc_retry_fills = 0
    e._session_direct_taker_attempts = 0
    e._session_direct_taker_fills = 0
    e._session_direct_taker_unfilled = 0
    e._recent_taker_tickers = {}
    e._state = MagicMock()
    e._ml = MagicMock()
    return e


def _retry_entry(*, ticker, stc_at_queue, queued_at, attempt=1,
                 total_filled=0, original_count=140, original_price=99,
                 strategy="decided_t1"):
    """Build a DC retry queue entry as the production code does.
    Mirrors line 18824 / 18896 / 18918 shape exactly."""
    cand = {
        "ticker": ticker,
        "calibrated_prob": 0.99,
        "best_yes_ask": original_price,
        "position_size": original_count - total_filled,
        "seconds_to_close": stc_at_queue,
    }
    return {
        "candidate": cand,
        "original_count": original_count,
        "total_filled": total_filled,
        "remaining": original_count - total_filled,
        "attempt": attempt,
        "next_retry_ts": queued_at,  # ready immediately
        "strategy": strategy,
        "original_price": original_price,
        "_queue_ts": queued_at,
    }


class TestDcRetryDropsOnWindowClosed(unittest.TestCase):
    """Window-settled retry must drop, not loop."""

    def test_stc_negative_drops_entry(self):
        """The actual 11:15 incident shape: candidate queued with STC=5s,
        retry fires after window close → effective STC is negative.
        Entry must be dropped, _submit_taker must NOT be called."""
        e = _make_executor()
        # Candidate queued with original STC=5; retry fires 10s later
        # → _stc_now = 5 - 10 = -5 ≤ 0.
        e._dc_retry_queue.append(_retry_entry(
            ticker="KXBTC15M-26APR260715-15",
            stc_at_queue=5,
            queued_at=time.time() - 10,  # 10s ago
        ))
        with patch.object(e, "_submit_taker") as mock_submit, \
                patch.object(e, "_dc_get_ask_with_depth") as mock_ask:
            mock_ask.return_value = (99, 0, "market_nbbo")  # phantom shape
            mock_submit.return_value = None  # IOC_ABORT_PHANTOM behavior
            e.process_dc_retries()
            mock_submit.assert_not_called()
        self.assertEqual(
            len(e._dc_retry_queue), 0,
            "Entry must be dropped from queue once window has settled "
            "— retrying on a closed window cannot fill, only burns "
            "scan-tick time")
        # Outcome recorded as unfilled_window_closed.
        e._state.update_evaluated_opportunity_order.assert_called()
        kwargs = e._state.update_evaluated_opportunity_order.call_args.kwargs
        self.assertEqual(kwargs.get("order_outcome"), "unfilled_window_closed")

    def test_stc_zero_drops_entry(self):
        """STC exactly 0 (settled at exactly retry time) — also drop.
        Edge case: a market closing at the same moment retry fires
        is functionally settled; IOC will not fill."""
        e = _make_executor()
        e._dc_retry_queue.append(_retry_entry(
            ticker="KXETH15M-26APR260715-15",
            stc_at_queue=10,
            queued_at=time.time() - 10,  # _stc_now = 0
        ))
        with patch.object(e, "_submit_taker") as mock_submit, \
                patch.object(e, "_dc_get_ask_with_depth") as mock_ask:
            mock_ask.return_value = (99, 0, "market_nbbo")
            mock_submit.return_value = None
            e.process_dc_retries()
            mock_submit.assert_not_called()
        self.assertEqual(len(e._dc_retry_queue), 0)

    def test_partial_filled_outcome_when_some_fills(self):
        """If the retry was queued AFTER a partial fill (line 18896),
        outcome must be `partial_filled`, NOT `unfilled_window_closed`,
        so historical reporting reflects what actually happened."""
        e = _make_executor()
        e._dc_retry_queue.append(_retry_entry(
            ticker="KXBTC15M-26APR260715-15",
            stc_at_queue=5,
            queued_at=time.time() - 10,
            total_filled=20,  # partial fill earlier
            original_count=140,
        ))
        with patch.object(e, "_submit_taker") as mock_submit, \
                patch.object(e, "_dc_get_ask_with_depth") as mock_ask:
            mock_ask.return_value = (99, 0, "market_nbbo")
            mock_submit.return_value = None
            e.process_dc_retries()
        kwargs = e._state.update_evaluated_opportunity_order.call_args.kwargs
        self.assertEqual(kwargs.get("order_outcome"), "partial_filled")

    def test_stc_positive_does_NOT_drop(self):
        """Sanity: when window is still open (STC > 0), do NOT drop —
        the existing retry behavior (re-queue, attempt _submit_taker)
        must be preserved. The fix is precisely scoped to STC ≤ 0."""
        e = _make_executor()
        e._dc_retry_queue.append(_retry_entry(
            ticker="KXBTC15M-26APR260715-15",
            stc_at_queue=600,  # 10 min remaining
            queued_at=time.time(),  # no age decay
        ))
        with patch.object(e, "_submit_taker") as mock_submit, \
                patch.object(e, "_dc_get_ask_with_depth") as mock_ask:
            mock_ask.return_value = (99, 5, "orderbook")  # real depth
            mock_submit.return_value = {"order_id": "x", "filled_count": 5}
            e.process_dc_retries()
            mock_submit.assert_called_once()
        # Entry still in queue with remaining contracts (5 of 140
        # filled this attempt → 135 remaining).
        self.assertEqual(len(e._dc_retry_queue), 1)

    def test_stc_positive_phantom_unfilled_requeues(self):
        """R3 [A3] coverage gap: the MOST COMMON production scenario
        the retry queue exists for — candidate fires mid-window
        (STC > 0), hits phantom depth, _submit_taker returns None
        (IOC_ABORT_PHANTOM), entry RE-QUEUES for next retry.

        Without this test, a future refactor that inverted the guard
        to `_orig_stc > 0 and _stc_now > 0` (drop when window IS open
        — exact opposite of intent) would still pass every other
        test in the suite. This test pins the active-retry-loop
        invariant: window-open + phantom = continue retrying."""
        e = _make_executor()
        e._dc_retry_queue.append(_retry_entry(
            ticker="KXBTC15M-26APR260715-15",
            stc_at_queue=120,  # 2 min remaining (mid-window)
            queued_at=time.time() - 5,  # 5s decay → STC≈115
        ))
        with patch.object(e, "_submit_taker") as mock_submit, \
                patch.object(e, "_dc_get_ask_with_depth") as mock_ask:
            mock_ask.return_value = (99, 0, "market_nbbo")  # phantom
            mock_submit.return_value = None  # IOC_ABORT_PHANTOM
            e.process_dc_retries()
            mock_submit.assert_called_once()
        # Entry MUST stay in queue for next retry — this is the
        # queue's intended purpose during open windows.
        self.assertEqual(
            len(e._dc_retry_queue), 1,
            "Mid-window phantom-abort must RE-QUEUE for next retry "
            "(queue's primary purpose). If queue is empty, the drop "
            "guard fired incorrectly — likely an inverted condition.")
        self.assertEqual(
            e._dc_retry_queue[0]["total_filled"], 0,
            "Total filled remains 0 (phantom abort produced no fill)")


class TestDcRetryDoesNotDropQueueWithoutOriginalSTC(unittest.TestCase):
    """R1 [A1] — the retry queue's primary purpose (per line 18822
    comment) is "book may appear later" — i.e., when fresh_ask is None
    on initial submission, queue and retry to give Kalshi time to
    publish the book. In that case, `seconds_to_close` may be 0 or
    None at queue time. The drop-on-window-closed logic must NOT fire
    on those entries — it must require ORIGINAL STC > 0 AND elapsed
    time has caused decay to ≤ 0.
    """

    def test_orig_stc_zero_does_not_drop_when_phantom(self):
        """The actual R1 [A1] regression: orig STC=0 + phantom depth +
        unfilled. Pre-A1-fix, a future "drop on _stc_now<=0" would
        fire and kill the queue's "book may appear later" purpose.
        Post-fix, the guard requires orig STC > 0 — so this entry
        does NOT drop, instead re-queues for next retry. Mock submit
        to None to mirror the real phantom-abort path."""
        e = _make_executor()
        e._dc_retry_queue.append(_retry_entry(
            ticker="KXBTC15M-26APR260715-15",
            stc_at_queue=0,  # ORIGINALLY 0 — preserve queue purpose
            queued_at=time.time() - 10,
        ))
        with patch.object(e, "_submit_taker") as mock_submit, \
                patch.object(e, "_dc_get_ask_with_depth") as mock_ask:
            mock_ask.return_value = (99, 0, "market_nbbo")  # phantom
            mock_submit.return_value = None  # IOC_ABORT_PHANTOM
            e.process_dc_retries()
            mock_submit.assert_called_once()  # submit DID run
        # Entry RE-QUEUED (still in queue, drop didn't fire).
        self.assertEqual(
            len(e._dc_retry_queue), 1,
            "Drop guard must NOT fire when orig STC=0 — entry stays "
            "in queue for legitimate 'book may appear later' retry "
            "(R1 [A1]). If this fails, the guard is too aggressive "
            "and kills the queue's stated purpose.")

    def test_orig_stc_none_does_not_drop_when_phantom(self):
        """Same as above with explicit None (some callers may pass
        seconds_to_close=None when STC is unknown)."""
        e = _make_executor()
        entry = _retry_entry(
            ticker="KXBTC15M-26APR260715-15",
            stc_at_queue=0,
            queued_at=time.time() - 10,
        )
        entry["candidate"]["seconds_to_close"] = None
        e._dc_retry_queue.append(entry)
        with patch.object(e, "_submit_taker") as mock_submit, \
                patch.object(e, "_dc_get_ask_with_depth") as mock_ask:
            mock_ask.return_value = (99, 0, "market_nbbo")
            mock_submit.return_value = None
            e.process_dc_retries()
            mock_submit.assert_called_once()
        self.assertEqual(
            len(e._dc_retry_queue), 1,
            "None STC must NOT trigger drop guard — entry re-queues")

    def test_orig_stc_string_does_not_crash(self):
        """R2 [A4]: defensive coercion — if a future caller passes
        seconds_to_close as a string (JSON-parse artifact), the retry
        loop must not crash. `float()` coercion swallows the value
        cleanly to 0 → orig_stc=0 → drop guard skipped → normal
        processing."""
        e = _make_executor()
        entry = _retry_entry(
            ticker="KXBTC15M-26APR260715-15",
            stc_at_queue=0,
            queued_at=time.time() - 10,
        )
        entry["candidate"]["seconds_to_close"] = "not_a_number"
        e._dc_retry_queue.append(entry)
        with patch.object(e, "_submit_taker") as mock_submit, \
                patch.object(e, "_dc_get_ask_with_depth") as mock_ask:
            mock_ask.return_value = (99, 0, "market_nbbo")
            mock_submit.return_value = None
            # MUST NOT raise.
            try:
                e.process_dc_retries()
            except (TypeError, ValueError) as ex:
                self.fail(
                    f"process_dc_retries crashed on string STC: "
                    f"{ex!r}. R2 [A4] coercion missing.")


class TestDcRetryQueueTsFallback(unittest.TestCase):
    """R1 [A5] — `_queue_ts` is set at every production append site
    today, but the `entry.get("_queue_ts", default)` fallback should
    bias toward DECAY (drop), not toward perpetual-retry. Pre-fix
    default `now` caused `_eval_age = 0` → `_stc_now` never decays →
    malformed entry retries the full 11 attempts. New default
    `now - DC_IOC_RETRY_DELAY` ensures at least one cycle of decay.
    """

    def test_missing_queue_ts_decays(self):
        """Construct an entry without `_queue_ts` (simulates a
        future/legacy code path that forgets to set it). Drop guard
        must still apply if STC has decayed."""
        e = _make_executor()
        entry = _retry_entry(
            ticker="KXBTC15M-26APR260715-15",
            stc_at_queue=5,  # original STC=5
            queued_at=time.time(),
        )
        del entry["_queue_ts"]  # remove the field
        e._dc_retry_queue.append(entry)
        with patch.object(e, "_submit_taker") as mock_submit, \
                patch.object(e, "_dc_get_ask_with_depth") as mock_ask:
            mock_ask.return_value = (99, 0, "market_nbbo")
            mock_submit.return_value = None
            e.process_dc_retries()
            # _eval_age = DC_IOC_RETRY_DELAY (default) = 8
            # _stc_now = 5 - 8 = max(0, -3) = 0 → drop fires.
            mock_submit.assert_not_called()
        self.assertEqual(len(e._dc_retry_queue), 0)


class TestDcRetryLogsDropDiagnostic(unittest.TestCase):
    """Operator must see WHY the entry dropped. Pre-fix, the queue
    silently churned; post-fix, we log the drop with attempt count
    and total_filled so an audit can correlate burn-time with the
    SCAN_UNPRODUCTIVE alert that fired."""

    def test_drop_emits_diagnostic_log(self):
        e = _make_executor()
        e._dc_retry_queue.append(_retry_entry(
            ticker="KXBTC15M-26APR260715-15",
            stc_at_queue=5,
            queued_at=time.time() - 10,
            attempt=2,
            total_filled=0,
        ))
        with patch.object(bot, "logging") as mock_log, \
                patch.object(e, "_submit_taker") as mock_submit, \
                patch.object(e, "_dc_get_ask_with_depth") as mock_ask:
            mock_ask.return_value = (99, 0, "market_nbbo")
            mock_submit.return_value = None
            e.process_dc_retries()
            # At least one log call mentions WINDOW_CLOSED with the
            # ticker so a journal grep finds it.
            log_msgs = [
                str(c.args[0]) if c.args else ""
                for c in mock_log.info.call_args_list
            ]
            matched = any(
                "WINDOW_CLOSED" in m
                and "KXBTC15M-26APR260715-15" in
                str(c.args)
                for c, m in zip(mock_log.info.call_args_list, log_msgs)
            )
            self.assertTrue(
                matched,
                f"process_dc_retries must log a WINDOW_CLOSED "
                f"diagnostic on drop. Got: {log_msgs}")


if __name__ == "__main__":
    unittest.main()
