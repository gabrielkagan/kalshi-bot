"""Phase H-3a: tests for MarketObservationsSnapshotter.

Phase H-3a is the foundation for H-3 (Kalshi fill simulator). It periodically
samples the in-memory WS orderbook cache for active 15M tickers and writes
a row per (ticker, tick) into `market_observations_continuous`.

Design: read-only consumer of `KalshiWebsocketClient.get_all_orderbooks()`.
No new REST traffic. Sub-second cache freshness. Filters to active 15M
tickers via an injected `active_tickers_provider` callable.

Schema (created idempotently by the snapshotter on first start):
    market_observations_continuous(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        observation_time TEXT NOT NULL,    -- ISO8601 with microseconds
        yes_bid_cents INTEGER,
        yes_ask_cents INTEGER,             -- 100 - max(no_levels.price)
        no_bid_cents INTEGER,
        no_ask_cents INTEGER,              -- 100 - max(yes_levels.price)
        bid_depth INTEGER,                 -- depth at best YES bid
        ask_depth INTEGER,                 -- depth at best YES ask (= best NO bid)
        source TEXT NOT NULL,              -- 'ws_cache' | 'ws_no_data' | 'ws_cache_stale'
        cache_age_ms INTEGER               -- (now - ob.ts) * 1000 if ws_cache, else NULL
    )

Source labels:
- 'ws_cache' — fresh cache hit (cache_age_ms < STALE_THRESHOLD_MS)
- 'ws_cache_stale' — cache present but stale (cache_age_ms >= threshold);
                     row written for forensics; bid/ask still recorded
                     (operator can filter on cache_age_ms at query time)
- 'ws_no_data' — ticker in active_tickers but no cache entry; covers
                 BOTH "subscribed-but-no-snapshot" (just-subscribed) AND
                 "subscription dropped on reconnect" (round-1 #8 rename).
                 Row has NULL bid/ask/depth, cache_age_ms NULL.

Coverage:
- schema migration creates table + indexes idempotently
- one row per active 15M ticker per tick
- top-of-book derivation (yes_bid, yes_ask, no_bid, no_ask, depths)
- empty-side handling (NULL when one side has no levels)
- empty cache handling (source = 'ws_no_data')
- stale cache handling (source = 'ws_cache_stale')
- batched commits (≤50 rows per commit per CLAUDE.md DB lock rules)
- thread lifecycle (start/stop via threading.Event)
- crash recovery (DB lock during a tick → log + continue, no thread death)
- metrics (rows_written, ticks, errors)
- non-15M tickers excluded
"""
from __future__ import annotations

