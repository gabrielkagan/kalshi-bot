#!/usr/bin/env python3
"""Phase G-3: cal_mlp annotation backfill on historical 15M shadow rows.

The live `CalMLPPostHocProcessor` daemon (scripts/cal_mlp/post_hoc_processor.py)
filters by `evaluation_time > now - 300s` (5-min recency window). Rows
older than 5 minutes are stranded without v1 predictions — currently
~80K such 15M shadow rows in production.

This script unsticks them in two stages:

  G-3a stamp_uuids_on_historical:
    Stamps `cal_mlp_request_id = uuid4().hex` on rows where:
      product_type='15m'
      AND cal_mlp_request_id IS NULL
      AND cal_mlp_skipped_reason IS NULL
      AND raw_prob IS NOT NULL
    Pure SQL — no ML deps.

  G-3b drain_via_processor:
    Constructs a CalMLPPostHocProcessor with `recent_window_sec=10**9`
    (effectively infinite — bypasses the recency filter). Calls
    `_poll_and_process` in a foreground loop until rows_seen drops to 0
    OR max_iterations is reached.

Operator workflow (run on VPS where ML deps are installed):
  python3 scripts/shadow_coverage_calmlp_backfill.py --db state.db --stage all

Master plan: kb/decisions/shadow-coverage-expansion-may01.md.
"""

# Phase G-3 round 2 C6 fix: pin OMP threads BEFORE numpy/scipy/torch
# load (transitively via cal_mlp.integration). Per CLAUDE.md "Critical
# rules" + bot/_impl.py's same-pattern import. No-op if bot package
# is absent (stamp-only stage skips the ML import).
import os as _os
import sys as _sys
_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _REPO)
try:
    import bot._thread_env  # noqa: F401
except ImportError:
    pass
# `scripts/cal_mlp/` on sys.path for the deferred `from integration import` and
# `from post_hoc_processor import` calls inside `main()`. Placed AFTER
# bot._thread_env so OMP_NUM_THREADS=1 is set before any of those modules
# transitively load numpy/scipy/torch.
_sys.path.insert(0, _os.path.join(_REPO, "scripts", "cal_mlp"))

import argparse
import logging
import os
import sqlite3
import sys
import time as _time_mod
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


# Reuse checkpoint helpers from G-1 harness via direct copy (avoids
# inter-script import deps; both scripts live in scripts/).

def read_checkpoint(checkpoint_dir: Optional[str], phase: str) -> int:
    if not checkpoint_dir:
        return 0
    path = os.path.join(checkpoint_dir, f"{phase}.last_id")
    if not os.path.exists(path):
        return 0
    try:
        with open(path) as f:
            return int((f.read() or "0").strip())
    except Exception:
        return 0


def write_checkpoint(checkpoint_dir: Optional[str], phase: str, last_id: int) -> None:
    if not checkpoint_dir:
        return
    os.makedirs(checkpoint_dir, exist_ok=True)
    path = os.path.join(checkpoint_dir, f"{phase}.last_id")
    with open(path, "w") as f:
        f.write(str(last_id))


# ── G-3a: Stamp uuids on historical rows ───────────────────────────────

