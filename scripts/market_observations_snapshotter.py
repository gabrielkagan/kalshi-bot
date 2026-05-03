"""Phase H-3a: continuous NBBO snapshotter.

Periodically samples the in-memory WS orderbook cache for active 15M tickers
and writes a row per (ticker, tick) into `market_observations_continuous`.
Foundation for Phase H-3 (Kalshi fill simulator).

Design choices (per kb/decisions/phase-h3-deferred-needs-nbbo-infra-may02.md):
- Read-only consumer of `KalshiWebsocketClient.get_all_orderbooks_snapshot()`.
  No new REST traffic. The `_snapshot` variant returns a DEEP copy under the
  WS lock — required because the WS thread mutates level lists in place
  (`_apply_fp_delta`). The non-deep variant would race during iteration.
- Single daemon thread; sleep-based cadence.
- Filters to active 15M tickers via injected `active_tickers_provider`.
  Provider must return ticker STRINGS (not window/market dicts). Typical
  bot.py wiring:
      lambda: [m["ticker"]
               for w in discover_active_windows(client)
               for m in w["markets"]
               if w.get("product_type") == "15m"]
- Batched commits ≤BATCH_SIZE rows per CLAUDE.md DB lock rules; uses
  `executemany` so each batch is a single Python→SQLite round-trip.
- Surfaces errors + liveness via `metrics` dict for dashboard observability.

Schema migration ownership: `ensure_schema()` is exposed as a module function
the bot.py main thread calls during startup migrations. The snapshotter's
`_run` does NOT call it — running schema bootstrap from a daemon thread
races with the bot's other ALTER TABLE migrations.

Timestamp join target: rows in this table can be joined to
`evaluated_opportunities.evaluation_time` lexically — both use
`%Y-%m-%dT%H:%M:%S.%fZ` (microsecond-bearing). The schema's bare-second
DEFAULT (`evaluation_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))`)
only fires on rows where the INSERT does not specify the column — production
inserts always specify with microseconds via Python.
"""
from __future__ import annotations

import datetime
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Cache freshness threshold. Beyond this, source label is 'ws_cache_stale'
# rather than 'ws_cache' but we still record the bid/ask for forensics.
# Matches the bot's existing scan-loop tolerance (ORDERBOOK_CACHE_TTL ~5s,
# WS-fallback up to 2× = 10s): readings >10s old aren't NBBO the bot
# would have acted on, so the H-3 fill simulator should treat them as
# untrusted by default.
STALE_THRESHOLD_S = 10.0

# Per CLAUDE.md "DB write batches: ≤50 rows per commit". We split snapshot
# rows into ≤BATCH_SIZE batches, committing between each via executemany.
BATCH_SIZE = 50

# Default polling cadence. 10s × ~30 active 15M tickers = ~3 inserts/sec
# (peak). Daily volume: 8640 ticks/day × ~30 tickers = ~260K rows/day.
DEFAULT_INTERVAL_S = 10.0

# Retention: rows older than this are deleted by the periodic sweep. The
# H-3 fill simulator only ever cares about NBBO around fill events; the
# full continuous corpus has no value beyond ~14 days. At ~260K rows/day
# this caps disk growth at ~3.6M rows / ~900MB. Configurable per env.
DEFAULT_RETENTION_DAYS = 14

# How often to run retention sweep (every N ticks). At 10s/tick × 360 =
# every 1 hour, sweep fires. Once-per-hour is conservative — retention
# bound is days, so within-hour drift is irrelevant.
RETENTION_SWEEP_EVERY_N_TICKS = 360

# ISO 8601 with microseconds + Z suffix — same shape as bot.py's
# Python-written timestamps. Lexical comparison works against
# evaluated_opportunities.evaluation_time when bot.py wrote that column.
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


# ── Schema ──────────────────────────────────────────────────────────────────


_DDL = """
CREATE TABLE IF NOT EXISTS market_observations_continuous (
    id INTEGER PRIMARY KEY,
    ticker TEXT NOT NULL,
    observation_time TEXT NOT NULL,
    yes_bid_cents INTEGER,
    yes_ask_cents INTEGER,
    no_bid_cents INTEGER,
    no_ask_cents INTEGER,
    bid_depth INTEGER,
    ask_depth INTEGER,
    source TEXT NOT NULL,
    cache_age_ms INTEGER
)
"""

_INDEX_TICKER_TIME = """
CREATE INDEX IF NOT EXISTS idx_moc_ticker_time
  ON market_observations_continuous (ticker, observation_time)
"""