import datetime
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Module lives at repo root (matches spx_engine, weather_engine pattern
# — bot/_impl.py imports as a top-level module).
sys.path.insert(0, str(ROOT))

import market_observations_snapshotter as mod  # noqa: E402


# ── Test doubles ────────────────────────────────────────────────────────────


class FakeWS:
    """Stand-in for KalshiWebsocketClient.get_all_orderbooks_snapshot().

    Matches the real client's contract: returns a deep copy under the lock,
    so callers can iterate level lists without racing the WS thread."""

    def __init__(self, orderbooks=None):
        self._orderbooks = dict(orderbooks or {})

    def get_all_orderbooks_snapshot(self):
        import copy
        return copy.deepcopy(self._orderbooks)

    def set(self, ticker, ob):
        self._orderbooks[ticker] = ob

    def remove(self, ticker):
        self._orderbooks.pop(ticker, None)


def _make_db(path: Path) -> sqlite3.Connection:
    """state.db-shaped connection factory matching production PRAGMA setup."""
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _ob(yes_levels, no_levels, ts):
    """Build a cached orderbook in the WS client's wire format."""
    return {"yes": list(yes_levels), "no": list(no_levels), "ts": ts}


# ── Schema migration ────────────────────────────────────────────────────────


def test_ensure_schema_creates_table_and_indexes(tmp_path):
    """First call creates table + indexes; second call is a no-op."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(market_observations_continuous)"
    ).fetchall()]
    expected = {
        "id", "ticker", "observation_time",
        "yes_bid_cents", "yes_ask_cents",
        "no_bid_cents", "no_ask_cents",
        "bid_depth", "ask_depth",
        "source", "cache_age_ms",
    }
    assert expected.issubset(set(cols))

    indexes = [r[1] for r in conn.execute(
        "PRAGMA index_list(market_observations_continuous)"
    ).fetchall()]
    # Time-range scan per ticker AND global time-range scan.
    assert any("ticker" in idx and "time" in idx for idx in indexes)
    assert any(idx.endswith("_time") for idx in indexes)

    # Idempotency.
    mod.ensure_schema(conn)
    cols2 = [r[1] for r in conn.execute(
        "PRAGMA table_info(market_observations_continuous)"
    ).fetchall()]
    assert cols == cols2


# ── Top-of-book derivation ──────────────────────────────────────────────────


def test_derive_top_of_book_full_book():
    """Full two-sided book with multiple levels."""
    yes_levels = [[55, 100], [54, 200], [53, 50]]
    no_levels = [[44, 80], [43, 150], [42, 30]]
    tob = mod.derive_top_of_book(yes_levels, no_levels)
    # Best YES bid = highest yes price (55), depth = qty at that level (100)
    assert tob["yes_bid_cents"] == 55
    assert tob["bid_depth"] == 100
    # Best NO bid = highest no price (44)
    assert tob["no_bid_cents"] == 44
    # YES ask = 100 - best_no_bid = 100 - 44 = 56; ask_depth = qty at best NO bid
    assert tob["yes_ask_cents"] == 56
    assert tob["ask_depth"] == 80
    # NO ask = 100 - best_yes_bid = 100 - 55 = 45
    assert tob["no_ask_cents"] == 45


def test_derive_top_of_book_one_sided_yes_only():
    """Only YES side has resting orders → no_bid_cents NULL, yes_ask NULL."""
    yes_levels = [[60, 100]]
    no_levels = []
    tob = mod.derive_top_of_book(yes_levels, no_levels)
    assert tob["yes_bid_cents"] == 60
    assert tob["bid_depth"] == 100
    assert tob["no_bid_cents"] is None
    assert tob["yes_ask_cents"] is None
    assert tob["ask_depth"] is None
    # NO ask still derivable from YES bid.
    assert tob["no_ask_cents"] == 40


def test_derive_top_of_book_empty_book():
    """Both sides empty → all NULLs."""
    tob = mod.derive_top_of_book([], [])
    assert tob["yes_bid_cents"] is None
    assert tob["yes_ask_cents"] is None
    assert tob["no_bid_cents"] is None
    assert tob["no_ask_cents"] is None
    assert tob["bid_depth"] is None
    assert tob["ask_depth"] is None


def test_derive_top_of_book_skips_zero_qty_levels():
    """Levels with qty<=0 are ignored (Kalshi shouldn't send these but
    delta-applied caches occasionally have stale-zero entries)."""
    yes_levels = [[60, 0], [59, 100]]
    no_levels = [[40, 0], [39, 80]]
    tob = mod.derive_top_of_book(yes_levels, no_levels)
    assert tob["yes_bid_cents"] == 59
    assert tob["bid_depth"] == 100
    assert tob["no_bid_cents"] == 39


def test_derive_top_of_book_handles_unsorted_levels():
    """Cache may not be sorted — derivation must scan all levels for max."""
    yes_levels = [[53, 50], [55, 100], [54, 200]]
    no_levels = [[42, 30], [44, 80], [43, 150]]
    tob = mod.derive_top_of_book(yes_levels, no_levels)
    assert tob["yes_bid_cents"] == 55
    assert tob["bid_depth"] == 100
    assert tob["no_bid_cents"] == 44
    assert tob["ask_depth"] == 80


# ── Snapshot collection ─────────────────────────────────────────────────────


def test_snapshot_writes_row_per_active_ticker(tmp_path):
    """Each active 15M ticker → one row in market_observations_continuous."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    ws = FakeWS({
        "KXBTCD-A": _ob([[55, 100]], [[44, 80]], ts=time.time()),
        "KXETHD-B": _ob([[60, 50]], [[39, 70]], ts=time.time()),
    })

    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: ["KXBTCD-A", "KXETHD-B"],
    )
    snap._tick_once(conn)

    rows = conn.execute(
        "SELECT ticker, source, yes_bid_cents, yes_ask_cents, "
        "bid_depth, ask_depth FROM market_observations_continuous "
        "ORDER BY ticker"
    ).fetchall()
    assert len(rows) == 2
    btc, eth = rows
    assert btc[0] == "KXBTCD-A"
    assert btc[1] == "ws_cache"
    assert btc[2] == 55  # yes_bid
    assert btc[3] == 56  # yes_ask = 100 - 44
    assert btc[4] == 100  # bid_depth
    assert btc[5] == 80   # ask_depth
    assert eth[0] == "KXETHD-B"
    assert eth[1] == "ws_cache"


