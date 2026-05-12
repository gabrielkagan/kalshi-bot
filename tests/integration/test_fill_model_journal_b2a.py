"""Sprint B Bit B.2a regression tests — fill_model_journal NULL audit + fix.

ClickUp ticket: 86b9vfznd

Production NULL audit on 10,210 rows (2026-05-12) found:
- queue_position_initial: 100% NULL  → DEPRECATED (never written anywhere)
- convergence_velocity:   100% NULL  → DEPRECATED (only on scanner helper dicts,
                                       never on candidate dict)
- fill_source:            100% NULL on IOC routes → writer bug (taker IOC path
                          never set fill_source — only maker WS/REST polls did)
- queue_position_final:   99% NULL on IOC, 91% on maker → predicate (only set
                          if maker survived ≥5s and queue was polled)

Fix shipped in same commit:
1. Remove deprecated dead reads (queue_position_initial + convergence_velocity).
2. Set fill_source="ioc_inline" for taker IOC outcome=filled (was None).
3. Add ob_snapshot_source predicate column ("scanner" | "addon_empty" | "missing").
4. Add queue_position_polled predicate (bool) — whether queue was ever sampled.

These tests fail on the pre-fix writer and pass after.
"""

import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bot.constants  # noqa: F401
import bot.executor  # noqa: F401


def _make_executor():
    """Stub OrderExecutor bypassing __init__."""
    e = bot.executor.OrderExecutor.__new__(bot.executor.OrderExecutor)
    e._kalshi_feed = MagicMock()
    e._kalshi_feed.is_connected = True
    return e


def _make_order(*, is_taker: bool, outcome: str = "filled",
                fill_source: str = None, queue_polled: bool = False,
                ob_snapshot=None, entry_path: str = None):
    """Build a minimal order dict mirroring production shape."""
    candidate = {
        "asset": "BTC",
        "best_yes_ask": 92,
        "vol_regime": "normal",
        "blended_rv": 0.0004,
        "z_score": 2.5,
        "edge": 0.03,
        "kelly_f": 0.15,
        "ob_snapshot": ob_snapshot if ob_snapshot is not None else {
            "best_ask": 92, "ask_depth": 30, "total_depth": 264374,
            "best_bid": 91, "bid_depth": 6210, "spread": 1,
        },
    }
    order = {
        "order_id": "test-oid",
        "ticker": "KXBTC15M-26MAY112300-00",
        "asset": "BTC",
        "price_cents": 92,
        "count": 50,
        "is_taker": is_taker,
        "submit_time": time.time() - 5.0,
        "seconds_to_close_at_submit": 300.0,
        "candidate": candidate,
        "execution_method": "ioc" if is_taker else "maker",
        "entry_path": entry_path or ("direct_taker" if is_taker else "maker"),
        "_last_queue_poll": 10.0 if queue_polled else 0.0,
    }
    if fill_source is not None:
        order["fill_source"] = fill_source
    if queue_polled:
        order["queue_position"] = 5
    return order


class _CaptureMixin(unittest.TestCase):
    """Captures the JSON line written by _log_fill_model_sample."""

    def _capture(self, executor, order, outcome, **kw):
        captured = []

        class _FakeFile:
            def write(self_inner, s):
                captured.append(s)

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                pass

        with patch("builtins.open", return_value=_FakeFile()):
            executor._log_fill_model_sample(order, outcome, **kw)
        assert captured, "writer produced no output"
        return json.loads(captured[0])


class TestDeprecatedFieldsRemoved(_CaptureMixin):
    """(iii) deprecated dead reads must be REMOVED from the writer output.

    These fields were 100% NULL in production because nothing ever wrote
    them. Keeping them in the JSONL is signal-free clutter that confuses
    downstream ML training.
    """

    def test_queue_position_initial_not_in_output(self):
        """queue_position_initial is never set anywhere — drop it."""
        e = _make_executor()
        order = _make_order(is_taker=False)
        sample = self._capture(e, order, "filled")
        self.assertNotIn(
            "queue_position_initial", sample,
            "queue_position_initial was 100% NULL in production "
            "(never written anywhere). Bit B.2a removed it.",
        )

    def test_convergence_velocity_not_in_output(self):
        """convergence_velocity is added to scanner helper dicts but
        never propagated to the candidate dict the writer reads. 100% NULL."""
        e = _make_executor()
        order = _make_order(is_taker=False)
        sample = self._capture(e, order, "filled")
        self.assertNotIn(
            "convergence_velocity", sample,
            "convergence_velocity was 100% NULL in production "
            "(scanner adds it to strategy-helper dicts only, never to "
            "the candidate). Bit B.2a removed it.",
        )


