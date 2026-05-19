"""D1.8 — Open-Meteo 429 storms are captured as bronze records.

Ticket `86ba0duck` (2026-05-18). The bot's existing weather poller has
a 429 backoff mechanism (weather_engine.py:329-335). The collector
does the same UPLOAD-FACING behavior — when Open-Meteo returns 429,
the diagnostic envelope still gets written (with ``http_status=429``
and an ``error`` field). The 429 storm is itself bronze data — D3.x
silver can model quota dynamics + correlate against bot's poll bursts.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


pytestmark = pytest.mark.integration


def _make_mock_429():
    mock = MagicMock()
    mock.status_code = 429
    mock.json.return_value = {"reason": "rate limited"}
    return mock


@pytest.fixture
def bronze_root(tmp_path):
    root = tmp_path / "bronze"
    root.mkdir()
    return root


def test_429_response_writes_diagnostic_envelope(bronze_root):
    """When Open-Meteo returns 429 for one of the model fetches, the
    archiver writes an envelope with ``http_status=429`` to the matching
    channel's writer. The 429 itself is bronze data (D1.8 plan-doc
    'Bronze record shape' worked example #2).
    """
    from collector.weather_archiver import WeatherArchiver
    from collector.writer import BronzeWriter
    import zstandard

    writers = {
        "ensemble_gfs": BronzeWriter(
            root_dir=bronze_root,
            source="open_meteo",
            channel="ensemble_gfs",
            conn=None,
            interval_seconds=3600,
        ),
    }
    archiver = WeatherArchiver(
        writers_by_channel=writers,
        cities=["NYC"],
        channels=("ensemble_gfs",),
        inter_city_sleep_seconds=0.0,
    )

    with patch("collector.weather_archiver.requests.get") as mock_get:
        mock_get.return_value = _make_mock_429()
        archiver.poll_once()
    writers["ensemble_gfs"].close()

    chunks = list(bronze_root.rglob("outbox/*.jsonl.zst"))
    assert chunks, "no chunk written for 429 — bronze must capture the storm"

    dctx = zstandard.ZstdDecompressor()
    found_429 = False
    for chunk in chunks:
        decompressed = dctx.decompress(chunk.read_bytes())
        for line in decompressed.decode("utf-8").splitlines():
            env = json.loads(line)
            raw = json.loads(env["_raw"])
            if raw.get("http_status") == 429:
                found_429 = True
                # Either an `error` field OR a falsy/absent `response` is
                # acceptable — the load-bearing pin is http_status=429.
                break
        if found_429:
            break

    assert found_429, (
        "No envelope with http_status=429 found in bronze. The 429 "
        "storm itself is bronze data — silver D3.x models quota "
        "dynamics from these records."
    )