def test_snapshot_no_data_for_subscribed_ticker(tmp_path):
    """Ticker in active list but absent from WS cache → row with NULLs +
    source='ws_no_data' (covers subscribed-but-no-snapshot AND dropped-
    subscription cases)."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    ws = FakeWS({})  # no cache yet
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: ["KXBTCD-A"],
    )
    snap._tick_once(conn)

    rows = conn.execute(
        "SELECT source, yes_bid_cents, yes_ask_cents, bid_depth, ask_depth, "
        "cache_age_ms FROM market_observations_continuous"
    ).fetchall()
    assert len(rows) == 1
    src, yb, ya, bd, ad, age = rows[0]
    # 'ws_no_data' covers both subscribed-but-no-snapshot AND
    # subscription-dropped-on-reconnect — round-1 #8 disambiguation.
    assert src == "ws_no_data"
    assert yb is None and ya is None
    assert bd is None and ad is None
    assert age is None


def test_snapshot_stale_cache(tmp_path):
    """Cache present but ts older than STALE_THRESHOLD_S → source='ws_cache_stale'.
    Bid/ask still recorded for forensics."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    now = 1_700_000_000.0
    stale_ts = now - mod.STALE_THRESHOLD_S - 1.0
    ws = FakeWS({
        "KXBTCD-A": _ob([[55, 100]], [[44, 80]], ts=stale_ts),
    })
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: ["KXBTCD-A"],
        clock=lambda: now,
    )
    snap._tick_once(conn)

    rows = conn.execute(
        "SELECT source, yes_bid_cents, cache_age_ms "
        "FROM market_observations_continuous"
    ).fetchall()
    assert len(rows) == 1
    src, yb, age_ms = rows[0]
    assert src == "ws_cache_stale"
    assert yb == 55  # recorded for forensics
    assert age_ms >= mod.STALE_THRESHOLD_S * 1000


def test_snapshot_records_cache_age_ms_for_fresh_cache(tmp_path):
    """Fresh cache → cache_age_ms reflects (now - ts) * 1000."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    now = 1_700_000_000.0
    ws = FakeWS({
        "KXBTCD-A": _ob([[55, 100]], [[44, 80]], ts=now - 2.5),
    })
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: ["KXBTCD-A"],
        clock=lambda: now,
    )
    snap._tick_once(conn)

    age = conn.execute(
        "SELECT cache_age_ms FROM market_observations_continuous"
    ).fetchone()[0]
    # 2500ms ± 50ms tolerance for floating-point math.
    assert 2450 <= age <= 2550


def test_snapshot_excludes_inactive_tickers(tmp_path):
    """WS cache may have tickers that aren't in active_tickers_provider's
    return value (e.g., recently-settled). They should not be sampled."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    ws = FakeWS({
        "KXBTCD-ACTIVE": _ob([[55, 100]], [[44, 80]], ts=time.time()),
        "KXBTCD-SETTLED": _ob([[55, 100]], [[44, 80]], ts=time.time()),
    })
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: ["KXBTCD-ACTIVE"],
    )
    snap._tick_once(conn)

    tickers = [r[0] for r in conn.execute(
        "SELECT ticker FROM market_observations_continuous"
    ).fetchall()]
    assert tickers == ["KXBTCD-ACTIVE"]


