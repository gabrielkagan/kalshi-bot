"""Phase G-4: path metrics backfill from Coinbase candles.

Per-row, computes the 4 path-metric fields populated live in Phase F:
  - time_above_strike_seconds (cumulative seconds spot was above strike)
  - time_below_strike_seconds (cumulative seconds spot was below strike)
  - max_excursion_from_strike (signed price; max-magnitude wins)
  - knockout_time_relative ((eval_ts - last_crossing) / (eval_ts - window_open))

Approach:
  Per row, the observation window = [eval_ts + seconds_to_close - 900, eval_ts]
  i.e., from the 15M-window-open through to evaluation_time. Pull all asset
  candles in that range, classify each as above/below strike, sum 60s per
  candle into the appropriate accumulator, find max |close - threshold|, and
  detect crossings between consecutive candles for the knockout time.

Per-row computation reuses the G-2 lookup dict (built once for the full
date range across all 4 assets, then reused per row). No new API calls.
"""

import os
import sys

import pytest
import bot.state  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))


def _make_db(tmp_path):
    import bot
    import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)
    return bot.state.StateManager(str(tmp_path / "test.db"))


def _candle(ts, close):
    """Coinbase format: [time, low, high, open, close, volume]."""
    return [ts, close, close, close, close, 1.0]


