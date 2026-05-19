"""D1.8 — envelope shape differential pin.

Ticket `86ba0duck` (2026-05-18). The WeatherArchiver's envelope-build
output must pass the same invariants as ``kalshi_wire.build_envelope``:
  - 6 fields: ``_wire_recv_ts`` + ``_source`` + ``_conn`` + ``_channel``
    + ``_collector_seq`` + ``_raw``
  - ``_wire_recv_ts`` is ISO-8601 UTC with microsecond precision +
    trailing ``Z``
  - ``_raw`` is a STRING (not a parsed dict — D0.3 §2)

Since the WeatherArchiver routes through ``kalshi_wire.build_envelope``
(pinned in test_collector_weather_archiver.py), this test is the
RUNTIME complement — actually invoking the archiver against a mock
response and confirming the resulting envelope satisfies invariants.

This is a CONTRACT-tier test (NOT equivalence-tier) because we're pinning
the envelope's STRUCTURAL invariants, not a numeric snapshot. Equivalence
tier is reserved for engine numeric snapshots (Pillar 3 isolation;
human-review-only regen). Envelopes are deterministic given inputs.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


pytestmark = pytest.mark.integration


@pytest.fixture
def bronze_root(tmp_path):
    root = tmp_path / "bronze"
    root.mkdir()
    return root


def _make_mock_200():
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {
        "daily": {
            "time": ["2026-05-19"],
            "temperature_2m_max_member01": [72.5],
        }
    }
    return mock


def test_envelope_has_six_required_fields(bronze_root):
    """Every envelope written carries exactly the 6 D0.3 §2 fields."""
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
        mock_get.return_value = _make_mock_200()
        archiver.poll_once()
    writers["ensemble_gfs"].close()

    dctx = zstandard.ZstdDecompressor()
    required_fields = {
        "_wire_recv_ts",
        "_source",
        "_conn",
        "_channel",
        "_collector_seq",
        "_raw",
    }
    chunks = list(bronze_root.rglob("outbox/*.jsonl.zst"))
    assert chunks, "no chunk written"
    for chunk in chunks:
        decompressed = dctx.decompress(chunk.read_bytes())
        for line in decompressed.decode("utf-8").splitlines():
            env = json.loads(line)
            assert set(env.keys()) == required_fields, (
                f"Envelope keys {set(env.keys())} != "
                f"required {required_fields}"
            )


def test_envelope_wire_recv_ts_iso_microsecond_with_z(bronze_root):
    """``_wire_recv_ts`` is ISO-8601 UTC + microsecond precision +
    trailing ``Z``. The format matches the kalshi_wire.build_envelope
    contract: ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""
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
        mock_get.return_value = _make_mock_200()
        archiver.poll_once()
    writers["ensemble_gfs"].close()

    ISO_RE = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
    )
    dctx = zstandard.ZstdDecompressor()
    chunks = list(bronze_root.rglob("outbox/*.jsonl.zst"))
    assert chunks
    for chunk in chunks:
        decompressed = dctx.decompress(chunk.read_bytes())
        for line in decompressed.decode("utf-8").splitlines():
            env = json.loads(line)
            assert ISO_RE.match(env["_wire_recv_ts"]), (
                f"_wire_recv_ts {env['_wire_recv_ts']!r} does not match "
                f"ISO-8601-UTC-µs-Z format YYYY-MM-DDTHH:MM:SS.ffffffZ"
            )


def test_envelope_raw_is_a_string(bronze_root):
    """``_raw`` is a JSON STRING (NOT a parsed dict) per D0.3 §2.

    Pin via type check — a regression that emits ``_raw`` as a dict
    would defeat the 'bronze captures what the wire actually said'
    invariant and break the silver D3.x parser dispatch.
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
        mock_get.return_value = _make_mock_200()
        archiver.poll_once()
    writers["ensemble_gfs"].close()

    dctx = zstandard.ZstdDecompressor()
    chunks = list(bronze_root.rglob("outbox/*.jsonl.zst"))
    assert chunks
    for chunk in chunks:
        decompressed = dctx.decompress(chunk.read_bytes())
        for line in decompressed.decode("utf-8").splitlines():
            env = json.loads(line)
            assert isinstance(env["_raw"], str), (
                f"_raw is {type(env['_raw']).__name__}, not str. D0.3 "
                f"§2 invariant: bronze captures _raw as a STRING; "
                f"parsing happens at silver, not bronze."
            )
            # Confirm the string is itself valid JSON (the worked-example
            # envelope from the plan-doc).
            parsed = json.loads(env["_raw"])
            assert isinstance(parsed, dict)