def test_snapshot_observation_time_iso8601_with_microseconds(tmp_path):
    """observation_time must use the same format as bot/_impl.py timestamps
    ('%Y-%m-%dT%H:%M:%S.%fZ') for lexical comparison consistency."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    ws = FakeWS({
        "KXBTCD-A": _ob([[55, 100]], [[44, 80]], ts=time.time()),
    })
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: ["KXBTCD-A"],
    )
    snap._tick_once(conn)

    ts = conn.execute(
        "SELECT observation_time FROM market_observations_continuous"
    ).fetchone()[0]
    # Format: YYYY-MM-DDTHH:MM:SS.ffffffZ
    assert len(ts) == 27, f"unexpected timestamp shape: {ts!r}"
    assert ts[10] == "T"
    assert ts.endswith("Z")
    assert ts[19] == "."


# ── Batched commits (≤50 rows per commit) ──────────────────────────────────


def test_snapshot_batches_writes_to_50_per_commit(tmp_path):
    """Per CLAUDE.md, no commit may exceed 50 rows. Even with 150 active
    tickers, the snapshot must split into ≥3 commits.

    sqlite3.Connection.commit is read-only at the Python level, so wrap the
    real connection in a delegating proxy that counts commits.
    """
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    n_tickers = 150
    ws = FakeWS({
        f"KXBTCD-{i}": _ob([[55, 100]], [[44, 80]], ts=time.time())
        for i in range(n_tickers)
    })
    active = [f"KXBTCD-{i}" for i in range(n_tickers)]

    class CommitCounting:
        def __init__(self, real):
            self._real = real
            self.commit_calls = 0

        def execute(self, *a, **k):
            return self._real.execute(*a, **k)

        def executemany(self, *a, **k):
            return self._real.executemany(*a, **k)

        def commit(self):
            self.commit_calls += 1
            return self._real.commit()

    proxy = CommitCounting(conn)

    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: active,
    )
    snap._tick_once(proxy)

    n_rows = conn.execute(
        "SELECT COUNT(*) FROM market_observations_continuous"
    ).fetchone()[0]
    assert n_rows == n_tickers
    # Round-2 #5 tightening: assert exact equality, not >=. A doubled-
    # commits regression (e.g., a stray conn.commit() per row) would
    # silently pass the >= check.
    import math
    expected = math.ceil(n_tickers / mod.BATCH_SIZE)
    assert proxy.commit_calls == expected, (
        f"expected exactly {expected} commits for {n_tickers} rows at "
        f"BATCH_SIZE={mod.BATCH_SIZE}, got {proxy.commit_calls}"
    )


# ── Thread lifecycle ────────────────────────────────────────────────────────


def test_thread_start_writes_rows_and_stops_cleanly(tmp_path):
    """End-to-end: start daemon thread, let it tick once, signal stop,
    verify thread joins and rows are present."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    ws = FakeWS({
        "KXBTCD-A": _ob([[55, 100]], [[44, 80]], ts=time.time()),
    })
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: ["KXBTCD-A"],
        interval_seconds=0.1,
    )
    snap.start()
    # Give the thread time for at least one tick.
    time.sleep(0.3)
    snap.stop()
    snap.join(timeout=5.0)
    assert not snap.is_alive(), "thread did not stop within timeout"

    n = conn.execute(
        "SELECT COUNT(*) FROM market_observations_continuous"
    ).fetchone()[0]
    assert n >= 1


def test_thread_survives_db_lock_error(tmp_path, caplog):
    """If a tick fails with sqlite3.OperationalError, the thread must log
    and continue, not die. Otherwise a single transient lock kills H-3a
    forever."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    ws = FakeWS({
        "KXBTCD-A": _ob([[55, 100]], [[44, 80]], ts=time.time()),
    })
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: ["KXBTCD-A"],
    )

    class LockingConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

        def executemany(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            pass

        def close(self):
            pass

    # Should not raise.
    snap._tick_once(LockingConn())
    assert snap.metrics["errors"] >= 1


# ── Metrics ─────────────────────────────────────────────────────────────────


def test_metrics_counts_rows_and_ticks(tmp_path):
    """metrics dict tracks rows_written + ticks across tick calls."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    ws = FakeWS({
        "KXBTCD-A": _ob([[55, 100]], [[44, 80]], ts=time.time()),
        "KXETHD-B": _ob([[60, 50]], [[39, 70]], ts=time.time()),
    })
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: ["KXBTCD-A", "KXETHD-B"],
    )
    snap._tick_once(conn)
    snap._tick_once(conn)

    assert snap.metrics["ticks"] == 2
    assert snap.metrics["rows_written"] == 4


