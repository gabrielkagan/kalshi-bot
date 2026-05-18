"""D3.0 — silver ETL pipeline regression tests (ticket 86b9zxc6t).

Plan doc: ``kb/decisions/d3-0-silver-foundations-plan.md``.

These tests exercise the silver ETL end-to-end against synthetic bronze
fixtures. They DO NOT hit live S3 or live AWS — fixtures emulate the
bronze chunk format (JSONL.zst with the D0.3 §2 envelope) on local disk;
the ETL is parameterized to read from a local fixtures root rather than
``s3://kalshi-bot-archive/bronze/...``.

The 9 tests defined here cover plan-doc test numbers 1-5 + 9-12:

  1. test_etl_processes_full_day_coinbase_ticker
  2. test_etl_idempotent_on_rerun  (also covers rerun-after-crash by determinism)
  3. test_etl_handles_missing_bronze_partition
  4. test_silver_schema_version_in_every_row
  5. test_kalshi_lifecycle_event_type_enum_permissive
  9. test_silver_status_arrays_preserved
  10. test_etl_skips_unrouted_bronze_partitions
  11. test_etl_skips_rest_snapshot_partitions
  12. test_kalshi_lifecycle_v2_suffix_preserved

Tests 6/7/8 are import-contract tests living at
``tests/contracts/test_silver_no_bot_imports.py``.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SILVER_DIR = REPO_ROOT / "silver"


# ─── Fixture helpers ────────────────────────────────────────────────────────


def _write_bronze_chunk(
    chunk_path: Path,
    source: str,
    channel: str,
    raws: list[dict],
    *,
    conn: str = "A",
    start_seq: int = 1,
    base_ts: datetime | None = None,
) -> None:
    """Write a bronze JSONL.zst chunk with the D0.3 §2 envelope.

    Each frame in ``raws`` becomes one envelope row. Compressed with zstd.
    """
    import zstandard as zstd

    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    if base_ts is None:
        base_ts = datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc)

    lines: list[bytes] = []
    for i, raw_obj in enumerate(raws):
        wire_recv_ts = (base_ts.replace(microsecond=(i % 1_000_000)))
        envelope = {
            "_wire_recv_ts": wire_recv_ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "_source": source,
            "_conn": conn,
            "_channel": channel,
            "_collector_seq": start_seq + i,
            "_raw": json.dumps(raw_obj, separators=(",", ":")),
        }
        lines.append((json.dumps(envelope, separators=(",", ":")) + "\n").encode("utf-8"))

    cctx = zstd.ZstdCompressor()
    chunk_path.write_bytes(cctx.compress(b"".join(lines)))


def _bronze_chunk_path(
    bronze_root: Path,
    source: str,
    channel: str,
    utc_date: str,
    hour: int = 14,
    *,
    conn: str = "A",
    start_seq: int = 1,
    end_seq: int | None = None,
) -> Path:
    """Compute the bronze chunk path per D0.3 §3 partition scheme.

    Example: ``bronze/coinbase_ws/ticker/year=2026/month=05/day=18/hour=14/conn=A/
              20260518T140000Z_to_20260518T140500Z_seq1-100.jsonl.zst``
    """
    y, m, d = utc_date.split("-")
    return (
        bronze_root / source / channel
        / f"year={y}" / f"month={m}" / f"day={d}" / f"hour={hour:02d}" / f"conn={conn}"
        / f"{y}{m}{d}T{hour:02d}0000Z_to_{y}{m}{d}T{hour:02d}0500Z_seq{start_seq}-{end_seq or start_seq + 99}.jsonl.zst"
    )


@pytest.fixture
def bronze_root(tmp_path: Path) -> Path:
    """Local bronze fixture root that emulates ``s3://kalshi-bot-archive/bronze/``."""
    root = tmp_path / "bronze"
    root.mkdir()
    return root


@pytest.fixture
def silver_root(tmp_path: Path) -> Path:
    """Local silver output root."""
    root = tmp_path / "silver" / "v1"
    root.mkdir(parents=True)
    return root


# ─── Import-the-ETL helper ──────────────────────────────────────────────────


