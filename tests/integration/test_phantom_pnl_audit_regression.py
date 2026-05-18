"""Regression for B4 (86b9zudcc) phantom_pnl_audit script.

The B4 ticket asks for a one-shot retroactive audit:
  - Iterate ``settled_trades`` rows in a window.
  - Query Kalshi REST for the true settled count + revenue.
  - When ``kalshi.count != settled_trades.count``, write a
    ``phantom_corrections`` row capturing the delta.
  - Recompute ``pnl_cents`` from Kalshi truth.

These tests pin the audit's core ``run_audit`` entry-point:

  T1. Phantom-inflated LOSS (HYPE-shape) → ``phantom_corrections`` row written
      with corrected pnl and accurate deltas.
  T2. Aligned local + Kalshi → no row written.
  T3. Empty Kalshi response (transient API failure) → no false-positive row.
  T4. ``--dry-run`` semantics — divergence found but NO row written.
  T5. Re-running with the same ``audit_run_id`` is idempotent (UNIQUE clause).
"""
import importlib
import logging
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)


def _import_audit_module():
    """Module is at scripts/audit/phantom_pnl_audit.py — not a package.

    Add scripts/audit to sys.path on demand so importlib can resolve it.
    """
    audit_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))),
        "scripts", "audit",
    )
    if audit_dir not in sys.path:
        sys.path.insert(0, audit_dir)
    return importlib.import_module("phantom_pnl_audit")


