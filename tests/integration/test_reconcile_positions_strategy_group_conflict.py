"""Regression for the 2026-05-13 _reconcile_positions UNIQUE-constraint crash.

When a settled/closed row exists at (ticker, strategy_group='main') and
the Kalshi API returns the same ticker with a non-zero position count,
the INSERT path in _reconcile_positions defaulted strategy_group='main'
and tripped the UNIQUE (ticker, strategy_group) constraint — killing
bot startup.

Fix: wrap the INSERT in try/except sqlite3.IntegrityError + log warning.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

import bot.state  # noqa: E402


class TestReconcilePositionsStrategyGroupConflict(unittest.TestCase):
    def _fresh(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return bot.state.StateManager(db_path=tmp.name)

    def test_insert_conflict_does_not_crash_reconcile(self):
        sm = self._fresh()
        # Seed: settled row at (ticker, strategy_group='main').
        sm.conn.execute(
            "INSERT INTO positions (ticker, event_ticker, asset, side, count, "
            "avg_price_cents, total_cost_cents, opened_at, updated_at, status, "
            "strategy_group) VALUES (?, ?, ?, 'yes', ?, ?, ?, "
            "'2026-05-13T00:00:00Z', '2026-05-13T00:00:00Z', 'settled', 'main')",
            ("KXBTC15M-OLD", "KXBTC15M", "BTC", 50, 99, 4950),
        )
        sm.conn.commit()

        # API returns same ticker as still open (position_count != 0).
        client = MagicMock()
        client.get_positions.return_value = {
            "market_positions": [{
                "ticker": "KXBTC15M-OLD",
                "position": 50,
                "market_exposure": 4950,
            }],
            "event_positions": [],
        }
        client.get_orders.return_value = {"orders": []}

        # Must NOT raise. With the fix, the INSERT-conflict is caught
        # and the settled row is reopened in place.
        sm.reconcile_with_api(client)

    def test_insert_conflict_reopens_settled_row_in_place(self):
        # Pins the money-loss-preventing UPDATE-in-place behavior FOR THE
        # ORPHAN CASE: no settled_trades entry (local DB stuck in settled
        # state without a real Kalshi settlement event). Reopen is right.
        sm = self._fresh()
        sm.conn.execute(
            "INSERT INTO positions (ticker, event_ticker, asset, side, count, "
            "avg_price_cents, total_cost_cents, opened_at, updated_at, status, "
            "strategy_group) VALUES (?, ?, ?, 'yes', ?, ?, ?, "
            "'2026-05-13T00:00:00Z', '2026-05-13T00:00:00Z', 'settled', 'main')",
            ("KXBTC15M-OLD", "KXBTC15M", "BTC", 50, 99, 4950),
        )
        # NO settled_trades entry — orphan settle.
        sm.conn.commit()

        client = MagicMock()
        client.get_positions.return_value = {
            "market_positions": [{
                "ticker": "KXBTC15M-OLD",
                "position": 50,
                "market_exposure": 4950,
            }],
            "event_positions": [],
        }
        client.get_orders.return_value = {"orders": []}

        sm.reconcile_with_api(client)

        rows = sm.get_open_positions()
        tickers = [r["ticker"] for r in rows]
        self.assertIn("KXBTC15M-OLD", tickers,
                      "orphan settle: row must be reopened to status='open'")

    def test_insert_conflict_does_not_reopen_when_settled_trades_exists(self):
        # Pins the -fu3 settled_trades-guard behavior: when a
        # settled_trades record exists (meaning we processed the Kalshi
        # settlement event correctly), trust local settle even if Kalshi
        # positions API still lists the ticker (Kalshi-side API lag).
        # This is the -fu2 phantom-row prevention.
        sm = self._fresh()
        sm.conn.execute(
            "INSERT INTO positions (ticker, event_ticker, asset, side, count, "
            "avg_price_cents, total_cost_cents, opened_at, updated_at, status, "
            "strategy_group) VALUES (?, ?, ?, 'yes', ?, ?, ?, "
            "'2026-05-13T00:00:00Z', '2026-05-13T00:00:00Z', 'settled', 'main')",
            ("KXSOL15M-SETTLED", "KXSOL15M", "SOL", 25, 92, 2300),
        )
        # settled_trades entry exists — real settlement event was processed.
        sm.conn.execute(
            "INSERT INTO settled_trades (ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES (?, ?, ?, 'yes', 'yes', ?, ?, ?, ?, ?, ?)",
            ("KXSOL15M-SETTLED", "KXSOL15M", "SOL", 25, 92, 2500, 5, 195,
             "2026-05-13T13:26:00Z"),
        )
        sm.conn.commit()

        client = MagicMock()
        client.get_positions.return_value = {
            "market_positions": [{
                "ticker": "KXSOL15M-SETTLED",
                "position": 25,
                "market_exposure": 2300,
            }],
            "event_positions": [],
        }
        client.get_orders.return_value = {"orders": []}

        sm.reconcile_with_api(client)

        rows = sm.get_open_positions()
        tickers = [r["ticker"] for r in rows]
        self.assertNotIn(
            "KXSOL15M-SETTLED", tickers,
            "Kalshi-lag positions with a processed settled_trades record must NOT be reopened",
        )
        # Settled row remains settled.
        row = sm.conn.execute(
            "SELECT status FROM positions WHERE ticker='KXSOL15M-SETTLED'"
        ).fetchone()
        self.assertEqual(dict(row)["status"], "settled")

    def test_reconcile_order_remaining_falls_back_to_legacy_on_fp_malformed(self):
        """remaining_count_fp unparseable must not zero remaining — try
        the integer remaining_count field (102ef9ac except-path restore).
        """
        sm = self._fresh()
        client = MagicMock()
        client.get_positions.return_value = {"market_positions": []}
        client.get_orders.return_value = {"orders": [{
            "order_id": "oid-rem-legacy",
            "client_order_id": "mk-rem-1",
            "ticker": "KXBTC15M-26MAR091200-B68500",
            "side": "yes",
            "action": "buy",
            "yes_price": 50,
            "remaining_count_fp": "N/A",
            "remaining_count": 3,
            "status": "resting",
            "created_time": "2026-09-06T00:00:00Z",
        }]}
        client.cancel_order.return_value = {"order": {"status": "canceled"}}

        sm.reconcile_with_api(client)

        row = sm.conn.execute(
            "SELECT count FROM pending_orders WHERE order_id='oid-rem-legacy'"
        ).fetchone()
        self.assertIsNotNone(row, "API-only resting order must be imported")
        self.assertEqual(
            row["count"], 3,
            "malformed remaining_count_fp must fall back to remaining_count=3, "
            "not unconditional 0",
        )


if __name__ == "__main__":
    unittest.main()
