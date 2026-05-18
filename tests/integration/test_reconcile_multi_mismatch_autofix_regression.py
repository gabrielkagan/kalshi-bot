"""Regression for B2 (86b9zud1p): RECONCILE_MULTI_MISMATCH must auto-fix
ghost-fill rows + log per-row evidence.

2026-05-18 HYPE incident postmortem (kb/failures/ghost-fill-retry-overcount-may18.md):
when the bot sees multiple local position rows for a ticker and Kalshi
positions API disagrees, the old code logged
`RECONCILE_MULTI_MISMATCH: ... — NOT auto-fixing` and walked away.
Settlement then read the inflated local count and reported a phantom loss.

This regression pins the B2 fix in two phases:

Phase 1 — log richer evidence: on mismatch, emit a per-row breakdown
(strategy_group, count, fill_source, delta = local_total - api_total)
so retroactive cleanup can reconstruct the ambiguity from logs alone.

Phase 2 — cautious auto-fix: when local_total > api_total AND there
exists ≥1 local row with fill_source LIKE 'ghost_fill%', deflate the
ghost-fill row(s) until sum(count) == api_total. Other rows
(non-ghost-fill) are left UNTOUCHED. If multiple ghost-fill rows exist,
deflate proportionally.

Negative cases pinned:
- No ghost-fill row present → keep the warning, do NOT mutate.
- Local matches API exactly → no warning, no mutation.
- Local < API (under-count) → keep current behavior (warning only).

These tests are TDD-RED against the unfixed code; the fix lives in
`bot/state.py::StateManager._reconcile_multi_mismatch` (~line 1308,
delegated from the `else:` arm of `_reconcile_positions` at ~line 1291).
"""
import logging
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


