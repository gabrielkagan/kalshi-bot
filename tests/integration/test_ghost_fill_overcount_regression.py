"""Regression for B1: ghost-fill retry double-counts via cumulative positions-API count.

2026-05-18 incident (KXHYPE15M-26MAY180530-30, ticket 86b9zuczz):
The `GHOST_FILL_DETECTED_VIA_POSITIONS` handler in `bot/executor.py` reads
Kalshi's positions API (which is ticker-level — sums across ALL local
strategy_groups) and passes that **cumulative** count to
`record_position_from_fill` as if it were the delta from this single fill
attempt. The dc_retry loop then accumulates the cumulative count into a
running total. After two ghost-fill events on a single trade attempt, the
bot's local books showed 121 contracts when Kalshi had only delivered 3 —
plus the 58 from a sibling terminal_momentum stack = 61 real on Kalshi vs
179 local. Reported as a $174 loss; real loss was $60.

Fix shape: compute the **delta** as
    delta = positions_api_count - local_count_across_all_strategy_groups
and only record the delta. Requires a new StateManager helper to sum local
positions across strategy_groups for a (ticker, side).

These tests are TDD-RED against the unfixed code:
  - The helper does not exist yet → AttributeError.
  - The delta-correct accounting test will fail with the old behavior.
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


class TestGhostFillOverCountRegression(unittest.TestCase):
    def _fresh(self):
        """Fresh StateManager + production-shaped composite PK on positions.

        The base `CREATE TABLE positions` in `bot/state.py` uses
        `ticker TEXT PRIMARY KEY` (single-column), but PRODUCTION sqlite was
        migrated long ago to `PRIMARY KEY (ticker, strategy_group)` (composite)
        — see `migrations/migrate_composite_pk.py` (the actual sqlite
        migration; `scripts/ops/supabase_migration_007_stacking.sql` is the
        Supabase mirror that runs the same shape on the replica). Without
        this rebuild, fresh test DBs cannot represent the stacked-strategies
        state that triggered the B1 incident.
        """
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        sm = bot.state.StateManager(db_path=tmp.name)
        # Rebuild positions table with composite PK to mirror production.
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

    # ── helper-method existence + correctness ────────────────────────────

    def test_get_local_position_count_for_ticker_sums_across_strategy_groups(self):
        """Helper must sum count across ALL open positions for (ticker, side),
        regardless of strategy_group. This is the truth source the ghost-fill
        handler needs to compute delta vs Kalshi's ticker-level positions API.
        """
        sm = self._fresh()
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=58, price_cents=98,
            strategy="terminal_momentum_98", is_taker=True,
            fill_source="ioc",
        )
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=3, price_cents=97,
            strategy="decided_t1", is_taker=True,
            fill_source="ioc",
        )

        # Cross-strategy-group sum.
        self.assertEqual(
            sm.get_local_position_count_for_ticker("KXHYPE-T", "yes"),
            61,
        )

    def test_helper_returns_zero_when_no_open_positions(self):
        sm = self._fresh()
        self.assertEqual(
            sm.get_local_position_count_for_ticker("KXHYPE-NEW", "yes"), 0,
        )

    def test_helper_ignores_closed_positions(self):
        """Settled / closed rows must NOT contribute to the local count —
        only status='open' counts as 'currently on-Kalshi'."""
        sm = self._fresh()
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=10, price_cents=97,
            strategy="decided_t1", is_taker=True,
            fill_source="ioc",
        )
        sm.conn.execute(
            "UPDATE positions SET status='settled' WHERE ticker=?",
            ("KXHYPE-T",),
        )
        sm.conn.commit()
        self.assertEqual(
            sm.get_local_position_count_for_ticker("KXHYPE-T", "yes"), 0,
        )

    def test_get_local_position_cost_for_ticker_sums_across_strategy_groups(self):
        """Cost helper (R1-M2): pair to the count helper. Sum total_cost_cents
        across all open positions for (ticker, side). Used by Layer B to
        attribute the new-contracts cost via `delta_cost = api_cost - local_cost`,
        avoiding the cumulative-avg-price drift when sibling-strategy fills
        landed at a different price than the new ghost-fill delta.
        """
        sm = self._fresh()
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=58, price_cents=98,
            strategy="terminal_momentum_98", is_taker=True, fill_source="ioc",
        )
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=3, price_cents=60,  # market moved; new fill at 60c
            strategy="decided_t1", is_taker=True, fill_source="ioc",
        )
        # Cumulative cost = 58*98 + 3*60 = 5684 + 180 = 5864
        self.assertEqual(
            sm.get_local_position_cost_for_ticker("KXHYPE-T", "yes"),
            5864,
        )

    def test_cost_delta_attributes_new_contracts_at_their_actual_price(self):
        """When sibling strategy filled 58 @ 98c and the new ghost-fill delta
        is 1 contract at 60c (market moved), the delta_cost = 5864-5684 = 180,
        so delta_avg = 180/1 = 60. The new contract is attributed at 60c, NOT
        at the cumulative-avg ~96c.
        """
        sm = self._fresh()
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=58, price_cents=98,
            strategy="terminal_momentum_98", is_taker=True, fill_source="ioc",
        )
        # Simulate the Layer B delta-cost calculation:
        api_count = 59
        api_cost = 58 * 98 + 1 * 60  # what Kalshi positions API would report
        local_count = sm.get_local_position_count_for_ticker("KXHYPE-T", "yes")
        local_cost = sm.get_local_position_cost_for_ticker("KXHYPE-T", "yes")
        delta_count = api_count - local_count
        delta_cost = api_cost - local_cost
        delta_avg = delta_cost // delta_count
        self.assertEqual(delta_count, 1)
        self.assertEqual(delta_cost, 60)
        self.assertEqual(delta_avg, 60,
                         "New contract attributed at actual fill price (60c), "
                         "not cumulative avg (~96c)")

    def test_helper_filters_by_side(self):
        """A YES position must not bleed into the NO sum and vice versa."""
        sm = self._fresh()
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=15, price_cents=97,
            strategy="decided_t1", is_taker=True, fill_source="ioc",
        )
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="no", count=7, price_cents=95,
            strategy="no_side_decided_t1", is_taker=True, fill_source="ioc",
        )
        self.assertEqual(
            sm.get_local_position_count_for_ticker("KXHYPE-T", "yes"), 15,
        )
        self.assertEqual(
            sm.get_local_position_count_for_ticker("KXHYPE-T", "no"), 7,
        )

    # ── HYPE incident scenario — full reproduction ───────────────────────

    def test_hype_incident_sibling_strategy_contamination(self):
        """Reproduces the 2026-05-18 HYPE incident:

        State at attempt-2:
          terminal_momentum_98 already filled 58 contracts on Kalshi (local + Kalshi).
          decided_t1 IOC submits count=1, fills 1 real contract on Kalshi.
          Local fill-polling MISSES the fill.
          Ghost-fill Layer B fires; Kalshi positions API returns
          position_count=59 (cumulative: 58 TM + 1 decided_t1).

        OLD (broken) behavior:
          record_position_from_fill(strategy='decided_t1', count=59)
          → local total = 58 (TM) + 59 (decided_t1) = 117  ❌

        NEW (correct) behavior — compute delta:
          local_count_for_ticker = 58
          delta = 59 - 58 = 1
          record_position_from_fill(strategy='decided_t1', count=1)
          → local total = 58 (TM) + 1 (decided_t1) = 59  ✓ matches Kalshi truth.
        """
        sm = self._fresh()
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=58, price_cents=98,
            strategy="terminal_momentum_98", is_taker=True,
            fill_source="ioc",
        )
        self.assertEqual(
            sm.get_local_position_count_for_ticker("KXHYPE-T", "yes"), 58,
        )

        # Ghost-fill #1: API shows 59, local has 58 → delta = 1
        api_count_1 = 59
        delta_1 = api_count_1 - sm.get_local_position_count_for_ticker(
            "KXHYPE-T", "yes")
        self.assertEqual(delta_1, 1,
                         "Delta must be the NEW contracts from this attempt")
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=delta_1, price_cents=97,
            strategy="decided_t1", is_taker=True,
            fill_source="ghost_fill_positions_api",
        )
        self.assertEqual(
            sm.get_local_position_count_for_ticker("KXHYPE-T", "yes"), 59,
            "After ghost-fill delta=1, local must match API truth=59",
        )

        # Ghost-fill #2: API now shows 61 (+2 since last ghost), local has 59
        # → delta = 2
        api_count_2 = 61
        delta_2 = api_count_2 - sm.get_local_position_count_for_ticker(
            "KXHYPE-T", "yes")
        self.assertEqual(delta_2, 2)
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=delta_2, price_cents=97,
            strategy="decided_t1", is_taker=True,
            fill_source="ghost_fill_positions_api",
        )

        # Final local must match Kalshi API truth (61), NOT the old buggy
        # accumulation (58 + 59 + 61 = 178).
        self.assertEqual(
            sm.get_local_position_count_for_ticker("KXHYPE-T", "yes"), 61,
            "Two ghost-fill events must NOT over-count to 178",
        )

    def test_ghost_fill_no_delta_when_api_matches_local(self):
        """When the positions API matches local exactly, delta=0 → skip recording.
        This is the steady-state after reconcile: positions API confirms what
        we already have. No new contracts means no new write.
        """
        sm = self._fresh()
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=10, price_cents=98,
            strategy="terminal_momentum_98", is_taker=True,
            fill_source="ioc",
        )
        api_count = 10
        delta = api_count - sm.get_local_position_count_for_ticker(
            "KXHYPE-T", "yes")
        self.assertEqual(delta, 0,
                         "When API matches local, delta must be 0 (no-op)")

    def test_ghost_fill_negative_delta_means_kalshi_already_caught_up(self):
        """If Kalshi positions API < local count, the local books are AHEAD of
        Kalshi (perhaps from a stale local write or a Kalshi-side rollback).
        Ghost-fill must not record a negative count; it must be a no-op.
        Subsequent reconcile is responsible for catching this divergence in
        the opposite direction (filed as B2).
        """
        sm = self._fresh()
        sm.record_position_from_fill(
            ticker="KXHYPE-T", event_ticker="KXHYPE", asset="HYPE",
            side="yes", count=15, price_cents=98,
            strategy="terminal_momentum_98", is_taker=True,
            fill_source="ioc",
        )
        api_count = 10  # Kalshi shows less than local
        delta = api_count - sm.get_local_position_count_for_ticker(
            "KXHYPE-T", "yes")
        self.assertLess(delta, 0,
                        "Negative delta = local ahead of API; must NOT record")


if __name__ == "__main__":
    unittest.main()
