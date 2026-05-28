"""B2a-1 — ``collector/venue_l2_main_loop.py`` orchestrator contract.

Ticket `86ba1zf5j`. Mirror of ``collector/coinbase_main_loop.py`` (D2.5)
adapted for the multi-venue lean recorder:
  - One BronzeWriter PER VENUE (not per channel) — each venue is its own
    bronze source with its own native L2 channel.
  - One drain thread fans out across all venue writers + writes the
    bronze_health.json sidecar (schema_version=1, single-archiver list).
  - NO ``bot.*`` imports (collector-no-bot — pinned globally by
    tests/contracts/test_collector_no_bot_imports.py, which AST-walks
    every collector/*.py including this new module).
"""
from __future__ import annotations

import json
from pathlib import Path

from collector.venue_l2_archiver import VENUE_CHANNELS, VENUE_SOURCES, VENUES
from collector.venue_l2_main_loop import (
    _build_writers,
    write_bronze_health_sidecar,
)


def test_build_writers_one_per_venue_with_correct_partition(tmp_path: Path):
    writers = _build_writers(tmp_path)
    assert set(writers) == set(VENUES), (
        "one BronzeWriter per venue (kraken/bitstamp/gemini)"
    )
    for venue, writer in writers.items():
        assert writer.source == VENUE_SOURCES[venue]
        assert writer.channel == VENUE_CHANNELS[venue]
        # Single-conn per venue (source distinguishes venue); mirror coinbase.
        assert writer.conn == "A"


def test_writers_use_bounded_rotation_size(tmp_path: Path):
    """The recorder writes synchronously on the asyncio thread (lean — no
    worker-thread decouple). A modest rotation-size cap bounds the
    per-rotation zstd-compress block so it can't stall the other venues'
    keepalive for multiple seconds (a 100MB chunk would). Pin the cap is
    well under the default 100MB."""
    writers = _build_writers(tmp_path)
    for writer in writers.values():
        assert writer._size_cap <= 32 * 1024 * 1024, (
            "venue-l2 writers must use a reduced size_threshold_bytes "
            "(<=32MB) so synchronous zstd at rotation stays short."
        )


def test_health_sidecar_schema(tmp_path: Path):
    """Sidecar mirrors the coinbase/weather schema so the cron-driven
    collector_health_monitor's check_dropped_frames consumes it
    unmodified."""

    class _FakeArchiver:
        def get_health_snapshot(self):
            return {
                "conn_id": "A",
                "dropped_frames": 0,
                "write_queue_size": 0,
                "write_queue_maxsize": 0,
                "write_worker_alive": True,
                "collector_seq": 42,
                "ack_frames_processed": 0,
            }

    sidecar = tmp_path / "bronze_health.json"
    write_bronze_health_sidecar(_FakeArchiver(), sidecar)
    data = json.loads(sidecar.read_text())
    assert data["schema_version"] == 1
    assert isinstance(data["archivers"], list) and len(data["archivers"]) == 1
    assert data["total_dropped_frames"] == 0
    assert data["total_queue_size"] == 0
    assert data["archivers"][0]["collector_seq"] == 42