class TestReconcileMultiMismatchAutofixRegression(unittest.TestCase):
    def _fresh(self):
        """Fresh StateManager with production-shaped composite-PK positions.

        Mirrors `tests/integration/test_ghost_fill_overcount_regression.py::_fresh`
        — the prod sqlite was migrated to composite PK long ago (see
        migrations/migrate_composite_pk.py); base CREATE in bot/state.py
        uses single-column PK which cannot represent stacked strategies.
        """
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        sm = bot.state.StateManager(db_path=tmp.name)
        sm.conn.executescript("""
            DROP TABLE IF EXISTS positions;
            CREATE TABLE positions (
                ticker TEXT,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                count INTEGER NOT NULL,
                avg_price_cents INTEGER NOT NULL,
                total_cost_cents INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                strategy TEXT,
                seconds_to_close REAL,
                fill_latency_seconds REAL,
                vol_regime TEXT,
                calibrated_prob REAL,
                edge REAL,
                kelly_f REAL,
                is_taker INTEGER,
                fill_source TEXT,
                execution_method TEXT,
                escalation_type TEXT,
                maker_price_cents INTEGER,
                maker_wait_seconds REAL,
                strategy_group TEXT DEFAULT 'main',
                is_stacked INTEGER DEFAULT 0,
                accumulated_fee_cents INTEGER DEFAULT 0,
                PRIMARY KEY (ticker, strategy_group)
            );
        """)
        sm.conn.commit()
        return sm

    def _seed_row(self, sm, *, ticker, strategy_group, count, avg, fill_source,
                  side="yes"):
        sm.conn.execute(
            "INSERT INTO positions (ticker, event_ticker, asset, side, count, "
            "avg_price_cents, total_cost_cents, opened_at, updated_at, status, "
            "strategy_group, fill_source) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, '2026-05-18T00:00:00Z', "
            "'2026-05-18T00:00:00Z', 'open', ?, ?)",
            (ticker, "KXHYPE15M-EVT", "HYPE", side, count, avg,
             count * avg, strategy_group, fill_source),
        )
        sm.conn.commit()

    def _mock_client(self, ticker, api_count, side="yes"):
        client = MagicMock()
        position_signed = api_count if side == "yes" else -api_count
        client.get_positions.return_value = {
            "market_positions": [{
                "ticker": ticker,
                "position": position_signed,
                "market_exposure": api_count * 98,  # arbitrary cost basis
            }],
            "event_positions": [],
        }
        client.get_orders.return_value = {"orders": []}
        return client

    # ── Phase 2: cautious auto-fix ───────────────────────────────────────

    def test_ghost_fill_row_deflated_when_local_exceeds_api(self):
        """HYPE-shape: terminal_momentum row (ioc) + decided row (ghost_fill).
        API truth says fewer contracts than the local sum. Auto-fix MUST
        deflate the ghost-fill row only, leaving the ioc row untouched.
        """
        sm = self._fresh()
        ticker = "KXHYPE15M-AUTOFIX1"
        self._seed_row(sm, ticker=ticker, strategy_group="terminal_momentum_98",
                       count=58, avg=98, fill_source="ioc")
        ioc_pre = sm.conn.execute(
            "SELECT opened_at, updated_at FROM positions "
            "WHERE ticker=? AND strategy_group='terminal_momentum_98'",
            (ticker,)).fetchone()
        ioc_opened_at_pre = dict(ioc_pre)["opened_at"]
        ioc_updated_at_pre = dict(ioc_pre)["updated_at"]
        self._seed_row(sm, ticker=ticker, strategy_group="decided",
                       count=3, avg=97, fill_source="ghost_fill_positions_api")
        # API truth: only 58 contracts on Kalshi (TM filled, decided
        # ghost-fill was entirely phantom).
        client = self._mock_client(ticker, api_count=58)

        with self.assertLogs(level="WARNING") as cm:
            sm.reconcile_with_api(client)

        # N3 — pin the distinct AUTOFIXED event tag (operator alert grep
        # surface; tag-rename would silently break this assert).
        self.assertIn("RECONCILE_MULTI_MISMATCH_AUTOFIXED", "\n".join(cm.output))

        rows = sm.conn.execute(
            "SELECT strategy_group, count, fill_source, status, opened_at, "
            "updated_at, event_ticker, asset, is_taker, accumulated_fee_cents "
            "FROM positions WHERE ticker=?", (ticker,)
        ).fetchall()
        by_sg = {dict(r)["strategy_group"]: dict(r) for r in rows}

        # ioc row UNTOUCHED at count=58 — including opened_at + updated_at +
        # event_ticker + asset (N2 negative-invariant pins).
        self.assertEqual(by_sg["terminal_momentum_98"]["count"], 58)
        self.assertEqual(by_sg["terminal_momentum_98"]["status"], "open")
        self.assertEqual(by_sg["terminal_momentum_98"]["fill_source"], "ioc")
        self.assertEqual(by_sg["terminal_momentum_98"]["opened_at"],
                         ioc_opened_at_pre,
                         "non-ghost-fill row opened_at must NOT change")
        self.assertEqual(by_sg["terminal_momentum_98"]["updated_at"],
                         ioc_updated_at_pre,
                         "non-ghost-fill row updated_at must NOT change")
        self.assertEqual(by_sg["terminal_momentum_98"]["event_ticker"],
                         "KXHYPE15M-EVT")
        self.assertEqual(by_sg["terminal_momentum_98"]["asset"], "HYPE")

        # Ghost-fill row deflated to 0 → DELETED (settled_trades pollution
        # avoidance — settlement reads `WHERE ticker=?` without status filter).
        self.assertNotIn("decided", by_sg,
                         "deflated-to-zero ghost-fill row must be DELETED, "
                         "not left as count=0 (else settlement writes phantom row)")

        # Open-row sum matches API truth (58).
        open_sum = sm.conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM positions "
            "WHERE ticker=? AND status='open'", (ticker,)
        ).fetchone()[0]
        self.assertEqual(open_sum, 58,
                         "after auto-fix, open-position sum must match API truth")

    def test_ghost_fill_row_partially_deflated(self):
        """Local=20 (ghost=10, ioc=10), API=15 → deflate ghost by 5 only.
        ghost ends at 5, ioc ends at 10, total = 15."""
        sm = self._fresh()
        ticker = "KXHYPE15M-AUTOFIX2"
        self._seed_row(sm, ticker=ticker, strategy_group="terminal_momentum_98",
                       count=10, avg=98, fill_source="ioc")
        ioc_pre = sm.conn.execute(
            "SELECT opened_at, updated_at FROM positions "
            "WHERE ticker=? AND strategy_group='terminal_momentum_98'",
            (ticker,)).fetchone()
        ioc_opened_at_pre = dict(ioc_pre)["opened_at"]
        ioc_updated_at_pre = dict(ioc_pre)["updated_at"]
        self._seed_row(sm, ticker=ticker, strategy_group="decided",
                       count=10, avg=97, fill_source="ghost_fill_positions_api")
        client = self._mock_client(ticker, api_count=15)

        with self.assertLogs(level="WARNING") as cm:
            sm.reconcile_with_api(client)
        self.assertIn("RECONCILE_MULTI_MISMATCH_AUTOFIXED", "\n".join(cm.output))

        rows = sm.conn.execute(
            "SELECT strategy_group, count, total_cost_cents, avg_price_cents, "
            "opened_at, updated_at, event_ticker, asset "
            "FROM positions WHERE ticker=? AND status='open'", (ticker,)
        ).fetchall()
        by_sg = {dict(r)["strategy_group"]: dict(r) for r in rows}

        self.assertEqual(by_sg["terminal_momentum_98"]["count"], 10,
                         "ioc row must NOT be deflated")
        # N2 negative-invariants on the surviving IOC row (R3 symmetry pin).
        self.assertEqual(by_sg["terminal_momentum_98"]["opened_at"],
                         ioc_opened_at_pre,
                         "non-ghost-fill row opened_at must NOT change")
        self.assertEqual(by_sg["terminal_momentum_98"]["updated_at"],
                         ioc_updated_at_pre,
                         "non-ghost-fill row updated_at must NOT change")
        self.assertEqual(by_sg["terminal_momentum_98"]["event_ticker"],
                         "KXHYPE15M-EVT")
        self.assertEqual(by_sg["terminal_momentum_98"]["asset"], "HYPE")
        self.assertEqual(by_sg["decided"]["count"], 5,
                         "ghost-fill row deflated by exactly the excess (5)")
        # total_cost_cents proportional: new_count * old_avg = 5 * 97 = 485
        self.assertEqual(by_sg["decided"]["total_cost_cents"], 5 * 97,
                         "total_cost_cents must rescale with new count at unchanged avg")
        # avg_price_cents preserved (N2: deflation does NOT touch avg).
        self.assertEqual(by_sg["decided"]["avg_price_cents"], 97,
                         "avg_price_cents on deflated ghost row must be preserved")

    def test_no_ghost_fill_row_keeps_warning_and_does_not_mutate(self):
        """When local > api but NO ghost-fill row exists, the auto-fix is
        UNSAFE (we can't identify the phantom rows). Old warning behavior
        preserved — no mutation."""
        sm = self._fresh()
        ticker = "KXHYPE15M-NOFIX"
        self._seed_row(sm, ticker=ticker, strategy_group="terminal_momentum_98",
                       count=10, avg=98, fill_source="ioc")
        self._seed_row(sm, ticker=ticker, strategy_group="decided",
                       count=10, avg=97, fill_source="ioc")
        client = self._mock_client(ticker, api_count=15)

        with self.assertLogs(level="WARNING") as cm:
            sm.reconcile_with_api(client)

        msgs = "\n".join(cm.output)
        self.assertIn("RECONCILE_MULTI_MISMATCH", msgs)
        self.assertIn(ticker, msgs)

        rows = sm.conn.execute(
            "SELECT strategy_group, count FROM positions "
            "WHERE ticker=? AND status='open'", (ticker,)
        ).fetchall()
        by_sg = {dict(r)["strategy_group"]: dict(r)["count"] for r in rows}
        self.assertEqual(by_sg["terminal_momentum_98"], 10,
                         "no ghost-fill row → no mutation, ioc row stays at 10")
        self.assertEqual(by_sg["decided"], 10,
                         "no ghost-fill row → no mutation, decided row stays at 10")

    def test_no_warning_when_local_matches_api(self):
        """Local sum == API count — no mismatch, no warning, no mutation."""
        sm = self._fresh()
        ticker = "KXHYPE15M-MATCH"
        self._seed_row(sm, ticker=ticker, strategy_group="terminal_momentum_98",
                       count=5, avg=98, fill_source="ioc")
        self._seed_row(sm, ticker=ticker, strategy_group="decided",
                       count=5, avg=97, fill_source="ghost_fill_positions_api")
        client = self._mock_client(ticker, api_count=10)

        # capture all warning logs
        with self.assertLogs(level="WARNING") as cm:
            # Need at least one warning for assertLogs to not raise; emit
            # a sentinel so the harness is happy. Then check that
            # RECONCILE_MULTI_MISMATCH is NOT among the captured warnings.
            logging.warning("test_sentinel_no_op")
            sm.reconcile_with_api(client)
        joined = "\n".join(cm.output)
        self.assertNotIn("RECONCILE_MULTI_MISMATCH", joined)

    def test_local_less_than_api_keeps_warning(self):
        """Local < API (under-count, possible missed maker fill). Auto-fix
        only inflates ghost-fill rows in the EXCESS direction; under-count
        is a different bug class — keep warning, no mutation."""
        sm = self._fresh()
        ticker = "KXHYPE15M-UNDER"
        self._seed_row(sm, ticker=ticker, strategy_group="terminal_momentum_98",
                       count=5, avg=98, fill_source="ioc")
        self._seed_row(sm, ticker=ticker, strategy_group="decided",
                       count=5, avg=97, fill_source="ghost_fill_positions_api")
        client = self._mock_client(ticker, api_count=20)

        with self.assertLogs(level="WARNING") as cm:
            sm.reconcile_with_api(client)
        msgs = "\n".join(cm.output)
        self.assertIn("RECONCILE_MULTI_MISMATCH", msgs)

        # No mutation of either row.
        rows = sm.conn.execute(
            "SELECT strategy_group, count FROM positions "
            "WHERE ticker=? AND status='open'", (ticker,)
        ).fetchall()
        by_sg = {dict(r)["strategy_group"]: dict(r)["count"] for r in rows}
        self.assertEqual(by_sg["terminal_momentum_98"], 5)
        self.assertEqual(by_sg["decided"], 5)

    def test_excess_equals_ghost_sum_deletes_all_ghost_rows(self):
        """Edge case (R2-Q5): excess==ghost_sum → ALL ghost rows reduce to 0,
        ALL DELETED. Surviving non-ghost rows match API truth.
        """
        sm = self._fresh()
        ticker = "KXHYPE15M-EQ"
        self._seed_row(sm, ticker=ticker, strategy_group="terminal_momentum_98",
                       count=20, avg=98, fill_source="ioc")
        self._seed_row(sm, ticker=ticker, strategy_group="ghost_a",
                       count=10, avg=98, fill_source="ghost_fill_positions_api")
        self._seed_row(sm, ticker=ticker, strategy_group="ghost_b",
                       count=10, avg=97, fill_source="ghost_fill_positions_api")
        client = self._mock_client(ticker, api_count=20)

        sm.reconcile_with_api(client)

        sgs_remaining = {
            dict(r)["strategy_group"] for r in sm.conn.execute(
                "SELECT strategy_group FROM positions WHERE ticker=?", (ticker,)
            ).fetchall()
        }
        self.assertEqual(sgs_remaining, {"terminal_momentum_98"},
                         "both ghost rows DELETED; only the ioc row survives")

    def test_multiple_ghost_fill_rows_deflated_proportionally(self):
        """2 ghost-fill rows (count=30 + count=29 = 59 total), API=29 (delta=30).
        Deflate proportionally so the sum lands exactly at 29."""
        sm = self._fresh()
        ticker = "KXHYPE15M-MULTIGHOST"
        self._seed_row(sm, ticker=ticker, strategy_group="ghost_a",
                       count=30, avg=98, fill_source="ghost_fill_positions_api")
        self._seed_row(sm, ticker=ticker, strategy_group="ghost_b",
                       count=29, avg=97, fill_source="ghost_fill_positions_api")
        client = self._mock_client(ticker, api_count=29)

        sm.reconcile_with_api(client)

        open_sum = sm.conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM positions "
            "WHERE ticker=? AND status='open'", (ticker,)
        ).fetchone()[0]
        self.assertEqual(open_sum, 29,
                         "post auto-fix open-row sum must equal API truth")

        # Both rows reduced (each by ~15 — not strict per-row equality
        # since rounding can land slightly differently).
        rows = sm.conn.execute(
            "SELECT strategy_group, count FROM positions "
            "WHERE ticker=? AND status='open'", (ticker,)
        ).fetchall()
        by_sg = {dict(r)["strategy_group"]: dict(r)["count"] for r in rows}
        self.assertLess(by_sg.get("ghost_a", 0), 30,
                        "ghost_a must be reduced from 30")
        self.assertLess(by_sg.get("ghost_b", 0), 29,
                        "ghost_b must be reduced from 29")

    # ── Phase 1: richer evidence log ─────────────────────────────────────

    def test_warning_log_includes_per_row_evidence(self):
        """When the warning fires (no auto-fix possible), the log line(s)
        must include per-row strategy_group + count + fill_source + delta
        so retroactive cleanup can reconstruct from logs."""
        sm = self._fresh()
        ticker = "KXHYPE15M-EVIDENCE"
        self._seed_row(sm, ticker=ticker, strategy_group="terminal_momentum_98",
                       count=10, avg=98, fill_source="ioc")
        self._seed_row(sm, ticker=ticker, strategy_group="decided",
                       count=10, avg=97, fill_source="ioc")
        client = self._mock_client(ticker, api_count=15)

        with self.assertLogs(level="WARNING") as cm:
            sm.reconcile_with_api(client)

        joined = "\n".join(cm.output)
        # Per-row evidence: strategy_group names + fill_source values
        self.assertIn("terminal_momentum_98", joined,
                      "evidence log must name the terminal_momentum_98 row")
        self.assertIn("decided", joined,
                      "evidence log must name the decided row")
        self.assertIn("ioc", joined,
                      "evidence log must include the fill_source values")
        # Delta = local_total - api_total = 20 - 15 = 5
        self.assertRegex(joined, r"\bdelta=5\b",
                         "evidence log must include 'delta=5' as a token")


if __name__ == "__main__":
    unittest.main()