def test_metrics_starts_zero():
    """A freshly-constructed snapshotter has all-zero metrics — proves we
    don't leak state between instances if tests instantiate multiple."""
    snap = mod.MarketObservationsSnapshotter(
        db_path=":memory:",
        ws_client=FakeWS({}),
        active_tickers_provider=lambda: [],
    )
    assert snap.metrics["ticks"] == 0
    assert snap.metrics["rows_written"] == 0
    assert snap.metrics["errors"] == 0


# ── Edge cases ──────────────────────────────────────────────────────────────


def test_active_tickers_provider_returning_empty_list_is_ok(tmp_path):
    """No active tickers → no rows written, no error."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=FakeWS({}),
        active_tickers_provider=lambda: [],
    )
    snap._tick_once(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM market_observations_continuous"
    ).fetchone()[0]
    assert n == 0
    assert snap.metrics["ticks"] == 1


def test_active_tickers_provider_raising_is_handled(tmp_path):
    """If active_tickers_provider raises (e.g., bot/_impl.py race during
    discovery), the tick must record an error and skip. Not crash."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    def boom():
        raise RuntimeError("provider broken")

    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=FakeWS({}),
        active_tickers_provider=boom,
    )
    snap._tick_once(conn)  # must not raise
    assert snap.metrics["errors"] >= 1


def test_double_start_is_a_noop(tmp_path):
    """Calling start() twice must not spawn a second thread — that would
    duplicate writes."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=FakeWS({}),
        active_tickers_provider=lambda: [],
        interval_seconds=10.0,
    )
    snap.start()
    first_thread = snap._thread
    snap.start()
    second_thread = snap._thread
    assert first_thread is second_thread
    snap.stop()
    snap.join(timeout=2.0)


def test_stale_threshold_boundary(tmp_path):
    """Round-1 #16: cache_age_s == STALE_THRESHOLD_S → 'ws_cache_stale'
    (>= boundary). cache_age_s just under → 'ws_cache'."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    now = 1_700_000_000.0

    # Just under threshold → fresh.
    ws_under = FakeWS({"K1": _ob([[55, 100]], [[44, 80]],
                                  ts=now - mod.STALE_THRESHOLD_S + 0.01)})
    snap_under = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=ws_under,
        active_tickers_provider=lambda: ["K1"],
        clock=lambda: now,
    )
    snap_under._tick_once(conn)

    # Exactly at threshold → stale (>= boundary).
    ws_at = FakeWS({"K2": _ob([[55, 100]], [[44, 80]],
                              ts=now - mod.STALE_THRESHOLD_S)})
    snap_at = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=ws_at,
        active_tickers_provider=lambda: ["K2"],
        clock=lambda: now,
    )
    snap_at._tick_once(conn)

    rows = dict(conn.execute(
        "SELECT ticker, source FROM market_observations_continuous"
    ).fetchall())
    assert rows["K1"] == "ws_cache"
    assert rows["K2"] == "ws_cache_stale"


def test_batch_boundary_at_50_and_51(tmp_path):
    """Round-1 #17: 50 rows = 1 commit; 51 rows = 2 commits (50 + 1)."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    class CommitCounting:
        def __init__(self, real):
            self._real = real
            self.commit_calls = 0

        def execute(self, *a, **k):
            return self._real.execute(*a, **k)

        def executemany(self, *a, **k):
            return self._real.executemany(*a, **k)

        def commit(self):
            self.commit_calls += 1
            return self._real.commit()

    # Exactly 50 → 1 commit.
    ws50 = FakeWS({f"T-{i}": _ob([[55, 100]], [[44, 80]], ts=time.time())
                   for i in range(50)})
    snap50 = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=ws50,
        active_tickers_provider=lambda: list(ws50._orderbooks.keys()),
    )
    proxy50 = CommitCounting(conn)
    snap50._tick_once(proxy50)
    assert proxy50.commit_calls == 1, (
        f"50 rows should fit in 1 batch; got {proxy50.commit_calls} commits"
    )

    # 51 → 2 commits (50 + 1).
    conn.execute("DELETE FROM market_observations_continuous")
    conn.commit()
    ws51 = FakeWS({f"U-{i}": _ob([[55, 100]], [[44, 80]], ts=time.time())
                   for i in range(51)})
    snap51 = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=ws51,
        active_tickers_provider=lambda: list(ws51._orderbooks.keys()),
    )
    proxy51 = CommitCounting(conn)
    snap51._tick_once(proxy51)
    assert proxy51.commit_calls == 2, (
        f"51 rows should split 50+1; got {proxy51.commit_calls} commits"
    )


def test_uses_deep_snapshot_method(tmp_path):
    """Round-1 #1+#2 critical fix: snapshotter MUST call the deep-copy
    variant of get_all_orderbooks. Calling the shallow variant races the
    WS thread's in-place level mutation."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    class TrackingWS:
        def __init__(self):
            self.shallow_calls = 0
            self.deep_calls = 0

        def get_all_orderbooks(self):
            self.shallow_calls += 1
            return {}

        def get_all_orderbooks_snapshot(self):
            self.deep_calls += 1
            return {}

    ws = TrackingWS()
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=ws,
        active_tickers_provider=lambda: ["K1"],
    )
    snap._tick_once(conn)
    assert ws.deep_calls == 1
    assert ws.shallow_calls == 0