def stamp_uuids_on_historical(
    conn: sqlite3.Connection,
    batch_size: int = 50,
    sleep_ms: int = 50,
    checkpoint_dir: Optional[str] = None,
) -> int:
    """Stamp `cal_mlp_request_id = uuid4().hex` on historical 15M shadow
    rows lacking one. Idempotent (WHERE IS NULL filter), batched,
    checkpointed. Pure SQL — no ML deps.

    Pre-filters: skips non-15M (daemon won't process), already-stamped,
    already-skipped, and no-raw-prob rows (daemon would only mark
    'missing_features' on those — wasted uuids inflate the queue)."""
    last_id = read_checkpoint(checkpoint_dir, "g3a_stamp")
    total = 0
    while True:
        rows = conn.execute(
            "SELECT id FROM evaluated_opportunities "
            "WHERE id > ? "
            "AND product_type='15m' "
            "AND cal_mlp_request_id IS NULL "
            "AND cal_mlp_skipped_reason IS NULL "
            "AND raw_prob IS NOT NULL "
            "ORDER BY id LIMIT ?",
            (last_id, batch_size),
        ).fetchall()
        if not rows:
            break
        for r in rows:
            request_id = uuid.uuid4().hex
            conn.execute(
                "UPDATE evaluated_opportunities "
                "SET cal_mlp_request_id = ? WHERE id = ?",
                (request_id, r["id"]),
            )
            total += 1
            last_id = r["id"]
        conn.commit()
        write_checkpoint(checkpoint_dir, "g3a_stamp", last_id)
        if sleep_ms > 0:
            _time_mod.sleep(sleep_ms / 1000.0)
        if len(rows) < batch_size:
            break
    return total


# ── G-3b: Drain stamped rows via the processor ─────────────────────────

def drain_via_processor(
    processor,
    conn,
    *,
    sleep_between_polls_s: float = 0.5,
    max_iterations: int = 10000,
) -> int:
    """Drive `processor._process_one_batch(conn)` in a foreground loop
    until per-tick `rows_seen` drops to 0 OR `max_iterations` is reached.
    Caller is responsible for constructing the processor with a large
    `recent_window_sec` so the recency filter doesn't skip historical
    rows AND for opening a sqlite connection (the daemon's `_run` opens
    its own; we have to do the same since we're not using `_run`).

    `conn` is REQUIRED — production calls _process_one_batch(conn). Test
    stubs pass MagicMock(); the mock accepts the positional arg + ignores
    it. Phase G-3 round 2 C5 fix: previously had a `conn=None` branch
    that was only reachable in tests, masking the production call shape
    (same C3-class bug as round 1).

    Returns iteration count. The processor logs rows_updated and skip-
    reasons via its own metrics dict."""
    n = 0
    while n < max_iterations:
        processor._process_one_batch(conn)
        n += 1
        # _last_tick_rows_seen is set by the wrapper around _process_one_batch
        # in main(). Tests set it via side_effect.
        last_tick = getattr(processor, "_last_tick_rows_seen", None)
        if last_tick is not None and last_tick == 0:
            logger.info("g3b: drain complete after %d polls (rows_seen=0)", n)
            return n
        if sleep_between_polls_s > 0:
            _time_mod.sleep(sleep_between_polls_s)
    logger.warning("g3b: hit max_iterations=%d without draining", max_iterations)
    return n


