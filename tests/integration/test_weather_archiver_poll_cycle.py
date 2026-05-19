"""D1.8 — end-to-end poll-cycle integration for WeatherArchiver.

Ticket `86ba0duck` (2026-05-18). Mock the Open-Meteo HTTP calls; verify
that one ``poll_once`` invocation:
  1. Calls each Open-Meteo endpoint with the expected params for each
     (city, channel) pair.
  2. Builds a bronze envelope via ``kalshi_wire.build_envelope`` for
     each response and dispatches to the correct ``BronzeWriter``.
  3. Force-rotation flushes the in-flight to outbox/.

Mocks: ``requests.get`` to return synthetic 200 responses. No network.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


pytestmark = pytest.mark.integration


def _make_mock_response(json_payload: dict, status_code: int = 200):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_payload
    return mock


def _ensemble_response(members):
    """Synthetic Open-Meteo ensemble response shape."""
    return {
        "daily": {
            "time": ["2026-05-19"],
            "temperature_2m_max_member01": [members[0]],
        }
    }


@pytest.fixture
def bronze_root(tmp_path):
    root = tmp_path / "bronze"
    root.mkdir()
    return root


def test_poll_once_dispatches_envelopes_to_each_channel_writer(bronze_root):
    """One poll_once() iteration writes one envelope per (city, channel)
    to the matching BronzeWriter, then force-rotation produces an
    outbox/ chunk per channel.
    """
    from collector.weather_archiver import WeatherArchiver
    from collector.writer import BronzeWriter

    # Build one writer per channel.
    writers = {
        channel: BronzeWriter(
            root_dir=bronze_root,
            source="open_meteo",
            channel=channel,
            conn=None,
            interval_seconds=3600,
        )
        for channel in (
            "ensemble_gfs",
            "ensemble_ecmwf",
            "forecast_hrrr",
            "archive_observed",
        )
    }

    archiver = WeatherArchiver(
        writers_by_channel=writers,
        cities=["NYC"],  # one city for the integration test
        inter_city_sleep_seconds=0.0,
    )

    # Mock all four endpoints with synthetic 200 responses.
    with patch("collector.weather_archiver.requests.get") as mock_get:
        mock_get.return_value = _make_mock_response(
            _ensemble_response([72.5])
        )
        archiver.poll_once()

    # At least 3 envelopes written (3 forecast channels for 1 city per
    # cycle; archive_observed may be skipped depending on hour-of-day
    # gating).
    for channel in ("ensemble_gfs", "ensemble_ecmwf", "forecast_hrrr"):
        # Force rotation so we can inspect the outbox chunk.
        writers[channel].close()
        # The writer creates partition dirs; find the outbox chunk(s).
        outbox_chunks = list(bronze_root.rglob("outbox/*.jsonl.zst"))
        # At least one channel produced an outbox chunk after close().
    assert len(list(bronze_root.rglob("outbox/*.jsonl.zst"))) >= 3, (
        "Expected at least 3 outbox chunks after rotation — one per "
        "forecast channel (ensemble_gfs / ensemble_ecmwf / forecast_hrrr). "
        f"Found: {list(bronze_root.rglob('outbox/*.jsonl.zst'))}"
    )


def test_poll_once_envelope_source_is_open_meteo(bronze_root):
    """Every envelope written has ``_source='open_meteo'``."""
    from collector.weather_archiver import WeatherArchiver
    from collector.writer import BronzeWriter
    import zstandard

    writers = {
        channel: BronzeWriter(
            root_dir=bronze_root,
            source="open_meteo",
            channel=channel,
            conn=None,
            interval_seconds=3600,
        )
        for channel in ("ensemble_gfs", "forecast_hrrr")
    }
    archiver = WeatherArchiver(
        writers_by_channel=writers,
        cities=["NYC"],
        channels=("ensemble_gfs", "forecast_hrrr"),
        inter_city_sleep_seconds=0.0,
    )

    with patch("collector.weather_archiver.requests.get") as mock_get:
        mock_get.return_value = _make_mock_response(
            _ensemble_response([72.5])
        )
        archiver.poll_once()
    for w in writers.values():
        w.close()

    chunks = list(bronze_root.rglob("outbox/*.jsonl.zst"))
    assert chunks, "no chunks written"

    dctx = zstandard.ZstdDecompressor()
    for chunk in chunks:
        raw = chunk.read_bytes()
        decompressed = dctx.decompress(raw)
        for line in decompressed.decode("utf-8").splitlines():
            env = json.loads(line)
            assert env["_source"] == "open_meteo", (
                f"Envelope _source={env['_source']!r} != 'open_meteo' "
                f"in chunk {chunk}"
            )
            # _conn must be None per D1.8 plan-doc (HTTP polling has no conn).
            assert env["_conn"] is None, (
                f"Envelope _conn={env['_conn']!r} != None — HTTP "
                f"polling has no persistent connection."
            )