def _import_etl_or_skip():
    """Import ``silver.scripts.etl_run`` or skip the test cleanly.

    During TDD-first RED phase, silver/ doesn't exist yet → all integration
    tests skip with a clear message. Once the implementation lands they
    will execute.

    Post-D3.0-fu1: ALSO checks that the ``dbt`` binary is reachable
    (either ``DBT_BIN`` env var or on PATH). The wrapper subprocess-
    launches dbt, so a missing binary would raise RuntimeError at the
    first ``run_for_date`` call. Skipping here gives a cleaner signal
    in environments without ``silver/requirements.txt`` installed
    (CI runners that haven't been extended; dev boxes that skipped
    the silver pip-install step). The CI gate that actually exercises
    these tests must install silver/requirements.txt — without it the
    tests skip silently.
    """
    try:
        from silver.scripts import etl_run  # type: ignore[import-not-found]
    except ImportError as exc:
        pytest.skip(f"silver.scripts.etl_run not importable (RED phase OK): {exc}")

    if not (os.environ.get("DBT_BIN") or shutil.which("dbt")):
        pytest.skip(
            "dbt binary not reachable (DBT_BIN unset + not on PATH). "
            "Install via `pip install -r silver/requirements.txt` to "
            "exercise the silver dbt-driven ETL integration tests."
        )
    return etl_run


# ─── Tests 1-5, 9-12 ────────────────────────────────────────────────────────


def test_etl_processes_full_day_coinbase_ticker(bronze_root: Path, silver_root: Path):
    """Test #1 — 3 bronze ticker chunks across different hours → silver Parquet
    with row count = sum of bronze frame counts.
    """
    etl = _import_etl_or_skip()
    raws = [
        {"type": "ticker", "sequence": 1000 + i, "product_id": "ETH-USD",
         "price": "2124.39", "best_bid": "2124.38", "best_bid_size": "1.0",
         "best_ask": "2124.41", "best_ask_size": "0.5", "side": "buy",
         "time": "2026-05-18T14:03:13.335970Z", "trade_id": 800000000 + i,
         "last_size": "0.5", "open_24h": "2100", "volume_24h": "1000",
         "low_24h": "2050", "high_24h": "2200", "volume_30d": "30000"}
        for i in range(5)
    ]
    for hour in (12, 13, 14):
        _write_bronze_chunk(
            _bronze_chunk_path(bronze_root, "coinbase_ws", "ticker", "2026-05-18", hour=hour),
            "coinbase_ws", "ticker", raws,
            start_seq=1 + hour * 100,
        )

    etl.run_for_date(
        target_date="2026-05-18",
        bronze_root=str(bronze_root),
        silver_root=str(silver_root),
    )

    import duckdb
    parquet_glob = str(silver_root / "coinbase_ticker" / "utc_date=2026-05-18" / "*.parquet")
    rows = duckdb.sql(f"SELECT COUNT(*) AS c FROM read_parquet('{parquet_glob}')").fetchone()
    assert rows[0] == 15, f"expected 15 silver rows (3 chunks × 5 frames), got {rows[0]}"


def test_etl_idempotent_on_rerun(bronze_root: Path, silver_root: Path):
    """Test #2 — running the ETL twice for the same date produces row-wise
    identical silver Parquet (`delete+insert` rewrites the partition wholesale).

    Also covers rerun-after-crash recovery BY ARGUMENT (R4-M2):
    the `delete+insert` semantics mean any partial state from a mid-run
    crash is wiped by the next run's DELETE phase before INSERT.
    """
    etl = _import_etl_or_skip()
    raws = [
        {"type": "ticker", "sequence": 1, "product_id": "BTC-USD", "price": "60000",
         "best_bid": "60000", "best_bid_size": "1", "best_ask": "60001",
         "best_ask_size": "1", "side": "buy",
         "time": "2026-05-18T14:00:00Z", "trade_id": 1, "last_size": "1",
         "open_24h": "60000", "volume_24h": "100", "low_24h": "59000",
         "high_24h": "61000", "volume_30d": "30000"},
    ]
    _write_bronze_chunk(
        _bronze_chunk_path(bronze_root, "coinbase_ws", "ticker", "2026-05-18"),
        "coinbase_ws", "ticker", raws,
    )

    etl.run_for_date("2026-05-18", str(bronze_root), str(silver_root))
    import duckdb
    parquet_glob = str(silver_root / "coinbase_ticker" / "utc_date=2026-05-18" / "*.parquet")
    first = duckdb.sql(
        f"SELECT * FROM read_parquet('{parquet_glob}') ORDER BY collector_seq"
    ).fetchall()

    etl.run_for_date("2026-05-18", str(bronze_root), str(silver_root))
    second = duckdb.sql(
        f"SELECT * FROM read_parquet('{parquet_glob}') ORDER BY collector_seq"
    ).fetchall()

    assert first == second, (
        f"silver ETL is NOT idempotent — rerun produced different rows.\n"
        f"first run: {first}\nsecond run: {second}"
    )


