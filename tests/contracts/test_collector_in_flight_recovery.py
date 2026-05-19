"""B-orphan-sweep — in_flight orphan recovery contract (filed 2026-05-19).

Sister to ``test_collector_idempotency.py``. Pins the recovery seam that
salvages bare-``in_flight_<usec>.jsonl`` orphans left behind when the
collector process is SIGKILL'd / OOM-killed by systemd (the graceful
``writer.close()`` in ``collector/main_loop.py``'s finally block does
NOT run on signal-9, leaving the open in_flight file un-rotated).

Pre-Bit history: 2026-05-17 15:15 UTC → 2026-05-19 ~08:30 UTC accumulated
~2,025 orphan files (19GB) on the production VPS — 84 systemd-driven
restarts × ~24 orphans per restart (7 conns × 3-4 channels each).
``RcloneUploader.sweep_outbox`` previously only iterated ``outbox/``,
leaving these orphans permanently invisible.

Plan-doc: ``kb/decisions/b-orphan-sweep-in-flight-recovery-plan.md``.

If this test fails:
- The restart-sweep no longer recovers in_flight orphans → bronze data
  loss on every collector crash. Read the plan-doc + RCA before
  changing the recovery surface.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import zstandard as zstd

from collector.uploader import RcloneUploader, _SALVAGE_ZSTD_LEVEL
from collector.writer import _ZSTD_LEVEL


# ─── 0. R2-M6 structural ratchet — salvage zstd level matches writer ────────


def test_salvage_zstd_level_matches_writer():
    """The salvage path's zstd level MUST equal the writer's _ZSTD_LEVEL.

    Drift would mean two different producers of bronze for the same
    logical chunk pair (graceful-rotation vs. crash-salvage) emit
    different compressed bytes for the same input — silver-ETL content
    hashes would diverge across recovery paths. The uploader module
    imports the writer's constant as the single source; this test
    encodes that lock-step behaviorally so a future Bit can't reintroduce
    the duplicate (R2-M6 ratchet).
    """
    assert _SALVAGE_ZSTD_LEVEL == _ZSTD_LEVEL, (
        f"_SALVAGE_ZSTD_LEVEL={_SALVAGE_ZSTD_LEVEL} drifted from writer's "
        f"_ZSTD_LEVEL={_ZSTD_LEVEL}. The salvage path must use the writer's "
        f"compression level so silver ETL sees byte-identical bronze "
        f"regardless of recovery path. Re-export via "
        f"`from collector.writer import _ZSTD_LEVEL as _WRITER_ZSTD_LEVEL` "
        f"and rebind `_SALVAGE_ZSTD_LEVEL` from that."
    )


def test_salvage_produces_byte_identical_zstd_to_writer():
    """Beyond the constant-equality check above: confirm that running
    zstd compression at the salvage path's level on a fixture payload
    matches the writer's compressed output for the same payload.

    This is the behavioral pin behind the structural pin — if zstandard
    ever changes default tuning at a given level (it has not in 0.x),
    this test catches the silver-ETL drift before ship.
    """
    payload = b"\n".join(
        json.dumps({"_seq": i, "_raw": f"frame-{i}"}).encode("utf-8")
        for i in range(50)
    ) + b"\n"
    salvage_bytes = zstd.ZstdCompressor(level=_SALVAGE_ZSTD_LEVEL).compress(payload)
    writer_bytes = zstd.ZstdCompressor(level=_ZSTD_LEVEL).compress(payload)
    assert salvage_bytes == writer_bytes, (
        "salvage zstd output differs from writer zstd output at the same "
        "level — silver ETL would see content-hash drift across recovery "
        "paths. Investigate before shipping."
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_envelope_line(
    *,
    seq: int,
    wire_recv_ts: datetime,
    raw: str = '{"type":"orderbook_delta","sid":1}',
    source: str = "kalshi_ws",
    channel: str = "orderbook_delta",
    conn: str = "A",
) -> bytes:
    """Build one JSONL envelope line matching the 6-field D0.3 §2 shape."""
    env = {
        "_wire_recv_ts": wire_recv_ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "_source": source,
        "_conn": conn,
        "_channel": channel,
        "_collector_seq": seq,
        "_raw": raw,
    }
    return (json.dumps(env, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _stage_orphan(
    partition_dir: Path,
    *,
    open_usec: int,
    lines: list[bytes],
    mtime_override: float | None = None,
) -> Path:
    """Create a bare-``in_flight_<usec>.jsonl`` orphan in the given partition.

    Returns the path to the created orphan file. Caller sets mtime via
    ``mtime_override`` (epoch seconds) for the 90s-safety-age test.
    """
    in_flight_dir = partition_dir / "in_flight"
    in_flight_dir.mkdir(parents=True, exist_ok=True)
    orphan = in_flight_dir / f"in_flight_{open_usec}.jsonl"
    with open(orphan, "wb") as fh:
        for line in lines:
            fh.write(line)
    if mtime_override is not None:
        os.utime(orphan, (mtime_override, mtime_override))
    return orphan


def _fake_rclone_run(local_size_by_name: dict[str, int]):
    """Return a subprocess.run mock that pretends rclone copyto + size succeed.

    ``local_size_by_name`` maps a stem (e.g. ``seq1-3``) to the expected
    byte count for the size-verify step. The fake matches the size against
    whichever stem appears in the size argv.
    """
    def _run(argv, *args, **kwargs):
        if "size" in argv:
            # Find which key's stem appears in the s3 dest argv.
            dest = argv[-1]
            for stem, size in local_size_by_name.items():
                if stem in dest:
                    return subprocess.CompletedProcess(
                        args=argv, returncode=0,
                        stdout=f'{{"count":1,"bytes":{size}}}\n',
                        stderr="",
                    )
            # Fallback — unknown chunk, signal mismatch so KEEP-local fires.
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout='{"count":1,"bytes":0}\n', stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
    return _run


# ─── 1. Core recovery — bare orphan re-rotated + uploaded ────────────────────


def test_sweep_recovers_bare_in_flight_orphan(tmp_path: Path):
    """A bare ``in_flight_<usec>.jsonl`` (no matching outbox stem, no chunk_id
    yet) is recovered by sweep_outbox: zstd-compressed → outbox/, renamed to
    chunk_id stem, then picked up by the existing upload path.

    Pre-fix behavior: orphan permanently invisible to sweep_outbox (which
    only iterates outbox/). Post-fix behavior: orphan re-rotated into the
    standard rotation shape and uploaded.
    """
    partition = (
        tmp_path / "kalshi_ws" / "orderbook_delta"
        / "year=2026" / "month=05" / "day=17" / "hour=15" / "conn=C"
    )
    base_ts = datetime(2026, 5, 17, 15, 15, 0, tzinfo=timezone.utc)
    lines = [
        _make_envelope_line(seq=100, wire_recv_ts=base_ts),
        _make_envelope_line(seq=101, wire_recv_ts=base_ts.replace(second=30)),
        _make_envelope_line(seq=102, wire_recv_ts=base_ts.replace(minute=16)),
    ]
    # Stage with old mtime (older than 90s safety threshold).
    orphan = _stage_orphan(
        partition,
        open_usec=1779030884572260,
        lines=lines,
        mtime_override=time.time() - 600,  # 10 min ago
    )

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")

    # The chunk_id derived from the first/last frame:
    # 20260517T151500Z_to_20260517T151600Z_seq100-102
    expected_chunk_id = "20260517T151500Z_to_20260517T151600Z_seq100-102"

    upload_argvs = []

    def fake_run(argv, *args, **kwargs):
        upload_argvs.append(argv)
        if "size" in argv:
            # Match the expected outbox file's byte count (we computed it
            # by compressing the lines below before the call).
            return subprocess.CompletedProcess(
                args=argv, returncode=0,
                stdout=f'{{"count":1,"bytes":{_expected_compressed_size}}}\n',
                stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    # Compute what the compressed bytes will be so the size-verify passes.
    cctx = zstd.ZstdCompressor(level=_ZSTD_LEVEL)
    _expected_compressed_size = len(cctx.compress(b"".join(lines)))

    with patch("collector.uploader.subprocess.run", side_effect=fake_run):
        n = uploader.sweep_outbox(root_dir=tmp_path)

    # At minimum one upload attempted; the chunk_id stem appears in the
    # copyto argv.
    assert n >= 1, "sweep_outbox returned 0 — orphan not recovered."
    copy_argvs = [
        a for a in upload_argvs
        if any("copy" in str(tok) for tok in a)
    ]
    referenced = " ".join(" ".join(str(x) for x in a) for a in copy_argvs)
    assert expected_chunk_id in referenced, (
        f"chunk_id stem {expected_chunk_id!r} not found in copy argvs. "
        f"argvs: {copy_argvs}"
    )
    # Original wall-clock-µs orphan filename is gone (renamed or deleted).
    assert not orphan.exists(), (
        "orphan in_flight_<usec>.jsonl still present after sweep — "
        "rename or delete path is broken."
    )


# ─── 2. Safety-age threshold — fresh orphans untouched ───────────────────────


def test_sweep_skips_fresh_orphan_under_safety_age(tmp_path: Path):
    """An in_flight file with mtime within the 90s safety threshold is left
    untouched — it could be an active writer's still-open first frame.

    Rationale: the writer's 5-min rotation cadence means a fresh in_flight
    is normal for up to 5 min, and the safety threshold preserves that
    window with a healthy margin.
    """
    partition = (
        tmp_path / "kalshi_ws" / "trade"
        / "year=2026" / "month=05" / "day=19" / "hour=08" / "conn=A"
    )
    base_ts = datetime(2026, 5, 19, 8, 45, 0, tzinfo=timezone.utc)
    lines = [_make_envelope_line(seq=1, wire_recv_ts=base_ts, channel="trade")]
    orphan = _stage_orphan(
        partition,
        open_usec=1779180000000000,
        lines=lines,
        mtime_override=time.time() - 30,  # 30s ago (under 90s threshold)
    )

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")
    with patch("collector.uploader.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        uploader.sweep_outbox(root_dir=tmp_path)

    assert orphan.exists(), (
        "fresh in_flight (mtime < 90s) was touched by sweep_outbox — "
        "the safety threshold prevents stealing from active writers."
    )


# ─── 3. zstd round-trip integrity — bit-identical bronze ─────────────────────


def test_sweep_round_trip_preserves_bytes(tmp_path: Path):
    """Bronze envelopes survive the salvage path bit-identically.

    Reads the post-sweep outbox .jsonl.zst, decompresses, and asserts each
    line is byte-identical to the original input. Critical: silver ETL
    must see the exact wire bytes the writer originally captured.
    """
    partition = (
        tmp_path / "kalshi_ws" / "orderbook_delta"
        / "year=2026" / "month=05" / "day=18" / "hour=09" / "conn=B"
    )
    base_ts = datetime(2026, 5, 18, 9, 0, 0, tzinfo=timezone.utc)
    lines = [
        _make_envelope_line(seq=500, wire_recv_ts=base_ts, conn="B"),
        _make_envelope_line(
            seq=501, wire_recv_ts=base_ts.replace(second=15), conn="B",
            raw='{"type":"orderbook_delta","sid":2,"price":54}',
        ),
    ]
    _stage_orphan(
        partition,
        open_usec=1779110800000000,
        lines=lines,
        mtime_override=time.time() - 600,
    )

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")

    # Stub rclone with returncode=1 so KEEP-local fires; we only need
    # the salvage path (compress + rename) to land the outbox file on
    # local disk, NOT the actual upload + delete.
    with patch("collector.uploader.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="stubbed-fail",
        )
        uploader.sweep_outbox(root_dir=tmp_path)

    # The outbox should have a .jsonl.zst file now (salvage succeeded,
    # upload stub-failed → KEEP-local).
    outbox_files = list((partition / "outbox").glob("*.jsonl.zst"))
    assert len(outbox_files) == 1, (
        f"expected exactly 1 salvaged outbox file, got {len(outbox_files)}: "
        f"{outbox_files}"
    )
    compressed = outbox_files[0].read_bytes()
    decompressed = zstd.ZstdDecompressor().decompress(compressed)
    assert decompressed == b"".join(lines), (
        "round-trip not bit-identical — silver ETL would see drifted bronze."
    )


# ─── 4. Empty orphan — deleted, no spurious upload ───────────────────────────


def test_sweep_deletes_empty_orphan(tmp_path: Path):
    """A 0-byte orphan has no derivable chunk_id; delete + log + continue.

    No outbox file should be created (an empty zstd-compressed file is
    not valid bronze).
    """
    partition = (
        tmp_path / "kalshi_ws" / "market_lifecycle_v2"
        / "year=2026" / "month=05" / "day=18" / "hour=11" / "conn=E"
    )
    in_flight_dir = partition / "in_flight"
    in_flight_dir.mkdir(parents=True)
    orphan = in_flight_dir / "in_flight_1779120000000000.jsonl"
    orphan.write_bytes(b"")
    os.utime(orphan, (time.time() - 600, time.time() - 600))

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")
    with patch("collector.uploader.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        uploader.sweep_outbox(root_dir=tmp_path)

    assert not orphan.exists(), (
        "empty orphan still present after sweep — should be deleted."
    )
    assert not (partition / "outbox").exists() or not list(
        (partition / "outbox").glob("*.jsonl.zst")
    ), "empty orphan produced a spurious outbox file."


# ─── 5a. R1-C1/C2 ratchet — chunk_id collision must preserve, not clobber ───


def test_sweep_preserves_orphan_on_outbox_chunk_id_collision(tmp_path: Path):
    """If salvage would write to an outbox/{chunk_id}.jsonl.zst that ALREADY
    exists (graceful chunk from a prior boot whose first/last ts+seq
    happen to match the orphan's derivation), the orphan is PRESERVED
    on disk — overwriting the canonical chunk with partial orphan content
    would be a silent bronze-loss class equivalent to the bug this method
    is fixing.

    Pre-fix-pre-ratchet behavior: os.replace silently clobbered. Ratchet
    encoded post-R1.
    """
    partition = (
        tmp_path / "kalshi_ws" / "orderbook_delta"
        / "year=2026" / "month=05" / "day=17" / "hour=15" / "conn=C"
    )
    base_ts = datetime(2026, 5, 17, 15, 15, 0, tzinfo=timezone.utc)
    lines = [
        _make_envelope_line(seq=100, wire_recv_ts=base_ts),
        _make_envelope_line(seq=102, wire_recv_ts=base_ts.replace(minute=16)),
    ]
    orphan = _stage_orphan(
        partition,
        open_usec=1779030884572260,
        lines=lines,
        mtime_override=time.time() - 600,
    )

    # Pre-seed a canonical outbox chunk that derives the SAME chunk_id
    # from the same first/last ts+seq.
    expected_chunk_id = "20260517T151500Z_to_20260517T151600Z_seq100-102"
    outbox_dir = partition / "outbox"
    outbox_dir.mkdir(parents=True)
    canonical = outbox_dir / f"{expected_chunk_id}.jsonl.zst"
    canonical_payload = b"\x28\xb5\x2f\xfd" + b"\x00" * 8  # zstd magic + fake body
    canonical.write_bytes(canonical_payload)

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")
    with patch("collector.uploader.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="stubbed-fail",
        )
        uploader.sweep_outbox(root_dir=tmp_path)

    # Canonical outbox chunk UNCHANGED — content bytes preserved.
    assert canonical.read_bytes() == canonical_payload, (
        "salvage clobbered a canonical outbox chunk on chunk_id collision "
        "— silent bronze-loss class. R1-C1 ratchet broken."
    )
    # Orphan preserved on disk for manual triage.
    assert orphan.exists(), (
        "salvage deleted/renamed an orphan that collided with a canonical "
        "chunk — should preserve for manual triage. R1-C1 ratchet broken."
    )


def test_sweep_preserves_orphan_on_in_flight_chunk_id_collision(tmp_path: Path):
    """Mirror of the outbox collision test: if salvage would rename
    orphan → in_flight/{chunk_id}.jsonl AND that target already exists
    (e.g., the prior boot's uploader finished outbox upload + delete but
    SIGKILL fired before the paired in_flight unlink), preserve the
    orphan instead of clobbering. Otherwise we create a NEW class of
    chunk_id-named orphan paired with no outbox — same data-loss
    severity as the bug this method fixes, just renamed.
    """
    partition = (
        tmp_path / "kalshi_ws" / "trade"
        / "year=2026" / "month=05" / "day=17" / "hour=15" / "conn=A"
    )
    base_ts = datetime(2026, 5, 17, 15, 15, 0, tzinfo=timezone.utc)
    lines = [
        _make_envelope_line(seq=200, wire_recv_ts=base_ts, channel="trade", conn="A"),
        _make_envelope_line(
            seq=201, wire_recv_ts=base_ts.replace(minute=16),
            channel="trade", conn="A",
        ),
    ]
    orphan = _stage_orphan(
        partition,
        open_usec=1779030884572261,
        lines=lines,
        mtime_override=time.time() - 600,
    )

    expected_chunk_id = "20260517T151500Z_to_20260517T151600Z_seq200-201"
    in_flight_dir = partition / "in_flight"
    canonical_in_flight = in_flight_dir / f"{expected_chunk_id}.jsonl"
    canonical_payload = b'{"_raw":"canonical"}\n'
    canonical_in_flight.write_bytes(canonical_payload)

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")
    with patch("collector.uploader.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="stubbed-fail",
        )
        uploader.sweep_outbox(root_dir=tmp_path)

    assert canonical_in_flight.read_bytes() == canonical_payload, (
        "salvage clobbered a canonical in_flight chunk on chunk_id "
        "collision. R1-C2 ratchet broken."
    )
    assert orphan.exists(), (
        "salvage clobbered an orphan that collided with a canonical "
        "in_flight chunk — R1-C2 ratchet broken."
    )


# ─── 4a. R3-m2 ratchet — salvage return value pins honest count ─────────────


def test_salvage_return_count_is_honest(tmp_path: Path):
    """``salvage_in_flight_orphans`` returns an int count of TRUE salvages
    (orphans that became outbox+in_flight pairs), NOT a count of orphans
    cleaned up via empty/whitespace-only/malformed/collision paths. R2-m1
    fix introduced this contract; R3-m2 pins it behaviorally so it cannot
    silently drift.
    """
    # Three orphans: one real salvage, one empty (delete-not-salvage),
    # one malformed (preserve-not-salvage).
    partition_a = (
        tmp_path / "kalshi_ws" / "orderbook_delta"
        / "year=2026" / "month=05" / "day=17" / "hour=15" / "conn=A"
    )
    real_lines = [
        _make_envelope_line(seq=10, wire_recv_ts=datetime(2026, 5, 17, 15, 15, 0, tzinfo=timezone.utc)),
        _make_envelope_line(seq=11, wire_recv_ts=datetime(2026, 5, 17, 15, 15, 30, tzinfo=timezone.utc)),
    ]
    _stage_orphan(partition_a, open_usec=1779030880000001, lines=real_lines, mtime_override=time.time() - 600)

    partition_b = (
        tmp_path / "kalshi_ws" / "trade"
        / "year=2026" / "month=05" / "day=17" / "hour=15" / "conn=B"
    )
    empty_dir = partition_b / "in_flight"
    empty_dir.mkdir(parents=True)
    empty_orphan = empty_dir / "in_flight_1779030880000002.jsonl"
    empty_orphan.write_bytes(b"")
    os.utime(empty_orphan, (time.time() - 600, time.time() - 600))

    partition_c = (
        tmp_path / "kalshi_ws" / "market_lifecycle_v2"
        / "year=2026" / "month=05" / "day=17" / "hour=15" / "conn=C"
    )
    malformed_dir = partition_c / "in_flight"
    malformed_dir.mkdir(parents=True)
    malformed = malformed_dir / "in_flight_1779030880000003.jsonl"
    malformed.write_bytes(b"this is not json\n")
    os.utime(malformed, (time.time() - 600, time.time() - 600))

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")
    n = uploader.salvage_in_flight_orphans(root_dir=tmp_path)
    assert n == 1, (
        f"salvage_in_flight_orphans returned {n}; expected 1 (only the "
        f"real orphan in partition_a counts; empty + malformed are "
        f"cleanup/skip, not salvage). R2-m1 honest-counter ratchet broken."
    )


def test_salvage_return_count_zero_on_empty_tree(tmp_path: Path):
    """An empty bronze tree → return 0, no errors."""
    (tmp_path / "kalshi_ws" / "orderbook_delta").mkdir(parents=True)
    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")
    n = uploader.salvage_in_flight_orphans(root_dir=tmp_path)
    assert n == 0, f"empty tree returned {n}; expected 0."


# ─── 5. Malformed orphan — KEEP-local for manual triage ──────────────────────


def test_sweep_keeps_malformed_orphan(tmp_path: Path):
    """An orphan with non-JSON content is preserved on disk (NOT deleted)
    so an operator can triage manually. Silent data-loss on corruption is
    worse than disk-pressure.
    """
    partition = (
        tmp_path / "kalshi_ws" / "orderbook_delta"
        / "year=2026" / "month=05" / "day=18" / "hour=13" / "conn=F"
    )
    in_flight_dir = partition / "in_flight"
    in_flight_dir.mkdir(parents=True)
    orphan = in_flight_dir / "in_flight_1779130000000000.jsonl"
    orphan.write_bytes(b"this is not json\nstill not json\n")
    os.utime(orphan, (time.time() - 600, time.time() - 600))

    uploader = RcloneUploader(rclone_remote="s3prod", bucket="kalshi-bot-archive")
    with patch("collector.uploader.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        uploader.sweep_outbox(root_dir=tmp_path)

    assert orphan.exists(), (
        "malformed orphan was DELETED — should be preserved for manual "
        "triage (data-loss-on-corruption is worse than disk-pressure)."
    )
