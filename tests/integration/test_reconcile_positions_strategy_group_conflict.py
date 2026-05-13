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
        # and logged. Without the fix, sqlite3.IntegrityError propagates.
        sm.reconcile_with_api(client)


if __name__ == "__main__":
    unittest.main()