def test_retention_sweep_deletes_old_rows(tmp_path):
    """Round-1 #5: retention sweep deletes rows older than retention_days."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    now_dt = datetime.datetime(2026, 5, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)
    now = now_dt.timestamp()

    # Insert 3 rows: 20 days old, 5 days old, current.
    conn.executemany(
        "INSERT INTO market_observations_continuous "
        "(ticker, observation_time, source) VALUES (?, ?, ?)",
        [
            ("OLD", "2026-04-25T12:00:00.000000Z", "ws_cache"),  # 20d old
            ("MID", "2026-05-10T12:00:00.000000Z", "ws_cache"),  # 5d old
            ("NEW", "2026-05-15T11:59:59.000000Z", "ws_cache"),  # ~now
        ],
    )
    conn.commit()

    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=FakeWS({}),
        active_tickers_provider=lambda: [],
        clock=lambda: now,
        retention_days=14,
    )
    snap._retention_sweep(conn)

    remaining = sorted(r[0] for r in conn.execute(
        "SELECT ticker FROM market_observations_continuous"
    ).fetchall())
    assert remaining == ["MID", "NEW"]
    assert snap.metrics["retention_deletes"] == 1


def test_retention_disabled_when_zero(tmp_path):
    """retention_days=0 → no sweep, no deletes."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)
    conn.execute(
        "INSERT INTO market_observations_continuous "
        "(ticker, observation_time, source) VALUES (?, ?, ?)",
        ("OLD", "2020-01-01T00:00:00.000000Z", "ws_cache"),
    )
    conn.commit()

    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=FakeWS({}),
        active_tickers_provider=lambda: [],
        retention_days=0,
    )
    snap._retention_sweep(conn)
    n = conn.execute(
        "SELECT COUNT(*) FROM market_observations_continuous"
    ).fetchone()[0]
    assert n == 1


def test_thread_alive_metric(tmp_path):
    """Round-1 #10: metrics['thread_alive'] flips True during run, False
    after stop."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=FakeWS({}),
        active_tickers_provider=lambda: [],
        interval_seconds=0.05,
    )
    assert snap.metrics["thread_alive"] is False
    snap.start()
    time.sleep(0.15)
    assert snap.metrics["thread_alive"] is True
    snap.stop()
    snap.join(timeout=2.0)
    assert snap.metrics["thread_alive"] is False


def test_skips_prices_outside_kalshi_range(tmp_path):
    """Defensive: prediction-market prices outside [1,99]¢ are nonsense.
    A delta-applied cache with price=100 (or 0) shouldn't crown that as
    the best bid."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    ws = FakeWS({
        "K": _ob(
            yes_levels=[[100, 50], [55, 200]],   # 100 is nonsense
            no_levels=[[0, 80], [44, 100]],       # 0 is nonsense
            ts=time.time(),
        ),
    })
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=ws,
        active_tickers_provider=lambda: ["K"],
    )
    snap._tick_once(conn)

    row = conn.execute(
        "SELECT yes_bid_cents, no_bid_cents FROM market_observations_continuous"
    ).fetchone()
    assert row[0] == 55  # 100 ignored, fall through to 55
    assert row[1] == 44  # 0 ignored, fall through to 44


