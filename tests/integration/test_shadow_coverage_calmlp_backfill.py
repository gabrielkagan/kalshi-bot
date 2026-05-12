"""Phase G-3: cal_mlp annotation backfill on historical 15M shadow rows.

The live bot's `CalMLPPostHocProcessor` daemon (scripts/cal_mlp/post_hoc_processor.py)
filters by `evaluation_time > now - 300s` (5-min recency) so rows older
than 5 min never get a v1 prediction. ~80K historical 15M shadow rows
are stranded without cal_mlp annotation, which means they can't feed
v2 retraining.

This phase has two stages:

  G-3a (SQL-only, this file's stamp_uuids_on_historical):
    Stamp `cal_mlp_request_id = uuid4().hex` on rows where:
      - product_type = '15m'
      - cal_mlp_request_id IS NULL
      - cal_mlp_skipped_reason IS NULL
      - raw_prob IS NOT NULL  (predict requirement; no point stamping
        rows the daemon would just stamp 'missing_features' on)
    Idempotent (only-IS-NULL filter), batched, checkpointed.

  G-3b (drain — needs ML deps loaded):
    Construct CalMLPPostHocProcessor with `recent_window_sec=10**9`
    (effectively infinite — bypasses the recency filter). Call
    `_process_one_batch` in a foreground loop until `rows_seen` per
    poll drops to 0.

Heavy ML deps (pandas/torch) only live in the drain — stamping is
pure SQL. Tests mock the processor for drain coverage.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest
import bot.state  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))


def _make_db(tmp_path):
    import bot
    import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)
    return bot.state.StateManager(str(tmp_path / "test.db"))


class TestStampUuidsOnHistorical:
    """Pure-SQL stamp logic (no ML deps)."""

    def test_stamps_rows_matching_predicate(self, tmp_path):
        from shadow_coverage_calmlp_backfill import stamp_uuids_on_historical
        sm = _make_db(tmp_path)
        # Row that SHOULD be stamped: 15M, no request_id, no skip, raw_prob set.
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, raw_prob, status) "
            "VALUES ('STAMP-ME', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', 0.85, 'pending')"
        )
        sm.conn.commit()

        n = stamp_uuids_on_historical(sm.conn)
        assert n == 1
        row = sm.conn.execute(
            "SELECT cal_mlp_request_id FROM evaluated_opportunities "
            "WHERE ticker='STAMP-ME'"
        ).fetchone()
        assert row["cal_mlp_request_id"] is not None
        # uuid4().hex is 32 hex chars.
        assert len(row["cal_mlp_request_id"]) == 32

    def test_skips_rows_with_existing_request_id(self, tmp_path):
        from shadow_coverage_calmlp_backfill import stamp_uuids_on_historical
        sm = _make_db(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, raw_prob, cal_mlp_request_id, status) "
            "VALUES ('LIVE', 'E', 'BTC', 'candidate', "
            "'2026-05-02T10:00:00Z', '15m', 0.85, 'existing_uuid', 'pending')"
        )
        sm.conn.commit()
        n = stamp_uuids_on_historical(sm.conn)
        assert n == 0
        row = sm.conn.execute(
            "SELECT cal_mlp_request_id FROM evaluated_opportunities "
            "WHERE ticker='LIVE'"
        ).fetchone()
        assert row["cal_mlp_request_id"] == "existing_uuid"  # untouched

    def test_skips_rows_with_skipped_reason(self, tmp_path):
        """Rows already marked skipped by the daemon (no_predictor,
        raw_prob_null, missing_features, etc.) must not be re-stamped."""
        from shadow_coverage_calmlp_backfill import stamp_uuids_on_historical
        sm = _make_db(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, raw_prob, cal_mlp_skipped_reason, status) "
            "VALUES ('SKIPPED', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', 0.85, 'no_predictor', 'pending')"
        )
        sm.conn.commit()
        n = stamp_uuids_on_historical(sm.conn)
        assert n == 0

    def test_skips_rows_without_raw_prob(self, tmp_path):
        """Daemon would stamp 'missing_features' on rows with no
        raw_prob. We pre-filter those — no point stamping a uuid the
        daemon will only mark skipped."""
        from shadow_coverage_calmlp_backfill import stamp_uuids_on_historical
        sm = _make_db(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, raw_prob, status) "
            "VALUES ('NO-RAW-PROB', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', NULL, 'pending')"
        )
        sm.conn.commit()
        n = stamp_uuids_on_historical(sm.conn)
        assert n == 0

    def test_skips_non_15m_rows(self, tmp_path):
        """Daemon's SELECT filters product_type='15m'. Stamping non-15M
        rows wouldn't cause harm but would inflate the daemon's queue
        with rows it'll never process. Pre-filter at stamp time."""
        from shadow_coverage_calmlp_backfill import stamp_uuids_on_historical
        sm = _make_db(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, raw_prob, status) "
            "VALUES ('HOURLY', 'E', 'BTC', 'hourly_observation', "
            "'2026-04-15T10:00:00Z', 'hourly', 0.85, 'pending')"
        )
        sm.conn.commit()
        n = stamp_uuids_on_historical(sm.conn)
        assert n == 0

    def test_idempotent(self, tmp_path):
        """Running twice on the same DB stamps once total."""
        from shadow_coverage_calmlp_backfill import stamp_uuids_on_historical
        sm = _make_db(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, raw_prob, status) "
            "VALUES ('R1', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', 0.85, 'pending')"
        )
        sm.conn.commit()
        n1 = stamp_uuids_on_historical(sm.conn)
        n2 = stamp_uuids_on_historical(sm.conn)
        assert n1 == 1
        assert n2 == 0  # already stamped

    def test_unique_uuids_per_row(self, tmp_path):
        """Each stamped row must get a DIFFERENT uuid (collisions break
        daemon's WHERE cal_mlp_request_id=? UPDATEs)."""
        from shadow_coverage_calmlp_backfill import stamp_uuids_on_historical
        sm = _make_db(tmp_path)
        for i in range(5):
            sm.conn.execute(
                "INSERT INTO evaluated_opportunities("
                "ticker, event_ticker, asset, filter_stage, evaluation_time, "
                "product_type, raw_prob, status) "
                "VALUES (?, 'E', 'BTC', 'low_price_shadow', "
                "'2026-04-15T10:00:00Z', '15m', 0.85, 'pending')",
                (f"UNIQ-{i}",)
            )
        sm.conn.commit()
        n = stamp_uuids_on_historical(sm.conn)
        assert n == 5
        uuids = [
            r["cal_mlp_request_id"]
            for r in sm.conn.execute(
                "SELECT cal_mlp_request_id FROM evaluated_opportunities"
            )
        ]
        assert len(set(uuids)) == 5  # all distinct


