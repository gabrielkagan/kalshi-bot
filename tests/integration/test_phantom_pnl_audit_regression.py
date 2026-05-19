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

    # ── T6-T9 — B.0a fills-purge guard (Data-Integrity Bit, ticket 86ba0zjqg) ─
    #
    # B.0 investigation (86ba0xpum) found that Kalshi's /portfolio/fills
    # endpoint purges history after ~60d retention while /portfolio/settlements
    # retains long-term. Pre-B.0a, audit_ticker() treated `fills=0 +
    # revenue>0` as a phantom-win divergence and "corrected" PnL by dropping
    # the cost basis (corrected = revenue - 0*price), inflating apparent wins
    # ~25×. 90d dry-run reported +$7,079 of delta — mostly fake recoveries.
    #
    # The fix adds a guard that returns ("unverified") when fills is empty
    # but settlements has revenue. T6 is the regression-RED test (fails on
    # master before fix). T7-T9 pin sibling failure modes that the fix must
    # NOT break.

    def _mock_client_fills_purged(self, ticker, *, revenue_cents,
                                  market_result="yes"):
        """Mock client for the fills-purge artifact: /fills returns empty
        but /settlements returns revenue (the bug pattern). Used for
        B.0a fills-purge guard tests. Side is implicitly determined by
        the local row seeded by the caller — the mock's /fills is empty
        regardless of side filter."""
        client = MagicMock()
        client.get_settlements.return_value = {
            "settlements": [
                {"ticker": ticker, "revenue": revenue_cents,
                 "market_result": market_result}
            ],
        }
        # Empty fills page — the canonical fills-purge shape (NOT None,
        # which would trigger the pagination-interrupted unverified path).
        client.get_fills.return_value = {"fills": []}
        return client

    def test_fills_zero_revenue_positive_returns_unverified_new_guard(self):
        """B.0a TDD-RED: legitimately-won old ticker where /fills purged
        but /settlements has revenue. Audit must mark unverified, NOT
        fabricate a phantom-win correction that drops cost basis.

        Pre-fix: this test FAILS — audit returns divergent with
        corrected_pnl = revenue (cost basis dropped), inflating the
        win. Post-fix: GREEN.

        Real-world fixture: KXSOL15M-26MAR041215-15 (B.0 investigation
        2026-05-19). Local says 150ct @ 96c won = +$6. Kalshi
        /settlements confirms revenue=$150. Kalshi /fills returns
        empty. Pre-fix audit would "correct" to +$150 (25× inflation).
        """
        # Local row matches real ticker shape: 150 @ 96c, WON, +$6 PnL.
        self._seed_row(
            ticker="KXSOL15M-PURGED",
            strategy_group="MAKER_PATIENT",
            count=150,
            entry=96,
            side="yes",
            market_result="yes",
            revenue_cents=15000,
            pnl_cents=15000 - (150 * 96),  # +600c = +$6
        )
        client = self._mock_client_fills_purged(
            "KXSOL15M-PURGED", revenue_cents=15000, market_result="yes")

        summary = self.audit.run_audit(
            self.conn, client, audit_run_id="test-b0a-r1", days=14,
            apply=True, limit=None)

        self.assertEqual(summary["n_audited"], 1)
        self.assertEqual(
            summary["n_divergent"], 0,
            "fills-purge artifact must NOT be reported as divergent "
            "(pre-B.0a this was 1; the corrected_pnl would drop the cost "
            "basis and inflate the win ~25×)")
        self.assertEqual(
            summary["n_unverified"], 1,
            "fills-purge artifact must be classified as unverified")

        # No phantom_corrections row written.
        tbl = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='phantom_corrections'").fetchone()
        if tbl:
            rows = self.conn.execute(
                "SELECT COUNT(*) FROM phantom_corrections WHERE ticker=?",
                ("KXSOL15M-PURGED",)).fetchone()
            self.assertEqual(
                rows[0], 0,
                "fills-purge artifact must not write a phantom_corrections "
                "row (would inflate apparent win)")

    def test_fills_zero_revenue_zero_returns_unverified_unchanged(self):
        """B.0a anti-regression: existing 'both zero' unverified path
        unchanged. Transient API failure where /fills AND /settlements
        both return empty should still be unverified (not divergent),
        same as before B.0a.
        """
        self._seed_row(
            ticker="KXBTC15M-BOTH-ZERO",
            strategy_group="main",
            count=20,
            entry=90,
            side="yes",
            market_result="yes",
            revenue_cents=2000,
            pnl_cents=2000 - (20 * 90),  # +200c
        )
        client = MagicMock()
        # Both endpoints return data with zero-equivalent content.
        client.get_settlements.return_value = {
            "settlements": [
                {"ticker": "KXBTC15M-BOTH-ZERO", "revenue": 0,
                 "market_result": "yes"}
            ],
        }
        client.get_fills.return_value = {"fills": []}

        summary = self.audit.run_audit(
            self.conn, client, audit_run_id="test-b0a-r2", days=14,
            apply=True, limit=None)

        self.assertEqual(summary["n_audited"], 1)
        self.assertEqual(summary["n_divergent"], 0)
        self.assertEqual(
            summary["n_unverified"], 1,
            "both-zero case still classified as unverified")

    def test_fills_present_returns_divergent_or_matched_unchanged(self):
        """B.0a anti-regression: when /fills returns non-zero counts,
        the existing divergent/matched paths must work unchanged. Fix
        must NOT short-circuit when fills are present.
        """
        # Local has 100ct, Kalshi has 60ct (real divergence).
        self._seed_row(
            ticker="KXETH15M-REAL-DIV",
            strategy_group="main",
            count=100,
            entry=97,
            side="yes",
            market_result="no",
            revenue_cents=0,
            pnl_cents=0 - (100 * 97),
        )
        client = self._mock_client(
            "KXETH15M-REAL-DIV", kalshi_count=60, kalshi_revenue_cents=0)

        summary = self.audit.run_audit(
            self.conn, client, audit_run_id="test-b0a-r3", days=14,
            apply=True, limit=None)

        self.assertEqual(summary["n_audited"], 1)
        self.assertEqual(
            summary["n_divergent"], 1,
            "real divergence (fills present, count mismatch) still detected")
        self.assertEqual(
            summary["sum_delta_count"], 40,
            "delta_count = local - kalshi = 100 - 60 = +40")

    def test_fills_zero_revenue_positive_no_side_only_classification(self):
        """B.0a anti-regression sister: the unverified classification
        must be INDEPENDENT of market_result direction. A NO-side
        settlement where /fills is purged should also be unverified —
        the fix's guard must not accidentally only fire for YES wins.

        Defends against a potential over-narrow fix that classifies
        only `revenue>0 AND market_result='yes'` as unverified, which
        would leave NO-side fills-purge artifacts in the divergent
        path (where they'd write a different bogus correction shape).
        """
        # Local: bought 50ct YES @ 80c, market settled NO (loss),
        # revenue=0 locally — but suppose /settlements happens to
        # return revenue>0 for some bookkeeping reason; the guard
        # should still mark unverified rather than fabricate a
        # contradictory correction.
        self._seed_row(
            ticker="KXXRP15M-PURGED-NO",
            strategy_group="main",
            count=50,
            entry=80,
            side="yes",
            market_result="no",
            revenue_cents=0,
            pnl_cents=-(50 * 80),
        )
        # Mock: /fills empty, /settlements returns revenue>0 with
        # market_result="no".
        client = self._mock_client_fills_purged(
            "KXXRP15M-PURGED-NO", revenue_cents=5000, market_result="no")

        summary = self.audit.run_audit(
            self.conn, client, audit_run_id="test-b0a-r4", days=14,
            apply=True, limit=None)

        self.assertEqual(summary["n_audited"], 1)
        self.assertEqual(
            summary["n_divergent"], 0,
            "fills-purge guard must fire regardless of market_result")
        self.assertEqual(summary["n_unverified"], 1)


if __name__ == "__main__":
    unittest.main()
