"""Ticket 86ba1xdwp — regression test for the daily 11:04-11:07 UTC writer-storm.

Pins the runtime invariant from `kb/decisions/settlement-weather-writer-storm-plan-may21.md`:
during `SettlementTracker._poll_evaluated_opportunities`, the weather settlement phase
must NOT hold the shared `StateManager.conn` writer lock across HTTP fetches.

Pre-fix shape (the bug):

    for (opp_id, ticker, row) in _weather_updates:
        _obs_high = _wx_eng._fetcher.fetch_observed_high(...)   # ← HTTP, 1-5s
        if _obs_high is not None:
            self._state.conn.execute("UPDATE evaluated_opportunities ...")  # auto-BEGIN tx
            _wx_dirty = True
            _wx_eng._model.update_bias(...)                                  # ← new conn write
                                                                             #   blocked 10s by
                                                                             #   the open tx
    if _wx_dirty:
        self._state.conn.commit()

Production evidence (`ubuntu-s-1vcpu-1gb-nyc3-01`):
  2026-05-20 + 2026-05-21 11:04-11:07 UTC — both daily slots cascaded
  10-30s `status=fail` writes across weather_engine save_bias, market_obs_snapshotter,
  fifteenm_shadow, phantom_reconcile.

This test simulates the cascade with N=3 weather brackets and a stub fetcher
that sleeps 0.2s per call (simulating HTTP latency without making the test
runtime ridiculous). A second thread attempts a separate-connection write to
`weather_bias` every 30ms and measures max acquisition time. Pre-fix:
the separate-thread max wait approaches the per-iteration lock-hold duration
(or the busy_timeout if N is large enough). Post-fix: max wait is dominated
by the ~10-50ms per-row UPDATE+commit window.

Threshold rationale:
  N=3 cities × 0.2s/HTTP = 0.6s pre-fix lock-hold (or 1s if busy_timeout exhausts
  the separate writer first). Post-fix: 3 × ~20ms = ~60ms.
  Threshold: max_wait_ms < 250 — gives generous headroom for Mac disk-sync
  variance while still firmly distinguishing pre-fix (1000+ ms) from post-fix.

TDD-RED state: this test FAILS against pre-fix `bot/settlement.py` and PASSES
after the Phase 3a/3b refactor lands.
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)

import bot.settlement  # noqa: E402
import bot.state  # noqa: E402


# 3 weather cities — minimum to bound CI runtime while keeping the
# cascade unambiguous.
_CITIES = ["BOS", "NYC", "CHI"]
_STUB_HTTP_LATENCY_S = 0.2

# Test-only busy_timeout for the separate-conn writers (stub bias model +
# contention probe). Production uses 10s; here we use 1s so the test bounds
# at ~3s wall instead of ~30s. The bug class is identical — sub-second is
# already a textbook starvation signal.
_TEST_BUSY_TIMEOUT_MS = 1000

# Pre-fix: separate-thread writer blocks ~busy_timeout per iteration (1000ms here).
# Post-fix: separate-thread writer blocks only during one UPDATE+commit window
# (~10-50ms).
# Threshold 250ms separates cleanly with Mac disk-sync jitter headroom.
_MAX_WAIT_THRESHOLD_MS = 250.0


class _StubFetcher:
    """Sleeps `latency_s`, returns the configured observed high."""

    def __init__(self, latency_s: float, returns: float):
        self._latency_s = latency_s
        self._returns = returns
        self.call_count = 0

    def fetch_observed_high(self, city: str, market_date: str):
        self.call_count += 1
        time.sleep(self._latency_s)
        return self._returns


class _StubBiasModel:
    """Stub weather bias model that mirrors the production
    `WeatherProbabilityModel._save_bias` pattern: opens a SEPARATE sqlite3
    connection per call and writes to `weather_bias`. This is what makes the
    test exercise the lock-contention surface end-to-end."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self.update_calls: list = []
        # Ensure weather_bias table exists for the stub writes.
        conn = sqlite3.connect(self._db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS weather_bias ("
            "city_code TEXT PRIMARY KEY, bias_value REAL NOT NULL, "
            "bias_count INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.commit()
        conn.close()

    def update_bias(self, city_code: str, actual_high: float, forecast_mean: float,
                    market_date=None):
        self.update_calls.append((city_code, actual_high, forecast_mean, market_date))
        # Mirror production _save_bias: own conn, busy_timeout, INSERT OR REPLACE.
        conn = sqlite3.connect(self._db_path, timeout=_TEST_BUSY_TIMEOUT_MS / 1000.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_TEST_BUSY_TIMEOUT_MS}")
        conn.execute(
            "INSERT OR REPLACE INTO weather_bias (city_code, bias_value, bias_count, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (city_code, actual_high - forecast_mean, 1,
             datetime.datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()


class _ContentionProbe:
    """Background thread that opens a separate connection and repeatedly
    attempts INSERT OR REPLACE on weather_bias. Records max wall-clock
    duration per attempt.

    The probe uses a separate sqlite3.connect() with the SAME PRAGMAs as
    the production WeatherProbabilityModel._save_bias path, so its
    lock-acquisition timing mirrors what production writers experience
    during the storm."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._stop = threading.Event()
        self.durations_ms: list = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.thread.join(timeout=15.0)

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=_TEST_BUSY_TIMEOUT_MS / 1000.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_TEST_BUSY_TIMEOUT_MS}")
        try:
            while not self._stop.wait(0.03):
                t0 = time.perf_counter()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO weather_bias "
                        "(city_code, bias_value, bias_count, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        ("__PROBE__", 0.0, 1, datetime.datetime.utcnow().isoformat())
                    )
                    conn.commit()
                except sqlite3.OperationalError:
                    pass
                self.durations_ms.append((time.perf_counter() - t0) * 1000.0)
        finally:
            conn.close()


class TestWeatherSettlementWriterStorm(unittest.TestCase):
    """Regression: settlement weather phase must release the shared-conn writer
    lock between rows so separate-conn writers (save_bias, market_obs_snapshotter,
    phantom_reconcile, posthoc) don't cascade-fail at 10s busy_timeout."""

    def _fresh_state(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        sm = bot.state.StateManager(db_path=tmp.name)
        # Some sister settlement loops query these; create as empty so the
        # loops no-op cleanly without raising.
        sm.conn.executescript(
            "CREATE TABLE IF NOT EXISTS low_price_shadow_signals ("
            " id INTEGER PRIMARY KEY, ticker TEXT, market_price INTEGER, "
            " full_kelly_contracts INTEGER, capped_contracts INTEGER, "
            " status TEXT, market_result TEXT, counterfactual_pnl_full INTEGER, "
            " counterfactual_pnl_capped INTEGER, settled_at TEXT);"
            "CREATE TABLE IF NOT EXISTS sol_pathc_shadow ("
            " ticker TEXT PRIMARY KEY, status TEXT, live_entry_price INTEGER, "
            " live_contracts INTEGER, position_size INTEGER, pathc_maker_price INTEGER, "
            " pathc_depth_at_maker INTEGER, obs_maker_price_touched INTEGER, "
            " pathc_esc_ask INTEGER, pathc_esc_depth INTEGER);"
        )
        sm.conn.commit()
        return sm, tmp.name

    def _seed_weather_brackets(self, sm: bot.state.StateManager, cities, market_date: str):
        """Insert one weather bracket per city, product_type='weather',
        status='pending', wx_actual_high_temp NULL."""
        for city in cities:
            # Ticker format `KX<CITY>15M-<date>-T75` is irrelevant; the test
            # only needs `_parse_weather_market_date(ticker)` to succeed.
            # Format match per bot/settlement.py `_parse_weather_market_date` —
            # parts[1] like '26MAY20' parses via %y%b%d.
            dt_obj = datetime.datetime.strptime(market_date, "%Y-%m-%d")
            short = dt_obj.strftime("%y%b%d").upper()
            ticker = f"KXHIGH{city}-{short}-T75"
            sm.conn.execute(
                "INSERT INTO evaluated_opportunities ("
                " ticker, event_ticker, asset, side, raw_prob, calibrated_prob,"
                " market_price, spot_price, filter_stage, status, evaluation_time,"
                " product_type) "
                "VALUES (?, ?, ?, 'yes', 0.5, 0.5, 50, 75.0,"
                " 'weather_observation', 'pending', ?, 'weather')",
                (ticker, ticker, f"{city}_TEMP",
                 datetime.datetime.utcnow().isoformat())
            )
        sm.conn.commit()

    def _build_tracker(self, sm, ticker_results: dict):
        """SettlementTracker with mocked client returning canned `get_market`
        responses keyed by ticker."""
        client = MagicMock()
        def _get_market(t):
            res = ticker_results.get(t)
            return {"market": {"result": res}} if res else None
        client.get_market.side_effect = _get_market
        client.get_balance.return_value = {"balance": 100000}
        client.get_fills.return_value = {"fills": []}
        logger = MagicMock()
        tracker = bot.settlement.SettlementTracker(client, sm, logger, main_loop=None)
        return tracker, client

    def test_separate_conn_writer_not_starved_during_weather_settlement(self):
        """During the weather settlement phase, a separate-connection writer
        (mirroring `WeatherProbabilityModel._save_bias` or
        `MarketObsSnapshotter._tick`) must acquire its writer lock within
        the threshold per attempt. Pre-fix: max wait approaches the 10s
        busy_timeout (or at minimum the full N×HTTP_LATENCY hold). Post-fix:
        per-row commit drops max wait to ~10-50 ms.
        """
        sm, db_path = self._fresh_state()
        # All settled tickers report 'yes' so the weather backfill path fires.
        # Use yesterday UTC so `_market_date < _today` passes.
        yesterday = (datetime.datetime.utcnow() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        self._seed_weather_brackets(sm, _CITIES, yesterday)

        # Build ticker_results map matching the seeded tickers.
        dt_obj = datetime.datetime.strptime(yesterday, "%Y-%m-%d")
        short = dt_obj.strftime("%y%b%d").upper()
        ticker_results = {f"KXHIGH{c}-{short}-T75": "yes" for c in _CITIES}
        tracker, _ = self._build_tracker(sm, ticker_results)

        # Inject a fake _ml with a weather_engine whose _fetcher sleeps and
        # _model writes via a separate conn — mirroring production
        # WeatherProbabilityModel + WeatherForecastFetcher shape.
        fake_wx_engine = MagicMock()
        fake_wx_engine._fetcher = _StubFetcher(latency_s=_STUB_HTTP_LATENCY_S, returns=75.0)
        fake_wx_engine._model = _StubBiasModel(db_path)
        fake_ml = MagicMock()
        fake_ml.weather_engine = fake_wx_engine
        # Other shadow attrs explicitly None so the sister settlement
        # loops no-op cleanly (gated by `getattr(self._ml, X, None)`).
        fake_ml.fifteenm_shadow = None
        fake_ml.hourly_alt_shadow = None
        fake_ml.spx_harrv_shadow = None
        tracker._ml = fake_ml

        # Start the contention probe BEFORE the settlement runs.
        probe = _ContentionProbe(db_path)
        probe.start()
        # Brief settle so the probe gets at least 1 baseline attempt before
        # the settlement-driven writer pressure begins.
        time.sleep(0.05)

        try:
            tracker._poll_evaluated_opportunities()
        finally:
            probe.stop()

        # All N cities should have had their bias updated (Phase 3b
        # post-commit; pre-fix even when the storm cascades, bias updates
        # eventually go through because each iter waits up to busy_timeout).
        self.assertEqual(
            len(fake_wx_engine._model.update_calls), len(_CITIES),
            f"Expected update_bias called for all {len(_CITIES)} cities; "
            f"got {len(fake_wx_engine._model.update_calls)} calls"
        )

        # CORE ASSERTION: the probe never sees a writer-lock wait beyond
        # the threshold. Pre-fix this fails with multi-second waits;
        # post-fix all waits are bounded by individual per-row tx duration.
        self.assertTrue(probe.durations_ms,
                        "Contention probe collected no samples — test setup race")
        max_wait_ms = max(probe.durations_ms)
        self.assertLess(
            max_wait_ms, _MAX_WAIT_THRESHOLD_MS,
            f"Separate-conn writer starved {max_wait_ms:.1f} ms while shared "
            f"StateManager.conn held writer lock across HTTP fetches "
            f"(threshold {_MAX_WAIT_THRESHOLD_MS:.0f} ms). The settlement "
            f"weather phase regressed to the pre-fix shape from ticket "
            f"86ba1xdwp — Phase 3 must split into Phase 3a (HTTP-only) and "
            f"Phase 3b (DB-only, per-row commit). See "
            f"kb/decisions/settlement-weather-writer-storm-plan-may21.md."
        )


if __name__ == "__main__":
    unittest.main()
