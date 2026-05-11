"""Backfill harness for the Phase B "shadow coverage" columns that can be
derived from existing DB state OR public APIs.

Tested phases (this file covers Phase G-1 SQL-only backfills):

  G-1 trivial backfills (no API):
    - `final_spot_price` ← `spot_price` (same value)
    - `maker_price_cents` ← `yes_bid_cents + 1` if (market_price - yes_bid_cents) > 1
    - `maker_depth_at_post` ← parse `orderbook_levels_json` for sum at maker_price level
    - `recent_n_outcome_streak` ← consecutive same-direction settled_trades.pnl outcomes
                                  with settled_at <= row.evaluation_time
    - `time_since_last_fill_s` ← row.evaluation_time minus MAX(prior fill ts) over
                                  positions.opened_at + settled_trades.settled_at

Future phases (separate test files / commits):
  - G-2: Coinbase candles → btc/eth/sol/xrp_spot_at_decision
  - G-3: cal_mlp annotation on historical shadows
  - G-4: Path metrics from candles (time_above/below, excursion, knockout)
  - G-5: OKX + Deribit funding rates

Master plan: kb/decisions/shadow-coverage-expansion-may01.md.
"""

import json
import os
import sys

import pytest
import bot.state  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))


def _make_db_with_eval_schema(tmp_path):
    """Construct a fresh sqlite DB with the production evaluated_opportunities
    schema (via bot.state.StateManager) so backfill SQL exercises real columns."""
    import bot
    db_path = str(tmp_path / "test.db")
    sm = bot.state.StateManager(db_path)
    return sm


class TestG1FinalSpotPriceBackfill:
    """`final_spot_price` is just a copy of `spot_price` — the simplest
    backfill in the harness."""

    def test_backfill_copies_spot_price(self, tmp_path):
        from shadow_coverage_backfill import backfill_final_spot_price
        sm = _make_db_with_eval_schema(tmp_path)
        # Insert via RAW SQL (bypasses the live auto-fill block which would
        # populate final_spot_price at insert-time — simulating a HISTORICAL
        # row that pre-dates the Phase F live-capture path).
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, spot_price, product_type, status) "
            "VALUES ('HAS-SPOT', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', 67432.5, '15m', 'pending')"
        )
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, spot_price, product_type, status) "
            "VALUES ('NO-SPOT', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', NULL, '15m', 'pending')"
        )
        sm.conn.commit()
        # Pre-condition: final_spot_price is NULL on both.
        rows = sm.conn.execute(
            "SELECT ticker, final_spot_price FROM evaluated_opportunities"
        ).fetchall()
        assert all(r["final_spot_price"] is None for r in rows)

        n = backfill_final_spot_price(sm.conn)
        assert n == 1, f"expected 1 row updated (the spot=non-NULL one), got {n}"

        rows = {
            r["ticker"]: r["final_spot_price"]
            for r in sm.conn.execute(
                "SELECT ticker, final_spot_price FROM evaluated_opportunities"
            )
        }
        assert rows["HAS-SPOT"] == pytest.approx(67432.5)
        assert rows["NO-SPOT"] is None  # NULL stays NULL

    def test_backfill_idempotent(self, tmp_path):
        """Running twice doesn't double-update (and doesn't overwrite
        rows that already had final_spot_price written by the live path)."""
        from shadow_coverage_backfill import backfill_final_spot_price
        sm = _make_db_with_eval_schema(tmp_path)
        # LIVE row: final_spot_price already populated (live capture path).
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, spot_price, final_spot_price, "
            "product_type, status) "
            "VALUES ('LIVE', 'E', 'BTC', 'candidate', '2026-05-02T10:00:00Z', "
            "67500.0, 99999.0, '15m', 'pending')"
        )
        # OLD row: final_spot_price NULL (pre-Phase-F historical).
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, spot_price, product_type, status) "
            "VALUES ('OLD', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', 67500.0, '15m', 'pending')"
        )
        sm.conn.commit()
        n = backfill_final_spot_price(sm.conn)
        assert n == 1  # only the OLD row, not LIVE

        live = sm.conn.execute(
            "SELECT final_spot_price FROM evaluated_opportunities WHERE ticker='LIVE'"
        ).fetchone()
        assert live["final_spot_price"] == 99999.0  # untouched

        # Run again — should be a no-op.
        n2 = backfill_final_spot_price(sm.conn)
        assert n2 == 0


