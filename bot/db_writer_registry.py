"""Process-global registry of currently-active sqlite3 writers.

When `database is locked` fires on `StateManager.conn`, the operator can
read the warning-log's `active_writers=...` field to see which OTHER
connection was holding the writer lock at that moment. Plus every
tracked write emits a single `WRITER_ACTIVE name=... duration_ms=...
status=...` log line, so even without the snapshot the journal can be
grepped for periodic patterns.

Background: the bot has 8 sqlite3 connections to state.db all in the
main process (`lsof -p` confirmed). Per-deploy + organic db-locked
errors with `in_tx=False` confirm busy_timeout-driven contention from
ANOTHER connection. This module identifies which one.

Usage:
    from bot.db_writer_registry import tracked_write

    with tracked_write("supabase_sync", "batch_mirror"):
        self._db.execute("INSERT ...")
        self._db.commit()

The context manager registers the writer at entry, unregisters at exit,
and logs the duration. Exceptions inside the block are propagated; the
log marks them as `status=fail`.

Thread-safety: a single `threading.Lock` serializes registry reads/writes.
The lock is held briefly (dict operations only) so it doesn't add
contention. Multiple threads can register concurrently safely.

Identity / re-entry: each register call returns a unique token (name +
started_ts + thread_name + counter for sub-microsecond races). Multiple
concurrent writes from the same module produce separate entries.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Deque, Dict, List, Tuple

# Process-global state. Module-level — singleton pattern.
_lock = threading.Lock()
_active_writers: Dict[str, Tuple[float, str, str]] = {}
# token (str) -> (started_ts, sql_kind, thread_name)

_token_counter = itertools.count()

# Ring buffer of recently-finished writes for intra-process culprit RCA.
# After Bit 4.5a + writer-tracking instrumentation deployed, observed db-locked
# hits showed `active_writers=[]` and `begin_immediate_duration_ms=0.1` —
# meaning BEGIN IMMEDIATE failed in 100µs (NOT busy_timeout-driven). The
# lock-holder is another connection in the SAME process that releases
# its lock JUST before our BEGIN IMMEDIATE returns SQLITE_BUSY (SQLite
# fails immediately for intra-process lock contention to avoid deadlock,
# bypassing busy_handler retry). snapshot_active() at the moment of
# failure shows [] because the lock-holder already finished.
#
# The ring buffer tracks the last N finished writes so the failure-path
# log can include "what finished JUST before the BUSY return" — that's
# the intra-process culprit.
#
# maxlen=50: covers ~5 seconds at peak write rate (~10 writes/sec). Bounded
# memory regardless of process uptime.
_recent_writes: Deque[Tuple[float, str, str, float, str]] = deque(maxlen=50)
# entries: (finished_ts, name, sql_kind, duration_ms, thread_name)


def register_write(name: str, sql_kind: str = "?") -> str:
    """Mark `name` as actively writing. Returns a token to pass to
    `unregister_write` after the write completes.

    Token format: f"{name}#{started_ts}#{thread_name}#{counter}". The
    counter component disambiguates same-thread same-instant registrations
    (rare but possible under sub-microsecond timing).
    """
    started = time.time()
    thread = threading.current_thread().name
    counter = next(_token_counter)
    token = f"{name}#{started:.6f}#{thread}#{counter}"
    with _lock:
        _active_writers[token] = (started, sql_kind, thread)
    return token


def unregister_write(token: str, success: bool = True) -> None:
    """Mark a previously-registered write as complete and emit the
    per-write log line. Silent no-op if `token` is unknown (defensive
    so error-path callers don't double-fail). Also appends to the
    `_recent_writes` ring buffer for intra-process culprit RCA."""
    with _lock:
        entry = _active_writers.pop(token, None)
    if entry is None:
        return
    started, sql_kind, thread = entry
    finished_ts = time.time()
    duration_ms = (finished_ts - started) * 1000.0
    name = token.split("#", 1)[0]
    status = "ok" if success else "fail"
    # Append to ring buffer (lock briefly so concurrent recent_writes()
    # callers don't see a half-modified deque).
    with _lock:
        _recent_writes.append((finished_ts, name, sql_kind, duration_ms, thread))
    logging.info(
        "WRITER_ACTIVE name=%r kind=%r thread=%r duration_ms=%.1f status=%s",
        name, sql_kind, thread, duration_ms, status,
    )


def recent_writes(window_s: float = 5.0) -> List[Tuple[str, str, float, float, str]]:
    """Return writes that finished within the last `window_s` seconds as
    a list of (name, sql_kind, duration_ms, finished_ts, thread_name)
    tuples. Used by `StateManager.insert_evaluated_opportunity`'s
    failure-path log to identify the intra-process lock-holder that JUST
    released — the FAST-fail mechanism (begin_immediate_duration_ms ~0.1ms)
    means snapshot_active() shows [] but recent_writes() captures the
    holder.

    Returns entries ordered oldest-first within the window.
    """
    cutoff = time.time() - window_s
    with _lock:
        snap = list(_recent_writes)
    # Filter and reshape: drop finished_ts from leading position
    out: List[Tuple[str, str, float, float, str]] = []
    for finished_ts, name, kind, dur, thread in snap:
        if finished_ts >= cutoff:
            out.append((name, kind, dur, finished_ts, thread))
    return out


def snapshot_active() -> List[Tuple[str, float, str, str]]:
    """Return a list of currently-active writers as
    ``(token, started_ts, sql_kind, thread_name)`` tuples. Used by
    `StateManager.insert_evaluated_opportunity`'s failure-path log to
    identify which OTHER connection held the writer lock when the
    insert failed with database-is-locked."""
    with _lock:
        return [
            (token, started, kind, thread)
            for token, (started, kind, thread) in _active_writers.items()
        ]


@contextmanager
def tracked_write(name: str, sql_kind: str = "?"):
    """Context manager wrapper around register/unregister.

    Usage:
        with tracked_write("module_name", "operation_kind"):
            self._db.execute(...)
            self._db.commit()

    On normal exit: unregister + log status=ok.
    On exception: unregister + log status=fail, then re-raise.
    """
    token = register_write(name, sql_kind)
    success = True
    try:
        yield
    except BaseException:
        success = False
        raise
    finally:
        unregister_write(token, success=success)
