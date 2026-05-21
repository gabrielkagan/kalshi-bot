"""Ticket 86ba1xdwp — settlement weather writer-storm behavioral regression.

The pre-fix `bot/settlement.py::SettlementTracker._poll_evaluated_opportunities`
held the shared-conn writer lock from the first `UPDATE evaluated_opportunities
SET wx_actual_high_temp=...` through the after-loop `self._state.conn.commit()`
— across N × HTTP latency (1-5s per Open-Meteo `fetch_observed_high`). Any
separate-conn writer (`weather_engine._save_bias`, `market_obs_snapshotter`,
`phantom_reconcile_monitor`, `CALMLP_POSTHOC`) busy-waited up to
`PRAGMA busy_timeout=10000`ms — the cascade we observed on 2026-05-21 in the
11:04-11:07 UTC settlement window.

This test simulates that scenario with N=12 settled weather brackets and a
stubbed fetcher that sleeps 1.0s per call. A second thread opens a separate
sqlite3.connect() to the same DB and attempts an INSERT OR REPLACE on a
secondary table every 100ms, measuring the max single-attempt wait time.

  Pre-fix:  max wait > 10000 ms (busy_timeout exhaustion → cascade exception)
  Post-fix: max wait < 250 ms   (per-row commit releases lock between rows)

Plus a positive correctness pin: all 12 `wx_actual_high_temp` values must be
persisted post-fix (the Phase 3a→3b refactor must not silently drop rows).
"""
from __future__ import annotations

import datetime
import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from typing import List, Tuple
from unittest.mock import MagicMock

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

import bot.settlement  # noqa: E402
import bot.state  # noqa: E402


N_WEATHER_ROWS = 12
FETCH_SLEEP_SECONDS = 1.0
HAMMER_INTERVAL_SECONDS = 0.1
POST_FIX_MAX_WAIT_MS = 250.0


def _seed_weather_rows(sm: bot.state.StateManager, n: int) -> List[Tuple[int, str]]:
    """Insert n pending weather-bracket evaluated_opportunity rows.

    Returns the list of (id, ticker) so the test can assert per-row outcomes.
    Each row uses a past `_market_date` so the Phase 3 weather block's
    `_market_date < _today` guard lets the fetch fire.
    """
    out: List[Tuple[int, str]] = []
    eval_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for i in range(n):
        # KXHIGHNY-<YYMMMDD>-T<strike>; date '24JAN01' parses to 2024-01-01,
        # comfortably in the past so _market_date < _today is satisfied.
        ticker = f"KXHIGHNY-24JAN01-T{50 + i}"
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities "
            "(ticker, event_ticker, asset, filter_stage, evaluation_time, "
            " market_price, status, product_type, side, raw_prob, "
            " spot_price, wx_actual_high_temp) "
            "VALUES (?, 'KXHIGHNY-24JAN01', 'NY_TEMP', 'weather_observation', "
            "?, 50, 'pending', 'weather', 'yes', 0.50, 70.0, NULL)",
            (ticker, eval_time),
        )
        opp_id = sm.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        out.append((opp_id, ticker))
    sm.conn.commit()
    return out


def _make_slow_weather_fetcher() -> MagicMock:
    """Stub fetcher that sleeps `FETCH_SLEEP_SECONDS` per call, returns 72.5F."""
    def _slow_fetch(city: str, market_date: str):
        time.sleep(FETCH_SLEEP_SECONDS)
        return 72.5
    fetcher = MagicMock()
    fetcher.fetch_observed_high.side_effect = _slow_fetch
    return fetcher


def _make_main_loop_with_weather_engine(db_path: str) -> MagicMock:
    """Build a mocked main_loop with weather_engine that has the .._fetcher /
    .._model surface the Phase 3 weather block walks.

    `_model.update_bias` is a no-op MagicMock — the cascade victim we measure
    in this test is the second thread's separate-conn INSERT on
    `bias_hammer_table` (a table we create just for this test), not the
    real weather_bias write. This isolates the lock-hold measurement from
    other moving parts of WeatherProbabilityModel.
    """
    ml = MagicMock()
    ml.weather_engine = MagicMock()
    ml.weather_engine._fetcher = _make_slow_weather_fetcher()
    ml.weather_engine._model = MagicMock()
    # update_bias is the call the Phase 3b loop makes after each per-row
    # commit. It must not raise; the test asserts it was invoked N times.
    ml.weather_engine._model.update_bias = MagicMock(return_value=None)
    return ml


