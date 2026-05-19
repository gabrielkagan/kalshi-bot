"""Kalshi REST snapshot — D1.4 (ticket 86b9ypn8r, 2026-05-16).

D1.3 (`86b9ypn72`) shipped first-bronze-flow but left the ticker map
on a file-based seam (``COLLECTOR_TICKERS_FILE``). D1.4 closes that
gap by pulling the live universe of open markets from Kalshi's REST
``/markets`` endpoint and re-planning subscriptions on a periodic
cadence.

Design (per D0.3 §0 architectural principle + the D1.4 pickup-prompt):

- **Single tier, all markets.** Tier classification is intentionally
  shallow until a measurement-driven split is justified. D0.2's
  scope-map decided all 59,904 non-MVE markets are in-scope; the
  simplest correct shape is one tier (``TIER_ALL = "1"``) containing
  every ``status=open`` market the REST endpoint returns. A future
  Bit can shard by volume / activity heuristics without changing
  this module's public surface — it would just split the dict.
- **Hourly refresh by default.** New markets surface every ~5 minutes
  during the trading day; hourly is the cost-vs-freshness tradeoff
  (Kalshi REST rate limit + bronze-side cost of force-reconnecting
  every WS conn). Tunable via ``RestSnapshotRefresher(interval_seconds=...)``
  or ``COLLECTOR_REST_REFRESH_SECONDS`` env in main_loop.
- **Auth via kalshi_wire.auth.** Post-D1.1.5 AMENDMENT, all RSA-PSS
  signing lives in ``kalshi_wire/auth.py``. ``rest_snapshot`` MUST
  NOT re-implement signing — pinned by
  ``tests/contracts/test_collector_rest_snapshot.py``
  ``::test_rest_snapshot_uses_kalshi_wire_auth_not_inline_rsa``.
- **Defensive non-raising.** Transient HTTP errors, malformed JSON,
  partial pages — all return ``None`` (NOT a partial dict) rather
  than propagating an exception. The caller (``_do_refresh``) treats
  ``None`` as a no-op and keeps the prior ticker map. Operators in
  live deploys MUST NOT lose their current subscription set on a
  single bad poll; the refresh thread keeps trying and the existing
  archivers keep serving whatever they already subscribed to.
  Returning a partial dict (D1.4 R1-M2 class) would propagate a
  partial subscribe → reconnect storm now + a recovery reconnect on
  the next successful tick — 2× reconnects per single REST hiccup.

D0.3 §1 medallion layout — REST snapshots themselves are NOT yet
written to bronze in D1.4 (the WS ``market_lifecycle_v2`` channel
covers most observable market state). A future D1.6+ sub-Bit may
add an S3 write of the raw REST snapshot payload under
``bronze/kalshi_rest/markets_snapshot/...`` per D0.3 §1.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

import requests

from kalshi_wire.auth import make_rest_headers


logger = logging.getLogger(__name__)


# Kalshi REST host + path (mirrors bot/constants.py:551-554 BASE_URL +
# API_PATH_PREFIX). Local copy here keeps the collector-no-bot contract
# clean; the value rarely changes and any production tweak would land
# in lockstep across both packages anyway.
DEFAULT_REST_BASE_URL: str = "https://api.elections.kalshi.com"
_REST_PATH_MARKETS: str = "/trade-api/v2/markets"


# Single-tier classification key. Until a measurement-driven split is
# justified (e.g., post-deploy volume telemetry showing latency-sensitive
# vs. cold-tail buckets), every open market lands under TIER_ALL.
TIER_ALL: str = "1"


# Hourly default — see module docstring for the cost / freshness
# rationale. The refresh thread polls REST every interval_seconds and
# force-reconnects WS conns (STAGGERED by
# ``collector.main_loop._RECONNECT_STAGGER_SECONDS`` per
# D1.3-fu4-oom-closure 2026-05-19, ticket 86b9zk4hz REUSED) when the
# ticker set changes.
DEFAULT_REFRESH_INTERVAL_SECONDS: float = 3600.0


# Per-page cap requested from Kalshi /markets. Production hits ~60K
# markets, so pagination is unavoidable. 200 is the cursor-page size
# bot/kalshi_client.py uses for the equivalent call — match it so
# Kalshi's per-page cost shape is identical to existing bot traffic.
_PAGE_LIMIT: int = 200


# Retry-policy defaults. Transient 5xx + 429 retry; non-429 4xx returns
# immediately (auth misconfig retrying would just spam Kalshi).
_DEFAULT_MAX_RETRIES: int = 3
_DEFAULT_BACKOFF_SECONDS: float = 2.0


# Per-request timeout. 10s matches bot/kalshi_client.py:134 for parity
# with the existing well-tuned REST timeout posture.
_REQUEST_TIMEOUT_SECONDS: float = 10.0


def fetch_tickers_by_tier(
    *,
    api_key: str,
    private_key,
    base_url: str = DEFAULT_REST_BASE_URL,
    session: Optional[requests.Session] = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    _test_skip_auth: bool = False,
    _test_backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS,
) -> Optional[Dict[str, List[str]]]:
    """GET ``/trade-api/v2/markets?status=open`` paginated, build tier map.

    Args:
        api_key: Kalshi API key id (passed through to
            ``kalshi_wire.auth.make_rest_headers`` per request).
        private_key: Loaded RSA-PSS private key
            (``kalshi_wire.auth.load_private_key`` result). When
            ``_test_skip_auth=True``, may be ``None`` — tests that
            point at a mock server skip header construction.
        base_url: Kalshi REST host. Defaults to production; tests
            point at a local mock.
        session: Optional ``requests.Session``. Defaults to a fresh
            one per call; production code shares a session across
            refresh ticks (RestSnapshotRefresher).
        max_retries: Transient-error retries before giving up.
        _test_skip_auth: When True, skips ``make_rest_headers`` and
            sends no auth headers — for tests pointing at fakes.
        _test_backoff_seconds: Base backoff seconds. Tests pass 0.0
            to skip sleeps; production uses ``_DEFAULT_BACKOFF_SECONDS``.

    Returns:
        - ``{TIER_ALL: [<sorted unique tickers>, ...]}`` on a COMPLETE
          successful fetch (all pages traversed; cursor exhausted).
          May be ``{TIER_ALL: []}`` if Kalshi legitimately reports zero
          open markets.
        - ``None`` when the fetch FAILED OR was PARTIAL (transport
          error exhausted retries, 4xx auth failure, malformed JSON,
          pagination broke mid-stream). The refresh thread MUST keep
          the prior ticker map and retry on the next interval — a
          partial set propagated to ``_replan_for_archivers`` would
          trigger a reconnect storm and then a recovery reconnect on
          the next successful fetch (R1-M2).

    NEVER raises — failures route to ``None``.
    """
    if session is None:
        session = requests.Session()
    url = base_url.rstrip("/") + _REST_PATH_MARKETS

    tickers: set = set()
    cursor: Optional[str] = None
    page_idx = 0
    while True:
        page_idx += 1
        params: Dict[str, Any] = {"limit": _PAGE_LIMIT, "status": "open"}
        if cursor:
            params["cursor"] = cursor

        body = _do_request_with_retry(
            session=session,
            url=url,
            params=params,
            api_key=api_key,
            private_key=private_key,
            max_retries=max_retries,
            skip_auth=_test_skip_auth,
            backoff_seconds=_test_backoff_seconds,
        )
        if body is None:
            # Transient errors exhausted retries OR non-retryable 4xx.
            # R1-M2: return None (not partial) so _do_refresh keeps the
            # prior ticker map. Propagating a partial set would cause a
            # reconnect storm on this tick + a recovery reconnect on
            # the next successful tick (2× reconnects per single REST
            # hiccup; bronze-data-loss class).
            logger.warning(
                "RestSnapshot: page %d failed after retries; aborting "
                "fetch (had %d partial tickers; refresher will retry "
                "next interval). Prior subscription set preserved.",
                page_idx, len(tickers),
            )
            return None

        markets = body.get("markets") if isinstance(body, dict) else None
        if not isinstance(markets, list):
            # Malformed page — treat as fetch failure (return None) so
            # _do_refresh keeps the prior subscription set. A 200 with
            # no 'markets' key is anomalous; trusting it would zero the
            # subscription map under R1-M1.
            logger.warning(
                "RestSnapshot: page %d malformed (no 'markets' list); "
                "aborting fetch. Prior subscription set preserved.",
                page_idx,
            )
            return None

        for row in markets:
            if not isinstance(row, dict):
                continue
            if row.get("status") not in ("open", "active"):
                # Defensive double-filter: drops closed/settled if a race
                # during settlement yields mixed statuses. Accepts both
                # trading-active labels because Kalshi's response body
                # uses a different vocabulary than the query parameter
                # — the ``status=open`` query returns rows whose response
                # field is ``status="active"``. Ticket ``86b9zjqhn``.
                continue
            ticker = row.get("ticker")
            if isinstance(ticker, str) and ticker:
                tickers.add(ticker)

        next_cursor = body.get("cursor") if isinstance(body, dict) else None
        if not next_cursor or not isinstance(next_cursor, str):
            break
        cursor = next_cursor

    return {TIER_ALL: sorted(tickers)}


def _do_request_with_retry(
    *,
    session: requests.Session,
    url: str,
    params: Dict[str, Any],
    api_key: str,
    private_key,
    max_retries: int,
    skip_auth: bool,
    backoff_seconds: float,
) -> Optional[Dict[str, Any]]:
    """Single-page GET with retry + backoff.

    Returns parsed JSON body on success; ``None`` on giveup.

    Retry policy:
      - 5xx + transport errors (connect timeout / DNS) → retry with
        exponential backoff up to ``max_retries``.
      - 429 → respect ``Retry-After`` header (or fall back to backoff)
        and retry.
      - Non-429 4xx → return None immediately (no retry — auth misconfig
        retrying would just spam Kalshi and burn rate-limit budget).
      - Malformed JSON → return None (treat as transient; refresh
        thread will try again next interval).
    """
    attempts = 0
    while True:
        attempts += 1
        try:
            if skip_auth:
                headers: Mapping[str, str] = {}
            else:
                headers = make_rest_headers(
                    api_key, private_key, "GET", _REST_PATH_MARKETS,
                )
            resp = session.get(
                url, params=params, headers=headers,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "RestSnapshot transport error (attempt %d/%d): %s",
                attempts, max_retries, exc,
            )
            if attempts >= max_retries:
                return None
            _sleep_backoff(attempts, backoff_seconds)
            continue

        status = getattr(resp, "status_code", None)
        if status == 200:
            try:
                return resp.json()
            except (ValueError, json.JSONDecodeError) as exc:
                logger.warning(
                    "RestSnapshot JSON decode failed (attempt %d/%d): %s",
                    attempts, max_retries, exc,
                )
                if attempts >= max_retries:
                    return None
                _sleep_backoff(attempts, backoff_seconds)
                continue

        if status == 429:
            retry_after_s = _retry_after_seconds(resp, backoff_seconds, attempts)
            logger.warning(
                "RestSnapshot 429 rate-limited (attempt %d/%d); sleeping %.1fs.",
                attempts, max_retries, retry_after_s,
            )
            if attempts >= max_retries:
                return None
            time.sleep(retry_after_s)
            continue

        if status is not None and 500 <= status < 600:
            logger.warning(
                "RestSnapshot 5xx (status=%s attempt %d/%d).",
                status, attempts, max_retries,
            )
            if attempts >= max_retries:
                return None
            _sleep_backoff(attempts, backoff_seconds)
            continue

        # Non-429 4xx / unexpected status — no retry.
        logger.warning(
            "RestSnapshot non-retryable status=%s; giving up.", status,
        )
        return None


def _retry_after_seconds(
    resp, fallback_seconds: float, attempts: int,
) -> float:
    """Parse ``Retry-After`` header. Falls back to exponential backoff
    when the header is absent or unparseable."""
    try:
        ra = resp.headers.get("Retry-After") if resp.headers else None
    except Exception:
        ra = None
    if ra is None:
        return _backoff_seconds(attempts, fallback_seconds)
    try:
        return float(ra)
    except (TypeError, ValueError):
        return _backoff_seconds(attempts, fallback_seconds)


def _backoff_seconds(attempts: int, base: float) -> float:
    """Exponential backoff: base * 2^(attempts-1). ``base=0`` → 0
    (test mode)."""
    if base <= 0:
        return 0.0
    return base * (2 ** max(0, attempts - 1))


def _sleep_backoff(attempts: int, base: float) -> None:
    secs = _backoff_seconds(attempts, base)
    if secs > 0:
        time.sleep(secs)


# ─── RestSnapshotRefresher — periodic refresh thread ──────────────────────


class RestSnapshotRefresher:
    """Background thread that periodically refreshes the ticker map and
    invokes ``on_refresh`` whenever the ticker set changes.

    Owned by ``collector/main_loop.py``. The refresher does NOT hold a
    reference to the archivers directly — it calls ``on_refresh(tickers_by_tier)``
    so main_loop can build the new ConnPlans and dispatch
    ``BronzeArchiver.request_reconnect()`` itself. Keeping the wiring
    one-directional (refresher → callback) simplifies tests and avoids
    a refresher↔archiver cycle.

    Lifecycle:
      - ``start()``: spawns a daemon thread that loops on
        ``shutdown_event.wait(timeout=interval)``. The first refresh
        runs IMMEDIATELY on start (so the WS comes up with a populated
        ticker set), then again every ``interval_seconds``.
      - ``stop()``: sets the shutdown_event the caller passed in.
        The caller owns the event so signals fan out across all
        archivers + drain thread + refresher consistently.

    Errors in ``on_refresh`` are caught and logged — a callback bug
    must not crash the refresh thread.
    """

    def __init__(
        self,
        *,
        api_key: str,
        private_key,
        on_refresh: Callable[[Dict[str, List[str]]], None],
        shutdown_event: threading.Event,
        interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        base_url: str = DEFAULT_REST_BASE_URL,
        session: Optional[requests.Session] = None,
    ):
        if interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds must be > 0 (got {interval_seconds}); "
                "the refresh loop sleeps for this duration between polls."
            )
        self._api_key = api_key
        self._private_key = private_key
        self._on_refresh = on_refresh
        self._shutdown_event = shutdown_event
        self._interval_seconds = interval_seconds
        self._base_url = base_url
        self._session = session if session is not None else requests.Session()
        self._thread: Optional[threading.Thread] = None
        self._last_tier_map: Dict[str, List[str]] = {}

    def start(self) -> None:
        """Spawn the daemon refresh thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="rest-snapshot-refresher",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown via the shared event. The thread observes it
        on the next ``wait`` tick."""
        self._shutdown_event.set()

    def _run(self) -> None:
        # First refresh fires immediately so WS conns boot with a
        # populated subscription set instead of waiting an hour for
        # the first interval to expire.
        self._do_refresh()
        while not self._shutdown_event.is_set():
            # wait() returns True if the event was set (shutdown), False
            # on timeout (run the next refresh).
            woke_for_shutdown = self._shutdown_event.wait(
                timeout=self._interval_seconds)
            if woke_for_shutdown:
                break
            self._do_refresh()

    def _do_refresh(self) -> None:
        try:
            new_map = fetch_tickers_by_tier(
                api_key=self._api_key,
                private_key=self._private_key,
                base_url=self._base_url,
                session=self._session,
            )
        except Exception:
            logger.exception(
                "RestSnapshotRefresher.fetch raised; keeping prior ticker "
                "set and trying again next interval."
            )
            return
        if new_map is None:
            # R1-M2: fetch FAILED or was PARTIAL — keep prior ticker set.
            # The warning already fired from inside fetch_tickers_by_tier;
            # nothing more to do until the next interval.
            return
        # R1-M1: defensive guard — refuse to wipe a non-empty subscription
        # set with a successful-but-empty REST response. Kalshi is never
        # legitimately at zero open markets in production; a 200/empty
        # response is anomalous (maintenance window, stale-cache, or
        # backend bug). Trusting it would force all archivers to drop
        # their subscriptions for the duration of the anomaly.
        new_total = sum(len(v) for v in new_map.values())
        prev_total = sum(len(v) for v in self._last_tier_map.values())
        if new_total == 0 and prev_total > 0:
            logger.warning(
                "RestSnapshotRefresher: REST returned 0 tickers but prior "
                "set had %d; treating as anomaly and keeping prior set. "
                "Investigate Kalshi /markets status before next refresh.",
                prev_total,
            )
            return
        if new_map == self._last_tier_map:
            # No-op refresh — ticker set unchanged. Skip the callback so
            # we don't force a WS reconnect storm when Kalshi's universe
            # is steady (the common case between trading sessions).
            logger.debug(
                "RestSnapshotRefresher: ticker set unchanged (%d tickers); "
                "skipping reconnect.",
                new_total,
            )
            return
        logger.info(
            "RestSnapshotRefresher: ticker set changed "
            "(prev=%d new=%d); invoking on_refresh.",
            prev_total, new_total,
        )
        self._last_tier_map = new_map
        try:
            self._on_refresh(new_map)
        except Exception:
            logger.exception(
                "RestSnapshotRefresher: on_refresh callback raised; the "
                "next refresh will retry. Subscription set may be stale "
                "until then."
            )
