"""Regression for IOC post-completion fill-leak phantom (2026-05-19).

Incident: KXHYPE15M-26MAY190645-45 yes, order f1e48bf1, CONFIRMATION_ADDON
IOC 31 ct @ 99¢. Kalshi REST /portfolio/fills returned 3 distinct-trade_id
fill events for the same order_id:
    17 @ 99¢   (10:44:17.238) → partial_fill, filled_so_far=17/31
    14 @ 98¢   (10:44:17.346) → fill (complete), filled_so_far=31/31
    10 @ 98¢   (10:44:17.457) → ★ should have been rejected / capped to 0,
                                  but bot wrote count=10 phantom row
Total local: 41 ct. Kalshi truth: 27 ct on this order. Phantom: 10 ct.
Phantom-reconcile alert at 2026-05-19T14:37 Δct=+14 Δpnl=+$13.80 across
this ticker (the +4 over the order's 10 is per-row drift on the
decided_t2_z25 sibling row; this test isolates the 10-ct CONFIRMATION_ADDON
mechanism).

Root cause — two collaborating bugs in bot/executor.py:
1. `_on_fill` cap guard at line 4459 was `if raw_fill_count > remaining > 0`.
   The `> 0` made it skip capping when `remaining == 0` (order already
   complete), allowing post-completion fills to leak through to
   record_position_from_fill with raw count.
2. `_submit_taker` IOC polling loops at lines 3832-3849 had no
   "if filled_so_far >= count: break" — they kept iterating
   _check_for_fill until it returned None, processing every fill
   returned by Kalshi even after the order was nominally complete.

Defense (fix):
- `_on_fill`: early-return 0 with WARNING when remaining <= 0.
- IOC polling loops: break when `order_info["filled_so_far"] >=
  order_info["count"]` after each `_on_fill` call.

The test drives `_submit_taker` end-to-end with a `get_fills` mock that
returns all 3 fill events, and asserts:
  - The total written to record_position_from_fill across calls is <= 31
    (the requested IOC count), NOT 41.
  - A 10-ct phantom write does NOT happen post-completion.

See `kb/failures/ghost-fill-retry-overcount-may18.md` (sibling B1 incident)
for the broader ghost-fill class. This is the post-B1 sibling mechanism.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

# Mock heavy deps before importing bot.
_MOCKED = []
for _mod in ["websockets", "websocket", "requests",
             "cryptography", "cryptography.hazmat",
             "cryptography.hazmat.primitives",
             "cryptography.hazmat.primitives.serialization",
             "cryptography.hazmat.primitives.hashes",
             "cryptography.hazmat.primitives.asymmetric",
             "cryptography.hazmat.primitives.asymmetric.padding"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
        _MOCKED.append(_mod)

from bot.executor import OrderExecutor  # noqa: E402
import bot.notifier  # noqa: F401,E402
import bot.scanner  # noqa: F401,E402


def _make_candidate(**overrides):
    base = {
        "ticker": "KXHYPE15M-26MAY190645-45",
        "event_ticker": "KXHYPE15M-26MAY190645",
        "asset": "HYPE",
        "best_yes_ask": 99,
        "position_size": 31,
        "calibrated_prob": 0.995,
        "edge": 0.013,
        "seconds_to_close": 80,
        "strategy": "CONFIRMATION_ADDON",
        "balance_at_scan": 54426,
        "spot": 4.5,
        "threshold": 4.5,
        "blended_rv": 0.0004,
        "z_score": -2.78,
        "vol_regime": "normal",
        "kelly_f": 0.01,
        "product_type": "15m",
        "ofa_adjustment": 0.0,
        "ob_snapshot": {"ask_depth": 65},
        "calibrated_prob_raw": 0.96,
        "drawdown_scaler": 1.0,
        "entry_path": "confirmation_addon",
    }
    base.update(overrides)
    return base


def _make_executor():
    client = MagicMock()
    client.get_orderbook.return_value = None
    state = MagicMock()
    logger = MagicMock()
    main_loop = MagicMock()
    kalshi_feed = MagicMock()
    kalshi_feed.is_connected = True
    kalshi_feed.pop_fills.return_value = []

    ex = OrderExecutor(
        client=client, state=state, logger=logger,
        main_loop=main_loop, kalshi_feed=kalshi_feed,
    )
    return ex


def _fill(order_id, trade_id, count, yes_price):
    """Build a Kalshi /portfolio/fills response entry."""
    return {
        "order_id": order_id,
        "trade_id": trade_id,
        "count": count,
        "yes_price": yes_price,
        "no_price": 100 - yes_price,
    }


class TestIOCPostCompletionFillLeak(unittest.TestCase):
    """The IOC submit-time poll loops must not record fills past the
    requested count, even when /portfolio/fills returns more fills than
    we asked for (duplicate trade_ids, cross-order misattribution,
    transient Kalshi over-fill, or any other source)."""

    def test_third_fill_after_completion_is_not_written_to_positions(self):
        """Recreates the 2026-05-19 incident shape: 17 + 14 + 10 fills on
        a 31-ct IOC. After the 14-ct fill, filled_so_far == count == 31
        and the order is complete; the 10-ct fill that arrives later
        must NOT add a phantom row."""
        ex = _make_executor()

        ex._client.place_order.return_value = {
            "order": {
                "order_id": "f1e48bf1-test",
                "remaining_count": 0,
                "fill_count_fp": None,
                "fill_count": 31,
            }
        }
        # /portfolio/fills returns all 3 fills (with distinct trade_ids
        # so _check_for_fill's dedup-by-id doesn't collapse them).
        ex._client.get_fills.return_value = {
            "fills": [
                _fill("f1e48bf1-test", "trade-1", 17, 99),
                _fill("f1e48bf1-test", "trade-2", 14, 98),
                _fill("f1e48bf1-test", "trade-3", 10, 98),  # the leak
            ]
        }

        candidate = _make_candidate()

        with patch("bot.executor.time") as mock_time, \
             patch("bot.executor.fp_str_to_int", side_effect=lambda x: x or 0), \
             patch("bot.executor.dollars_str_to_cents", side_effect=lambda x: x or 0):
            mock_time.time.return_value = 1_700_000_000.0
            mock_time.sleep = MagicMock()
            result = ex._submit_taker(candidate)

        self.assertIsNotNone(result, "IOC must return order_info after fills")

        # Sum the count across every record_position_from_fill call.
        rpff_calls = ex._state.record_position_from_fill.call_args_list
        total_recorded = sum(c.kwargs.get("count", 0) for c in rpff_calls)
        call_counts = [c.kwargs.get("count") for c in rpff_calls]

        # Strictness ratchet (R2 MINOR-3 follow-up): assert EXACTLY two
        # record_position_from_fill calls with counts [17, 14]. The looser
        # "total <= 31" assertion would pass a buggy fix that wrote
        # `record_position_from_fill(count=0)` for the leak, creating a
        # zero-count ghost row (a separate bug class — see B2 at
        # bot/state.py:1340 "When a ghost-fill row reaches count=0 it is
        # DELETED"). Two calls, exact counts, no third row.
        self.assertEqual(
            len(rpff_calls), 2,
            f"Expected exactly 2 record_position_from_fill calls (17+14=31); "
            f"got {len(rpff_calls)} with counts {call_counts}. The leak fill "
            f"must be dropped entirely, not written with count=0.",
        )
        self.assertEqual(
            sorted(call_counts), [14, 17],
            f"Expected calls with counts [17, 14]; got {call_counts}.",
        )
        self.assertEqual(
            total_recorded, 31,
            f"Total recorded must equal IOC request (31); got {total_recorded}.",
        )

    def test_exact_count_fills_recorded_unchanged(self):
        """Sanity: if the sum of fills equals the requested count (no
        leak), all fills are recorded in full. Guards against an
        over-eager fix that would truncate legitimate fills."""
        ex = _make_executor()

        ex._client.place_order.return_value = {
            "order": {
                "order_id": "order-exact",
                "remaining_count": 0,
                "fill_count_fp": None,
                "fill_count": 31,
            }
        }
        # 17 + 14 = 31 (exact). No leak.
        ex._client.get_fills.return_value = {
            "fills": [
                _fill("order-exact", "trade-a", 17, 99),
                _fill("order-exact", "trade-b", 14, 98),
            ]
        }

        candidate = _make_candidate()

        with patch("bot.executor.time") as mock_time, \
             patch("bot.executor.fp_str_to_int", side_effect=lambda x: x or 0), \
             patch("bot.executor.dollars_str_to_cents", side_effect=lambda x: x or 0):
            mock_time.time.return_value = 1_700_000_000.0
            mock_time.sleep = MagicMock()
            ex._submit_taker(candidate)

        rpff_calls = ex._state.record_position_from_fill.call_args_list
        total_recorded = sum(c.kwargs.get("count", 0) for c in rpff_calls)
        self.assertEqual(
            total_recorded, 31,
            f"Exact-fill scenario must record all 31 contracts; got "
            f"{total_recorded} across {len(rpff_calls)} call(s).",
        )


if __name__ == "__main__":
    unittest.main()