class TestPhantomPnlAudit(unittest.TestCase):
    def setUp(self):
        self.audit = _import_audit_module()
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.addCleanup(os.unlink, tmp.name)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        # Minimal settled_trades table — mirrors bot/state.py schema (with
        # extras the audit may read).
        self.conn.executescript("""
            CREATE TABLE settled_trades (
                ticker TEXT,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                market_result TEXT NOT NULL,
                side TEXT NOT NULL,
                count INTEGER NOT NULL,
                entry_price_cents INTEGER NOT NULL,
                revenue_cents INTEGER NOT NULL,
                fee_cents INTEGER NOT NULL,
                pnl_cents INTEGER NOT NULL,
                settled_at TEXT NOT NULL,
                strategy_group TEXT DEFAULT 'main',
                PRIMARY KEY (ticker, strategy_group)
            );
        """)
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _seed_row(self, *, ticker, strategy_group, count, entry, side="yes",
                  market_result="no", revenue_cents=0, fee_cents=0,
                  pnl_cents=None, settled_at=None, asset="HYPE"):
        if pnl_cents is None:
            pnl_cents = revenue_cents - (count * entry)
        if settled_at is None:
            settled_at = "2026-05-18T05:30:00Z"
        self.conn.execute(
            "INSERT INTO settled_trades (ticker, event_ticker, asset, "
            "market_result, side, count, entry_price_cents, revenue_cents, "
            "fee_cents, pnl_cents, settled_at, strategy_group) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, f"{ticker[:-3]}EVT", asset, market_result, side, count,
             entry, revenue_cents, fee_cents, pnl_cents, settled_at,
             strategy_group),
        )
        self.conn.commit()

    def _mock_client(self, ticker, *, kalshi_count, kalshi_revenue_cents,
                     side="yes"):
        client = MagicMock()
        client.get_settlements.return_value = {
            "settlements": [
                {"ticker": ticker, "revenue": kalshi_revenue_cents,
                 "market_result": "no"}
            ],
        }
        # Single page of fills, sized to deliver `kalshi_count`. count_fp is
        # the plain count rendered as a decimal string ('61.00'); the bot's
        # ``fp_str_to_int`` helper rounds float(s) → int.
        fills_resp = {
            "fills": [
                {"order_id": "ord-1", "ticker": ticker, "side": side,
                 "count": kalshi_count, "count_fp": f"{kalshi_count}.00"},
            ],
        }
        client.get_fills.return_value = fills_resp
        return client

    # ── T1 — phantom-inflated LOSS writes phantom_corrections row ────────

    def test_divergent_loss_writes_phantom_correction(self):
        """HYPE-shape: local books say 179ct, Kalshi truth is 61ct.
        Expect a phantom_corrections row with the deltas and a corrected
        pnl computed from Kalshi truth.
        """
        # Two strategy_group rows on same ticker, summing to 179.
        self._seed_row(ticker="KXHYPE15M-T1", strategy_group="terminal_momentum_98",
                       count=58, entry=98, market_result="no", revenue_cents=0,
                       pnl_cents=-58 * 98)
        self._seed_row(ticker="KXHYPE15M-T1", strategy_group="decided_t1",
                       count=121, entry=97, market_result="no", revenue_cents=0,
                       pnl_cents=-121 * 97)

        client = self._mock_client(
            "KXHYPE15M-T1", kalshi_count=61, kalshi_revenue_cents=0)

        summary = self.audit.run_audit(
            self.conn, client, audit_run_id="test-r1", days=14,
            apply=True, limit=None)

        self.assertEqual(summary["n_audited"], 1)
        self.assertEqual(summary["n_divergent"], 1)
        # delta_count = local - kalshi = 179 - 61 = +118 (over-count)
        self.assertEqual(summary["sum_delta_count"], 118)

        # phantom_corrections row written.
        row = self.conn.execute(
            "SELECT * FROM phantom_corrections WHERE ticker=?",
            ("KXHYPE15M-T1",)).fetchone()
        self.assertIsNotNone(row, "phantom_corrections row must exist")
        d = dict(row)
        self.assertEqual(d["local_count"], 179)
        self.assertEqual(d["kalshi_count"], 61)
        self.assertEqual(d["delta_count"], 118)
        # Weighted avg price: (58*98 + 121*97) / 179 = (5684 + 11737)/179
        # = 17421/179 = 97.32 → rounded 97.
        # corrected_pnl = 0 - (61 × 97) = -5917
        self.assertEqual(d["avg_price_cents"], 97)
        self.assertEqual(d["corrected_pnl_cents"], 0 - 61 * 97)
        # local_pnl_cents = -58*98 + -121*97 = -5684 + -11737 = -17421
        self.assertEqual(d["local_pnl_cents"], -17421)
        # delta_pnl_cents = corrected - local = -5917 - (-17421) = +11504
        # (positive means local was MORE NEGATIVE — i.e., overstated loss).
        self.assertEqual(d["delta_pnl_cents"],
                         d["corrected_pnl_cents"] - d["local_pnl_cents"])
        self.assertEqual(d["audit_run_id"], "test-r1")
        self.assertEqual(d["audit_window_days"], 14)
        self.assertEqual(d["market_result"], "no")
        self.assertEqual(d["side"], "yes")

    # ── T2 — aligned books → no row written ──────────────────────────────

    def test_aligned_writes_no_row(self):
        self._seed_row(ticker="KXBTC15M-T1", strategy_group="main",
                       count=50, entry=80, market_result="yes",
                       revenue_cents=50 * 100, pnl_cents=50 * (100 - 80))
        # Kalshi truth matches local exactly.
        client = self._mock_client(
            "KXBTC15M-T1", kalshi_count=50, kalshi_revenue_cents=5000)

        summary = self.audit.run_audit(
            self.conn, client, audit_run_id="test-r2", days=14,
            apply=True, limit=None)

        self.assertEqual(summary["n_audited"], 1)
        self.assertEqual(summary["n_divergent"], 0)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM phantom_corrections WHERE ticker=?",
            ("KXBTC15M-T1",)).fetchone()
        self.assertEqual(rows[0], 0)

    # ── T3 — empty Kalshi response → no false-positive row ───────────────

    def test_empty_kalshi_response_does_not_write_row(self):
        """Transient Kalshi API failure: get_fills + get_settlements return
        empty/None. Audit must NOT fabricate a divergence."""
        self._seed_row(ticker="KXSOL15M-T1", strategy_group="main",
                       count=20, entry=90, market_result="no",
                       revenue_cents=0, pnl_cents=-1800)
        client = MagicMock()
        client.get_settlements.return_value = None
        client.get_fills.return_value = None

        summary = self.audit.run_audit(
            self.conn, client, audit_run_id="test-r3", days=14,
            apply=True, limit=None)

        self.assertEqual(summary["n_divergent"], 0)
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM phantom_corrections "
            "WHERE ticker=?", ("KXSOL15M-T1",)).fetchone()
        self.assertEqual(rows[0], 0)

    # ── T4 — dry-run finds but does NOT write ────────────────────────────

    def test_dry_run_finds_but_does_not_write(self):
        self._seed_row(ticker="KXETH15M-T1", strategy_group="main",
                       count=100, entry=98, market_result="no",
                       revenue_cents=0, pnl_cents=-9800)
        client = self._mock_client(
            "KXETH15M-T1", kalshi_count=50, kalshi_revenue_cents=0)

        summary = self.audit.run_audit(
            self.conn, client, audit_run_id="test-r4", days=14,
            apply=False, limit=None)

        self.assertEqual(summary["n_divergent"], 1,
                         "divergence must still be reported in dry-run")
        self.assertEqual(len(summary["findings"]), 1)
        # phantom_corrections row must NOT have been written (apply=False).
        # Table may not even exist — query via sqlite_master to dodge errors.
        tbl = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='phantom_corrections'").fetchone()
        if tbl:
            rows = self.conn.execute(
                "SELECT COUNT(*) FROM phantom_corrections WHERE ticker=?",
                ("KXETH15M-T1",)).fetchone()
            self.assertEqual(rows[0], 0,
                             "dry-run must not write to phantom_corrections")

    # ── T5 — re-run with same audit_run_id is idempotent ─────────────────

    def test_rerun_same_run_id_is_idempotent(self):
        self._seed_row(ticker="KXXRP15M-T1", strategy_group="main",
                       count=80, entry=95, market_result="no",
                       revenue_cents=0, pnl_cents=-7600)
        client = self._mock_client(
            "KXXRP15M-T1", kalshi_count=40, kalshi_revenue_cents=0)

        for _ in range(3):
            self.audit.run_audit(
                self.conn, client, audit_run_id="dup-run", days=14,
                apply=True, limit=None)

        cnt = self.conn.execute(
            "SELECT COUNT(*) FROM phantom_corrections WHERE ticker=?",
            ("KXXRP15M-T1",)).fetchone()[0]
        self.assertEqual(cnt, 1,
                         "re-runs with same audit_run_id must not duplicate")


if __name__ == "__main__":
    unittest.main()