def test_etl_handles_missing_bronze_partition(bronze_root: Path, silver_root: Path):
    """Test #3 — bronze partition empty for a date → ETL skips with logged
    warning, does NOT crash, does NOT write empty silver Parquet."""
    etl = _import_etl_or_skip()
    etl.run_for_date(
        target_date="2026-05-18",
        bronze_root=str(bronze_root),
        silver_root=str(silver_root),
    )
    # No silver output should land — bronze was empty.
    silver_files = list(silver_root.rglob("*.parquet"))
    assert not silver_files, (
        f"silver ETL wrote Parquet despite empty bronze: {silver_files}. "
        "Expected SKIP semantics (decision #3 in plan doc test #3)."
    )


def test_silver_schema_version_in_every_row(bronze_root: Path, silver_root: Path):
    """Test #4 — every silver row has non-null `silver_schema_version`
    matching the model name + version (e.g. "coinbase_ticker_v1")."""
    etl = _import_etl_or_skip()
    raws = [
        {"type": "ticker", "sequence": 1, "product_id": "BTC-USD", "price": "60000",
         "best_bid": "60000", "best_bid_size": "1", "best_ask": "60001",
         "best_ask_size": "1", "side": "buy", "time": "2026-05-18T14:00:00Z",
         "trade_id": 1, "last_size": "1", "open_24h": "60000", "volume_24h": "100",
         "low_24h": "59000", "high_24h": "61000", "volume_30d": "30000"},
    ]
    _write_bronze_chunk(
        _bronze_chunk_path(bronze_root, "coinbase_ws", "ticker", "2026-05-18"),
        "coinbase_ws", "ticker", raws,
    )
    etl.run_for_date("2026-05-18", str(bronze_root), str(silver_root))

    import duckdb
    parquet_glob = str(silver_root / "coinbase_ticker" / "utc_date=2026-05-18" / "*.parquet")
    versions = duckdb.sql(
        f"SELECT DISTINCT silver_schema_version FROM read_parquet('{parquet_glob}')"
    ).fetchall()
    assert versions == [("coinbase_ticker_v1",)], (
        f"silver_schema_version drift: {versions}. "
        "Every silver_coinbase_ticker_v1 row should have "
        "silver_schema_version='coinbase_ticker_v1'."
    )


