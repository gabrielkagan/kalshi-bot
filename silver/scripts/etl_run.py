"""silver.scripts.etl_run — nightly silver ETL entry point (D3.0, ticket 86b9zxc6t).

Reads bronze JSONL.zst chunks for a target UTC date and writes typed Parquet
to `silver/v1/<source>/utc_date=YYYY-MM-DD/silver-<source>-<runts>.parquet`.

CLI:
    silver/scripts/etl_run.sh                 # processes (now_utc - 1 day)
    silver/scripts/etl_run.sh --date 2026-05-18
    silver/scripts/etl_run.sh --date 2026-05-18 --bronze-root s3://... --silver-root s3://...

Programmatic:
    from silver.scripts import etl_run
    etl_run.run_for_date("2026-05-18", bronze_root="s3://...", silver_root="s3://...")

Architecture:
- Per D0.3 §13:422-423 the stack is DuckDB + dbt. D3.0 ships the DuckDB
  parse+write layer directly (no dbt project at first-Bit kickoff per the
  Bit-kickoff verify list item #2: dbt-duckdb's `incremental + delete+insert
  + partition_by` interaction is non-obvious, so D3.0 implements the
  semantics via direct DuckDB SQL with idempotent overwrite per partition).
  A follow-up Bit may layer dbt models on top once the verify-list item
  resolves.
- Per D0.3 §3 partition layout, bronze is under
  `<source>/<channel>/year=YYYY/month=MM/day=DD/hour=HH/conn=<X>/*.jsonl.zst`.
- Silver flattens to `<source-table>/utc_date=YYYY-MM-DD/*.parquet`
  (decision #8 — silver chooses single-col date partition for analyst
  query patterns).
- 5 Tier-1 silver sources (decision #3 — `kalshi_trade` deferred to D3.3):
  - `coinbase_ticker_v1` ← bronze `coinbase_ws/ticker/`
  - `coinbase_matches_v1` ← bronze `coinbase_ws/matches/`
  - `coinbase_heartbeat_v1` ← bronze `coinbase_ws/heartbeat/`
  - `coinbase_status_v1` ← bronze `coinbase_ws/status/`
  - `kalshi_market_lifecycle_v2_v1` ← bronze `kalshi_ws/market_lifecycle_v2/`
- _unrouted/ partitions and non-WS sources (REST snapshots etc.) are
  defensively SKIPPED per plan-doc tests #10 + #11.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# ─── Source registry ────────────────────────────────────────────────────────


# (bronze_source, bronze_channel, silver_table, schema_version)
# Order: Coinbase first (5 channels → 4 Tier-1 + 1 Tier-2 deferred),
# Kalshi second (4 channels → 1 Tier-1 + 3 deferred).
TIER_1_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    ("coinbase_ws", "ticker",                "coinbase_ticker",                 "coinbase_ticker_v1"),
    ("coinbase_ws", "matches",               "coinbase_matches",                "coinbase_matches_v1"),
    ("coinbase_ws", "heartbeat",             "coinbase_heartbeat",              "coinbase_heartbeat_v1"),
    ("coinbase_ws", "status",                "coinbase_status",                 "coinbase_status_v1"),
    ("kalshi_ws",   "market_lifecycle_v2",   "kalshi_market_lifecycle_v2",      "kalshi_market_lifecycle_v2_v1"),
)


# ─── SQL templates per source ───────────────────────────────────────────────


def _envelope_select(silver_schema_version: str) -> str:
    """Shared envelope-col projection (decision-#7 macro inlined here).

    Returns the 6 envelope cols projected from a bronze row whose JSON
    has `_wire_recv_ts`, `_source`, `_conn`, `_channel`, `_collector_seq`,
    `_raw`. `utc_date` is derived from `_wire_recv_ts`.
    """
    return (
        f"CAST(_wire_recv_ts AS TIMESTAMP) AS wire_recv_ts,\n"
        f"        _source AS source,\n"
        f"        _conn   AS conn,\n"
        f"        _channel AS channel,\n"
        f"        CAST(_collector_seq AS BIGINT) AS collector_seq,\n"
        f"        '{silver_schema_version}' AS silver_schema_version,\n"
        f"        CAST(_wire_recv_ts AS DATE) AS utc_date"
    )


def _sql_silver_coinbase_ticker(bronze_glob: str) -> str:
    """Project coinbase_ticker bronze → typed silver columns."""
    return f"""
    SELECT
        {_envelope_select("coinbase_ticker_v1")},
        json_extract_string(_raw, '$.product_id')       AS product_id,
        CAST(json_extract(_raw, '$.sequence') AS BIGINT) AS sequence,
        CAST(json_extract_string(_raw, '$.price')        AS DOUBLE) AS price,
        CAST(json_extract_string(_raw, '$.best_bid')     AS DOUBLE) AS best_bid,
        CAST(json_extract_string(_raw, '$.best_bid_size') AS DOUBLE) AS best_bid_size,
        CAST(json_extract_string(_raw, '$.best_ask')     AS DOUBLE) AS best_ask,
        CAST(json_extract_string(_raw, '$.best_ask_size') AS DOUBLE) AS best_ask_size,
        json_extract_string(_raw, '$.side')              AS side,
        CAST(json_extract_string(_raw, '$.time')         AS TIMESTAMP) AS exchange_time,
        CAST(json_extract(_raw, '$.trade_id')            AS BIGINT) AS trade_id,
        CAST(json_extract_string(_raw, '$.last_size')    AS DOUBLE) AS last_size,
        CAST(json_extract_string(_raw, '$.open_24h')     AS DOUBLE) AS open_24h,
        CAST(json_extract_string(_raw, '$.volume_24h')   AS DOUBLE) AS volume_24h,
        CAST(json_extract_string(_raw, '$.low_24h')      AS DOUBLE) AS low_24h,
        CAST(json_extract_string(_raw, '$.high_24h')     AS DOUBLE) AS high_24h,
        CAST(json_extract_string(_raw, '$.volume_30d')   AS DOUBLE) AS volume_30d
    FROM read_json_auto('{bronze_glob}', format='newline_delimited', compression='zstd', columns={{
        _wire_recv_ts: 'VARCHAR',
        _source: 'VARCHAR',
        _conn: 'VARCHAR',
        _channel: 'VARCHAR',
        _collector_seq: 'BIGINT',
        _raw: 'VARCHAR'
    }})
    """


def _sql_silver_coinbase_matches(bronze_glob: str) -> str:
    return f"""
    SELECT
        {_envelope_select("coinbase_matches_v1")},
        json_extract_string(_raw, '$.product_id') AS product_id,
        CAST(json_extract(_raw, '$.sequence') AS BIGINT) AS sequence,
        CAST(json_extract(_raw, '$.trade_id') AS BIGINT) AS trade_id,
        json_extract_string(_raw, '$.maker_order_id') AS maker_order_id,
        json_extract_string(_raw, '$.taker_order_id') AS taker_order_id,
        json_extract_string(_raw, '$.side') AS side,
        CAST(json_extract_string(_raw, '$.size')  AS DOUBLE) AS size,
        CAST(json_extract_string(_raw, '$.price') AS DOUBLE) AS price,
        CAST(json_extract_string(_raw, '$.time')  AS TIMESTAMP) AS exchange_time
    FROM read_json_auto('{bronze_glob}', format='newline_delimited', compression='zstd', columns={{
        _wire_recv_ts: 'VARCHAR',
        _source: 'VARCHAR',
        _conn: 'VARCHAR',
        _channel: 'VARCHAR',
        _collector_seq: 'BIGINT',
        _raw: 'VARCHAR'
    }})
    """


def _sql_silver_coinbase_heartbeat(bronze_glob: str) -> str:
    return f"""
    SELECT
        {_envelope_select("coinbase_heartbeat_v1")},
        json_extract_string(_raw, '$.product_id') AS product_id,
        CAST(json_extract(_raw, '$.sequence') AS BIGINT) AS sequence,
        CAST(json_extract(_raw, '$.last_trade_id') AS BIGINT) AS last_trade_id,
        CAST(json_extract_string(_raw, '$.time') AS TIMESTAMP) AS exchange_time
    FROM read_json_auto('{bronze_glob}', format='newline_delimited', compression='zstd', columns={{
        _wire_recv_ts: 'VARCHAR',
        _source: 'VARCHAR',
        _conn: 'VARCHAR',
        _channel: 'VARCHAR',
        _collector_seq: 'BIGINT',
        _raw: 'VARCHAR'
    }})
    """


def _sql_silver_coinbase_status(bronze_glob: str) -> str:
    """Status preserves arrays as JSON strings; counts COALESCE-to-0 (M6)."""
    return f"""
    SELECT
        {_envelope_select("coinbase_status_v1")},
        json_extract(_raw, '$.currencies')::VARCHAR AS currencies_json,
        json_extract(_raw, '$.products')::VARCHAR   AS products_json,
        CAST(COALESCE(json_array_length(json_extract(_raw, '$.currencies')), 0) AS INTEGER) AS currency_count,
        CAST(COALESCE(json_array_length(json_extract(_raw, '$.products')),   0) AS INTEGER) AS product_count
    FROM read_json_auto('{bronze_glob}', format='newline_delimited', compression='zstd', columns={{
        _wire_recv_ts: 'VARCHAR',
        _source: 'VARCHAR',
        _conn: 'VARCHAR',
        _channel: 'VARCHAR',
        _collector_seq: 'BIGINT',
        _raw: 'VARCHAR'
    }})
    """


def _sql_silver_kalshi_lifecycle(bronze_glob: str) -> str:
    """Kalshi market_lifecycle_v2 wide-table; event-type cols are NULL-able."""
    return f"""
    SELECT
        {_envelope_select("kalshi_market_lifecycle_v2_v1")},
        CAST(json_extract(_raw, '$.sid') AS INTEGER) AS sid,
        CAST(json_extract(_raw, '$.seq') AS BIGINT)  AS wire_seq,
        json_extract_string(_raw, '$.msg.event_type')     AS event_type,
        json_extract_string(_raw, '$.msg.market_ticker')  AS market_ticker,
        CAST(json_extract(_raw, '$.msg.floor_strike')     AS DOUBLE) AS floor_strike,
        json_extract_string(_raw, '$.msg.yes_sub_title')  AS yes_sub_title,
        CAST(json_extract(_raw, '$.msg.determination_ts') AS BIGINT) AS determination_ts,
        json_extract_string(_raw, '$.msg.result')         AS result,
        CAST(json_extract_string(_raw, '$.msg.settlement_value') AS DOUBLE) AS settlement_value
    FROM read_json_auto('{bronze_glob}', format='newline_delimited', compression='zstd', columns={{
        _wire_recv_ts: 'VARCHAR',
        _source: 'VARCHAR',
        _conn: 'VARCHAR',
        _channel: 'VARCHAR',
        _collector_seq: 'BIGINT',
        _raw: 'VARCHAR'
    }})
    """


_SQL_BY_SCHEMA: dict[str, Any] = {
    "coinbase_ticker_v1":             _sql_silver_coinbase_ticker,
    "coinbase_matches_v1":            _sql_silver_coinbase_matches,
    "coinbase_heartbeat_v1":          _sql_silver_coinbase_heartbeat,
    "coinbase_status_v1":             _sql_silver_coinbase_status,
    "kalshi_market_lifecycle_v2_v1":  _sql_silver_kalshi_lifecycle,
}


# ─── Bronze chunk discovery ─────────────────────────────────────────────────


def _bronze_glob_for_date(bronze_root: str, source: str, channel: str, utc_date: str) -> str:
    """Compute a glob pattern that DuckDB `read_json_auto` accepts for the
    bronze partition holding a single UTC date.

    Per D0.3 §3 partition layout, bronze is under
    `<source>/<channel>/year=YYYY/month=MM/day=DD/hour=HH/conn=<X>/*.jsonl.zst`.
    A glob for one date covers all 24 hours × all conns × all chunks for
    that day.
    """
    y, m, d = utc_date.split("-")
    base = bronze_root.rstrip("/")
    return f"{base}/{source}/{channel}/year={y}/month={m}/day={d}/hour=*/conn=*/*.jsonl.zst"


def _has_bronze_chunks(bronze_root: str, source: str, channel: str, utc_date: str) -> bool:
    """Return True if at least one bronze chunk exists for the partition.

    For local fs paths: glob the directory. For S3 paths: skip the
    pre-check (DuckDB returns empty result for missing globs; test #3
    asserts SKIP-on-empty semantics post-hoc via output check).
    """
    if bronze_root.startswith("s3://"):
        # S3 path: defer to DuckDB; empty result handled downstream.
        return True
    y, m, d = utc_date.split("-")
    base = Path(bronze_root) / source / channel
    if not base.is_dir():
        return False
    day_dir = base / f"year={y}" / f"month={m}" / f"day={d}"
    if not day_dir.is_dir():
        return False
    # Cheap check — at least one .jsonl.zst exists somewhere under day_dir
    return any(day_dir.rglob("*.jsonl.zst"))


# ─── Per-source ETL ────────────────────────────────────────────────────────


def _process_source(
    conn: Any,  # duckdb.DuckDBPyConnection
    bronze_root: str,
    silver_root: str,
    source: str,
    channel: str,
    silver_table: str,
    silver_schema_version: str,
    utc_date: str,
) -> int:
    """Run the silver SQL for one source/channel/date and write Parquet.

    Returns the row count written. Returns 0 (and SKIPS the Parquet
    write) if bronze has no chunks for that date — per plan-doc test #3
    (decision: SKIP, no zero-row writes).
    """
    if not _has_bronze_chunks(bronze_root, source, channel, utc_date):
        logger.info(
            "silver:%s utc_date=%s — no bronze chunks; SKIP",
            silver_table, utc_date,
        )
        return 0

    bronze_glob = _bronze_glob_for_date(bronze_root, source, channel, utc_date)
    sql_fn = _SQL_BY_SCHEMA[silver_schema_version]
    select_sql = sql_fn(bronze_glob)

    # Output path: <silver_root>/<silver_table>/utc_date=YYYY-MM-DD/silver-<table>-<runts>.parquet
    out_dir = Path(silver_root) / silver_table / f"utc_date={utc_date}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Idempotent overwrite per decision #5 (delete+insert semantics
    # implemented as partition rewrite — wipe existing Parquet for this
    # date then write new). Per the Bit-kickoff verify list item #2,
    # we implement this directly rather than relying on dbt-duckdb's
    # incremental_strategy='delete+insert' + partition_by interaction
    # which is non-obvious. The semantics (5-col unique within partition,
    # full partition rewrite per run) match D0.3 §13:424 incremental
    # backfill behavior.
    for stale in out_dir.glob("*.parquet"):
        stale.unlink()
    for stale in out_dir.glob("*.parquet.tmp"):
        stale.unlink()

    run_ts = int(time.time())
    out_path = out_dir / f"silver-{silver_table}-{run_ts}.parquet"
    tmp_path = out_path.with_suffix(".parquet.tmp")

    # Materialize via DuckDB COPY (atomic local-write then rename)
    copy_sql = f"COPY ({select_sql}) TO '{tmp_path}' (FORMAT PARQUET)"
    conn.execute(copy_sql)
    tmp_path.rename(out_path)

    rowcount = conn.execute(
        f"SELECT COUNT(*) FROM read_parquet('{out_path}')"
    ).fetchone()[0]
    logger.info(
        "silver:%s utc_date=%s rows=%d path=%s",
        silver_table, utc_date, rowcount, out_path,
    )
    return rowcount


# ─── Public entry points ────────────────────────────────────────────────────


def run_for_date(
    target_date: str,
    bronze_root: str,
    silver_root: str,
) -> dict[str, int]:
    """Process one UTC date end-to-end for all 5 Tier-1 silver sources.

    Returns a dict ``{silver_table: rowcount}`` for the run.
    """
    import duckdb
    logger.info(
        "silver ETL run start: utc_date=%s bronze_root=%s silver_root=%s",
        target_date, bronze_root, silver_root,
    )
    conn = duckdb.connect(":memory:")
    # If bronze_root is S3, ensure httpfs + s3 extension loaded; for local
    # paths DuckDB native file reading handles it.
    if bronze_root.startswith("s3://") or silver_root.startswith("s3://"):
        conn.execute("INSTALL httpfs; LOAD httpfs;")
        # Operator must have AWS creds configured (env or ~/.aws/credentials).

    results: dict[str, int] = {}
    for source, channel, silver_table, schema_version in TIER_1_SOURCES:
        try:
            rows = _process_source(
                conn=conn,
                bronze_root=bronze_root,
                silver_root=silver_root,
                source=source,
                channel=channel,
                silver_table=silver_table,
                silver_schema_version=schema_version,
                utc_date=target_date,
            )
            results[silver_table] = rows
        except Exception:
            logger.exception(
                "silver ETL FAILED for source=%s channel=%s utc_date=%s",
                source, channel, target_date,
            )
            raise
    logger.info("silver ETL run end: utc_date=%s totals=%s", target_date, results)
    return results


def _target_date_default() -> str:
    """Default target date = (now_utc - 1 day).date() per decision #2.

    Schedule fires at 02:00 Mac-local; ETL computes target_date at runtime
    so the schedule is TZ-independent.
    """
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=1)).date().isoformat()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--date",
        default=None,
        help="UTC date to process (YYYY-MM-DD); default = (now_utc - 1 day)",
    )
    parser.add_argument(
        "--bronze-root",
        default="s3://kalshi-bot-archive/bronze",
        help="bronze root (s3://... or local path); default S3 production",
    )
    parser.add_argument(
        "--silver-root",
        default="s3://kalshi-bot-archive/silver/v1",
        help="silver root (s3://... or local path); default S3 production",
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG-level logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    target = args.date or _target_date_default()
    run_for_date(target, args.bronze_root, args.silver_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