def test_ws_ob_with_malformed_levels_is_resilient(tmp_path):
    """A cache entry with malformed level data shouldn't crash the tick."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    # Production cache should never produce these, but defense-in-depth.
    ws = FakeWS({
        "KXBTCD-A": {
            "yes": [[55, "not_a_qty"], "not_a_level", None],
            "no": [],
            "ts": time.time(),
        },
    })
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db),
        ws_client=ws,
        active_tickers_provider=lambda: ["KXBTCD-A"],
    )
    snap._tick_once(conn)
    # Row written; bid/ask may be NULL but no exception escaped.
    n = conn.execute(
        "SELECT COUNT(*) FROM market_observations_continuous"
    ).fetchone()[0]
    assert n == 1


def test_dict_shaped_levels_supported(tmp_path):
    """Round-2 #2: levels in dict shape (legacy schema fallback bot/_impl.py
    supports) must be parsed, not silently NULL'd."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    ws = FakeWS({
        "K": {
            "yes": [{"price": 55, "quantity": 100}],
            "no": [{"price": 44, "qty": 80}],  # 'qty' alias
            "ts": time.time(),
        },
    })
    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=ws,
        active_tickers_provider=lambda: ["K"],
    )
    snap._tick_once(conn)
    row = conn.execute(
        "SELECT yes_bid_cents, no_bid_cents, bid_depth, ask_depth "
        "FROM market_observations_continuous"
    ).fetchone()
    assert row == (55, 44, 100, 80)


def test_negative_retention_days_clamped_to_zero(tmp_path):
    """Round-2 #1: retention_days=-N must NOT delete the entire table.
    Without the __init__ clamp, the cutoff computes to `now + |N|d` (in
    the future) and the DELETE matches every row."""
    db = tmp_path / "state.db"
    conn = _make_db(db)
    mod.ensure_schema(conn)

    conn.execute(
        "INSERT INTO market_observations_continuous "
        "(ticker, observation_time, source) VALUES (?, ?, ?)",
        ("K", "2026-05-15T12:00:00.000000Z", "ws_cache"),
    )
    conn.commit()

    snap = mod.MarketObservationsSnapshotter(
        db_path=str(db), ws_client=FakeWS({}),
        active_tickers_provider=lambda: [],
        retention_days=-1,  # malicious / typo
    )
    snap._retention_sweep(conn)

    n = conn.execute(
        "SELECT COUNT(*) FROM market_observations_continuous"
    ).fetchone()[0]
    assert n == 1, "negative retention_days must NOT delete rows"


def test_ensure_schema_not_called_from_run():
    """Round-2 #6: AST regression. `_run` must NOT call `ensure_schema()`
    — schema bootstrap belongs in bot/_impl.py main thread, not in the daemon
    thread. A future refactor that moves it back races the bot/_impl.py ALTER
    TABLE migrations at startup."""
    import ast
    src = (ROOT / "market_observations_snapshotter.py").read_text()
    tree = ast.parse(src)

    run_method = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run":
            run_method = node
            break
    assert run_method is not None, "could not find _run method"

    for sub in ast.walk(run_method):
        if isinstance(sub, ast.Call):
            f = sub.func
            name = (
                getattr(f, "id", None)
                or getattr(f, "attr", None)
            )
            assert name != "ensure_schema", (
                "_run calls ensure_schema — schema bootstrap must be in "
                "bot/_impl.py main thread to avoid racing ALTER TABLE migrations"
            )


def test_get_all_orderbooks_snapshot_method_exists_in_bot_py():
    """Round-2 #10: AST regression. `KalshiWebsocketClient` must define
    `get_all_orderbooks_snapshot` (the deep-copy method the snapshotter
    relies on for cross-thread iteration safety). If a future refactor
    drops or renames it, this test surfaces the regression at CI time
    rather than at production deploy."""
    import ast
    src = (ROOT / "bot/_impl.py").read_text()
    tree = ast.parse(src)

    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_all_orderbooks_snapshot":
            found = True
            # Body must reference self._lock (deep-copy under lock).
            body_src = ast.unparse(node) if hasattr(ast, "unparse") else ""
            assert "_lock" in body_src or "self._lock" in body_src, (
                "get_all_orderbooks_snapshot must hold self._lock during "
                "deepcopy — otherwise the WS thread's in-place delta "
                "mutation races with the deep-copy walk"
            )
            break
    assert found, (
        "bot/_impl.py KalshiWebsocketClient is missing get_all_orderbooks_snapshot "
        "— H-3a snapshotter requires this deep-copy method"
    )