class TestG4ComputePathMetricsForRow:
    """Pure unit: given asset_lookup dict + row params, returns the
    4 path metrics."""

    def test_all_above_strike_full_window(self):
        """All candles above strike → time_above = 900s, below = 0,
        excursion positive, knockout = 1.0 (no crossings since open)."""
        from shadow_coverage_backfill import compute_path_metrics_for_row
        # Build candle lookup: 15 candles (one per minute of the window).
        # window = [eval_ts - 900, eval_ts]
        eval_ts_min = 29603530  # arbitrary epoch-minute
        # Phase G-4 round 2: range walks [eval-15..eval-1] (exclusive of
        # eval; Coinbase candles are bucket-start so eval's candle covers
        # post-eval time). Provide candles at i=1..15.
        lookup = {
            (eval_ts_min - i): 67500.0  # all 67500
            for i in range(1, 16)
        }
        out = compute_path_metrics_for_row(
            asset_lookup=lookup,
            eval_epoch_min=eval_ts_min,
            seconds_to_close=0.0,  # eval at window-close
            threshold=67000.0,  # spot > strike
        )
        assert out["time_above_strike_seconds"] == 900.0
        assert out["time_below_strike_seconds"] == 0.0
        # max_excursion = 67500 - 67000 = 500 (positive).
        assert out["max_excursion_from_strike"] == pytest.approx(500.0)
        # No crossings — fully decided since open.
        assert out["knockout_time_relative"] == pytest.approx(1.0)

    def test_all_below_strike_full_window(self):
        """Mirror image — all below."""
        from shadow_coverage_backfill import compute_path_metrics_for_row
        eval_ts_min = 29603530
        lookup = {(eval_ts_min - i): 66500.0 for i in range(1, 16)}
        out = compute_path_metrics_for_row(
            asset_lookup=lookup, eval_epoch_min=eval_ts_min,
            seconds_to_close=0.0, threshold=67000.0,
        )
        assert out["time_above_strike_seconds"] == 0.0
        assert out["time_below_strike_seconds"] == 900.0
        # max_excursion = 66500 - 67000 = -500 (negative).
        assert out["max_excursion_from_strike"] == pytest.approx(-500.0)
        assert out["knockout_time_relative"] == pytest.approx(1.0)

    def test_single_crossing_midwindow(self):
        """First half above, second half below → 1 crossing midwindow.
        Time-above ≈ 450s, time-below ≈ 450s. Max-magnitude excursion
        wins for sign. Knockout = (eval - mid) / (eval - open) = 0.5."""
        from shadow_coverage_backfill import compute_path_metrics_for_row
        eval_ts_min = 29603530
        # First 7 minutes (older): below strike. Last 8 minutes (newer): above.
        # NOTE: the helper iterates from oldest to newest, so older =
        # window_open side, newer = eval_ts side.
        # Phase G-4 round 2: range is [eval-15, eval-1] inclusive (15
        # minutes, exclusive of eval — Coinbase candles are bucket-start).
        lookup = {}
        for i in range(15):
            min_offset_from_eval = 15 - i  # i=0 → eval-15 (oldest); i=14 → eval-1
            ts_min = eval_ts_min - min_offset_from_eval
            # Older half BELOW (i < 7); newer half ABOVE.
            lookup[ts_min] = 66500.0 if i < 7 else 67500.0
        out = compute_path_metrics_for_row(
            asset_lookup=lookup, eval_epoch_min=eval_ts_min,
            seconds_to_close=0.0, threshold=67000.0,
        )
        # 7 candles below × 60s + 8 candles above × 60s = 420 + 480 = 900.
        assert out["time_above_strike_seconds"] == pytest.approx(480.0)
        assert out["time_below_strike_seconds"] == pytest.approx(420.0)
        # max_excursion = max(|+500|, |-500|) → tie, prefer above (signed +500).
        assert out["max_excursion_from_strike"] == pytest.approx(500.0)
        # Crossing happened between candles 6 and 7 (0-indexed).
        # Newest candle is i=14 (eval_ts), crossing at boundary i=6→7
        # which is roughly 8 candles before eval = ~480s before eval.
        # (eval - last_crossing) / (eval - window_open) = ~480 / 900 ≈ 0.53
        assert 0.45 <= out["knockout_time_relative"] <= 0.6

    def test_no_candles_in_window_returns_nones(self):
        """If lookup has no candles in the window range → None for all."""
        from shadow_coverage_backfill import compute_path_metrics_for_row
        out = compute_path_metrics_for_row(
            asset_lookup={}, eval_epoch_min=29603530,
            seconds_to_close=0.0, threshold=67000.0,
        )
        assert out["time_above_strike_seconds"] is None
        assert out["time_below_strike_seconds"] is None
        assert out["max_excursion_from_strike"] is None
        assert out["knockout_time_relative"] is None

    def test_threshold_zero_returns_nones(self):
        """Defensive: threshold=0 would div-by-zero in any % calc; we
        return Nones to signal 'cannot compute'."""
        from shadow_coverage_backfill import compute_path_metrics_for_row
        eval_ts_min = 29603530
        # Phase G-4 round 2: candle range is [window_start_min, eval_epoch_min)
        # — exclusive of eval. So fixtures should populate minutes
        # [eval-15 .. eval-1] (i in 1..15), NOT [eval-14 .. eval] (i in 0..14).
        lookup = {(eval_ts_min - i): 67500.0 for i in range(1, 16)}
        out = compute_path_metrics_for_row(
            asset_lookup=lookup, eval_epoch_min=eval_ts_min,
            seconds_to_close=0.0, threshold=0.0,
        )
        assert all(v is None for v in out.values())

    def test_eval_mid_window(self):
        """seconds_to_close=300 → eval is 300s into the 900s window;
        observation window = [eval - 600, eval] (only 600s seen so far)."""
        from shadow_coverage_backfill import compute_path_metrics_for_row
        eval_ts_min = 29603530
        # 10 candles available (covering the 600s pre-eval observation window).
        lookup = {(eval_ts_min - i): 67500.0 for i in range(1, 11)}
        out = compute_path_metrics_for_row(
            asset_lookup=lookup, eval_epoch_min=eval_ts_min,
            seconds_to_close=300.0, threshold=67000.0,
        )
        # 10 candles × 60s = 600s above (window observed so far is 600s).
        assert out["time_above_strike_seconds"] == pytest.approx(600.0)
        assert out["time_below_strike_seconds"] == 0.0
        assert out["knockout_time_relative"] == pytest.approx(1.0)

    def test_negative_seconds_to_close_clamps_to_900(self):
        """Phase G-4 round 3 MEDIUM regression: a negative seconds_to_close
        (post-settlement straggler / clock skew) would have made
        observed_secs > 900s, walking more candles than the window has,
        violating the time-bound invariant. Fix clamps observed_secs to
        [0, 900]."""
        from shadow_coverage_backfill import compute_path_metrics_for_row
        eval_ts_min = 29603530
        # Provide ample candles before eval.
        lookup = {(eval_ts_min - i): 67500.0 for i in range(1, 25)}
        out = compute_path_metrics_for_row(
            asset_lookup=lookup, eval_epoch_min=eval_ts_min,
            seconds_to_close=-100.0, threshold=67000.0,
        )
        total = (out["time_above_strike_seconds"]
                 + out["time_below_strike_seconds"])
        assert total <= 900.0, (
            f"with negative seconds_to_close, total time must still be "
            f"clamped at 900s; got {total}"
        )

    def test_total_time_does_not_exceed_observed_secs(self):
        """Phase G-4 round 1 H2 regression: previously the range walked
        observed_min+1 candles (16 for a 900s window) → time_above +
        time_below summed to 960s. Fix walks exactly observed_min
        candles so the sum is bounded by observed_secs."""
        from shadow_coverage_backfill import compute_path_metrics_for_row
        eval_ts_min = 29603530
        # Provide MORE candles than the window has room for — confirms
        # the iteration is bounded by observed_min, not by lookup size.
        lookup = {(eval_ts_min - i): 67500.0 for i in range(20)}
        out = compute_path_metrics_for_row(
            asset_lookup=lookup, eval_epoch_min=eval_ts_min,
            seconds_to_close=0.0, threshold=67000.0,
        )
        total = (out["time_above_strike_seconds"]
                 + out["time_below_strike_seconds"])
        assert total == pytest.approx(900.0), (
            f"path-metric time-sum should equal observed window (900s); "
            f"got {total} → off-by-one regression"
        )