def test_kalshi_lifecycle_event_type_enum_permissive(bronze_root: Path, silver_root: Path):
    """Test #5 — known event_types (`metadata_updated`, `determined`) pass;
    unknown event_type routes to QA-detection log (not crash, not silent drop).
    """
    etl = _import_etl_or_skip()
    raws = [
        {"type": "market_lifecycle_v2", "sid": 4, "seq": 1,
         "msg": {"event_type": "metadata_updated",
                 "market_ticker": "KXBTC15M-26MAY172015-15",
                 "floor_strike": 77414.96, "yes_sub_title": "T"}},
        {"type": "market_lifecycle_v2", "sid": 4, "seq": 2,
         "msg": {"event_type": "determined",
                 "market_ticker": "KXBTC15M-26MAY172015-15",
                 "determination_ts": 1779062402, "result": "yes",
                 "settlement_value": "1.0000"}},
        # Unknown event_type — should NOT fail run
        {"type": "market_lifecycle_v2", "sid": 4, "seq": 3,
         "msg": {"event_type": "future_unknown_event",
                 "market_ticker": "KXBTC15M-26MAY172015-15"}},
    ]
    _write_bronze_chunk(
        _bronze_chunk_path(bronze_root, "kalshi_ws", "market_lifecycle_v2", "2026-05-18"),
        "kalshi_ws", "market_lifecycle_v2", raws,
    )
    # Should NOT raise on unknown event_type
    etl.run_for_date("2026-05-18", str(bronze_root), str(silver_root))

    import duckdb
    parquet_glob = str(
        silver_root / "kalshi_market_lifecycle_v2"
        / "utc_date=2026-05-18" / "*.parquet"
    )
    rows = duckdb.sql(
        f"SELECT DISTINCT event_type FROM read_parquet('{parquet_glob}') ORDER BY event_type"
    ).fetchall()
    event_types = {r[0] for r in rows}
    assert {"metadata_updated", "determined"} <= event_types, (
        f"known event_types missing from silver: {event_types}"
    )
    # Unknown event_type SHOULD be present (permissive enum); enforcement is
    # at the QA model level (D3.4), not at ETL parse time.
    assert "future_unknown_event" in event_types, (
        f"unknown event_type silently dropped — permissive enum violated. "
        f"got: {event_types}"
    )


def test_silver_status_arrays_preserved(bronze_root: Path, silver_root: Path):
    """Test #9 — `coinbase_status` silver row has `currencies_json` +
    `products_json` as valid JSON strings; `currency_count` + `product_count`
    are NEVER NULL (COALESCE-to-0).
    """
    etl = _import_etl_or_skip()
    raws = [
        {"type": "status",
         "currencies": [{"id": "BTC", "name": "Bitcoin"}, {"id": "ETH", "name": "Ethereum"}],
         "products": [{"id": "BTC-USD"}, {"id": "ETH-USD"}, {"id": "SOL-USD"}]},
        # Missing arrays — currency_count / product_count must COALESCE to 0
        {"type": "status"},
    ]
    _write_bronze_chunk(
        _bronze_chunk_path(bronze_root, "coinbase_ws", "status", "2026-05-18"),
        "coinbase_ws", "status", raws,
    )
    etl.run_for_date("2026-05-18", str(bronze_root), str(silver_root))

    import duckdb
    parquet_glob = str(silver_root / "coinbase_status" / "utc_date=2026-05-18" / "*.parquet")
    rows = duckdb.sql(
        f"SELECT currency_count, product_count FROM read_parquet('{parquet_glob}') "
        f"ORDER BY collector_seq"
    ).fetchall()
    assert rows[0] == (2, 3), f"first row counts wrong: {rows[0]}"
    assert rows[1] == (0, 0), (
        f"second row should COALESCE missing arrays to 0, got: {rows[1]}. "
        "currency_count/product_count must NEVER be NULL per plan-doc M6."
    )


def test_etl_skips_unrouted_bronze_partitions(bronze_root: Path, silver_root: Path):
    """Test #10 — bronze includes `_unrouted/` partition (collector routing
    edge case) → silver ETL does NOT crash + does NOT silently merge
    unrouted frames into known channels.
    """
    etl = _import_etl_or_skip()
    # Write a real ticker chunk + an _unrouted chunk
    ticker_raws = [
        {"type": "ticker", "sequence": 1, "product_id": "BTC-USD", "price": "60000",
         "best_bid": "60000", "best_bid_size": "1", "best_ask": "60001",
         "best_ask_size": "1", "side": "buy", "time": "2026-05-18T14:00:00Z",
         "trade_id": 1, "last_size": "1", "open_24h": "60000", "volume_24h": "100",
         "low_24h": "59000", "high_24h": "61000", "volume_30d": "30000"},
    ]
    _write_bronze_chunk(
        _bronze_chunk_path(bronze_root, "coinbase_ws", "ticker", "2026-05-18"),
        "coinbase_ws", "ticker", ticker_raws,
    )
    # _unrouted: arbitrary unmapped payload
    _write_bronze_chunk(
        _bronze_chunk_path(bronze_root, "kalshi_ws", "_unrouted", "2026-05-18"),
        "kalshi_ws", "_unrouted",
        [{"type": "unknown_kalshi_msg", "payload": "..."}],
    )
    # Should NOT raise
    etl.run_for_date("2026-05-18", str(bronze_root), str(silver_root))

    # ticker silver should have only the 1 ticker row (no _unrouted merge)
    import duckdb
    ticker_glob = str(silver_root / "coinbase_ticker" / "utc_date=2026-05-18" / "*.parquet")
    rows = duckdb.sql(f"SELECT COUNT(*) FROM read_parquet('{ticker_glob}')").fetchone()
    assert rows[0] == 1, (
        f"expected 1 ticker row, got {rows[0]} — possible _unrouted merge"
    )
    # No silver _unrouted output dir
    unrouted_dir = silver_root / "_unrouted"
    assert not unrouted_dir.exists(), (
        f"unexpected silver/_unrouted dir: {unrouted_dir}. "
        "D3.0 defers _unrouted provenance to D3.6 — silver should skip."
    )


