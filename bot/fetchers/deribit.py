"""DeribitDVOLFetcher — Deribit DVOL index poller.

Extracted from bot/_impl.py in Sprint 4 Bit 4.4 (2026-05-08). Daemon
thread that fetches the Deribit DVOL implied-volatility index for BTC
and ETH every ``DVOL_FETCH_INTERVAL`` seconds, caches the result with a
``DVOL_CACHE_TTL`` staleness guard, and maintains a ``DVOL_HOURLY_AVG_MAXLEN``
rolling window for the hourly-average getter.

Imports are deliberate: stdlib + ``requests`` + the constants needed
to drive the fetch loop. Does NOT import ``bot._impl`` (would create
a circular import).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Dict, Optional, Tuple

import requests

from bot.constants import (
    DERIBIT_DVOL_CURRENCIES,
    DERIBIT_DVOL_URL,
    DVOL_CACHE_TTL,
    DVOL_FETCH_INTERVAL,
    DVOL_HOURLY_AVG_MAXLEN,
    DVOL_HOURLY_AVG_MIN,
    DVOL_REQUEST_TIMEOUT,
)
from config import DVOL_ANNUALIZED_TO_5S


class DeribitDVOLFetcher:
    """Daemon thread that fetches Deribit DVOL index for BTC/ETH."""

    def __init__(self):
        self._cache: Dict[str, Tuple[float, float]] = {}   # asset → (dvol_5s, fetch_time)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hourly_dvol: Dict[str, deque] = {
            a: deque(maxlen=DVOL_HOURLY_AVG_MAXLEN) for a in DERIBIT_DVOL_CURRENCIES
        }

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_dvol(self, asset: str) -> Optional[float]:
        """Return cached DVOL in per-5-second scale, or None if stale/missing."""
        with self._lock:
            entry = self._cache.get(asset)
        if entry is None:
            return None
        dvol_5s, fetch_time = entry
        if time.time() - fetch_time > DVOL_CACHE_TTL:
            return None
        return dvol_5s

    def get_dvol_hourly_avg(self, asset: str) -> Optional[float]:
        """Return 1h rolling average of DVOL (per-5s scale), or None if insufficient data."""
        with self._lock:
            buf = self._hourly_dvol.get(asset)
            if buf is None or len(buf) < DVOL_HOURLY_AVG_MIN:
                return None
            return sum(buf) / len(buf)

    def _run(self):
        while not self._stop.is_set():
            for currency_key, currency in DERIBIT_DVOL_CURRENCIES.items():
                try:
                    dvol = self._fetch_latest_dvol(currency)
                    if dvol is not None:
                        dvol_5s = dvol * DVOL_ANNUALIZED_TO_5S
                        with self._lock:
                            self._cache[currency_key] = (dvol_5s, time.time())
                            self._hourly_dvol[currency_key].append(dvol_5s)
                except Exception:
                    logging.debug(f"DVOL fetch failed for {currency}", exc_info=True)
            self._stop.wait(timeout=DVOL_FETCH_INTERVAL)

    def _fetch_latest_dvol(self, currency: str) -> Optional[float]:
        """Fetch latest DVOL from Deribit. Returns annualized vol as decimal (0.57 = 57%)."""
        now_ms = int(time.time() * 1000)
        one_hour_ago_ms = now_ms - 3600 * 1000
        params = {
            "currency": currency,
            "start_timestamp": one_hour_ago_ms,
            "end_timestamp": now_ms,
            "resolution": 1,
        }
        resp = requests.get(DERIBIT_DVOL_URL, params=params, timeout=DVOL_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", {})
        candles = result.get("data", [])
        if not candles:
            return None
        # Each candle: [timestamp, open, high, low, close]
        last_candle = candles[-1]
        close_dvol = last_candle[4]   # close value
        return close_dvol / 100.0     # percentage → decimal
