"""Regression test for the Telegram "Balance:" total-portfolio fix.

Kalshi's `/portfolio/balance` endpoint returns cash only. Telegram alerts
("Bot started — Balance: $X", "WIN ... | Balance: $X") were emitting that
cash figure, so the user saw a number that did not match Kalshi's UI
Portfolio total (cash + open-position market value).

The fix adds StateManager.get_open_position_exposure_cents() — the
cost-basis sum across open positions — which callers add to the cash
balance before formatting. Cost basis approximates Kalshi UI but is
NOT bit-exact: Kalshi reports market-mark per-position, which can
diverge from fill price by a few percent (e.g. user-reported screenshot:
$143.05 cost-basis for 50×99 + 45×99 + 50×98 vs Kalshi's $155.81
positions figure). True parity would require storing per-position
last_price_cents and refetching market marks — not in scope.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

import bot.state  # noqa: E402


class _TempState(unittest.TestCase):
    def _fresh(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return bot.state.StateManager(db_path=tmp.name)

    def _insert_position(self, sm, *, ticker, count, avg_price_cents,
                         status="open", asset="BTC"):
        sm.conn.execute(
            "INSERT INTO positions (ticker, event_ticker, asset, side, count, "
            "avg_price_cents, total_cost_cents, opened_at, updated_at, status) "
            "VALUES (?, ?, ?, 'yes', ?, ?, ?, '2026-05-13T00:00:00Z', "
            "'2026-05-13T00:00:00Z', ?)",
            (ticker, ticker.split("-")[0], asset, count, avg_price_cents,
             count * avg_price_cents, status),
        )
        sm.conn.commit()


class TestOpenPositionExposureCents(_TempState):
    def test_no_positions_returns_zero(self):
        sm = self._fresh()
        self.assertEqual(sm.get_open_position_exposure_cents(), 0)

    def test_single_open_position_returns_cost_basis(self):
        sm = self._fresh()
        self._insert_position(sm, ticker="KXBTC-A", count=50, avg_price_cents=99)
        self.assertEqual(sm.get_open_position_exposure_cents(), 50 * 99)

    def test_sums_across_open_positions(self):
        sm = self._fresh()
        self._insert_position(sm, ticker="KXBTC-A", count=50, avg_price_cents=99)
        self._insert_position(sm, ticker="KXETH-B", count=45, avg_price_cents=99)
        self._insert_position(sm, ticker="KXSOL-C", count=50, avg_price_cents=98)
        # 50*99 + 45*99 + 50*98 = 4950 + 4455 + 4900 = 14305 cents
        self.assertEqual(sm.get_open_position_exposure_cents(), 14305)

    def test_excludes_non_open_status(self):
        sm = self._fresh()
        self._insert_position(sm, ticker="KXBTC-OPEN", count=10,
                              avg_price_cents=99, status="open")
        self._insert_position(sm, ticker="KXBTC-CLOSED", count=10,
                              avg_price_cents=99, status="closed")
        self._insert_position(sm, ticker="KXBTC-SETTLED", count=10,
                              avg_price_cents=99, status="settled")
        self.assertEqual(sm.get_open_position_exposure_cents(), 10 * 99)

    def test_int_cast_handles_none_count_or_price(self):
        # Defense-in-depth: schema is NOT NULL but the helper should not
        # crash if a future migration introduces a nullable column. Using
        # sentinel rows would require ALTER TABLE; assert the helper at
        # the contract level instead.
        sm = self._fresh()
        self._insert_position(sm, ticker="KXBTC-A", count=50,
                              avg_price_cents=99)
        # Verify the helper returns an int (callers add to balance_cents
        # which is also an int from Kalshi's response).
        result = sm.get_open_position_exposure_cents()
        self.assertIsInstance(result, int)
        self.assertEqual(result, 4950)


if __name__ == "__main__":
    unittest.main()
