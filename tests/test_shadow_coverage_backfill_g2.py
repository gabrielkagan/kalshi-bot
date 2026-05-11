"""Phase G-2: Coinbase candles → btc/eth/sol/xrp_spot_at_decision backfill.

Coinbase Exchange `/products/{X}-USD/candles` endpoint provides 1-min OHLCV
candles publicly without auth. Returns `[time, low, high, open, close, volume]`
per candle, max 300 per request.

Backfill strategy:
  1. For each asset BTC/ETH/SOL/XRP, paginate the Coinbase API across
     the historical evaluation_time range (clamped to [earliest, latest]
     of evaluated_opportunities rows).
  2. Build a per-asset minute-resolution lookup table: {epoch_minute → close}.
  3. For each row missing cross-asset spots, look up each asset's close
     at the row's evaluation_time minute. UPDATE the row.

Tested in isolation — Coinbase API mocked.
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
    import bot
    sm = bot.state.StateManager(str(tmp_path / "test.db"))
    return sm


class TestG2CoinbaseLookupBuilder:
    """Unit-level: convert raw Coinbase candle responses → minute-keyed
    lookup dict."""

    def test_build_lookup_indexes_by_epoch_minute(self):
        from shadow_coverage_backfill import build_candle_lookup
        # Coinbase format: [time_epoch_sec, low, high, open, close, volume]
        candles = [
            [1776211800, 74572.75, 74667.21, 74667.21, 74589.99, 13.9],
            [1776211740, 74656.0, 74726.93, 74704.63, 74666.6, 9.13],
        ]
        lookup = build_candle_lookup(candles)
        # Keyed by epoch-minute (epoch-second // 60).
        assert lookup[1776211800 // 60] == pytest.approx(74589.99)
        assert lookup[1776211740 // 60] == pytest.approx(74666.6)

    def test_build_lookup_handles_empty_input(self):
        from shadow_coverage_backfill import build_candle_lookup
        assert build_candle_lookup([]) == {}

    def test_build_lookup_skips_malformed_rows(self):
        from shadow_coverage_backfill import build_candle_lookup
        candles = [
            [1776211800, 74572.75, 74667.21, 74667.21, 74589.99, 13.9],
            ["bad", "row"],  # malformed
            None,             # malformed
            [1776211740, 74656.0, 74726.93, 74704.63, 74666.6, 9.13],
        ]
        lookup = build_candle_lookup(candles)
        assert len(lookup) == 2  # only the 2 valid rows


class TestG2RowSpotLookup:
    """Unit-level: given a row evaluation_time + per-asset lookup, the
    helper returns 4-tuple of spot prices (or None if minute absent)."""

    def test_lookup_returns_close_at_eval_minute(self):
        from shadow_coverage_backfill import lookup_xasset_spots_for_row
        # ISO-8601 → epoch_min = 29603530 (UTC: 2026-04-15T10:00Z minute index)
        eval_time = "2026-04-15T10:00:00Z"
        # Build minute-keyed lookups with deterministic values.
        import datetime as _dt
        epoch_sec = int(_dt.datetime(2026, 4, 15, 10, 0, 0,
                                       tzinfo=_dt.timezone.utc).timestamp())
        epoch_min = epoch_sec // 60
        lookups = {
            "BTC": {epoch_min: 67432.5},
            "ETH": {epoch_min: 3210.5},
            "SOL": {epoch_min: 142.7},
            "XRP": {epoch_min: 2.51},
        }
        out = lookup_xasset_spots_for_row(eval_time, lookups)
        assert out["btc_spot_at_decision"] == pytest.approx(67432.5)
        assert out["eth_spot_at_decision"] == pytest.approx(3210.5)
        assert out["sol_spot_at_decision"] == pytest.approx(142.7)
        assert out["xrp_spot_at_decision"] == pytest.approx(2.51)

    def test_lookup_returns_none_for_missing_minute(self):
        from shadow_coverage_backfill import lookup_xasset_spots_for_row
        out = lookup_xasset_spots_for_row(
            "2026-04-15T10:00:00Z",
            {"BTC": {}, "ETH": {}, "SOL": {}, "XRP": {}},
        )
        assert all(v is None for v in out.values())

    def test_lookup_falls_back_to_nearest_minute_within_3min(self):
        """If exact minute is missing, fall back to nearest within ±3 min.
        Beyond that, return None — staler is dishonest for a minute-grade
        feature."""
        from shadow_coverage_backfill import lookup_xasset_spots_for_row
        import datetime as _dt
        eval_time = "2026-04-15T10:00:30Z"
        eval_min = int(_dt.datetime(2026, 4, 15, 10, 0, 0,
                                       tzinfo=_dt.timezone.utc).timestamp()) // 60
        # Only have a candle 2 min before — within ±3 min window → use it.
        lookups = {
            "BTC": {eval_min - 2: 67000.0},
            "ETH": {}, "SOL": {}, "XRP": {},
        }
        out = lookup_xasset_spots_for_row(eval_time, lookups)
        assert out["btc_spot_at_decision"] == pytest.approx(67000.0)


class TestG2BackfillIntegrationWithMockedAPI:
    """End-to-end: backfill_xasset_spots fetches candles via injected
    fetcher, then UPDATEs rows."""

    def test_backfill_writes_xasset_spots(self, tmp_path):
        from shadow_coverage_backfill import backfill_xasset_spots
        sm = _make_db_with_eval_schema(tmp_path)
        # Insert a historical row missing cross-asset spots.
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, product_type, status) "
            "VALUES ('TEST', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', 'pending')"
        )
        sm.conn.commit()

        # Mock fetcher: returns canned candles for each asset.
        import datetime as _dt
        epoch_sec = int(_dt.datetime(2026, 4, 15, 10, 0, 0,
                                       tzinfo=_dt.timezone.utc).timestamp())
        def _mock_fetch(asset, start_iso, end_iso):
            # T1.5: COINBASE_PRODUCTS extended to 6 assets — mock must
            # cover DOGE+HYPE or backfill_xasset_spots will KeyError on
            # the new asset iteration.
            base = {"BTC": 67432.5, "ETH": 3210.5,
                    "SOL": 142.7, "XRP": 2.51,
                    "DOGE": 0.18, "HYPE": 24.5}[asset]
            return [[epoch_sec, base, base, base, base, 1.0]]

        n = backfill_xasset_spots(sm.conn, fetcher=_mock_fetch, batch_size=10)
        assert n == 1
        row = sm.conn.execute(
            "SELECT btc_spot_at_decision, eth_spot_at_decision, "
            "sol_spot_at_decision, xrp_spot_at_decision "
            "FROM evaluated_opportunities WHERE ticker='TEST'"
        ).fetchone()
        assert row["btc_spot_at_decision"] == pytest.approx(67432.5)
        assert row["eth_spot_at_decision"] == pytest.approx(3210.5)
        assert row["sol_spot_at_decision"] == pytest.approx(142.7)
        assert row["xrp_spot_at_decision"] == pytest.approx(2.51)

    def test_backfill_aborts_when_asset_returns_zero_candles(self, tmp_path):
        """Phase G-2 round 1 HIGH regression: if Coinbase returns zero
        candles for any asset (e.g., complete API outage during backfill),
        the harness must REFUSE to write all-NULL spots — abort with
        CoinbaseFetchError instead. Prevents silent NULL pollution that
        an operator would mistake for a successful backfill."""
        from shadow_coverage_backfill import (
            backfill_xasset_spots, CoinbaseFetchError,
        )
        sm = _make_db_with_eval_schema(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, product_type, status) "
            "VALUES ('TEST', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', 'pending')"
        )
        sm.conn.commit()

        # Mock returns empty for ALL assets — simulating API outage.
        def _mock_empty(asset, start_iso, end_iso):
            return []

        with pytest.raises(CoinbaseFetchError, match="ZERO candles"):
            backfill_xasset_spots(sm.conn, fetcher=_mock_empty,
                                   batch_size=10, sleep_ms=0)
        # Critical: row's btc_spot_at_decision must STILL be NULL — no
        # garbage written before the abort.
        row = sm.conn.execute(
            "SELECT btc_spot_at_decision FROM evaluated_opportunities "
            "WHERE ticker='TEST'"
        ).fetchone()
        assert row["btc_spot_at_decision"] is None

    def test_backfill_skips_already_populated(self, tmp_path):
        from shadow_coverage_backfill import backfill_xasset_spots
        sm = _make_db_with_eval_schema(tmp_path)
        # Row that ALREADY has all 6 cross-asset spots populated (live
        # row, post-Bit-2). Bit 2 (2026-05-11) widened "already
        # populated" from "btc IS NOT NULL" to "all 6 IS NOT NULL" —
        # so a fully-populated row must include hype+doge to be
        # considered done. R1 adversarial review M1 fix.
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, product_type, status, "
            "btc_spot_at_decision, eth_spot_at_decision, "
            "sol_spot_at_decision, xrp_spot_at_decision, "
            "hype_spot_at_decision, doge_spot_at_decision) "
            "VALUES ('LIVE', 'E', 'BTC', 'candidate', "
            "'2026-05-02T10:00:00Z', '15m', 'pending', "
            "99999.0, 99999.0, 99999.0, 99999.0, 99999.0, 99999.0)"
        )
        sm.conn.commit()
        def _mock_fetch(asset, start_iso, end_iso):
            return []  # would return nothing; test verifies we never call it
        n = backfill_xasset_spots(sm.conn, fetcher=_mock_fetch, batch_size=10)
        assert n == 0
        row = sm.conn.execute(
            "SELECT btc_spot_at_decision FROM evaluated_opportunities "
            "WHERE ticker='LIVE'"
        ).fetchone()
        assert row["btc_spot_at_decision"] == 99999.0  # untouched
