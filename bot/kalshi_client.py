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
from bot.helpers.strings import (
    cents_to_dollars_str,
    fp_str_to_int,
    int_to_fp_str,
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

# Transport bounds (kb/failures/scan-body-5-8s-collecting-mode-sep06.md).
# Scalar timeout=10 let connect+read each run 10s; 429 Retry-After was
# slept verbatim and retried without a wall-clock cap.
REST_CONNECT_TIMEOUT_S = 3.0
REST_READ_TIMEOUT_S = 7.0
# POST /orders: a false timeout returns None and the bot abandons the
# order with no order_id to reconcile. Keep the pre-PR 10s read bound
# on writes. Reads stay (3, 7).
REST_WRITE_READ_TIMEOUT_S = 10.0
REST_429_MAX_SLEEP_S = 5.0
REST_429_MAX_RETRIES = 3
REST_429_WALL_CLOCK_CAP_S = 8.0
from bot.trading_mode import asset_from_ticker as _tm_asset_from_ticker, is_live as _tm_is_live, strategy_is_live as _tm_strategy_is_live, strategy_from_client_order_id as _tm_strategy_from_coid  # modular live/shadow backstop

# Create-order V2 (2026-09-06): POST /portfolio/orders returns 410
# deprecated_v1_order_endpoint even under /trade-api/v2. New path is
# /portfolio/events/orders; side is bid/ask on the YES book; price and
# count are fixed-point strings. Executor callers keep yes/no + cents.
_CREATE_ORDER_V2_PATH = f"{API_PATH_PREFIX}/portfolio/events/orders"
_V2_STP_DEFAULT = "taker_at_cross"


def _v2_book_side_and_price(
    side: str,
    action: str,
    yes_price: Optional[int],
    no_price: Optional[int],
) -> Optional[tuple]:
    """Map (yes/no, buy, cents) to (bid/ask, dollar-str). None = unmapped."""
    if (action or "").lower() != "buy":
        return None
    s = (side or "").lower()
    if s == "yes":
        if yes_price is None:
            return None
        px = int(yes_price)
        if not (0 < px < 100):
            return None
        return ("bid", cents_to_dollars_str(px))
    if s == "no":
        if no_price is None:
            return None
        yes_equiv = 100 - int(no_price)
        if not (0 < yes_equiv < 100):
            return None
        return ("ask", cents_to_dollars_str(yes_equiv))
    return None


def _wrap_v2_create_order_response(raw: Optional[Dict]) -> Optional[Dict]:
    """Keep executor's {order: {order_id, fill_count_fp, remaining_count}} shape."""
    if not raw or not isinstance(raw, dict):
        return raw
    if raw.get("_status_code") == 404:
        return raw
    if "order" in raw:
        return raw
    oid = raw.get("order_id")
    if not oid:
        return raw
    fill = raw.get("fill_count")
    remaining = raw.get("remaining_count")
    wrapped = {
        "order_id": oid,
        "client_order_id": raw.get("client_order_id"),
        "fill_count_fp": fill,
        "remaining_count_fp": remaining,
    }
    if fill is not None:
        wrapped["fill_count"] = fp_str_to_int(fill)
    if remaining is not None:
        wrapped["remaining_count"] = fp_str_to_int(remaining)
    return {"order": wrapped}


def _wrap_v2_cancel_order_response(raw: Optional[Dict]) -> Optional[Dict]:
    """V2 cancel is flat {order_id, reduced_by}. Do not invent fill_count."""
    if not raw or not isinstance(raw, dict):
        return raw
    if raw.get("_status_code") == 404:
        return raw
    if "order" in raw:
        return raw
    oid = raw.get("order_id")
    if not oid:
        return raw
    reduced = raw.get("reduced_by")
    wrapped = {
        "order_id": oid,
        "client_order_id": raw.get("client_order_id"),
        "reduced_by_fp": reduced,
        "remaining_count_fp": reduced,
    }
    if reduced is not None:
        wrapped["remaining_count"] = fp_str_to_int(reduced)
    return {"order": wrapped}


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
                 json_body: Optional[Dict] = None,
                 *,
                 _429_retries: int = 0,
                 _429_t0: Optional[float] = None) -> Optional[Dict]:
        """
        Execute an authenticated request. Path must start with /trade-api/v2.
        Returns parsed JSON or None on failure. Never raises.

        429 retry budget is per-call (kwargs), not instance state. Settlement
        and the scan thread share this client; a shared counter + finally:0
        let concurrent GETs reopen each other's budget.

        REST_429_WALL_CLOCK_CAP_S is a remaining-time budget: sleep is
        min(Retry-After, 5s, time left), and we give up when remaining <= 0
        so total backoff cannot overshoot the cap by a full sleep.
        """
        if _429_t0 is None:
            _429_t0 = time.monotonic()
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
            read_s = REST_WRITE_READ_TIMEOUT_S if is_write else REST_READ_TIMEOUT_S
            resp = self.session.request(
                method, url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=(REST_CONNECT_TIMEOUT_S, read_s),
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
                try:
                    retry_after = float(resp.headers.get("Retry-After", "1"))
                except (TypeError, ValueError):
                    retry_after = 1.0
                if retry_after < 0:
                    retry_after = 1.0
                now_m = time.monotonic()
                retries = _429_retries + 1
                elapsed = now_m - _429_t0
                remaining = REST_429_WALL_CLOCK_CAP_S - elapsed
                if retries > REST_429_MAX_RETRIES or remaining <= 0:
                    logging.error(
                        "Rate limited %s times (elapsed=%.1fs), giving up: %s %s",
                        retries, elapsed, method, path)
                    return None
                sleep_s = min(retry_after, REST_429_MAX_SLEEP_S, remaining)
                logging.warning(
                    "Rate limited, sleeping %.1fs (attempt %d/%d, Retry-After=%s)",
                    sleep_s, retries, REST_429_MAX_RETRIES, retry_after)
                time.sleep(sleep_s)
                return self._request(
                    method, path, params, json_body,
                    _429_retries=retries, _429_t0=_429_t0,
                )
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
        # ── Trading-mode hard backstop (modular live/shadow) ──────────────
        # Backstops ALL order PLACEMENT at the API boundary: every placement path
        # (initial maker, taker escalation, any caller) reaches the Kalshi API
        # here, so paths that don't re-enter executor.execute() (e.g. mid-flight
        # taker escalation) are still caught. If this ticker is a governed
        # crypto-15M market whose asset is in shadow, refuse to place. Sibling
        # order-WORKING call amend_order is gated identically below; cancel_order
        # is intentionally ungated (reduces exposure). Together that makes a
        # runtime flag-flip a kill-switch even for a resting order on an open
        # position. Non-crypto tickers (asset_from_ticker→None) untouched.
        # Strategy-aware form (longshot R1-M4, generalized at Bit T-1):
        # recovers the engine-owned strategy from the client_order_id
        # prefix (the only strategy signal at this API boundary) via the
        # single-sourced bot.constants.ENGINE_OWNED_OID_PREFIX_TO_STRATEGY
        # map ('ls-' -> longshot, 'tw-' -> twaplock) so each engine's
        # *_LIVE_OVERRIDE flag can pass that ONE strategy's orders while
        # everything else keeps plain is_live semantics. Mirrors the
        # executor.execute() chokepoint exactly (the two must never
        # disagree, else override-mode placements die here).
        _tm_asset = _tm_asset_from_ticker(ticker)
        if _tm_asset is not None and not _tm_strategy_is_live(
                _tm_strategy_from_coid(client_order_id), _tm_asset):
            logging.warning(
                "SHADOW_BLOCK: %s %s %s count=%s — trading-mode shadow, no order placed",
                ticker, side, action, count)
            return None
        mapped = _v2_book_side_and_price(side, action, yes_price, no_price)
        if mapped is None:
            logging.error(
                "PLACE_ORDER_V2_UNMAPPED: %s side=%s action=%s "
                "yes_price=%s no_price=%s — not posting deprecated "
                "/portfolio/orders", ticker, side, action, yes_price, no_price)
            return None
        book_side, price_str = mapped
        body: Dict = {
            "ticker": ticker,
            "side": book_side,
            "count": int_to_fp_str(int(count)),
            "price": price_str,
            "time_in_force": time_in_force or "good_till_canceled",
            "self_trade_prevention_type": _V2_STP_DEFAULT,
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        if post_only is not None:
            body["post_only"] = post_only
        raw = self._request("POST", _CREATE_ORDER_V2_PATH, json_body=body)
        return _wrap_v2_create_order_response(raw)

    def cancel_order(self, order_id: str, ticker: Optional[str] = None) -> Optional[Dict]:
        params: Optional[Dict] = None
        if ticker:
            params = {"market_ticker": ticker, "exchange_index": -1}
        raw = self._request(
            "DELETE",
            f"{API_PATH_PREFIX}/portfolio/events/orders/{order_id}",
            params=params,
        )
        return _wrap_v2_cancel_order_response(raw)

    def amend_order(self, order_id: str, ticker: str, side: str, action: str,
                    count: Optional[int] = None,
                    yes_price: Optional[int] = None,
                    no_price: Optional[int] = None) -> Optional[Dict]:
        """Amend an existing order in-place (price/count). Saves cancel+re-place."""
        # Trading-mode backstop (same as place_order): amend is an order-WORKING
        # API call — gating it makes the kill-switch hold even for a resting maker
        # on an asset flipped to shadow mid-flight. Scoped to governed crypto-15M
        # tickers; non-crypto untouched. (cancel_order is intentionally ungated —
        # it only REDUCES exposure.)
        _tm_asset = _tm_asset_from_ticker(ticker)
        if _tm_asset is not None and not _tm_is_live(_tm_asset):
            logging.warning(
                "SHADOW_BLOCK: amend %s %s %s — trading-mode shadow, not amended",
                ticker, side, action)
            return None
        mapped = _v2_book_side_and_price(side, action, yes_price, no_price)
        if mapped is None or count is None:
            logging.error(
                "AMEND_ORDER_V2_UNMAPPED: %s side=%s action=%s "
                "yes_price=%s no_price=%s count=%s — not posting "
                "deprecated /portfolio/orders amend",
                ticker, side, action, yes_price, no_price, count)
            return None
        book_side, price_str = mapped
        body: Dict = {
            "ticker": ticker,
            "side": book_side,
            "price": price_str,
            "count": int_to_fp_str(int(count)),
        }
        raw = self._request(
            "POST",
            f"{API_PATH_PREFIX}/portfolio/events/orders/{order_id}/amend",
            json_body=body,
        )
        return _wrap_v2_create_order_response(raw)

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
                  limit: int = 200,
                  cursor: Optional[str] = None) -> Optional[Dict]:
        params: Dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = min_ts
        if cursor:
            params["cursor"] = cursor
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
