"""D1.2 — collector rotation contract (ticket 86b9ypn66, 2026-05-16).

Pins the §4 rotation rule: 5 minutes OR 100 MB whichever first, per
(source, channel, conn) tuple independently.

Sister to ``test_collector_writer.py`` (envelope) + ``test_collector_uploader.py``
(KEEP-local) + ``test_collector_idempotency.py`` (atomic rename).

If this test fails:
- Rotation cadence change requires updating D0.3 §4 + this test atomically.
  Read §4 "Per-hour chunk count projection" before tuning either trigger.
- The OR (not AND) semantics is load-bearing per §4 last bullet ("peak
  intervals can plausibly drive 3-5× the byte rate; fixed time = bloated
  peak chunks, fixed size = sparse off-peak chunks").
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from collector.writer import BronzeWriter


# ─── Rotation thresholds (per D0.3 §4) ───────────────────────────────────────


def test_rotation_constants_match_d0_3_spec():
    """The two threshold constants in collector/writer.py match D0.3 §4:
    300 seconds (5 minutes) AND 100 * 1024 * 1024 bytes (100 MB).

    Constants live in the writer module; if a future tune happens, this
    test fails and the operator is forced to update D0.3 §4 in the
    same commit (the doc is the bronze tape spec).
    """
    from collector import writer as W

    assert W.ROTATION_INTERVAL_SECONDS == 300, (
        f"ROTATION_INTERVAL_SECONDS = {W.ROTATION_INTERVAL_SECONDS} != 300 "
        f"(5 minutes). D0.3 §4 locks this; update doc + constant + this "
        f"test atomically."
    )
    assert W.ROTATION_SIZE_BYTES == 100 * 1024 * 1024, (
        f"ROTATION_SIZE_BYTES = {W.ROTATION_SIZE_BYTES} != "
        f"{100 * 1024 * 1024} (100 MB). D0.3 §4 locks this; update doc + "
        f"constant + this test atomically."
    )


# ─── 5-minute timer trigger ──────────────────────────────────────────────────


def test_rotation_fires_on_5_minute_timer(tmp_path: Path):
    """A frame written at t+5min after the in-flight file opened triggers
    rotation, regardless of file size.

    The writer accepts an injectable wall-clock for deterministic testing
    (the production caller passes ``time.time`` or a wrapped clock).
    Without the injection seam, this test would have to sleep 300s.
    """
    fake_now = [1_716_000_000.0]  # wall-clock seconds; manipulated by closure

    def clock():
        return fake_now[0]

    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
        now_fn=clock,
    )
    # First frame — opens in-flight file at fake_now[0].
    writer.write_frame(
        wire_recv_ts=datetime.fromtimestamp(fake_now[0], tz=timezone.utc),
        raw_payload='{"i":1}',
    )
    in_flights_pre = list(tmp_path.rglob("*.jsonl"))
    assert len(in_flights_pre) == 1, "expected 1 in-flight file after first write"

    # Advance time by 4:59 — no rotation yet.
    fake_now[0] += 299.0
    writer.write_frame(
        wire_recv_ts=datetime.fromtimestamp(fake_now[0], tz=timezone.utc),
        raw_payload='{"i":2}',
    )
    in_flights_mid = list(tmp_path.rglob("*.jsonl"))
    assert len(in_flights_mid) == 1, (
        f"rotation fired prematurely at t+4:59 — D0.3 §4 specifies "
        f"5 minutes, not 4:59. Files: {in_flights_mid}"
    )

    # Advance to t+5:00 — next write_frame triggers rotation.
    fake_now[0] += 1.0  # now at t+5:00 from open
    writer.write_frame(
        wire_recv_ts=datetime.fromtimestamp(fake_now[0], tz=timezone.utc),
        raw_payload='{"i":3}',
    )
    # After rotation, there should be at least one CLOSED file
    # (zstd-compressed in tmp/ or outbox/, depending on uploader contract)
    # and a fresh in-flight .jsonl.
    files_post = list(tmp_path.rglob("*"))
    closed = [p for p in files_post if p.is_file() and p.name.endswith(".jsonl.zst")]
    in_flights_post = [p for p in files_post if p.is_file() and p.suffix == ".jsonl"]
    assert closed, (
        f"rotation at t+5:00 did not produce a .jsonl.zst closed chunk.\n"
        f"files: {files_post}"
    )
    assert in_flights_post, (
        f"rotation did not open a new in-flight .jsonl for the post-rotation "
        f"frame.\nfiles: {files_post}"
    )


# ─── 100-MB size trigger ─────────────────────────────────────────────────────


def test_rotation_fires_on_100mb_size_trigger(tmp_path: Path):
    """A frame whose append crosses the 100 MB uncompressed cap triggers
    rotation before the 5-minute timer.

    Uses a smaller cap via the ``size_threshold_bytes`` injectable to keep
    the test fast (writing 100 MB of JSON would take ~1s + a lot of fixture
    bytes). The constant-from-spec is pinned separately by
    ``test_rotation_constants_match_d0_3_spec``; this test pins the
    triggering BEHAVIOR around whatever the cap is.
    """
    SMALL_CAP = 4_096  # 4KB — easy to overflow with a handful of frames
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
        size_threshold_bytes=SMALL_CAP,
    )
    big_payload = "x" * 1_000  # ~1KB per frame after envelope wrapping
    base_ts = datetime(2026, 5, 16, 12, 0, 0, 1, tzinfo=timezone.utc)
    n_writes = 0
    while True:
        n_writes += 1
        writer.write_frame(
            wire_recv_ts=base_ts + timedelta(microseconds=n_writes),
            raw_payload=big_payload,
        )
        closed = list(tmp_path.rglob("*.jsonl.zst"))
        if closed:
            break
        assert n_writes < 50, (
            f"size-trigger did not fire after {n_writes} writes of "
            f"~1KB each (cap = {SMALL_CAP}B). Trigger likely broken."
        )
    assert closed, "no .jsonl.zst chunk produced"
    # An in-flight .jsonl should exist for the most recent frame.
    in_flights = list(tmp_path.rglob("*.jsonl"))
    assert in_flights, "no fresh in-flight after rotation"


# ─── OR-semantics (whichever fires first) ────────────────────────────────────


def test_rotation_is_OR_not_AND(tmp_path: Path):
    """Either trigger alone fires rotation — not both required.

    Mutation defense: if the implementation accidentally ANDs the two
    conditions (waits for BOTH 5min AND 100MB), peak intervals would
    produce huge multi-hundred-MB chunks (silver compaction unhappy)
    and slow channels would never rotate (sparse-stream data loss
    window expands beyond 5min).

    The test verifies size-only trigger fires WITHOUT advancing the clock,
    and (separately, via the timer test above) time-only fires WITHOUT
    crossing the size cap. If implementation ANDs, this size-only test
    would loop forever past the assert-len check.
    """
    SMALL_CAP = 2_048
    fake_now = [1_716_000_000.0]
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
        size_threshold_bytes=SMALL_CAP,
        now_fn=lambda: fake_now[0],
    )
    base_ts = datetime(2026, 5, 16, 12, 0, 0, 1, tzinfo=timezone.utc)
    big = "y" * 800
    # Write enough to cross the size cap but DO NOT advance the clock.
    # If the implementation requires BOTH triggers, no rotation will fire.
    for i in range(10):
        writer.write_frame(
            wire_recv_ts=base_ts + timedelta(microseconds=i),
            raw_payload=big,
        )
    closed = list(tmp_path.rglob("*.jsonl.zst"))
    assert closed, (
        "size trigger alone did not fire rotation — implementation may "
        "have AND'd the triggers (5min AND 100MB) instead of OR'd. "
        "D0.3 §4 specifies OR (whichever first)."
    )


# ─── Per-(source, channel, conn) isolation ──────────────────────────────────


def test_rotation_per_conn_channel_isolated(tmp_path: Path):
    """Two separate writers (different channel) rotate independently.

    The in-flight file is keyed by (source, channel, conn) — a rotation
    on one tuple must not flush or otherwise touch another tuple's
    in-flight state.

    This tests the data-structure invariant; the production rotation-
    handler dispatch is exercised by the integration smoke.
    """
    writer_ob = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
        size_threshold_bytes=2_048,
    )
    writer_trade = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="trade",
        conn="A",
        size_threshold_bytes=2_048,
    )
    base_ts = datetime(2026, 5, 16, 12, 0, 0, 1, tzinfo=timezone.utc)
    # Drive writer_ob past its size cap; writer_trade gets only one small frame.
    big = "z" * 800
    for i in range(10):
        writer_ob.write_frame(
            wire_recv_ts=base_ts + timedelta(microseconds=i),
            raw_payload=big,
        )
    writer_trade.write_frame(
        wire_recv_ts=base_ts + timedelta(microseconds=100),
        raw_payload='{"i":"trade"}',
    )
    # writer_ob should have rotated; writer_trade should NOT have rotated.
    ob_closed = [p for p in tmp_path.rglob("*.jsonl.zst") if "orderbook_delta" in p.as_posix()]
    trade_closed = [p for p in tmp_path.rglob("*.jsonl.zst") if "channel=trade" in p.as_posix() or "/trade/" in p.as_posix()]
    assert ob_closed, (
        f"writer_ob (orderbook_delta) should have rotated past 2KB cap; "
        f"found no closed chunks under orderbook_delta. all files: "
        f"{list(tmp_path.rglob('*'))}"
    )
    assert not trade_closed, (
        f"writer_trade (trade) rotation fired but it only wrote ~30 bytes. "
        f"per-(source,channel,conn) isolation broken. closed trade chunks: "
        f"{trade_closed}"
    )


# ─── Chunk-id construction (D0.3 §3) ─────────────────────────────────────────


def test_chunk_id_includes_first_last_ts_and_seq_range(tmp_path: Path):
    """Chunk filename embeds start/end ``_wire_recv_ts`` and seq range
    per D0.3 §3:
    ``<start_iso_compact>_to_<end_iso_compact>_seq<start>-<end>.jsonl.zst``

    Start/end are FIRST and LAST `_wire_recv_ts` in the chunk (NOT the
    rotation wall-clock — those diverge during slow zstd flushes per §3).
    """
    SMALL_CAP = 2_048
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
        size_threshold_bytes=SMALL_CAP,
    )
    first_ts = datetime(2026, 5, 16, 14, 0, 0, 0, tzinfo=timezone.utc)
    last_ts = first_ts + timedelta(seconds=30)
    big = "p" * 800
    writer.write_frame(wire_recv_ts=first_ts, raw_payload=big)
    for i in range(8):
        writer.write_frame(
            wire_recv_ts=first_ts + timedelta(seconds=i + 1),
            raw_payload=big,
        )
    writer.write_frame(wire_recv_ts=last_ts, raw_payload=big)
    # rglob() order is undefined (APFS scandir != alphabetic); sort by
    # name so closed_files[0] is the chunk whose chunk_id starts with
    # the first frame ts.
    closed_files = sorted(tmp_path.rglob("*.jsonl.zst"), key=lambda p: p.name)
    assert closed_files, "no chunk produced"
    # Filename format: <compact_iso_start>_to_<compact_iso_end>_seq<a>-<b>.jsonl.zst
    # Compact ISO = YYYYMMDDTHHMMSSZ (per §3 example)
    name = closed_files[0].name
    import re as _re
    m = _re.match(
        r"(\d{8}T\d{6}Z)_to_(\d{8}T\d{6}Z)_seq(\d+)-(\d+)\.jsonl\.zst$",
        name,
    )
    assert m, (
        f"chunk filename {name!r} does not match D0.3 §3 format "
        f"`<compact_iso_start>_to_<compact_iso_end>_seq<a>-<b>.jsonl.zst`."
    )
    start, end, seq_start, seq_end = m.groups()
    assert start == "20260516T140000Z", (
        f"start ISO = {start!r}, expected 20260516T140000Z (first frame ts)"
    )
    assert int(seq_end) > int(seq_start), (
        f"seq range start={seq_start}, end={seq_end} — should strictly increase"
    )


def test_chunk_partition_path_is_hive_style(tmp_path: Path):
    """Chunk lands under
    ``<root>/<source>/<channel>/year=YYYY/month=MM/day=DD/hour=HH/conn=<X>/<chunk>.jsonl.zst``
    per D0.3 §3.

    Hive-style key=value partitioning is auto-detected by DuckDB / Athena /
    Spark / dbt-duckdb without configuration — silver ETL gets partition
    pushdown for free.
    """
    SMALL_CAP = 2_048
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
        size_threshold_bytes=SMALL_CAP,
    )
    ts = datetime(2026, 5, 20, 14, 0, 0, 0, tzinfo=timezone.utc)
    for i in range(10):
        writer.write_frame(
            wire_recv_ts=ts + timedelta(seconds=i),
            raw_payload="q" * 800,
        )
    closed = list(tmp_path.rglob("*.jsonl.zst"))
    assert closed
    p = closed[0]
    rel = p.relative_to(tmp_path).as_posix()
    # Expected: kalshi_ws/orderbook_delta/year=2026/month=05/day=20/hour=14/conn=A/<chunk>.jsonl.zst
    expected_prefix = "kalshi_ws/orderbook_delta/year=2026/month=05/day=20/hour=14/conn=A/"
    assert rel.startswith(expected_prefix), (
        f"chunk landed at {rel!r}, expected prefix {expected_prefix!r}\n"
        f"D0.3 §3 partition scheme: source/channel/year=/month=/day=/hour=/conn=/<chunk>"
    )