_INDEX_TIME = """
CREATE INDEX IF NOT EXISTS idx_moc_time
  ON market_observations_continuous (observation_time)
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent: creates table + indexes if missing.

    Call from bot.py main schema migration block — NOT from the
    snapshotter's daemon thread. Running schema bootstrap from a thread
    that races with bot.py's ALTER TABLE migrations causes lock
    contention at startup and offers no benefit.
    """
    conn.execute(_DDL)
    conn.execute(_INDEX_TICKER_TIME)
    conn.execute(_INDEX_TIME)
    conn.commit()


# ── Top-of-book derivation ────────────────────────────────────────────────


def _level_price_qty(entry: Any) -> Optional[tuple]:
    """Extract (price_cents:int, qty:int) from a level entry, or None on
    malformed input.

    Tolerates the three shapes bot.py's WS client supports (round-2 #2):
    - list/tuple [price, qty]    — Kalshi 2026 normalized format
    - dict {price, quantity}     — legacy schema fallback in bot.py
    Anything else returns None (the row gets NULL bid/ask for forensics
    rather than crashing the tick).
    """
    # list/tuple shape
    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
        try:
            price = int(entry[0])
            qty = int(entry[1])
        except (ValueError, TypeError):
            return None
        return price, qty
    # dict shape (matches bot.py's _level_price/_level_qty fallback)
    if isinstance(entry, dict):
        p = entry.get("price")
        q = entry.get("quantity", entry.get("qty"))
        if p is None or q is None:
            return None
        try:
            price = int(p)
            qty = int(q)
        except (ValueError, TypeError):
            return None
        return price, qty
    return None


def _best_bid(levels: Iterable) -> Optional[tuple]:
    """Return (best_price_cents, depth_at_best) across `levels`, or None
    if empty / all-zero-qty / all-malformed. Levels are unsorted in
    general (delta-applied caches can scramble order)."""
    best_price = None
    best_qty = 0
    for entry in levels:
        pq = _level_price_qty(entry)
        if pq is None:
            continue
        price, qty = pq
        if qty <= 0:
            continue
        # Defensive: prediction-market prices outside [1, 99]¢ are
        # nonsense for YES/NO bid intent (1-99¢ Kalshi range). Skip.
        if not (1 <= price <= 99):
            continue
        if best_price is None or price > best_price:
            best_price = price
            best_qty = qty
    if best_price is None:
        return None
    return best_price, best_qty


def derive_top_of_book(yes_levels: Iterable, no_levels: Iterable) -> Dict[str, Optional[int]]:
    """Compute yes/no bid+ask cents and depth-at-best from a Kalshi book.

    Convention:
    - YES level [price, qty] = "buy YES at price, qty contracts" (a YES bid).
    - NO level [price, qty] = "buy NO at price, qty contracts" (a NO bid).
    - YES ask = 100 - best_no_bid (selling YES = buying NO at 100-X).
    - NO ask = 100 - best_yes_bid.
    - bid_depth = depth at best YES bid (single level).
    - ask_depth = depth at best NO bid (= depth at best YES ask single level).
    """
    yes_top = _best_bid(yes_levels)
    no_top = _best_bid(no_levels)

    yes_bid_cents = yes_top[0] if yes_top else None
    bid_depth = yes_top[1] if yes_top else None
    no_bid_cents = no_top[0] if no_top else None
    ask_depth = no_top[1] if no_top else None

    yes_ask_cents = (100 - no_bid_cents) if no_bid_cents is not None else None
    no_ask_cents = (100 - yes_bid_cents) if yes_bid_cents is not None else None

    return {
        "yes_bid_cents": yes_bid_cents,
        "yes_ask_cents": yes_ask_cents,
        "no_bid_cents": no_bid_cents,
        "no_ask_cents": no_ask_cents,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
    }


# ── Snapshotter ────────────────────────────────────────────────────────────


