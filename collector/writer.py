"""Bronze JSONL writer + rotation — D1.2 (ticket 86b9ypn66, 2026-05-16).

D0.3 §2 envelope (6 fields, IRREVERSIBLE):
``_wire_recv_ts`` + ``_source`` + ``_conn`` + ``_channel`` +
``_collector_seq`` + ``_raw``. Captured at frame ingress BEFORE any
deserialization — ``_raw`` is the full wire payload as a string, NOT a
parsed dict (parsing would let bronze drift from "what the wire
actually said").

D0.3 §4 rotation: 5-minute timer OR 100MB whichever first. Per-conn,
per-channel rotation cadence; rotation handler zstd-compresses to tmp/,
atomic-renames to outbox/, and hands the path off to
``collector/uploader.py``.

D0.3 §3 partition path:
``bronze/{source}/{channel}/year=YYYY/month=MM/day=DD/hour=HH/conn=<X>/<chunk>.jsonl.zst``

Contract pins:
- tests/contracts/test_collector_writer.py — envelope shape + capture-
  before-deserialize + JSONL discipline
- tests/contracts/test_collector_rotation.py — 5min OR 100MB triggers +
  per-(source,channel,conn) isolation + chunk_id format + hive-style
  partition path
- tests/contracts/test_collector_idempotency.py — atomic rename + zstd
  round-trip
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import zstandard as zstd

from kalshi_wire.ws_client import build_envelope


# D0.3 §4 — rotation triggers. Pinned by
# tests/contracts/test_collector_rotation.py::test_rotation_constants_match_d0_3_spec.
ROTATION_INTERVAL_SECONDS: int = 300                  # 5 minutes
ROTATION_SIZE_BYTES: int = 100 * 1024 * 1024          # 100 MB

# zstd level 6 per D0.3 §7 "Why each step" — inline-rotation-handler
# speed-vs-ratio sweet spot. Operator-side silver backfill (D1.8) may
# use level 9-19 separately.
_ZSTD_LEVEL: int = 6


class BronzeWriter:
    """One in-flight JSONL file per (source, channel, conn) tuple.

    Each call to ``write_frame`` appends one envelope-wrapped line. When
    EITHER the 5-min timer or the 100MB cap trips, ``_rotate()`` zstd-
    compresses the in-flight to a tmp file, atomic-renames it to outbox/,
    and opens a fresh in-flight.

    The class deliberately does NOT spawn threads or async tasks — the
    caller (collector/main_loop.py) drives ``write_frame`` synchronously
    from the WS read loop. This matches CLAUDE.md anti-pattern "Don't
    add async. Synchronous + threading for WS feeds is the design."
    """

    def __init__(
        self,
        root_dir: Path,
        source: str,
        channel: str,
        conn: Optional[str],
        *,
        size_threshold_bytes: int = ROTATION_SIZE_BYTES,
        interval_seconds: int = ROTATION_INTERVAL_SECONDS,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.source = source
        self.channel = channel
        self.conn = conn
        self._size_cap = size_threshold_bytes
        self._interval = interval_seconds
        self._now = now_fn

        # Per-frame seq, monotone within process lifetime per D0.3 §2.
        # Resets across process restarts — silver QA cross-references with
        # a separate boot-record stream (out of scope at D1.2).
        self._seq: int = 0

        # In-flight state.
        self._in_flight_path: Optional[Path] = None
        self._in_flight_fh = None
        self._in_flight_open_ts: Optional[float] = None
        self._in_flight_bytes: int = 0
        self._frame_ts_first: Optional[datetime] = None
        self._frame_ts_last: Optional[datetime] = None
        self._frame_seq_first: Optional[int] = None
        self._frame_seq_last: Optional[int] = None

        # Each rotation appends an (outbox_path, in_flight_path) pair —
        # main_loop drains the list and hands both paths to the
        # uploader. Explicit pairing avoids name-derivation drift between
        # the in-flight name (wall-clock-µs at open) and the chunk_id
        # (frame-ts + seq at rotation) — the two never matched, so
        # name-based recovery would never find the in-flight to delete.
        # The uploader uses BOTH paths in the delete-on-success step
        # (D0.3 §7 step 6).
        self.rotated_outbox_paths: List[Tuple[Path, Path]] = []

    # ── Public API ──────────────────────────────────────────────────────

    def write(self, envelope: Dict[str, Any]) -> None:
        """Append a pre-built bronze envelope as one JSONL line.

        This is the production seam called by ``BronzeArchiver._on_frame``
        with envelopes from ``kalshi_wire.build_envelope`` — kalshi_wire
        owns envelope construction (D0.3 §5 AMENDMENT 2026-05-16 "two
        sides of the same coin"). The writer is a dumb persister.

        Validates that ``_source``/``_channel``/``_conn`` match the
        writer's constructor args — silver-ETL dispatch must see a
        consistent partition vs. envelope-field invariant. A mismatch
        means the caller routed a frame to the wrong writer instance;
        raise rather than silently corrupt the bronze.

        Triggers rotation BEFORE the write if either the time or size
        threshold has been exceeded for the currently-open in-flight.
        """
        if envelope.get("_source") != self.source:
            raise ValueError(
                f"envelope _source={envelope.get('_source')!r} does not "
                f"match writer.source={self.source!r}. Each writer is "
                f"scoped to one (source, channel, conn) partition."
            )
        if envelope.get("_channel") != self.channel:
            raise ValueError(
                f"envelope _channel={envelope.get('_channel')!r} does not "
                f"match writer.channel={self.channel!r}."
            )
        if envelope.get("_conn") != self.conn:
            raise ValueError(
                f"envelope _conn={envelope.get('_conn')!r} does not "
                f"match writer.conn={self.conn!r}."
            )

        # Extract wire_recv_ts for rotation + partition logic. Envelope
        # carries the ISO-8601-UTC-µs string; we parse back into a
        # datetime for the rotation triggers / hive-partition derivation.
        ts_str = envelope["_wire_recv_ts"]
        # strptime+microsecond %f handles the trailing Z by replace.
        wire_recv_ts = datetime.strptime(
            ts_str, "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=timezone.utc)
        seq = int(envelope["_collector_seq"])

        # Check rotation triggers against the EXISTING in-flight (if any).
        if self._in_flight_path is not None and self._should_rotate():
            self._rotate()

        if self._in_flight_path is None:
            # Partition derives from THIS frame's _wire_recv_ts — the
            # first frame of the new chunk. D0.3 §3 anchors the hive
            # partition on the frame ts, not wall-clock-at-open.
            self._open_in_flight(seed_ts=wire_recv_ts)

        line = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False) + "\n"
        line_bytes = line.encode("utf-8")
        self._in_flight_fh.write(line_bytes)
        # Flush to OS so concurrent readers (silver QA tooling running
        # in parallel on the same host) see the bytes immediately. Does
        # NOT fsync — bronze durability is the uploader's contract
        # (D0.3 §7 fsyncs at rotation time before zstd-compress).
        self._in_flight_fh.flush()
        self._in_flight_bytes += len(line_bytes)

        if self._frame_ts_first is None:
            self._frame_ts_first = wire_recv_ts
            self._frame_seq_first = seq
        self._frame_ts_last = wire_recv_ts
        self._frame_seq_last = seq

    def write_frame(self, wire_recv_ts: datetime, raw_payload: str) -> None:
        """Convenience wrapper: build envelope via ``kalshi_wire.build_envelope``
        then route through ``write(envelope)``.

        Used by tests and single-source callers that don't need to manage
        their own seq counter (the writer assigns a monotone process-
        lifetime seq via ``self._seq``). Production WS frames flow through
        ``BronzeArchiver`` which assigns its own seq AND calls ``write()``
        directly — write_frame is NOT in that path.

        ``wire_recv_ts`` MUST be tz-aware (UTC).
        """
        if wire_recv_ts.tzinfo is None:
            raise ValueError(
                "wire_recv_ts must be tz-aware (UTC). Naive datetimes "
                "cannot be unambiguously serialized as UTC."
            )
        self._seq += 1
        envelope = build_envelope(
            raw=raw_payload,
            source=self.source,
            channel=self.channel,
            conn=self.conn,
            collector_seq=self._seq,
            wire_recv_ts=wire_recv_ts,
        )
        self.write(envelope)

    def close(self) -> None:
        """Force-rotate the in-flight (if any) and release the file handle."""
        if self._in_flight_path is not None:
            self._rotate()

    # ── Internals ───────────────────────────────────────────────────────

    def _should_rotate(self) -> bool:
        """Return True if either trigger has fired for the current in-flight.

        OR-semantics per D0.3 §4 — fixed time = bloated peak chunks,
        fixed size = sparse off-peak chunks. The whichever-first rule.
        """
        if self._in_flight_open_ts is None:
            return False
        elapsed = self._now() - self._in_flight_open_ts
        if elapsed >= self._interval:
            return True
        if self._in_flight_bytes >= self._size_cap:
            return True
        return False

    def _partition_dir(self, ts: datetime) -> Path:
        """Compute the hive-style partition dir for a frame timestamp.

        D0.3 §3: ``<root>/<source>/<channel>/year=YYYY/month=MM/day=DD/hour=HH/conn=<X>/``
        REST snapshots have ``conn=None`` — D0.3 §3 doesn't apply a conn=
        segment to REST; for in-tree consistency we still nest under a
        conn= segment using the literal "none" string. Same fallback
        applied to channel=None (D1.1.5 scaffold-scope BronzeArchiver
        passes channel=None until D1.3 subscription_manager wires the
        per-sid → channel mapping). Silver ETL dispatches on ``_source``
        for the path-shape anyway.
        """
        conn_str = "none" if self.conn is None else self.conn
        channel_str = "_unrouted" if self.channel is None else self.channel
        return (
            self.root_dir
            / self.source
            / channel_str
            / f"year={ts.year:04d}"
            / f"month={ts.month:02d}"
            / f"day={ts.day:02d}"
            / f"hour={ts.hour:02d}"
            / f"conn={conn_str}"
        )

    def _open_in_flight(self, seed_ts: datetime) -> None:
        """Open a new in-flight file under the partition derived from ``seed_ts``.

        The partition is derived from the FIRST FRAME'S ``_wire_recv_ts``
        per D0.3 §3 (not wall-clock-at-open). The caller must invoke this
        AFTER the first frame's ts is known so the partition's hour/day
        segment reflects when the data was observed, not when the writer
        happened to open the file. This decouples partition placement
        from process-restart timing.
        """
        now = self._now()
        partition = self._partition_dir(seed_ts)
        tmp_dir = partition / "tmp"
        outbox_dir = partition / "outbox"
        in_flight_dir = partition / "in_flight"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        outbox_dir.mkdir(parents=True, exist_ok=True)
        in_flight_dir.mkdir(parents=True, exist_ok=True)
        in_flight_name = f"in_flight_{int(now * 1_000_000)}.jsonl"
        self._in_flight_path = in_flight_dir / in_flight_name
        self._in_flight_fh = open(self._in_flight_path, "wb")
        self._in_flight_open_ts = now
        self._in_flight_bytes = 0
        self._frame_ts_first = None
        self._frame_ts_last = None
        self._frame_seq_first = None
        self._frame_seq_last = None

    def _rotate(self) -> None:
        """Close current in-flight, zstd-compress to tmp/, atomic-rename
        to outbox/, RENAME in-flight to share the chunk_id, and append
        the (outbox, in_flight) pair to ``rotated_outbox_paths``.

        D0.3 §7 KEEP-local discipline: BOTH the .jsonl (in_flight/) and
        .jsonl.zst (outbox/) stay on disk until the uploader's
        rclone-copy + size-verify succeed. The writer's contract is
        "produce a closed chunk pair"; deletion belongs to the uploader.

        Rename rationale: the in-flight file is created with a wall-
        clock-µs filename at _open_in_flight time (no chunk_id is
        known then). At rotation we know the chunk_id, so we rename
        the in-flight to share that stem. This (a) makes the
        (outbox, in_flight) pairing unambiguous on disk for sweep_outbox
        recovery, (b) prevents the in-flight from being mistaken for an
        unrelated chunk's leftover.
        """
        if self._in_flight_path is None:
            return
        # Close + fsync the in-flight.
        self._in_flight_fh.flush()
        os.fsync(self._in_flight_fh.fileno())
        self._in_flight_fh.close()

        # Compute chunk_id from actual frame timestamp range + seq range.
        if self._frame_ts_first is None or self._frame_ts_last is None:
            # Empty rotation (rotate called with no frames written) —
            # skip and clean up.
            try:
                self._in_flight_path.unlink()
            except FileNotFoundError:
                pass
            self._reset_in_flight_state()
            return
        chunk_id = (
            f"{_format_compact_iso(self._frame_ts_first)}"
            f"_to_{_format_compact_iso(self._frame_ts_last)}"
            f"_seq{self._frame_seq_first}-{self._frame_seq_last}"
        )

        partition = self._in_flight_path.parent.parent  # strip in_flight/
        tmp_path = partition / "tmp" / f"{chunk_id}.jsonl.zst.tmp"
        outbox_path = partition / "outbox" / f"{chunk_id}.jsonl.zst"

        # zstd-compress in-flight → tmp/.
        cctx = zstd.ZstdCompressor(level=_ZSTD_LEVEL)
        with open(self._in_flight_path, "rb") as src, open(tmp_path, "wb") as dst:
            dst.write(cctx.compress(src.read()))
            dst.flush()
            os.fsync(dst.fileno())

        # POSIX-atomic rename tmp/ → outbox/. Same fs since both live
        # under <partition>/.
        os.replace(tmp_path, outbox_path)

        # RENAME the in-flight to share the chunk_id stem. After this,
        # both the .jsonl and the .jsonl.zst on disk have the same
        # stem differing only in suffix — the uploader's name-based
        # pairing (sweep_outbox) can find them deterministically.
        in_flight_renamed = (
            self._in_flight_path.parent / f"{chunk_id}.jsonl"
        )
        os.replace(self._in_flight_path, in_flight_renamed)

        self.rotated_outbox_paths.append((outbox_path, in_flight_renamed))
        self._reset_in_flight_state()

    def _reset_in_flight_state(self) -> None:
        self._in_flight_path = None
        self._in_flight_fh = None
        self._in_flight_open_ts = None
        self._in_flight_bytes = 0
        self._frame_ts_first = None
        self._frame_ts_last = None
        self._frame_seq_first = None
        self._frame_seq_last = None


# ── Timestamp formatters ───────────────────────────────────────────────
#
# NOTE: ISO-8601 UTC µs formatting for ``_wire_recv_ts`` lives in
# ``kalshi_wire.ws_client.build_envelope`` (single source of truth per
# the 2026-05-16 §5 AMENDMENT). The compact-ISO helper below is for
# chunk filenames only — it's NOT a substitute for the build_envelope
# formatter.


def _format_compact_iso(ts: datetime) -> str:
    """Compact ISO for chunk-filename use: ``YYYYMMDDTHHMMSSZ`` (no µs).

    D0.3 §3 example: ``20260520T140000Z``. Microsecond precision is
    intentionally dropped here — the chunk filename is for human
    diagnostics + range-narrowing, NOT byte-exact ordering (that's
    what _wire_recv_ts inside the records is for).
    """
    utc_ts = ts.astimezone(timezone.utc)
    return utc_ts.strftime("%Y%m%dT%H%M%SZ")