class TestG1MakerCounterfactualBackfill:
    """`maker_price_cents` + `maker_depth_at_post` are derivable from
    `yes_bid_cents`, `market_price`, and `orderbook_levels_json` (where
    populated). Where ladder JSON is missing, depth defaults to 0."""

    def test_backfill_wide_spread_populates_maker_price(self, tmp_path):
        from shadow_coverage_backfill import backfill_maker_counterfactual
        sm = _make_db_with_eval_schema(tmp_path)
        sm.insert_evaluated_opportunity(
            ticker="WIDE", event_ticker="E", asset="BTC",
            filter_stage="low_price_shadow",
            yes_bid_cents=75, market_price=80,
            product_type="15m",
        )
        n = backfill_maker_counterfactual(sm.conn)
        assert n == 1
        row = sm.conn.execute(
            "SELECT maker_price_cents, maker_depth_at_post "
            "FROM evaluated_opportunities WHERE ticker='WIDE'"
        ).fetchone()
        assert row["maker_price_cents"] == 76
        # No ladder → depth is NULL (Phase G-1 round-1 fix distinguishes
        # "no ladder data" from "ladder present, level absent = 0").
        assert row["maker_depth_at_post"] is None

    def test_backfill_tight_spread_writes_null(self, tmp_path):
        from shadow_coverage_backfill import backfill_maker_counterfactual
        sm = _make_db_with_eval_schema(tmp_path)
        sm.insert_evaluated_opportunity(
            ticker="TIGHT", event_ticker="E", asset="BTC",
            filter_stage="low_price_shadow",
            yes_bid_cents=79, market_price=80,
            product_type="15m",
        )
        n = backfill_maker_counterfactual(sm.conn)
        # Row was processed but writes NULL — count includes it.
        assert n == 1
        row = sm.conn.execute(
            "SELECT maker_price_cents, maker_depth_at_post "
            "FROM evaluated_opportunities WHERE ticker='TIGHT'"
        ).fetchone()
        assert row["maker_price_cents"] is None
        assert row["maker_depth_at_post"] is None

    def test_backfill_uses_ladder_for_depth_when_present(self, tmp_path):
        from shadow_coverage_backfill import backfill_maker_counterfactual
        sm = _make_db_with_eval_schema(tmp_path)
        ladder = json.dumps({
            "yes_bids": [[76, 50], [75, 100]],
            "yes_asks": [[80, 200]],
        })
        sm.insert_evaluated_opportunity(
            ticker="LADDER", event_ticker="E", asset="BTC",
            filter_stage="low_price_shadow",
            yes_bid_cents=75, market_price=80,
            orderbook_levels_json=ladder,
            product_type="15m",
        )
        backfill_maker_counterfactual(sm.conn)
        row = sm.conn.execute(
            "SELECT maker_price_cents, maker_depth_at_post "
            "FROM evaluated_opportunities WHERE ticker='LADDER'"
        ).fetchone()
        assert row["maker_price_cents"] == 76
        assert row["maker_depth_at_post"] == 50

    def test_backfill_skips_already_populated_rows(self, tmp_path):
        from shadow_coverage_backfill import backfill_maker_counterfactual
        sm = _make_db_with_eval_schema(tmp_path)
        sm.insert_evaluated_opportunity(
            ticker="LIVE-MAKER", event_ticker="E", asset="BTC",
            filter_stage="candidate",
            yes_bid_cents=75, market_price=80,
            maker_price_cents=999, maker_depth_at_post=999,
            product_type="15m",
        )
        n = backfill_maker_counterfactual(sm.conn)
        assert n == 0
        row = sm.conn.execute(
            "SELECT maker_price_cents FROM evaluated_opportunities "
            "WHERE ticker='LIVE-MAKER'"
        ).fetchone()
        assert row["maker_price_cents"] == 999


class TestG1RecentStreakBackfill:
    """`recent_n_outcome_streak` = signed streak over settled_trades with
    settled_at <= row.evaluation_time. Pushes break the streak."""

    def test_backfill_three_wins_then_eval_yields_streak_3(self, tmp_path):
        from shadow_coverage_backfill import backfill_recent_streak
        sm = _make_db_with_eval_schema(tmp_path)
        # Three wins finishing before the eval row.
        for i in range(3):
            sm.conn.execute(
                "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
                "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
                "settled_at) VALUES (?, 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, ?)",
                (f"W-{i}", f"2026-05-01T1{i}:00:00Z"),
            )
        sm.conn.commit()
        sm.insert_evaluated_opportunity(
            ticker="EVAL", event_ticker="E", asset="BTC",
            filter_stage="low_price_shadow", product_type="15m",
        )
        # Backfill uses the row's evaluation_time (NOW-ish).
        n = backfill_recent_streak(sm.conn)
        assert n == 1
        row = sm.conn.execute(
            "SELECT recent_n_outcome_streak FROM evaluated_opportunities "
            "WHERE ticker='EVAL'"
        ).fetchone()
        assert row["recent_n_outcome_streak"] == 3

    def test_backfill_only_counts_settlements_before_eval_time(self, tmp_path):
        """Settlements AFTER the row's evaluation_time must NOT contribute
        to that row's streak — would be future leakage."""
        from shadow_coverage_backfill import backfill_recent_streak
        sm = _make_db_with_eval_schema(tmp_path)
        # Insert eval row at a fixed past time.
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, product_type, status) "
            "VALUES ('EVAL-OLD', 'E', 'BTC', 'low_price_shadow', "
            "'2026-05-01T10:00:00Z', '15m', 'pending')"
        )
        # Win BEFORE the eval row.
        sm.conn.execute(
            "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES ('OLD-WIN', 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, "
            "'2026-05-01T09:00:00Z')"
        )
        # Win AFTER — must be ignored.
        sm.conn.execute(
            "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES ('FUTURE-WIN', 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, "
            "'2026-05-01T11:00:00Z')"
        )
        sm.conn.commit()
        backfill_recent_streak(sm.conn)
        row = sm.conn.execute(
            "SELECT recent_n_outcome_streak FROM evaluated_opportunities "
            "WHERE ticker='EVAL-OLD'"
        ).fetchone()
        # Only the prior win counts — streak +1.
        assert row["recent_n_outcome_streak"] == 1


