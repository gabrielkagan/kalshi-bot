"""CoinGlassFetcher — CoinGlass funding-rate poller.

Extracted from bot/_impl.py in Sprint 4 Bit 4.4 (2026-05-08). Daemon
thread that fetches the average funding rate across exchanges from
the CoinGlass v3 API for every symbol in ``config.ASSETS``
(BTC/ETH/SOL/XRP/HYPE/DOGE post-T1 2026-05-10) every
``COINGLASS_FETCH_INTERVAL`` seconds (10 min by default — 100
calls/day budget). Cache stales after ``COINGLASS_CACHE_TTL``.

Disabled when ``COINGLASS_API_KEY`` env var is unset (start() short-circuits
with a one-line info log).

Imports are deliberate: stdlib + ``requests`` + the constants needed
to drive the fetch loop. Does NOT import ``bot._impl`` (would create
a circular import).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Optional

import requests

from bot.constants import (
    COINGLASS_API_URL,
    COINGLASS_CACHE_TTL,
    COINGLASS_FETCH_INTERVAL,
    COINGLASS_REQUEST_TIMEOUT,
    COINGLASS_SYMBOLS,
)


class CoinGlassFetcher:
    """Daemon thread that fetches funding rates from CoinGlass API."""

    def __init__(self):
        self._api_key = os.environ.get("COINGLASS_API_KEY", "")
        self._cache: Dict[str, Dict] = {}  # asset -> {"funding_rate": float, "fetch_time": float}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not self._api_key:
            logging.info("COINGLASS_API_KEY not set — CoinGlass funding rates disabled")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_funding_rate(self, asset: str) -> Optional[float]:
        """Return cached average funding rate, or None if stale/missing."""
        with self._lock:
            entry = self._cache.get(asset)
        if entry is None:
            return None
        if time.time() - entry["fetch_time"] > COINGLASS_CACHE_TTL:
            return None
        return entry["funding_rate"]

    def _run(self):
        while not self._stop.is_set():
            for asset, symbol in COINGLASS_SYMBOLS.items():
                try:
                    rate = self._fetch_funding(symbol)
                    if rate is not None:
                        with self._lock:
                            self._cache[asset] = {
                                "funding_rate": rate,
                                "fetch_time": time.time(),
                            }
                except Exception:
                    logging.debug(f"CoinGlass fetch failed for {symbol}", exc_info=True)
            self._stop.wait(timeout=COINGLASS_FETCH_INTERVAL)

    def _fetch_funding(self, symbol: str) -> Optional[float]:
        """Fetch current funding rates from CoinGlass, return average across exchanges."""
        url = f"{COINGLASS_API_URL}/futures/funding/current"
        headers = {"CG-API-KEY": self._api_key}
        resp = requests.get(
            url, params={"symbol": symbol}, headers=headers,
            timeout=COINGLASS_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        data_list = body.get("data", [])
        if not data_list:
            return None
        rates = []
        for entry in data_list:
            rate = entry.get("rate")
            if rate is not None:
                try:
                    rates.append(float(rate))
                except (ValueError, TypeError):
                    pass
        if not rates:
            return None
        return sum(rates) / len(rates)