def test_etl_skips_rest_snapshot_partitions(bronze_root: Path, silver_root: Path):
    """Test #11 — bronze includes a REST-snapshot partition (`_source` not in
    {"kalshi_ws", "coinbase_ws"}) → silver ETL skips with logged info, does
    NOT attempt to parse.
    """
    etl = _import_etl_or_skip()
    # Bronze REST-snapshot partition (events_snapshot per D0.3 §1 example)
    rest_raws = [
        {"type": "rest_response", "endpoint": "/markets", "data": [{"ticker": "X"}]},
    ]
    _write_bronze_chunk(
        _bronze_chunk_path(bronze_root, "kalshi_rest", "events_snapshot", "2026-05-18"),
        "kalshi_rest", "events_snapshot", rest_raws,
    )
    # Should NOT raise
    etl.run_for_date("2026-05-18", str(bronze_root), str(silver_root))

    # No silver kalshi_rest output dir
    rest_dirs = list(silver_root.glob("kalshi_rest*"))
    assert not rest_dirs, (
        f"unexpected silver dir for REST source: {rest_dirs}. "
        "D3.0 silver scope is WS sources only (per plan-doc out-of-scope §)."
    )


def test_kalshi_lifecycle_v2_suffix_preserved(bronze_root: Path, silver_root: Path):
    """Test #12 — silver Parquet output path for Kalshi market_lifecycle
    includes `kalshi_market_lifecycle_v2/` (NOT bare `kalshi_market_lifecycle/`).

    R1-C1: the bronze channel name is `market_lifecycle_v2` (D0.3 §2:99 —
    a v2-rename of an upstream channel). Silver must preserve the `_v2`
    suffix so a future `market_lifecycle_v3` rename produces a parallel
    silver path, NOT an in-place clobber.
    """
    etl = _import_etl_or_skip()
    raws = [
        {"type": "market_lifecycle_v2", "sid": 4, "seq": 1,
         "msg": {"event_type": "metadata_updated",
                 "market_ticker": "KXBTC15M-26MAY172015-15",
                 "floor_strike": 77414.96, "yes_sub_title": "T"}},
    ]
    _write_bronze_chunk(
        _bronze_chunk_path(bronze_root, "kalshi_ws", "market_lifecycle_v2", "2026-05-18"),
        "kalshi_ws", "market_lifecycle_v2", raws,
    )
    etl.run_for_date("2026-05-18", str(bronze_root), str(silver_root))

    v2_dir = silver_root / "kalshi_market_lifecycle_v2"
    bare_dir = silver_root / "kalshi_market_lifecycle"
    assert v2_dir.is_dir(), (
        f"expected silver dir '{v2_dir}' with _v2 suffix, not found. "
        "Bronze channel is market_lifecycle_v2; silver must preserve "
        "the channel-rename suffix."
    )
    assert not bare_dir.exists(), (
        f"unexpected silver dir '{bare_dir}' without _v2 suffix. "
        "R1-C1: silver source name must match bronze channel "
        "`market_lifecycle_v2`, not the pre-rename `market_lifecycle`."
    )