class TestSymbolsExistOnRealClasses:
    """Phase G-3 round 1 C3 regression: pure mocks let C1/C2 (missing
    `warmup` symbol + missing `_poll_and_process`) ship undetected.
    These AST-level checks catch the symbol-existence bug class
    immediately, even when the drain test uses MagicMock."""

    def test_calmlp_predictor_class_exists(self):
        """Drain CLI imports `CalMLPPredictor` from integration."""
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "cal_mlp"))
        from integration import CalMLPPredictor
        assert hasattr(CalMLPPredictor, "warmup")

    def test_post_hoc_processor_method_name(self):
        """Drain CLI calls `processor._process_one_batch(conn)`. If a
        future refactor renames it to `_poll_and_process` (or anything
        else), this test fails immediately."""
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "cal_mlp"))
        from post_hoc_processor import CalMLPPostHocProcessor
        assert hasattr(CalMLPPostHocProcessor, "_process_one_batch"), (
            "CalMLPPostHocProcessor._process_one_batch missing — drain "
            "CLI calls this method; rename will silently break backfill."
        )


class TestDrainViaProcessor:
    """Drain logic that wraps CalMLPPostHocProcessor. Mocks the heavy
    bits (predictor, _process_one_batch)."""

    def test_drain_loops_until_rows_seen_zero(self, tmp_path):
        from shadow_coverage_calmlp_backfill import drain_via_processor
        # Build a fake processor that reports rows_seen via metrics dict.
        # First two ticks: 50 each. Third tick: 0. Drain stops.
        processor = MagicMock()
        rows_seen_log = [50, 50, 0]
        call_count = {"n": 0}

        def _fake_poll(_conn):
            i = call_count["n"]
            processor._metrics = {"rows_seen": sum(rows_seen_log[:i + 1])}
            processor._last_tick_rows_seen = rows_seen_log[i]
            call_count["n"] += 1

        processor._process_one_batch.side_effect = _fake_poll
        # The drain function uses _last_tick_rows_seen on the processor;
        # we set it via side_effect.

        drain_via_processor(processor, MagicMock(), max_iterations=10)
        assert call_count["n"] == 3  # called 3 times (third returns 0 → stop)

    def test_drain_caps_at_max_iterations(self, tmp_path):
        """If rows keep coming (somehow — buggy daemon, infinite live
        writes), bail at max_iterations to prevent runaway."""
        from shadow_coverage_calmlp_backfill import drain_via_processor
        processor = MagicMock()
        call_count = {"n": 0}

        def _fake_poll(_conn):
            call_count["n"] += 1
            processor._last_tick_rows_seen = 1  # never zero

        processor._process_one_batch.side_effect = _fake_poll
        drain_via_processor(processor, MagicMock(), max_iterations=3)
        assert call_count["n"] == 3
