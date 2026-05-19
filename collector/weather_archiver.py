"""Weather bronze archiver — D1.8 (ticket 86ba0duck, 2026-05-18).

First non-WS bronze source. Mirrors the structural shape of
``CoinbaseArchiver`` (D2.2) adapted for HTTP polling:

  - **No persistent connection.** HTTP-poll loop fires one cycle
    per ``poll_once()`` call (driven by ``weather_main_loop`` on a
    60-min cadence). ``_conn=None`` on every envelope (writer.py:245
    emits ``conn=none`` in the partition string for HTTP-polled sources).
  - **4 channels** dispatched per cycle: ``ensemble_gfs`` /
    ``ensemble_ecmwf`` / ``forecast_hrrr`` / ``archive_observed``.
    The archive_observed channel only fires once/day (UTC-day gate)
    for next-day verification of yesterday's settled markets.
  - **Source name** ``open_meteo`` (NOT ``nws_hrrr`` as the D0.3 §1
    slot reservation suggested) — the bot polls Open-Meteo's
    aggregated HRRR via ``OPEN_METEO_FORECAST_URL`` with
    ``models=ncep_hrrr_conus``, NOT NWS direct. Bronze naming
    matches the actual provider.
  - **Envelope via** ``kalshi_wire.ws_client.build_envelope`` per
    D0.3 §2 + the 2026-05-16 §5 AMENDMENT. Single source of truth
    for the 6-field envelope shape.
  - **Non-200 responses are bronze.** Open-Meteo 429 storms get
    written with the diagnostic shape (``http_status`` + ``error``);
    silver D3.x can model quota dynamics + correlate against the
    bot's poll bursts.

Per CLAUDE.md anti-patterns: synchronous (NO asyncio), threading-safe
via the ``BronzeWriter`` contract.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from typing import Dict, Iterable, Mapping, Optional, Sequence

import requests

from kalshi_wire.ws_client import build_envelope

logger = logging.getLogger(__name__)

# Bronze ``_source`` value pinned at module level. Mirrors the
# ``_BRONZE_SOURCE`` constant in coinbase_main_loop.py + bronze
# partition prefix at ``bronze/open_meteo/``.
SOURCE: str = "open_meteo"

# Channels — one BronzeWriter per channel. Silver D3.x dispatches
# parsing on the channel name.
DEFAULT_CHANNELS: tuple[str, ...] = (
    "ensemble_gfs",
    "ensemble_ecmwf",
    "forecast_hrrr",
    "archive_observed",
)

# Open-Meteo endpoints — same as bot/engines/weather_engine.py.
OPEN_METEO_ENSEMBLE_URL: str = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_FORECAST_URL: str = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL: str = "https://archive-api.open-meteo.com/v1/archive"

# Open-Meteo model identifiers per channel.
_CHANNEL_TO_MODEL: Mapping[str, str] = {
    "ensemble_gfs": "gfs_seamless",
    "ensemble_ecmwf": "ecmwf_ifs025",
    "forecast_hrrr": "ncep_hrrr_conus",
}

# Sleep between intra-cycle API calls to stay under Open-Meteo's
# burst rate-limit (~30 req/min). Mirrors the bot's 3.0s spacing
# (weather_engine.py:1004 — tightened after Apr 11 2026 429
# cascade). Configurable so tests can pass 0.0.
DEFAULT_INTER_CITY_SLEEP_SECONDS: float = 3.0

# UTC hour at which archive_observed runs (once/day). 06:00 UTC =
# midnight Pacific + dawn Eastern; yesterday's daily-high has
# definitely settled in every CONUS time zone by then. Pinned as a
# constant so silver D3.x knows when archive_observed records
# anchor.
ARCHIVE_OBSERVED_HOUR_UTC: int = 6

# All 19 cities the bot's weather engine tracks. Mirrors
# ``bot/engines/weather_engine.py::WEATHER_CITIES`` (lat/lon only —
# the collector doesn't need series_ticker or nws_station). Keep
# this list LOCK-STEP with the bot's WEATHER_CITIES: a new city in
# the bot WITHOUT a matching entry here means the collector won't
# capture that city's bronze, and a settled market would have no
# replay corpus. Tracked in ``tests/contracts/test_collector_weather_archiver.py``
# only at presence-of-cities granularity; the bot's list is the
# authoritative source — adding new cities is a coordinated edit.
WEATHER_CITIES: Mapping[str, tuple[float, float]] = {
    "NYC": (40.7128, -74.0060),
    "CHI": (41.8781, -87.6298),
    "MIA": (25.7617, -80.1918),
    "DEN": (39.7392, -104.9903),
    "LAX": (34.0522, -118.2437),
    "AUS": (30.2672, -97.7431),
    "ATL": (33.7490, -84.3880),
    "SFO": (37.7749, -122.4194),
    "DAL": (32.7767, -96.7970),
    "PHX": (33.4484, -112.0740),
    "PHI": (39.9526, -75.1652),
    "MIN": (44.9778, -93.2650),
    "SEA": (47.6062, -122.3321),
    "HOU": (29.7604, -95.3698),
    "BOS": (42.3601, -71.0589),
    "LAS": (36.1699, -115.1398),
    "OKC": (35.4676, -97.5164),
    "DCA": (38.9072, -77.0369),
    "MSY": (29.9511, -90.0715),
}


def _iso_utc_microsecond_z(ts: _dt.datetime) -> str:
    """Format a tz-aware UTC datetime as ISO-8601 with µs + trailing Z.

    Mirrors the format kalshi_wire.build_envelope expects — passing a
    naive datetime would silently corrupt the timestamp; we always
    normalize to UTC.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    else:
        ts = ts.astimezone(_dt.timezone.utc)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class WeatherArchiver:
    """Polls Open-Meteo and writes bronze envelopes.

    One ``WeatherArchiver`` instance is constructed by
    ``weather_main_loop.run`` with a per-channel ``BronzeWriter`` map
    and the static city + channel lists. ``poll_once()`` drives one
    cycle (all configured cities × all configured channels); the
    main loop sleeps between cycles.
    """

    def __init__(
        self,
        *,
        writers_by_channel: Mapping[str, "object"],
        cities: Optional[Sequence[str]] = None,
        channels: Optional[Sequence[str]] = None,
        inter_city_sleep_seconds: float = DEFAULT_INTER_CITY_SLEEP_SECONDS,
        request_timeout_seconds: float = 30.0,
        archive_observed_hour_utc: int = ARCHIVE_OBSERVED_HOUR_UTC,
        now_fn=lambda: _dt.datetime.now(_dt.timezone.utc),
    ) -> None:
        self._writers_by_channel = dict(writers_by_channel)
        self._cities: tuple[str, ...] = tuple(
            cities if cities is not None else WEATHER_CITIES.keys()
        )
        self._channels: tuple[str, ...] = tuple(
            channels if channels is not None else DEFAULT_CHANNELS
        )
        # Sanity-check: every requested channel must have a matching writer.
        for channel in self._channels:
            assert channel in self._writers_by_channel, (
                f"WeatherArchiver: channel {channel!r} requested but "
                f"no writer in writers_by_channel "
                f"(keys: {list(self._writers_by_channel)})"
            )
        # Sanity-check: every requested city must have a lat/lon entry.
        for city in self._cities:
            assert city in WEATHER_CITIES, (
                f"WeatherArchiver: city {city!r} not in WEATHER_CITIES — "
                f"add its lat/lon to weather_archiver.WEATHER_CITIES "
                f"(lock-step with bot/engines/weather_engine.WEATHER_CITIES)."
            )
        self._inter_city_sleep_seconds = inter_city_sleep_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._archive_observed_hour_utc = archive_observed_hour_utc
        self._now_fn = now_fn

        # Monotone per-archiver collector_seq. Resets on process restart
        # per D0.3 §2; silver QA cross-references against a boot record
        # stream (out of scope at D1.8).
        self._collector_seq: int = 0

    # ── Public API ────────────────────────────────────────────────────

    def poll_once(self) -> None:
        """Run one full poll cycle across cities × channels.

        Per-cycle order: forecast channels first (every cycle), then
        archive_observed (once/day on the UTC-hour gate). Inter-call
        sleeps stay below Open-Meteo's burst rate-limit.

        Best-effort posture: per-call exceptions are swallowed +
        WARN-logged so one city's failure doesn't abort the rest of
        the cycle. The 429 storm + connection errors get written as
        bronze records (with ``http_status`` or ``error`` diagnostic
        fields) — diagnostic data on quota dynamics is itself signal.
        """
        now = self._now_fn()
        target_date = now.strftime("%Y-%m-%d")
        yesterday_date = (now - _dt.timedelta(days=1)).strftime("%Y-%m-%d")

        # Forecast channels — every cycle.
        forecast_channels = tuple(
            c for c in self._channels if c != "archive_observed"
        )
        for channel in forecast_channels:
            if channel not in _CHANNEL_TO_MODEL:
                logger.warning(
                    "WeatherArchiver: channel %r has no model mapping; "
                    "skipping. Add to _CHANNEL_TO_MODEL if intentional.",
                    channel,
                )
                continue
            model = _CHANNEL_TO_MODEL[channel]
            url = (
                OPEN_METEO_FORECAST_URL
                if channel == "forecast_hrrr"
                else OPEN_METEO_ENSEMBLE_URL
            )
            for city_code in self._cities:
                self._poll_one(
                    channel=channel,
                    city_code=city_code,
                    url=url,
                    model=model,
                    target_date=target_date,
                )
                if self._inter_city_sleep_seconds > 0:
                    time.sleep(self._inter_city_sleep_seconds)

        # archive_observed — once/day at the UTC-hour gate.
        if "archive_observed" in self._channels:
            if now.hour == self._archive_observed_hour_utc:
                for city_code in self._cities:
                    self._poll_archive_observed(
                        city_code=city_code,
                        target_date=yesterday_date,
                    )
                    if self._inter_city_sleep_seconds > 0:
                        time.sleep(self._inter_city_sleep_seconds)
            else:
                logger.debug(
                    "WeatherArchiver: archive_observed skipped (hour=%d, "
                    "gate=%d).",
                    now.hour, self._archive_observed_hour_utc,
                )

    # ── Internals ─────────────────────────────────────────────────────

    def _poll_one(
        self,
        *,
        channel: str,
        city_code: str,
        url: str,
        model: str,
        target_date: str,
    ) -> None:
        """Fetch one (city, channel) and write an envelope."""
        lat, lon = WEATHER_CITIES[city_code]
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "models": model,
            "temperature_unit": "fahrenheit",
            "start_date": target_date,
            "end_date": target_date,
            "timezone": "America/New_York",
        }
        self._fetch_and_write(
            channel=channel,
            city_code=city_code,
            target_date=target_date,
            model=model,
            url=url,
            params=params,
        )

    def _poll_archive_observed(
        self,
        *,
        city_code: str,
        target_date: str,
    ) -> None:
        """Fetch one city's observed daily high (yesterday's settled value)."""
        lat, lon = WEATHER_CITIES[city_code]
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "start_date": target_date,
            "end_date": target_date,
            "timezone": "America/New_York",
        }
        self._fetch_and_write(
            channel="archive_observed",
            city_code=city_code,
            target_date=target_date,
            model="archive",
            url=OPEN_METEO_ARCHIVE_URL,
            params=params,
        )

    def _fetch_and_write(
        self,
        *,
        channel: str,
        city_code: str,
        target_date: str,
        model: str,
        url: str,
        params: Dict[str, object],
    ) -> None:
        """Fire one HTTP request + write one envelope, regardless of outcome.

        Builds the diagnostic-envelope payload (``city_code`` +
        ``target_date`` + ``model`` + ``http_status`` + ``elapsed_ms`` +
        either ``response`` (on success) or ``error`` (on failure)) and
        wraps it in the 6-field bronze envelope.

        Bronze captures the failure modes too — quota storms, connection
        errors, and parse errors are all signal for silver D3.x analysis.
        """
        t0 = time.time()
        diag: Dict[str, object] = {
            "city_code": city_code,
            "target_date": target_date,
            "model": model,
        }
        try:
            resp = requests.get(
                url, params=params, timeout=self._request_timeout_seconds,
            )
            elapsed_ms = int((time.time() - t0) * 1000)
            diag["http_status"] = resp.status_code
            diag["elapsed_ms"] = elapsed_ms
            if resp.status_code == 200:
                try:
                    diag["response"] = resp.json()
                except (ValueError, json.JSONDecodeError) as exc:
                    diag["error"] = f"json_parse_error: {exc!r}"
            else:
                # Non-200 → still bronze, with a synthetic error field.
                # Try to pull Open-Meteo's reason string if present.
                try:
                    err_payload = resp.json()
                    reason = err_payload.get("reason") if isinstance(
                        err_payload, dict
                    ) else None
                    diag["error"] = (
                        f"http_{resp.status_code}: {reason!r}"
                        if reason else f"http_{resp.status_code}"
                    )
                except (ValueError, json.JSONDecodeError):
                    diag["error"] = f"http_{resp.status_code}"
        except requests.RequestException as exc:
            elapsed_ms = int((time.time() - t0) * 1000)
            diag["http_status"] = None
            diag["elapsed_ms"] = elapsed_ms
            diag["error"] = f"request_exception: {type(exc).__name__}: {exc!r}"

        self._write_envelope(channel=channel, diag=diag)

    def _write_envelope(
        self,
        *,
        channel: str,
        diag: Dict[str, object],
    ) -> None:
        """Wrap the diagnostic payload in the bronze envelope + write."""
        self._collector_seq += 1
        wire_recv_ts = self._now_fn()
        # _raw is a STRING per D0.3 §2.
        raw_str = json.dumps(diag, separators=(",", ":"), ensure_ascii=False)
        envelope = build_envelope(
            raw=raw_str,
            source=SOURCE,
            channel=channel,
            conn=None,
            collector_seq=self._collector_seq,
            wire_recv_ts=wire_recv_ts,
        )
        writer = self._writers_by_channel[channel]
        writer.write(envelope)