class MarketObservationsSnapshotter:
    """Daemon thread that periodically samples the WS orderbook cache.

    Public:
    - start() / stop() / join(timeout)
    - is_alive() -> bool
    - metrics: dict (ticks, rows_written, errors, last_tick_ts,
      thread_alive, retention_deletes)
    """

    def __init__(
        self,
        db_path: str,
        ws_client: Any,
        active_tickers_provider: Callable[[], List[str]],
        interval_seconds: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.time,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        retention_sweep_every_n_ticks: int = RETENTION_SWEEP_EVERY_N_TICKS,
    ):
        self._db_path = db_path
        self._ws_client = ws_client
        self._active_tickers_provider = active_tickers_provider
        self._interval_s = float(interval_seconds)
        self._clock = clock
        # Round-2 #1: clamp negative retention to 0. A negative value
        # (e.g., from a typo) computed `now - timedelta(-N days)` = a
        # cutoff in the FUTURE, so the next sweep would DELETE every row.
        self._retention_days = max(0, int(retention_days))
        self._retention_sweep_every = int(retention_sweep_every_n_ticks)

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        self.metrics: Dict[str, Any] = {
            "ticks": 0,
            "rows_written": 0,
            "errors": 0,
            "last_tick_ts": 0.0,
            "thread_alive": False,
            "retention_deletes": 0,
        }

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                # Idempotent — re-calling start() should not spawn a duplicate.
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="MarketObsSnapshotter",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)

    def is_alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    # ── Main loop ──────────────────────────────────────────────────────

    def _run(self) -> None:
        # Each thread gets its own connection — sqlite3 connections are
        # not safe to share across threads under any circumstances.
        conn = None
        try:
            conn = sqlite3.connect(self._db_path, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self.metrics["thread_alive"] = True
            # Round-2 #8: run one retention sweep at startup so a
            # restart after retention_days was reduced (or after a long
            # outage) doesn't wait 1h to cleanup. Cheap when nothing's
            # old. Only fires if retention is enabled.
            if self._retention_days > 0:
                try:
                    self._retention_sweep(conn)
                except Exception:
                    self.metrics["errors"] += 1
                    logger.warning(
                        "startup retention sweep failed", exc_info=True,
                    )
            while not self._stop_event.is_set():
                try:
                    self._tick_once(conn)
                    if (
                        self._retention_sweep_every > 0
                        and self.metrics["ticks"] % self._retention_sweep_every == 0
                    ):
                        self._retention_sweep(conn)
                except Exception:
                    # Defense in depth — _tick_once already swallows its own
                    # exceptions, but if anything escapes (e.g., metrics
                    # mutation TypeError), keep the thread alive.
                    self.metrics["errors"] += 1
                    logger.warning("snapshotter tick failed", exc_info=True)
                # Sleep with stop-event interrupt for fast shutdown.
                self._stop_event.wait(self._interval_s)
        except Exception:
            # Top-level catch: surface that the thread died via metric so
            # bot.py's watchdog (when wired) can detect + restart.
            logger.exception(
                "MarketObsSnapshotter thread crashed at top level — will exit",
            )
            self.metrics["errors"] += 1
        finally:
            self.metrics["thread_alive"] = False
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    # ── One tick ────────────────────────────────────────────────────────

    def _tick_once(self, conn: Any) -> None:
        """Sample WS cache for active 15M tickers, write rows in batches.

        `conn` is duck-typed — accepts any object with .execute() / .commit().
        Tests pass an in-process sqlite3.Connection or a mock LockingConn.
        """
        self.metrics["ticks"] += 1
        self.metrics["last_tick_ts"] = self._clock()

        # 1. Resolve active tickers (defensive — provider can raise).
        try:
            active = list(self._active_tickers_provider() or [])
        except Exception:
            self.metrics["errors"] += 1
            logger.warning(
                "active_tickers_provider raised — skipping tick",
                exc_info=True,
            )
            return

        if not active:
            return

        # 2. Snapshot WS cache once per tick (DEEP copy under WS lock).
        # See KalshiWebsocketClient.get_all_orderbooks_snapshot — required
        # for safe cross-thread iteration (round 1 #1+#2 critical fix).
        try:
            cache = self._ws_client.get_all_orderbooks_snapshot()
        except Exception:
            self.metrics["errors"] += 1
            logger.warning(
                "ws_client.get_all_orderbooks_snapshot() raised — skipping tick",
                exc_info=True,
            )
            return

        # 3. Build rows per active ticker.
        now = self._clock()
        observation_time = (
            datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc)
            .strftime(_ISO_FORMAT)
        )

        rows_to_write: List[tuple] = []
        for ticker in active:
            row = self._build_row(ticker, cache, now, observation_time)
            if row is not None:
                rows_to_write.append(row)

        # 4. Batched insert. _write_batches credits rows_written per
        # successful batch so a partial-failure tick (e.g., 2 of 3 batches
        # commit before lock-out) doesn't lose accounting (round-2 #7).
        try:
            self._write_batches(conn, rows_to_write)
        except sqlite3.Error:
            # Catches OperationalError (lock), DatabaseError (corruption),
            # IntegrityError (NOT NULL violation), InterfaceError. Round-2
            # #7 broadened from OperationalError-only — we still want the
            # thread alive on any of these but log the type for triage.
            self.metrics["errors"] += 1
            logger.warning(
                "snapshot batch insert failed — will retry next tick",
                exc_info=True,
            )
            return

    def _build_row(
        self,
        ticker: str,
        cache: Dict[str, Dict],
        now: float,
        observation_time: str,
    ) -> Optional[tuple]:
        """Return one row tuple for INSERT, or None if the ticker should be
        skipped entirely."""
        ob = cache.get(ticker)

        if ob is None:
            # Ticker is in active list but WS hasn't populated cache yet.
            # Note: 'ws_no_data' instead of 'ws_subscribed_no_data' — the
            # cache absence covers BOTH "subscribed-but-no-snapshot" and
            # "subscription dropped on reconnect". Rename per round-1 #8.
            return (
                ticker, observation_time,
                None, None, None, None,
                None, None,
                "ws_no_data", None,
            )

        # Contract: ws_client.get_all_orderbooks_snapshot returns dict[str, dict].
        # If `ob` is not a dict, that's a contract violation — let it raise so
        # the bug surfaces rather than silently writing NULL rows. The defensive
        # AttributeError catch was removed per round-1 #14.
        yes_levels = ob.get("yes") or []
        no_levels = ob.get("no") or []
        ts = ob.get("ts") or 0.0

        # Try to derive top-of-book; on malformed levels, return NULL fields
        # rather than dropping the row.
        try:
            tob = derive_top_of_book(yes_levels, no_levels)
        except Exception:
            self.metrics["errors"] += 1
            logger.warning(
                "derive_top_of_book raised on ticker %s", ticker, exc_info=True,
            )
            tob = {
                "yes_bid_cents": None, "yes_ask_cents": None,
                "no_bid_cents": None, "no_ask_cents": None,
                "bid_depth": None, "ask_depth": None,
            }

        cache_age_s = max(0.0, now - ts)
        cache_age_ms = int(round(cache_age_s * 1000))
        source = "ws_cache_stale" if cache_age_s >= STALE_THRESHOLD_S else "ws_cache"

        return (
            ticker, observation_time,
            tob["yes_bid_cents"], tob["yes_ask_cents"],
            tob["no_bid_cents"], tob["no_ask_cents"],
            tob["bid_depth"], tob["ask_depth"],
            source, cache_age_ms,
        )

    def _write_batches(self, conn: Any, rows: List[tuple]) -> None:
        """Insert rows in ≤BATCH_SIZE chunks, committing between each.

        Uses executemany so each batch is a single Python→SQLite round-trip
        (round-1 #3 fix). Per-batch crediting to `rows_written` so a
        mid-stream failure (round-2 #7) doesn't lose accounting on the
        batches that succeeded before the failing one.
        """
        sql = (
            "INSERT INTO market_observations_continuous "
            "(ticker, observation_time, "
            "yes_bid_cents, yes_ask_cents, no_bid_cents, no_ask_cents, "
            "bid_depth, ask_depth, source, cache_age_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        for start in range(0, len(rows), BATCH_SIZE):
            batch = rows[start:start + BATCH_SIZE]
            conn.executemany(sql, batch)
            conn.commit()
            self.metrics["rows_written"] += len(batch)

    # ── Retention ──────────────────────────────────────────────────────

    def _retention_sweep(self, conn: Any) -> None:
        """Delete rows older than `retention_days`. Idempotent + cheap when
        nothing's old. Skipped if retention_days <= 0 (operator-disabled)."""
        if self._retention_days <= 0:
            return
        cutoff_dt = (
            datetime.datetime.fromtimestamp(self._clock(), tz=datetime.timezone.utc)
            - datetime.timedelta(days=self._retention_days)
        )
        cutoff_iso = cutoff_dt.strftime(_ISO_FORMAT)
        try:
            cur = conn.execute(
                "DELETE FROM market_observations_continuous "
                "WHERE observation_time < ?",
                (cutoff_iso,),
            )
            conn.commit()
            # Round-3 #1: cur.rowcount returns -1 in some DB-API impls
            # when row count is unavailable. The previous `... or 0`
            # treated -1 as truthy, which would have decremented the
            # metric. max(0, ...) guards properly.
            n = max(0, getattr(cur, "rowcount", 0) or 0)
            self.metrics["retention_deletes"] += n
            if n > 0:
                logger.info(
                    "MarketObs retention sweep: deleted %d rows older than %s",
                    n, cutoff_iso,
                )
        except sqlite3.Error:
            # Round-3 #3: broadened to sqlite3.Error parity with
            # _tick_once's broadened catch. Same trade-off — keep the
            # thread alive on any DB-layer error; resurface via metric.
            self.metrics["errors"] += 1
            logger.warning(
                "retention sweep failed — will retry next sweep window",
                exc_info=True,
            )
