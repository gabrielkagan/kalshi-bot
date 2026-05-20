"""D1.2 — collector/writer.py envelope + capture-discipline contract (ticket 86b9ypn66, 2026-05-16).

Pins the IRREVERSIBLE bronze envelope per kb/decisions/data-corpus-architecture.md
§2 — the 6-field shape locks the on-disk JSONL schema for the lifetime of
the corpus. Once a chunk is written + uploaded, the envelope can't be revved.

Sister to ``tests/contracts/test_collector_rotation.py`` (5min OR 100MB) +
``tests/contracts/test_collector_uploader.py`` (rclone KEEP-local) +
``tests/contracts/test_collector_idempotency.py`` (atomic rename + restart
re-uploads).

If this test fails:
- Adding a NEW envelope key is BACKWARD-COMPATIBLE per §2 ("any silver
  consumer that doesn't recognize the new field ignores it") — extend
  EXPECTED_FIELDS + the ordered-key check.
- REMOVING or RENAMING an envelope key is a forced-rev; out of scope.
  Read §2 "Irreversibility" before touching this test.
- Changing the timestamp format is a forced-rev (silver QA reads
  `_wire_recv_ts` directly via DuckDB ISO-8601 parsing).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import zstandard as zstd

from collector.writer import BronzeWriter


def _read_envelope_lines(tmp_path: Path) -> list[str]:
    """Helper: decompress all .jsonl.zst chunks under tmp_path and concat.

    After writer.close() forces a rotation, the envelope-wrapped lines
    live inside a zstd-compressed chunk in outbox/. This helper reads
    them back for envelope-shape assertions.
    """
    dctx = zstd.ZstdDecompressor()
    lines: list[str] = []
    for p in sorted(tmp_path.rglob("*.jsonl.zst")):
        with p.open("rb") as f:
            decompressed = dctx.decompress(f.read()).decode("utf-8")
        lines.extend(line for line in decompressed.splitlines() if line.strip())
    return lines

# Per D0.3 §2 field contract — these 6 fields in this exact order
# (JSON object key ordering is preserved in Python 3.7+ dicts and json.dumps
# preserves dict iteration order).
EXPECTED_FIELDS = (
    "_wire_recv_ts",
    "_source",
    "_conn",
    "_channel",
    "_collector_seq",
    "_raw",
)

# ISO-8601 UTC with microsecond precision: "2026-05-15T18:35:12.034501Z"
# Z suffix (not +00:00) per D0.3 §2 example. μs precision (6 fractional digits).
ISO_UTC_US_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


# ─── 1. Envelope shape ───────────────────────────────────────────────────────


def test_writer_envelope_has_exactly_six_fields(tmp_path: Path):
    """The bronze envelope contains EXACTLY the 6 fields in §2 — no more, no less.

    Adding a field is backward-compatible (silver ignores unknown keys);
    this test fails on add so the addition is deliberate + sister-doc
    updated in the same Bit. Removing a field is a forced-rev — read
    §2 before deleting.
    """
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
    )
    writer.write_frame(
        wire_recv_ts=datetime(2026, 5, 16, 12, 0, 0, 123456, tzinfo=timezone.utc),
        raw_payload='{"type":"orderbook_delta","msg":{"market_ticker":"KXBTCD-T1"}}',
    )
    writer.close()

    lines = _read_envelope_lines(tmp_path)
    assert lines, f"no envelope lines found after close()"
    record = json.loads(lines[0])
    assert set(record.keys()) == set(EXPECTED_FIELDS), (
        f"envelope has wrong keys.\n  expected: {sorted(EXPECTED_FIELDS)}\n"
        f"  actual:   {sorted(record.keys())}\n"
        f"D0.3 §2 locks the 6-field envelope as IRREVERSIBLE bronze schema."
    )


def test_writer_envelope_preserves_field_order(tmp_path: Path):
    """JSON key order matches §2: _wire_recv_ts, _source, _conn, _channel,
    _collector_seq, _raw.

    Order matters because silver/gold consumers may use ordered-dict-aware
    parsers (e.g., for byte-stable hashing of envelope-only metadata). Python
    3.7+ dict iteration order is the contract; ``json.dumps`` preserves it.
    """
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="trade",
        conn="B",
    )
    writer.write_frame(
        wire_recv_ts=datetime(2026, 5, 16, 12, 0, 0, 1, tzinfo=timezone.utc),
        raw_payload='{"type":"trade"}',
    )
    writer.close()

    lines = _read_envelope_lines(tmp_path)
    assert lines
    line = lines[0]
    # Parse with object_pairs_hook to preserve order from the raw bytes.
    pairs = json.loads(line, object_pairs_hook=list)
    actual_order = tuple(k for k, _ in pairs)
    assert actual_order == EXPECTED_FIELDS, (
        f"envelope key order is wrong.\n  expected: {EXPECTED_FIELDS}\n"
        f"  actual:   {actual_order}\n"
        f"D0.3 §2 example pins the order. Use an ordered dict construction "
        f"(record = {{k: v for k,v in [...]}} in §2 order, or a manual "
        f"OrderedDict)."
    )


# ─── 2. _wire_recv_ts format + UTC precision ─────────────────────────────────


def test_wire_recv_ts_is_iso_utc_with_microsecond_precision(tmp_path: Path):
    """``_wire_recv_ts`` matches the §2 example: ISO-8601 UTC with 6
    fractional digits (microseconds), Z suffix.

    Silver QA (D2.x) reads this directly via DuckDB's ISO-8601 timestamp
    parser. Changing the format would force a coordinated rev across every
    downstream consumer.

    Regex (anchored): ``^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}\\.\\d{6}Z$``
    """
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
    )
    writer.write_frame(
        wire_recv_ts=datetime(2026, 5, 15, 18, 35, 12, 34501, tzinfo=timezone.utc),
        raw_payload="{}",
    )
    writer.close()

    lines = _read_envelope_lines(tmp_path)
    record = json.loads(lines[0])
    ts = record["_wire_recv_ts"]
    assert ISO_UTC_US_RE.match(ts), (
        f"_wire_recv_ts={ts!r} does not match D0.3 §2 format "
        f"(ISO-8601 UTC, 6 fractional digits, Z suffix). Example from §2: "
        f"\"2026-05-15T18:35:12.034501Z\"."
    )


def test_wire_recv_ts_rejects_naive_datetime(tmp_path: Path):
    """write_frame() must reject naive datetimes (no tzinfo).

    A naive datetime cannot be unambiguously serialized as UTC — encoding
    it as if-UTC would silently corrupt the timestamp for any frame
    captured during DST transitions or in a non-UTC environment. D0.3
    §2 implies wire-ingress capture; the surrounding event loop must
    pass tz-aware UTC. The writer is the last defense.
    """
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
    )
    with pytest.raises((ValueError, TypeError, AssertionError)):
        writer.write_frame(
            wire_recv_ts=datetime(2026, 5, 15, 18, 35, 12),  # naive — no tzinfo
            raw_payload="{}",
        )


# ─── 3. _raw is literal payload string (capture-before-deserialize) ──────────


def test_raw_field_is_literal_string_not_parsed(tmp_path: Path):
    """``_raw`` is the wire payload verbatim, NOT a parsed dict.

    D0.3 §2 "Why include _raw as a string and not parse it": "if we parse
    and re-emit the structured fields, bronze becomes 'what we thought
    the wire said' not 'what the wire actually said.'"

    The capture must happen BEFORE deserialization — the writer never
    calls json.loads(raw_payload). Test passes a raw string containing
    structural-ish JSON, asserts the bronze line round-trips it as a
    STRING (not as a nested object).
    """
    raw = '{"type":"orderbook_delta","msg":{"market_ticker":"KXBTCD-26MAY16-T123","yes":[[50,1234]]}}'
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
    )
    writer.write_frame(
        wire_recv_ts=datetime(2026, 5, 16, 12, 0, 0, 1, tzinfo=timezone.utc),
        raw_payload=raw,
    )
    writer.close()

    lines = _read_envelope_lines(tmp_path)
    record = json.loads(lines[0])
    assert isinstance(record["_raw"], str), (
        f"_raw is type {type(record['_raw']).__name__}; D0.3 §2 says STRING. "
        "Parsing the payload would let bronze drift from 'what the wire "
        "actually said.'"
    )
    assert record["_raw"] == raw, (
        f"_raw round-trip mismatch.\n  in:  {raw!r}\n  out: {record['_raw']!r}\n"
        "Bronze must preserve the wire payload byte-for-byte (modulo "
        "string-encoding)."
    )


def test_raw_payload_with_unicode_preserved(tmp_path: Path):
    """Non-ASCII characters in the payload survive verbatim.

    Kalshi payloads include market titles that may contain unicode
    (e.g., en-dashes in event titles). The bronze writer must not
    coerce or escape these in a way that breaks round-trip.
    """
    raw = '{"title":"BTC – above $50,000 by 1pm"}'
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="market_lifecycle_v2",
        conn="C",
    )
    writer.write_frame(
        wire_recv_ts=datetime(2026, 5, 16, 12, 0, 0, 1, tzinfo=timezone.utc),
        raw_payload=raw,
    )
    writer.close()

    lines = _read_envelope_lines(tmp_path)
    record = json.loads(lines[0])
    assert record["_raw"] == raw


# ─── 4. _collector_seq monotone-increasing ───────────────────────────────────


def test_collector_seq_monotone_within_run(tmp_path: Path):
    """``_collector_seq`` increases monotonically across writes within
    a single writer lifecycle.

    Per D0.3 §2: "Monotone-increasing per-collector-process sequence
    number from boot, used by silver QA (D2.4) to detect gaps independent
    of `_wire_recv_ts`. Resets on collector restart."

    Cross-restart reset is out of scope here (covered by
    test_collector_idempotency); this pins the within-run monotonicity.
    """
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
    )
    base_ts = datetime(2026, 5, 16, 12, 0, 0, 1, tzinfo=timezone.utc)
    for i in range(5):
        writer.write_frame(
            wire_recv_ts=base_ts.replace(microsecond=base_ts.microsecond + i),
            raw_payload=f'{{"i":{i}}}',
        )
    writer.close()

    lines = next(tmp_path.rglob("*.jsonl")).read_text().splitlines()
    seqs = [json.loads(line)["_collector_seq"] for line in lines]
    assert seqs == sorted(seqs), (
        f"_collector_seq is not monotone-increasing: {seqs}. "
        f"Silver QA (D2.4) requires monotonicity within a run."
    )
    # Strict-increasing — no two frames share a seq.
    assert len(set(seqs)) == len(seqs), (
        f"_collector_seq has duplicates: {seqs}. Sequence is per-frame, "
        f"strictly increasing."
    )


# ─── 5. _source / _conn / _channel passthrough ───────────────────────────────


def test_source_conn_channel_passthrough(tmp_path: Path):
    """The writer's source/channel/conn constructor args land in the envelope
    verbatim. These are the silver-ETL dispatch keys per D0.3 §2 + §3."""
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_rest",
        channel="events_snapshot",
        conn=None,  # null for REST snapshots per §2 _conn type contract
    )
    writer.write_frame(
        wire_recv_ts=datetime(2026, 5, 16, 12, 0, 0, 1, tzinfo=timezone.utc),
        raw_payload="{}",
    )
    writer.close()

    line = next(tmp_path.rglob("*.jsonl")).read_text().splitlines()[0]
    record = json.loads(line)
    assert record["_source"] == "kalshi_rest"
    assert record["_channel"] == "events_snapshot"
    assert record["_conn"] is None, (
        "REST snapshots have _conn=null per D0.3 §2 (Type: 'string | null'). "
        "Python None serializes to JSON null."
    )


# ─── 6. JSONL one-line-per-frame discipline ──────────────────────────────────


def test_one_line_per_frame_no_embedded_newlines(tmp_path: Path):
    """Each frame is ONE line of JSONL. A payload containing literal newlines
    (e.g., a `\\n`-rich error blob) must be JSON-escaped, not break the
    line-delimited format.

    Silver ETL uses ``zstdcat | jq`` and pyarrow's JSONL reader — both
    assume one record per line. A broken line breaks every consumer.
    """
    raw_with_newlines = '{"a":1,"b":"line1\\nline2"}'  # the \n inside is escaped IN the JSON
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
    )
    writer.write_frame(
        wire_recv_ts=datetime(2026, 5, 16, 12, 0, 0, 1, tzinfo=timezone.utc),
        raw_payload=raw_with_newlines,
    )
    # Even a payload containing a literal \n character (untypical but
    # possible from a malformed Kalshi frame) must JSON-escape on serialization.
    writer.write_frame(
        wire_recv_ts=datetime(2026, 5, 16, 12, 0, 0, 2, tzinfo=timezone.utc),
        raw_payload="raw payload\nwith literal newline",
    )
    writer.close()

    text = next(tmp_path.rglob("*.jsonl")).read_text()
    # Exactly 2 records → exactly 2 lines (trailing newline OK).
    lines = [line for line in text.split("\n") if line.strip()]
    assert len(lines) == 2, (
        f"expected 2 JSONL records, got {len(lines)} non-empty lines.\n"
        f"text:\n{text!r}"
    )
    # Each line is valid JSON on its own.
    for i, line in enumerate(lines):
        try:
            json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"line {i} is not valid JSON: {exc}\n"
                f"line:\n{line!r}\n"
                "Bronze must escape embedded newlines, not let them break "
                "the line-delimited format."
            )


def test_writer_appends_to_in_flight_until_rotate(tmp_path: Path):
    """Multiple write_frame() calls before rotation append to the SAME
    in-flight file.

    The rotation is the only event that creates a new file; absent
    rotation, all frames land in one growing .jsonl. Test pins the
    one-file invariant for a small N writes well below the 100MB / 5min
    rotation thresholds.

    Post-P1-A (ticket 86ba1pqqx, 2026-05-20) the writer no longer
    fflushes per-frame, so reading the in-flight file from a separate
    fd may see empty until the writer flushes (at rotation or close).
    The test explicitly forces a buffered flush via the writer's own
    file handle to verify the on-disk content WITHOUT triggering
    rotation. The "one file" invariant under the rotation thresholds
    is what's being pinned; the flush is test-scaffolding only.
    """
    writer = BronzeWriter(
        root_dir=tmp_path,
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
    )
    base_ts = datetime(2026, 5, 16, 12, 0, 0, 1, tzinfo=timezone.utc)
    for i in range(10):
        writer.write_frame(
            wire_recv_ts=base_ts.replace(microsecond=base_ts.microsecond + i),
            raw_payload=f'{{"i":{i}}}',
        )
    # P1-A: force a buffered flush so the on-disk content reflects the
    # writes. This is test scaffolding — production callers rely on
    # rotation (or close) to durably persist; per-frame flush was the
    # 2026-05-20 hot-path waste removed for ~thousands of syscalls/sec.
    # DO NOT call writer.close() — closing forces rotation, which would
    # break the "one in-flight file pre-rotation" invariant being pinned.
    writer._in_flight_fh.flush()
    in_flights = list(tmp_path.rglob("*.jsonl"))
    assert len(in_flights) == 1, (
        f"expected 1 in-flight .jsonl, got {len(in_flights)}: {in_flights}. "
        f"No rotation should fire under the 100MB / 5min thresholds."
    )
    lines = in_flights[0].read_text().strip().splitlines()
    assert len(lines) == 10
