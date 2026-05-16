"""KalshiClient — Kalshi API HTTP client with RSA-PSS auth and rate limiting.

Extracted from `bot/_impl.py` in Sprint 4 Bit 4.3 (2026-05-08). The class
is re-imported into `bot/_impl.py` so `bot.KalshiClient`, `bot._impl.KalshiClient`,
and `bot.kalshi_client.KalshiClient` are all the same class object.

D1.1.5 (ticket 86b9zdhz2, 2026-05-16): RSA-PSS-SHA256 sign + PEM load
DELEGATED to the shared transport library `kalshi_wire/auth.py` per the
2026-05-16 AMENDMENT to ``kb/decisions/data-corpus-architecture.md`` §5.
Pre-D1.1.5 the inline ``padding.PSS(...)`` construction lived here at
lines 60-72; post-D1.1.5 the cryptographic primitive moves to
``kalshi_wire.auth.sign``. Byte-equivalence is pinned by
``tests/contracts/test_kalshi_wire_auth.py::test_sign_parity_with_bot_kalshi_client``.
The class retains all its rate-limiting + circuit-breaker wrapper logic
+ request/response handling — only the cryptographic step delegates.

Imports are deliberate: stdlib + ``requests`` + the constants/breakers
needed to drive class-body decoration and method bodies + the
``kalshi_wire.auth`` delegation surface. The ``cryptography`` library
imports moved to ``kalshi_wire.auth``. Does NOT import ``bot._impl``
(deleted in Bit 9.3-iii.c).
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from datetime import timezone
from typing import Dict, List, Optional

import requests

from bot.constants import (
    API_PATH_PREFIX,
    BASE_URL,
    READ_RATE_LIMIT,
    WRITE_RATE_LIMIT,
)
from bot.helpers.breakers import (
    _breaker_config,
    _kalshi_breaker,
    _kalshi_breaker_success,
    _kalshi_series_key,
)
from bot.infra.circuit_breaker import REGISTRY as _BREAKER_REGISTRY  # Sprint 10.5a (2026-05-11)
from kalshi_wire.auth import load_private_key as _wire_load_private_key
from kalshi_wire.auth import sign as _wire_sign


class KalshiClient:
    """Handles all Kalshi API communication with RSA-PSS auth and rate limiting."""

    def __init__(self, api_key: str, private_key_path: str):
        self.api_key = api_key
        self.private_key = self._load_private_key(private_key_path)
        self.session = requests.Session()
        self._read_timestamps: List[float] = []
        self._write_timestamps: List[float] = []
        self._rate_lock = threading.Lock()

    # ── Auth (D1.1.5: delegates to kalshi_wire.auth) ──────────────────────

    @staticmethod
    def _load_private_key(key_path: str):
        """Load a PEM-encoded private key (delegates to ``kalshi_wire.auth``)."""
        return _wire_load_private_key(key_path)

    def _create_signature(self, timestamp_ms: str, method: str, path: str) -> str:
        """Sign timestamp_ms + METHOD + path (without query params) using RSA-PSS.

        D1.1.5: delegates to ``kalshi_wire.auth.sign``. The wire library
        owns the byte-level RSA-PSS-SHA256 construction; this method
        retains its public signature for compatibility with the existing
        callers in ``_request``.
        """
        return _wire_sign(self.private_key, timestamp_ms, method, path)

    # ── Rate Limiting ─────────────────────────────────────────────────────

    def _rate_limit_wait(self, is_write: bool):
        with self._rate_lock:
            now = time.time()
            timestamps = self._write_timestamps if is_write else self._read_timestamps
            limit = WRITE_RATE_LIMIT if is_write else READ_RATE_LIMIT

            # Purge timestamps older than 1 second
            cutoff = now - 1.0
            while timestamps and timestamps[0] < cutoff:
                timestamps.pop(0)

            if len(timestamps) >= limit:
                sleep_time = timestamps[0] + 1.0 - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                # Purge again after sleeping
                now = time.time()
                cutoff = now - 1.0
                while timestamps and timestamps[0] < cutoff:
                    timestamps.pop(0)

            timestamps.append(time.time())

    # ── Core Request ──────────────────────────────────────────────────────

    def _request(self, method: str, path: str,
                 params: Optional[Dict] = None,
                 json_body: Optional[Dict] = None) -> Optional[Dict]:
        """
        Execute an authenticated request. Path must start with /trade-api/v2.
        Returns parsed JSON or None on failure. Never raises.
        """
        is_write = method in ("POST", "PUT", "DELETE")
        self._rate_limit_wait(is_write)

        timestamp_ms = str(int(time.time() * 1000))
        url = f"{BASE_URL}{path}"
        signature = self._create_signature(timestamp_ms, method, path)

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
        }

        try:
            resp = self.session.request(
                method, url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=10,
            )
            # Clock drift detection from server Date header
            server_date = resp.headers.get("Date")
            if server_date:
                try:
                    from email.utils import parsedate_to_datetime
                    server_time = parsedate_to_datetime(server_date)
                    drift = abs((datetime.datetime.now(timezone.utc) - server_time).total_seconds())
                    if drift > 2.0:
                        logging.warning(f"clock_drift_detected: {drift:.1f}s vs server")
                except Exception:
                    pass

            if resp.status_code == 429:
                if method == "POST" and "/orders" in path:
                    logging.error(f"Rate limited on POST {path} — NOT retrying to prevent duplicate orders")
                    return None
                retry_after = float(resp.headers.get("Retry-After", "1"))
                retries = getattr(self, '_429_retries', 0) + 1
                if retries > 3:
                    logging.error(f"Rate limited {retries} times, giving up: {method} {path}")
                    self._429_retries = 0
                    return None
                self._429_retries = retries
                logging.warning(f"Rate limited, sleeping {retry_after}s (attempt {retries}/3)")
                time.sleep(retry_after)
                result = self._request(method, path, params, json_body)
                self._429_retries = 0
                return result
            # Idempotent-DELETE 404: Kalshi has already expired/canceled
            # the resource. Return a sentinel so callers can distinguish
            # "gone" (success-equivalent) from None (transient → retry).
            # Other methods' 404s preserve current None semantics.
            # See kb/failures/cancel-404-asset-lockout-may04.md
            if resp.status_code == 404 and method == "DELETE":
                return {"_error": True, "_status_code": 404}
            if resp.status_code >= 400:
                body_text = resp.text[:500] if resp.text else "(empty)"
                logging.error(f"API error: {method} {path} -> {resp.status_code} body={body_text}")
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.exceptions.RequestException as e:
            logging.error(f"API error: {method} {path} -> {e}")
            return None

    # ── Public API Methods ────────────────────────────────────────────────

    @_kalshi_breaker
    @_breaker_config(key_fn=lambda self: "kalshi_balance",
                     recovery_seconds=300)
    def get_balance(self) -> Optional[Dict]:
        return self._request("GET", f"{API_PATH_PREFIX}/portfolio/balance")

    @_kalshi_breaker
    @_breaker_config(
        key_fn=lambda self, series_ticker=None, **_:
            f"kalshi_markets_{series_ticker or 'all'}",
        recovery_seconds=300)
    def get_markets(self, series_ticker: Optional[str] = None,
                    status: Optional[str] = None,
                    min_close_ts: Optional[int] = None,
                    max_close_ts: Optional[int] = None,
                    cursor: Optional[str] = None,
                    limit: int = 200) -> Optional[Dict]:
        params: Dict = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            params["max_close_ts"] = max_close_ts
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"{API_PATH_PREFIX}/markets", params=params)

    @_kalshi_breaker
    @_breaker_config(
        # Per-(series, status) breaker keys. Apr 25 11:13 UTC
        # discovery: consecutive-failure semantics + sports
        # discovery's 2-call pattern (status=open succeeds,
        # status=active fails per series) caused failures to
        # never accumulate — open's success kept resetting
        # active's counter. Independent keys per (series, status)
        # let active's breaker trip after 3 consecutive failures.
        # Sliding-window CB semantics (planned step 11) would be a
        # more general fix.
        key_fn=lambda self, series_ticker=None, status=None, **_:
            f"kalshi_events_{series_ticker or 'all'}_"
            f"{status or 'any'}",
        recovery_seconds=300)
    def get_events(self, series_ticker: Optional[str] = None,
                   status: Optional[str] = None,
                   with_nested_markets: bool = False,
                   limit: int = 100) -> Optional[Dict]:
        params: Dict = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        return self._request("GET", f"{API_PATH_PREFIX}/events", params=params)

    @_kalshi_breaker
    @_breaker_config(
        key_fn=lambda self, ticker: _kalshi_series_key(ticker, "market"),
        recovery_seconds=120)
    def get_market(self, ticker: str) -> Optional[Dict]:
        return self._request("GET", f"{API_PATH_PREFIX}/markets/{ticker}")

    @_kalshi_breaker
    @_breaker_config(
        key_fn=lambda self, ticker, depth=10:
            _kalshi_series_key(ticker, "orderbook"),
        recovery_seconds=120)
    def get_orderbook(self, ticker: str, depth: int = 10) -> Optional[Dict]:
        return self._request(
            "GET", f"{API_PATH_PREFIX}/markets/{ticker}/orderbook",
            params={"depth": depth},
        )

    def place_order(self, ticker: str, side: str, action: str, count: int,
                    yes_price: Optional[int] = None,
                    no_price: Optional[int] = None,
                    client_order_id: Optional[str] = None,
                    post_only: Optional[bool] = None,
                    time_in_force: Optional[str] = None) -> Optional[Dict]:
        body: Dict = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if client_order_id:
            body["client_order_id"] = client_order_id
        if post_only is not None:
            body["post_only"] = post_only
        if time_in_force is not None:
            body["time_in_force"] = time_in_force
        return self._request("POST", f"{API_PATH_PREFIX}/portfolio/orders",
                             json_body=body)

    def cancel_order(self, order_id: str) -> Optional[Dict]:
        return self._request("DELETE",
                             f"{API_PATH_PREFIX}/portfolio/orders/{order_id}")

    def amend_order(self, order_id: str, ticker: str, side: str, action: str,
                    count: Optional[int] = None,
                    yes_price: Optional[int] = None,
                    no_price: Optional[int] = None) -> Optional[Dict]:
        """Amend an existing order in-place (price/count). Saves cancel+re-place."""
        body: Dict = {"ticker": ticker, "side": side, "action": action}
        if count is not None:
            body["count"] = count
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        return self._request("POST",
                             f"{API_PATH_PREFIX}/portfolio/orders/{order_id}/amend",
                             json_body=body)

    def get_queue_position(self, order_id: str) -> Optional[int]:
        """Get queue position for a resting order. Returns position or None.

        Manually wrapped (not via decorator) because of post-processing —
        the breaker needs to see the raw response to classify success/
        failure, but the public return type is the unwrapped int.

        Round-3 audit: the only caller (OrderExecutor maker polling
        loop, ~line 15872) reads `qpos = get_queue_position(...)`
        and only updates `order["queue_position"]` when `qpos is not
        None`. OPEN-breaker returning None has identical semantics
        to a transient API failure: the polling loop skips this
        update and tries again next 5s tick. No semantic mismatch
        verified.

        Recovery 60s tracks maker latency budget — short enough that
        a transient Kalshi blip doesn't lock us out for too long."""
        breaker = _BREAKER_REGISTRY.get(
            "kalshi_queue_position",
            failures_to_open=3, recovery_seconds=60)
        gen = breaker.acquire()
        if gen is None:
            return None
        try:
            resp = self._request(
                "GET",
                f"{API_PATH_PREFIX}/portfolio/orders/{order_id}/queue_position")
        except Exception:
            breaker.record_result(gen, success=False)
            raise
        breaker.record_result(gen, success=_kalshi_breaker_success(resp))
        if resp is None:
            return None
        return resp.get("queue_position")

    @_kalshi_breaker
    @_breaker_config(key_fn=lambda self, **_: "kalshi_orders",
                     recovery_seconds=120)
    def get_orders(self, ticker: Optional[str] = None,
                   status: Optional[str] = None) -> Optional[Dict]:
        params: Dict = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return self._request("GET", f"{API_PATH_PREFIX}/portfolio/orders",
                             params=params)

    @_kalshi_breaker
    @_breaker_config(key_fn=lambda self, **_: "kalshi_fills",
                     recovery_seconds=120)
    def get_fills(self, ticker: Optional[str] = None,
                  min_ts: Optional[int] = None,
                  limit: int = 200) -> Optional[Dict]:
        params: Dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = min_ts
        return self._request("GET", f"{API_PATH_PREFIX}/portfolio/fills",
                             params=params)

    @_kalshi_breaker
    @_breaker_config(
        key_fn=lambda self, ticker=None, **_:
            f"kalshi_settlements_{ticker or 'all'}",
        recovery_seconds=300)
    def get_settlements(self, ticker: Optional[str] = None,
                        min_ts: Optional[int] = None,
                        limit: int = 200) -> Optional[Dict]:
        params: Dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = min_ts
        return self._request("GET", f"{API_PATH_PREFIX}/portfolio/settlements",
                             params=params)

    @_kalshi_breaker
    @_breaker_config(
        key_fn=lambda self, event_ticker=None, **_:
            f"kalshi_positions_{event_ticker or 'all'}",
        recovery_seconds=120)
    def get_positions(self, event_ticker: Optional[str] = None) -> Optional[Dict]:
        # Round-2 A1 fix: was previously unwrapped — same failure
        # class as everything else here.
        params: Dict = {}
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._request("GET", f"{API_PATH_PREFIX}/portfolio/positions",
                             params=params)