class TestFillSourceIOCPopulated(_CaptureMixin):
    """(i) writer-bug fix: IOC fills must record fill_source.

    Pre-fix: order["fill_source"] is only set inside the maker
    polling path (websocket vs rest_poll). IOC taker fills never set
    it, so 100% of IOC rows had fill_source=null.
    """

    def test_ioc_filled_has_ioc_inline_source(self):
        e = _make_executor()
        order = _make_order(is_taker=True, entry_path="direct_taker")
        sample = self._capture(e, order, "filled")
        self.assertEqual(
            sample["fill_source"], "ioc_inline",
            "IOC outcome=filled rows must record fill_source='ioc_inline' "
            "(the inline poll path in _submit_taker). Was 100% NULL.",
        )

    def test_maker_websocket_fill_source_preserved(self):
        """Maker WS-fill path explicitly sets order['fill_source']
        before _log_fill_model_sample is called. Don't overwrite it."""
        e = _make_executor()
        order = _make_order(is_taker=False, fill_source="websocket")
        sample = self._capture(e, order, "filled")
        self.assertEqual(
            sample["fill_source"], "websocket",
            "Existing fill_source must not be overwritten.",
        )

    def test_maker_rest_poll_fill_source_preserved(self):
        e = _make_executor()
        order = _make_order(is_taker=False, fill_source="rest_poll")
        sample = self._capture(e, order, "filled")
        self.assertEqual(sample["fill_source"], "rest_poll")


class TestPredicateColumns(_CaptureMixin):
    """(ii) only-when-applicable predicates: add columns that tell the
    consumer which rows the existing NULL-prone fields apply to.

    db_schema.md "Storage paths" now documents these predicates.
    """

    def test_ob_snapshot_source_scanner_when_populated(self):
        e = _make_executor()
        order = _make_order(is_taker=False)  # default ob_snapshot is full
        sample = self._capture(e, order, "filled")
        self.assertEqual(sample["ob_snapshot_source"], "scanner")

    def test_ob_snapshot_source_addon_empty_for_addon_path(self):
        """confirmation_addon + dip_addon set ob_snapshot={} because no
        fresh scanner OB exists mid-execution. Distinguishes from
        missing-data NULL."""
        e = _make_executor()
        order = _make_order(
            is_taker=True, entry_path="confirmation_addon", ob_snapshot={})
        sample = self._capture(e, order, "filled")
        self.assertEqual(sample["ob_snapshot_source"], "addon_empty")

    def test_queue_position_polled_false_when_not_polled(self):
        """Maker orders that fill <5s never get queue polled. The
        predicate tells the consumer queue_position_final=NULL is by
        design, not a missing data point."""
        e = _make_executor()
        order = _make_order(is_taker=False, queue_polled=False)
        sample = self._capture(e, order, "filled")
        self.assertFalse(sample["queue_position_polled"])

    def test_queue_position_polled_true_when_polled(self):
        e = _make_executor()
        order = _make_order(is_taker=False, queue_polled=True)
        sample = self._capture(e, order, "filled")
        self.assertTrue(sample["queue_position_polled"])
        self.assertEqual(sample["queue_position_final"], 5)


class TestSurfaceShapeStable(_CaptureMixin):
    """Lock the post-fix surface so future drift surfaces here loudly."""

    EXPECTED_KEYS = {
        # identity
        "type", "ts", "ticker", "asset", "outcome",
        # fill details
        "fill_latency_s", "fill_source",
        # submission context
        "price_cents", "fair_value", "offset_cents", "count", "post_only",
        # market context
        "seconds_to_close", "vol_regime", "blended_rv",
        "ask_depth", "total_ob_depth", "spread_at_submit", "bid_depth",
        "z_score", "edge", "kelly_f",
        # queue (queue_position_final + new predicate)
        "queue_position_final", "queue_position_polled",
        # execution
        "execution_method", "entry_path", "cancel_reason", "elapsed_seconds",
        # WS / config
        "ws_connected", "maker_only_threshold",
        # new predicate
        "ob_snapshot_source",
    }

    def test_post_fix_keys(self):
        e = _make_executor()
        order = _make_order(is_taker=False)
        sample = self._capture(e, order, "filled")
        actual = set(sample.keys())
        unexpected = actual - self.EXPECTED_KEYS
        missing = self.EXPECTED_KEYS - actual
        self.assertFalse(
            unexpected,
            f"Unexpected keys in fill_model_sample: {sorted(unexpected)}",
        )
        self.assertFalse(
            missing,
            f"Missing expected keys in fill_model_sample: {sorted(missing)}",
        )


if __name__ == "__main__":
    unittest.main()