class TestG1TimeSinceLastFillBackfill:
    """`time_since_last_fill_s` = row.evaluation_time - MAX(prior fill ts)
    over positions.opened_at UNION settled_trades.settled_at."""

    def test_backfill_uses_max_over_positions_and_settled(self, tmp_path):
        from shadow_coverage_backfill import backfill_tslf
        sm = _make_db_with_eval_schema(tmp_path)
        # Eval row at fixed timestamp.
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, product_type, status) "
            "VALUES ('EVAL', 'E', 'BTC', 'low_price_shadow', "
            "'2026-05-01T10:00:00Z', '15m', 'pending')"
        )
        # Open position 5 min before eval.
        sm.conn.execute(
            "INSERT INTO positions(ticker, event_ticker, asset, side, count, "
            "avg_price_cents, total_cost_cents, opened_at, updated_at, status) "
            "VALUES ('POS', 'E', 'BTC', 'yes', 1, 80, 80, "
            "'2026-05-01T09:55:00Z', '2026-05-01T09:55:00Z', 'open')"
        )
        # Settled trade 10 min before eval (older).
        sm.conn.execute(
            "INSERT INTO settled_trades(ticker, event_ticker, asset, market_result, "
            "side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents, "
            "settled_at) VALUES ('OLD', 'E', 'BTC', 'yes', 'yes', 1, 80, 100, 1, 20, "
            "'2026-05-01T09:50:00Z')"
        )
        sm.conn.commit()
        backfill_tslf(sm.conn)
        row = sm.conn.execute(
            "SELECT time_since_last_fill_s FROM evaluated_opportunities "
            "WHERE ticker='EVAL'"
        ).fetchone()
        # 5 min = 300s — uses positions.opened_at (more recent).
        assert row["time_since_last_fill_s"] == pytest.approx(300.0, abs=1.0)


class TestEnsureIndexes:
    """Phase G-1 round 2 LOW regression: a future refactor that typos
    a column name or drops a CREATE INDEX silently regresses streak/tslf
    to full-table scans (slow but correct → tests still pass)."""

    def test_indexes_present_after_call(self, tmp_path):
        from shadow_coverage_backfill import ensure_indexes
        sm = _make_db_with_eval_schema(tmp_path)
        ensure_indexes(sm.conn)
        idx_names = {
            r[0] for r in sm.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_st_settled_at" in idx_names
        assert "idx_pos_opened_at" in idx_names

    def test_idempotent(self, tmp_path):
        """Re-runs are no-op (don't error)."""
        from shadow_coverage_backfill import ensure_indexes
        sm = _make_db_with_eval_schema(tmp_path)
        ensure_indexes(sm.conn)
        ensure_indexes(sm.conn)  # must not raise

    def test_does_not_create_eval_opp_index(self, tmp_path):
        """Phase G-1 round 2: must NOT create an index on
        evaluated_opportunities (142K-row table; build would block live
        writers ~30-60s)."""
        from shadow_coverage_backfill import ensure_indexes
        sm = _make_db_with_eval_schema(tmp_path)
        ensure_indexes(sm.conn)
        eval_opp_indexes = sm.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='evaluated_opportunities'"
        ).fetchall()
        # Pre-existing schema indexes (idx_eval_opp_*) are OK; only the
        # backfill-added one (idx_eval_opp_id_eval_time) should be absent.
        names = {r[0] for r in eval_opp_indexes}
        assert "idx_eval_opp_id_eval_time" not in names


class TestBackfillCheckpointing:
    """Each phase must be resumable. The harness writes a per-phase
    `last_id` checkpoint after each batch and starts from the checkpoint
    on resume. All phases share the same checkpoint dir."""

    def test_checkpoint_state_round_trips(self, tmp_path):
        from shadow_coverage_backfill import (
            read_checkpoint, write_checkpoint,
        )
        cp_dir = str(tmp_path / "checkpoints")
        write_checkpoint(cp_dir, "g1_final_spot", 12345)
        assert read_checkpoint(cp_dir, "g1_final_spot") == 12345
        # Phase that's never been run returns 0.
        assert read_checkpoint(cp_dir, "never_seen") == 0