# ── Driver ─────────────────────────────────────────────────────────────

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="cal_mlp annotation backfill on historical 15M shadow rows",
    )
    parser.add_argument("--db", required=True, help="path to state.db")
    parser.add_argument(
        "--stage", required=True,
        choices=["stamp", "drain", "all"],
        help="stamp = stamp uuids; drain = run processor with infinite recency; all = both",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument(
        "--sleep-ms", type=int, default=50,
        help="ms between stamp batches (yields DB lock to live writers)",
    )
    parser.add_argument(
        "--checkpoint-dir", default="data/backfill_ckpt",
        help="directory for resumable checkpoints",
    )
    parser.add_argument(
        "--max-drain-iterations", type=int, default=10000,
        help="cap on drain poll iterations to prevent runaway",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.stage in ("stamp", "all"):
        conn = _connect(args.db)
        logger.info("g3a: starting stamp_uuids_on_historical")
        n = stamp_uuids_on_historical(
            conn, batch_size=args.batch_size, sleep_ms=args.sleep_ms,
            checkpoint_dir=args.checkpoint_dir,
        )
        logger.info("g3a: stamped %d historical rows", n)
        conn.close()

    if args.stage in ("drain", "all"):
        # Phase G-3 round 1 C4 fix: assert CALMLP_ENABLED is truthy or
        # _process_one_batch will silently no-op the entire backfill.
        # Force-set if unset; abort if explicitly disabled.
        env_val = os.environ.get("CALMLP_ENABLED", "1").strip().lower()
        if env_val not in ("1", "true", "yes"):
            logger.error(
                "g3b: CALMLP_ENABLED=%r is falsy — _process_one_batch "
                "would no-op every tick. Set CALMLP_ENABLED=1 and re-run.",
                env_val,
            )
            return 2
        os.environ.setdefault("CALMLP_ENABLED", "1")
        logger.info("g3b: CALMLP_ENABLED=%s", os.environ["CALMLP_ENABLED"])

        # Drain requires ML deps. Late-import to keep stamp-only usage
        # working on systems without pandas/torch installed.
        # `from integration import` resolves via the `scripts/cal_mlp/` entry
        # added to sys.path at the top of this module — placed AFTER
        # bot._thread_env so OMP_NUM_THREADS=1 is already set when integration
        # transitively loads numpy/scipy/torch (Phase G-3 round 3 C8 contract).
        # Predictor loading mirrors bot/_impl.py:5596 — construct one
        # CalMLPPredictor per asset, call .warmup(), drop unloaded.
        # Phase G-3 round 1 C1 fix (was: from integration import warmup
        # — `warmup` is a method, not a module function).
        from integration import CalMLPPredictor
        predictors = {a: CalMLPPredictor(a) for a in ("BTC", "ETH", "SOL", "XRP")}
        for a, p in list(predictors.items()):
            try:
                p.warmup()
            except Exception as e:
                logger.warning("g3b: %s predictor warmup raised: %s", a, e)
        predictors = {a: p for a, p in predictors.items()
                      if getattr(p, "_loaded", False)}
        if not predictors:
            logger.error("g3b: no predictors loaded — cannot drain")
            return 2
        logger.info("g3b: loaded predictors for %s", sorted(predictors.keys()))

        from post_hoc_processor import CalMLPPostHocProcessor
        processor = CalMLPPostHocProcessor(
            db_path=args.db, predictors=predictors,
            poll_interval_sec=0.0,  # foreground loop sets cadence
            batch_size=args.batch_size,
            recent_window_sec=10**9,  # ~30 years — effectively infinite
        )
        # Open OUR own connection — _process_one_batch requires one;
        # the daemon's _run normally owns it but we're not using _run.
        # Phase G-3 round 1 C2 fix.
        drain_conn = sqlite3.connect(
            args.db, timeout=2.0, isolation_level=None,
            check_same_thread=False,
        )
        drain_conn.execute("PRAGMA journal_mode=WAL")
        drain_conn.execute("PRAGMA busy_timeout=10000")
        # Wrap _process_one_batch to record per-tick rows_seen for drain
        # termination.
        _orig_proc = processor._process_one_batch

        def _wrapped_proc(c):
            seen_before = processor._metrics.get("rows_seen", 0)
            _orig_proc(c)
            seen_after = processor._metrics.get("rows_seen", 0)
            processor._last_tick_rows_seen = seen_after - seen_before

        processor._process_one_batch = _wrapped_proc
        logger.info("g3b: starting drain (recent_window_sec=infinite)")
        try:
            drain_via_processor(
                processor, drain_conn,
                max_iterations=args.max_drain_iterations,
            )
        finally:
            # Phase G-3 round 2 C7 fix: restore the un-wrapped method so
            # subsequent uses of this processor instance (re-runs, future
            # in-process callers) don't compound wrappers / leak _orig_proc.
            processor._process_one_batch = _orig_proc
            try:
                drain_conn.close()
            except Exception:
                pass
        # Print final metrics so the operator sees how many actually
        # got predicted vs skipped.
        logger.info("g3b: final metrics: %s", processor._metrics)

    return 0


if __name__ == "__main__":
    sys.exit(main())