class _SecondThreadHammer:
    """Opens its own sqlite3.connect() on the same DB and attempts an
    INSERT OR REPLACE every 100ms while `stop_event` is unset. Records
    the wall-clock wait time per attempt.

    The connection sets `PRAGMA busy_timeout=10000` to match the
    production weather_engine._save_bias path — that's the timeout that
    masks (then cascades) the writer lock pre-fix.
    """

    def __init__(self, db_path: str, stop_event: threading.Event) -> None:
        self._db_path = db_path
        self._stop = stop_event
        self.wait_times_ms: List[float] = []
        self.exceptions: List[BaseException] = []
        self.thread = threading.Thread(target=self._run, name="bias_hammer", daemon=True)

    def _run(self) -> None:
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS bias_hammer_table ("
                "  key TEXT PRIMARY KEY, value REAL, updated_at TEXT)"
            )
            conn.commit()
            i = 0
            while not self._stop.is_set():
                t0 = time.monotonic()
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO bias_hammer_table (key, value, updated_at) "
                        "VALUES (?, ?, ?)",
                        (f"k{i}", float(i),
                         datetime.datetime.now(datetime.timezone.utc).isoformat()),
                    )
                    conn.commit()
                    self.wait_times_ms.append((time.monotonic() - t0) * 1000.0)
                except BaseException as e:  # noqa: BLE001
                    self.exceptions.append(e)
                    self.wait_times_ms.append((time.monotonic() - t0) * 1000.0)
                i += 1
                self._stop.wait(HAMMER_INTERVAL_SECONDS)
        finally:
            try:
                conn.close()
            except Exception:
                pass