class TestG4BackfillPathMetricsIntegration:
    """End-to-end: backfill_path_metrics fetches candles via injected
    fetcher, then UPDATEs rows."""

    def test_backfill_writes_4_path_metric_columns(self, tmp_path):
        from shadow_coverage_backfill import backfill_path_metrics
        sm = _make_db(tmp_path)
        # Insert a historical row missing path metrics.
        # eval_time = 2026-04-15T10:00:00Z, threshold=67000, seconds_to_close=0
        # → window = [09:45:00, 10:00:00].
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, threshold, seconds_to_close, status) "
            "VALUES ('TEST', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', 67000.0, 0.0, 'pending')"
        )
        sm.conn.commit()

        # Mock fetcher returns 15 candles all above strike for BTC.
        import datetime as _dt
        eval_ts = int(_dt.datetime(2026, 4, 15, 10, 0, 0,
                                     tzinfo=_dt.timezone.utc).timestamp())

        def _mock_fetch(asset, start_iso, end_iso):
            # Phase G-4 round 1 H1: abort fires if ANY asset has zero
            # candles, so the mock must return non-empty for all assets
            # in COINBASE_PRODUCTS.
            # T1.5: extended to 6 assets — must cover DOGE+HYPE or the
            # abort trips. BTC values matter for the assertion; other
            # assets just need >0 entries.
            base = {"BTC": 67500.0, "ETH": 3200.0, "SOL": 140.0, "XRP": 2.5,
                    "DOGE": 0.18, "HYPE": 24.5}[asset]
            # Phase G-4 round 2: candle range walks [eval-15..eval-1]
            # (exclusive of eval). Provide candles at i=1..15.
            return [_candle(eval_ts - i * 60, base) for i in range(1, 16)]

        n = backfill_path_metrics(
            sm.conn, fetcher=_mock_fetch, batch_size=10, sleep_ms=0,
        )
        assert n == 1
        row = sm.conn.execute(
            "SELECT time_above_strike_seconds, time_below_strike_seconds, "
            "max_excursion_from_strike, knockout_time_relative "
            "FROM evaluated_opportunities WHERE ticker='TEST'"
        ).fetchone()
        assert row["time_above_strike_seconds"] == pytest.approx(900.0)
        assert row["time_below_strike_seconds"] == 0.0
        assert row["max_excursion_from_strike"] == pytest.approx(500.0)
        assert row["knockout_time_relative"] == pytest.approx(1.0)

    def test_backfill_aborts_when_asset_returns_zero_candles(self, tmp_path):
        """Phase G-4 round 1 H1 regression: mirrors G-2's abort. If
        ANY asset's lookup is empty after pagination, refuse to write
        all-NULL path metrics (otherwise rows are marked 'processed'
        and re-runs won't retry — silent corruption)."""
        from shadow_coverage_backfill import (
            backfill_path_metrics, CoinbaseFetchError,
        )
        sm = _make_db(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, threshold, seconds_to_close, status) "
            "VALUES ('TEST', 'E', 'BTC', 'low_price_shadow', "
            "'2026-04-15T10:00:00Z', '15m', 67000.0, 0.0, 'pending')"
        )
        sm.conn.commit()

        def _mock_empty(asset, start_iso, end_iso):
            return []

        with pytest.raises(CoinbaseFetchError, match="ZERO candles"):
            backfill_path_metrics(
                sm.conn, fetcher=_mock_empty, batch_size=10, sleep_ms=0,
            )
        # Critical: no garbage written — row's path-metric cols still NULL.
        row = sm.conn.execute(
            "SELECT time_above_strike_seconds FROM evaluated_opportunities "
            "WHERE ticker='TEST'"
        ).fetchone()
        assert row["time_above_strike_seconds"] is None

    def test_backfill_skips_already_populated(self, tmp_path):
        from shadow_coverage_backfill import backfill_path_metrics
        sm = _make_db(tmp_path)
        # Row that ALREADY has time_above populated (live row, post-Phase-F).
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities("
            "ticker, event_ticker, asset, filter_stage, evaluation_time, "
            "product_type, threshold, seconds_to_close, status, "
            "time_above_strike_seconds) "
            "VALUES ('LIVE', 'E', 'BTC', 'candidate', "
            "'2026-05-02T10:00:00Z', '15m', 67000.0, 0.0, 'pending', "
            "999.0)"
        )
        sm.conn.commit()

        def _mock_fetch(asset, start_iso, end_iso):
            return []

        n = backfill_path_metrics(
            sm.conn, fetcher=_mock_fetch, batch_size=10, sleep_ms=0,
        )
        assert n == 0
        row = sm.conn.execute(
            "SELECT time_above_strike_seconds FROM evaluated_opportunities "
            "WHERE ticker='LIVE'"
        ).fetchone()
        assert row["time_above_strike_seconds"] == 999.0  # untouched
