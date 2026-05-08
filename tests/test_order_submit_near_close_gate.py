"""Pre-submit STC gate — kill 409/404 settlement race.

Apr 26 incident (kb/failures/order-submit-settlement-race-2026-04-26.md):
ETH had ~106 api_errors / 3 days at avg 98.8¢ near settlement, 30% of
ETH order submissions Apr 24-26 raced settlement and got Kalshi
`409 market_closed` or `404 market_not_found`. BTC had 31; SOL/XRP
~20-28 each. ETH stands out (3x more), driven by frequent late-window
TM/DC candidates in the high-price range where phantom depth + late
STC compound.

Mechanism: candidate fires at STC≤2s, bot calls `place_order`,
network round-trip + Kalshi processing ~200ms-2s, by the time the
order hits the matching engine the window has settled → 409. If the
ticker has been pruned from the active list → 404.

Fix: at every `_client.place_order` call site (3 in OrderExecutor —
`_submit_maker` line 19482, `_submit_taker` line 19888, maker tail
line 20432), check `candidate["seconds_to_close"]`. If < 3s, skip
the submission entirely, log `ORDER_ABORT_NEAR_CLOSE`, set
order_outcome="skipped_near_close" so the forensic record reflects
why no fill happened.

Trade-off: candidates at STC<3s lose their fill chance. Empirically
~30% of ETH submissions in this zone returned api_error anyway (so
those losses are baseline). Fills that DID happen at STC<3s were a
small fraction. Prevention cost: ~5-10 lost fills/day; benefit:
eliminate ~35 api_errors/day on ETH alone.

Tests pin:
  - STC < threshold: skip, no place_order call, outcome recorded
  - STC >= threshold: submit normally
  - STC None / missing: submit normally (no info to gate on)
  - Threshold boundary: exactly 3.0 → submit; 2.999 → skip
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


def _make_executor():
    """OrderExecutor stub bypassing __init__ — only the attrs the
    submit paths need."""
    e = bot.OrderExecutor.__new__(bot.OrderExecutor)
    e._client = MagicMock()
    e._state = MagicMock()
    e._ml = MagicMock()
    e._ml.scanner = MagicMock()
    e._ml.scanner._ws_drift_cooldown = {}
    e._session_post_only_rejections = 0
    e._session_ioc_unfilled = 0
    e._session_direct_taker_attempts = 0
    e._session_direct_taker_fills = 0
    e._session_direct_taker_unfilled = 0
    e._ticker_api_errors = {}
    e._recent_taker_tickers = {}
    e._logger = MagicMock()
    e._active_orders = {}
    e._notifier = MagicMock()
    return e


def _candidate(stc=10, ticker="KXETH15M-26APR260715-15", price=98,
               count=50, side="yes"):
    """Minimal candidate dict with the fields the submit paths read."""
    return {
        "ticker": ticker,
        "event_ticker": ticker,
        "asset": "ETH",
        "side": side,
        "best_yes_ask": price,
        "position_size": count,
        "balance_at_scan": 1000_00,
        "calibrated_prob": 0.95,
        "seconds_to_close": stc,
        "strategy": "terminal_momentum",
    }


class TestShouldSkipNearCloseHelper(unittest.TestCase):
    """Helper logic — the contract that the 3 call sites share."""

    def test_stc_below_threshold_returns_true(self):
        e = _make_executor()
        c = _candidate(stc=2.0)
        self.assertTrue(
            e._should_skip_near_close(c),
            "STC=2.0 < MIN_ORDER_SUBMIT_STC_S(3.0) → skip")

    def test_stc_at_threshold_does_not_skip(self):
        """Boundary: STC=3.0 exactly. Use `<` not `<=` so candidates
        at exactly the threshold still get a chance."""
        e = _make_executor()
        c = _candidate(stc=3.0)
        self.assertFalse(e._should_skip_near_close(c))

    def test_stc_above_threshold_does_not_skip(self):
        e = _make_executor()
        c = _candidate(stc=10.0)
        self.assertFalse(e._should_skip_near_close(c))

    def test_stc_zero_skips(self):
        """Settled-at-submit-time. Definitely should skip."""
        e = _make_executor()
        c = _candidate(stc=0.0)
        self.assertTrue(e._should_skip_near_close(c))

    def test_stc_negative_skips(self):
        e = _make_executor()
        c = _candidate(stc=-5.0)
        self.assertTrue(e._should_skip_near_close(c))

    def test_stc_none_does_not_skip(self):
        """Missing STC → can't gate, allow submit. The bot has many
        non-15M paths (weather, sports) where seconds_to_close may
        be unset for the orders coming through this path."""
        e = _make_executor()
        c = _candidate(stc=None)
        self.assertFalse(e._should_skip_near_close(c))

    def test_stc_string_does_not_skip(self):
        """Defensive coercion — bad data shouldn't crash. Treat
        non-numeric as 'no info'."""
        e = _make_executor()
        c = _candidate(stc="not_a_number")
        self.assertFalse(e._should_skip_near_close(c))


class TestSubmitTakerSkipsNearClose(unittest.TestCase):
    """The actual incident path: candidate fires at STC=2s, IOC
    submission would race settlement. Gate must prevent the
    place_order call."""

    def test_low_stc_skips_place_order(self):
        e = _make_executor()
        c = _candidate(stc=1.5)
        result = e._submit_taker(c)
        self.assertIsNone(result)
        e._client.place_order.assert_not_called()

    def test_high_stc_proceeds(self):
        """Sanity: normal STC values still submit. No regression on
        the happy path."""
        e = _make_executor()
        c = _candidate(stc=120)
        e._client.place_order.return_value = {
            "order": {"order_id": "ok", "status": "executed",
                      "remaining_count": 0}}
        # Mock the dependencies _submit_taker reaches into.
        e._ml.scanner._get_orderbook_cached = MagicMock(
            return_value=({"yes": [], "no": []}, "orderbook"))
        with patch.object(bot, "calculate_taker_fee", return_value=2):
            try:
                e._submit_taker(c)
            except Exception:
                # The full _submit_taker path has many branches;
                # exact behavior past place_order isn't this test's
                # concern. We only need to verify place_order was
                # CALLED (not gated out).
                pass
        e._client.place_order.assert_called_once()


class TestSubmitMakerSkipsNearClose(unittest.TestCase):
    """Same gate must apply to the maker submit path."""

    def test_low_stc_skips_place_order(self):
        e = _make_executor()
        c = _candidate(stc=1.0)
        # Add the fields _submit_maker needs (it computes fair_value
        # and does pricing checks before place_order).
        c["fair_value"] = 95
        # Mock state methods called inside _submit_maker.
        result = e._submit_maker(c)
        # Whatever the return shape (None or void), the assertion
        # is that place_order was NOT called.
        e._client.place_order.assert_not_called()


class TestSkipRecordsOutcome(unittest.TestCase):
    """Forensic: when we skip, evaluated_opportunities row outcome
    must reflect it. Otherwise a future audit can't distinguish
    'we skipped this' from 'we never tried'."""

    def test_skip_calls_update_outcome(self):
        e = _make_executor()
        c = _candidate(stc=1.5)
        e._submit_taker(c)
        # Outcome update should have been recorded with
        # skipped_near_close.
        called_with_skipped = any(
            call.kwargs.get("order_outcome") == "skipped_near_close"
            for call in e._state.update_evaluated_opportunity_order
                                 .call_args_list)
        self.assertTrue(
            called_with_skipped,
            "Skipping a submission must record "
            "order_outcome='skipped_near_close' so the eval row "
            "carries the forensic explanation. "
            f"Got calls: {e._state.update_evaluated_opportunity_order.call_args_list}")


class TestConstantExists(unittest.TestCase):
    """Pin the constant value so any future tuning is intentional."""

    def test_constant_default_3s(self):
        self.assertEqual(
            bot.MIN_ORDER_SUBMIT_STC_S, 3.0,
            "MIN_ORDER_SUBMIT_STC_S should be 3.0 (Kalshi processing "
            "latency ~200ms-2s + clock-drift margin). If retuning, "
            "update this test AND the KB article in same commit.")

    def test_env_var_override_pattern(self):
        """The constant uses os.environ.get(..., '3.0') — a future
        operator can disable the gate by setting MIN_ORDER_SUBMIT_STC_S=0
        on the VPS without a redeploy. Pin the env-override pattern in
        source so it survives refactors.

        Bit 3.1: MIN_ORDER_SUBMIT_STC_S definition lives in bot/constants.py
        post-move; the env-getter literal travels with it. Function-body
        usages still reference the name from bot/_impl.py via star-import.
        """
        import bot.constants
        with open(bot.constants.__file__, "r") as f:
            src = f.read()
        self.assertIn(
            'os.environ.get("MIN_ORDER_SUBMIT_STC_S"', src,
            "MIN_ORDER_SUBMIT_STC_S must remain env-overridable so the "
            "gate can be disabled live without redeploy. Pattern: "
            "`float(os.environ.get('MIN_ORDER_SUBMIT_STC_S', '3.0'))`.")


if __name__ == "__main__":
    unittest.main()