class TestSettlementWeatherWriterStormRegression(unittest.TestCase):
    """86ba1xdwp — Phase 3 weather settlement writer-lock cascade."""

    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.addCleanup(os.unlink, self.db_path)
        # WAL sidecars
        for ext in ("-wal", "-shm"):
            self.addCleanup(lambda p=self.db_path + ext: os.path.exists(p) and os.unlink(p))
        self.sm = bot.state.StateManager(db_path=self.db_path)

    def _build_tracker(self) -> bot.settlement.SettlementTracker:
        # All 12 seeded tickers resolve as YES — the Phase 3 weather queue
        # populates only on settled results in ("yes", "all_yes", "no", "all_no").
        client = MagicMock()
        client.get_market.return_value = {"market": {"result": "yes"}}
        logger = MagicMock()
        logger.log_rejection = MagicMock()
        main_loop = _make_main_loop_with_weather_engine(self.db_path)
        return bot.settlement.SettlementTracker(client, self.sm, logger, main_loop=main_loop)

    def test_per_row_lock_release_during_weather_settlement(self) -> None:
        """The 2nd-thread separate-conn INSERT must complete in <250ms per
        attempt during a 12-row weather settlement with a 1s/row stubbed
        fetcher. Pre-fix this assertion FAILS because the shared-conn
        writer lock is held continuously across all 12 fetches (~12s).
        """
        seeded = _seed_weather_rows(self.sm, N_WEATHER_ROWS)
        tracker = self._build_tracker()

        stop = threading.Event()
        hammer = _SecondThreadHammer(self.db_path, stop)
        hammer.thread.start()
        # Let the hammer get a few baseline writes in before settlement starts.
        time.sleep(0.3)

        # Silence the verbose info-spam from settlement during the run.
        prior_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.WARNING)
        try:
            tracker._poll_evaluated_opportunities()
        finally:
            logging.getLogger().setLevel(prior_level)
            stop.set()
            hammer.thread.join(timeout=5.0)

        # Sanity: at least one hammer attempt should have happened DURING
        # the settlement run (which takes ~12s pre-fix; ~1s+12×Δ post-fix —
        # the fetches still take 12s; only DB lock hold compresses).
        self.assertGreaterEqual(
            len(hammer.wait_times_ms),
            10,
            f"second-thread hammer recorded only {len(hammer.wait_times_ms)} attempts; "
            f"settlement should have spanned ~12s, plenty of room for ≥10 at 100ms cadence",
        )

        max_wait = max(hammer.wait_times_ms)
        self.assertLess(
            max_wait,
            POST_FIX_MAX_WAIT_MS,
            f"second-thread INSERT max wait was {max_wait:.1f} ms (limit "
            f"{POST_FIX_MAX_WAIT_MS:.0f} ms). Pre-86ba1xdwp the shared-conn "
            f"writer lock is held across the 12-row weather fetch loop "
            f"(~12s); post-fix each per-row commit releases between rows. "
            f"Wait-time distribution: min={min(hammer.wait_times_ms):.1f} "
            f"median={sorted(hammer.wait_times_ms)[len(hammer.wait_times_ms)//2]:.1f} "
            f"max={max_wait:.1f}",
        )
        # And the hammer must not have raised — busy_timeout exhaustion
        # would surface here as `database is locked` OperationalError.
        self.assertEqual(
            hammer.exceptions,
            [],
            f"second-thread INSERT raised {len(hammer.exceptions)} exception(s) "
            f"during weather settlement: {hammer.exceptions[:3]!r}. This is the "
            f"cascade victim class the fix is designed to prevent.",
        )

    def test_all_weather_temps_persisted(self) -> None:
        """All N weather rows must have `wx_actual_high_temp` persisted
        after settlement. Pin against silent row-drop in the Phase 3a→3b
        split (e.g., the observations list missing the forecast_mean or
        market_date for some rows).
        """
        seeded = _seed_weather_rows(self.sm, N_WEATHER_ROWS)
        tracker = self._build_tracker()

        prior_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.WARNING)
        try:
            tracker._poll_evaluated_opportunities()
        finally:
            logging.getLogger().setLevel(prior_level)

        rows = self.sm.conn.execute(
            "SELECT id, wx_actual_high_temp FROM evaluated_opportunities "
            "WHERE product_type='weather'"
        ).fetchall()
        persisted = [r for r in rows if r[1] is not None]
        self.assertEqual(
            len(persisted),
            N_WEATHER_ROWS,
            f"only {len(persisted)} of {N_WEATHER_ROWS} weather rows got "
            f"wx_actual_high_temp persisted. The Phase 3a→3b refactor "
            f"must round-trip every row from the observations list to a "
            f"committed UPDATE. Missing ids: "
            f"{[r[0] for r in rows if r[1] is None]}",
        )
        for _, temp in persisted:
            self.assertAlmostEqual(temp, 72.5, places=2)

    def test_update_bias_invoked_per_row(self) -> None:
        """`weather_engine._model.update_bias(...)` must be called once per
        successfully-fetched row, AFTER the per-row commit (the plan-doc's
        Phase 3b sequence). This pins the cascade-prevention property: the
        per-row commit must precede update_bias so update_bias's
        separate-conn write does not contend with an active shared-conn tx.

        Counts only — call-order is structurally enforced by the T1 AST
        guard (`test_phase3b_commits_per_row`) which pins
        `self._state.conn.commit()` inside the Phase 3b loop body.
        """
        seeded = _seed_weather_rows(self.sm, N_WEATHER_ROWS)
        tracker = self._build_tracker()

        prior_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.WARNING)
        try:
            tracker._poll_evaluated_opportunities()
        finally:
            logging.getLogger().setLevel(prior_level)

        update_bias = tracker._ml.weather_engine._model.update_bias
        self.assertEqual(
            update_bias.call_count,
            N_WEATHER_ROWS,
            f"update_bias was called {update_bias.call_count} times; "
            f"expected {N_WEATHER_ROWS} (one per fetched row). Phase 3b "
            f"loop body must invoke update_bias for every observation.",
        )


if __name__ == "__main__":
    unittest.main()
