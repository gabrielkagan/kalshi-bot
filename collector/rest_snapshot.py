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

D0.3 §1 medallion layout — D1.9 (ticket ``86ba0pmzz``, 2026-05-19)
extended this module to write each REST page to bronze under
``bronze/kalshi_rest/markets/...``. ``fetch_tickers_by_tier`` and
``RestSnapshotRefresher`` each accept an optional ``bronze_writer``;
when present, every page (success or failure) emits one JSONL record
via ``writer.write_frame``. Bronze write is best-effort — any
exception in ``write_frame`` is swallowed so production subscription
planning keeps working. Default ``None`` preserves the pre-D1.9 shape
for offline / test callers.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

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


# Firehose series excluded from the WS subscription (ticket 86ba76adw,
# 2026-05-30). Measured 2026-05-30 from bronze: these two esports series are
# 193,174 + 118,377 = 90.5% of the ~344K-market open universe. Subscribing
# them blasts an oversized session_start subscribe burst (PRODUCTION-OBSERVED
# ~1098 subscribe_frames/conn per the "Replan conn=… subscribe_frames=1098"
# logs — count is markets×channels÷conns at the live batch_size, not derivable
# from the in-repo DEFAULT_BATCH_SIZE alone) that overwhelms the WS outbound
# send path → `socket.send() raised exception` storm (~433/s) → abrupt
# disconnect (`no close frame received`) → synchronized 7-conn reconnect storm.
# Result: every lower-volume market (ALL crypto-15M, the corpus's priority) is
# captured SNAPSHOT-ONLY (0 deltas/window) because the conn cycles every ~20s
# before a non-firehose book accumulates deltas. Excluding these two shrinks
# the burst ~10x (~344K→~33K markets) — well under the breaking threshold (a
# clean conn handles 10K markets fine, verified by the
# incremental_subscribe_probe_v3 spike).
#
# SCOPE OF DATA LOSS (R1-M1 — do not understate): excluding a series drops it
# from ALL subscribed channels (orderbook_delta + trade + market_lifecycle_v2 —
# see subscription_manager CHANNELS_DEFAULT), so esports go FULLY dark in bronze
# going forward — NOT just orderbook. This INCLUDES the silver Tier-1 source
# `kalshi_market_lifecycle_v2_v1` (silver/models/kalshi/…, which has no series
# filter): its esports settlement/`determined` rows will stop accruing. The bot
# does NOT trade esports and no current model/strategy consumes esports bronze,
# and crypto is the corpus priority, so this (broader-than-orderbook) loss is
# accepted per operator direction ("crypto is the priority; we can ignore
# esports if needed", 2026-05-30). REVERSIBLE: set COLLECTOR_EXCLUDED_SERIES=""
# to re-collect everything (escape hatch), or list other series comma-separated.
# Matched on the leading KX<SERIES> dash-segment (exact, never a substring).
DEFAULT_EXCLUDED_SERIES: tuple = (
    "KXMVESPORTSMULTIGAMEEXTENDED",
    "KXMVECROSSCATEGORY",
)


def resolve_excluded_series(env_value: Optional[str]) -> tuple:
    """Resolve the ``COLLECTOR_EXCLUDED_SERIES`` env value into the exclude
    tuple (ticket 86ba76adw). Three documented semantics, kept as a pure
    function so the load-bearing unset-vs-empty-vs-list branch is unit-tested
    (a future ``os.environ.get(key, "")`` refactor would silently turn the
    default-exclude OFF — this pins against that):
      - ``None`` (env UNSET) → ``DEFAULT_EXCLUDED_SERIES`` (the esports default).
      - ``""`` / whitespace / comma-only → ``()`` (escape hatch: exclude nothing).
      - ``"A, B"`` → ``("A", "B")`` (trimmed, blanks dropped, order preserved).
    """
    if env_value is None:
        return DEFAULT_EXCLUDED_SERIES
    return tuple(s.strip() for s in env_value.split(",") if s.strip())


# Hourly default — see module docstring for the cost / freshness
# rationale. The refresh thread polls REST every interval_seconds and
# force-reconnects WS conns (STAGGERED by
# ``collector.main_loop._RECONNECT_STAGGER_SECONDS`` per
# D1.3-fu4-oom-closure 2026-05-19, ticket 86b9zk4hz REUSED) when the
# ticker set changes.
DEFAULT_REFRESH_INTERVAL_SECONDS: float = 3600.0


# ─── Sub-hourly incremental discovery (ticket 86ba74hzy, 2026-05-30) ────────
#
# RCA: the hourly full-snapshot + force-reconnect mechanism above is correct
# for markets whose lifespan >> the poll interval, but STRUCTURALLY undersamples
# markets that open AND close inside a single poll gap. 15M crypto windows live
# ~15 min, so an hourly poll catches only ~25% (15/60); the other ~75% are never
# subscribed → permanent bronze loss. See
# kb/decisions/collector-sub-hourly-incremental-subscribe-plan.md.
#
# Fix: a fast (default 10s — tightened from the 60s ship default in the
# 86ba74hzy follow-up; see DEFAULT_INCREMENTAL_REFRESH_SECONDS below) discovery
# poll SCOPED to the crypto-15M series that dispatches subscribe frames
# MID-SESSION via BronzeArchiver.add_subscriptions (no reconnect — so the
# D1.3-fu4 ack-flood / OOM class cannot reopen).
#
# CRYPTO_15M_SERIES mirrors bot.constants.SERIES_TICKERS.values(). The collector
# cannot import bot.* (collector-no-bot contract), so this is a hand-mirror with
# a drift-pin contract test
# (tests/contracts/test_collector_incremental_subscribe.py
# ::test_crypto_15m_series_mirrors_bot_series_tickers) — same pattern as the
# LEAGUES_ESPN mirror. A new Kalshi 15M crypto series means a 1-line edit here +
# a kalshi-collector restart.
CRYPTO_15M_SERIES: tuple = (
    "KXBTC15M",
    "KXETH15M",
    "KXSOL15M",
    "KXXRP15M",
    "KXHYPE15M",
    "KXDOGE15M",
    "KXBNB15M",
    "KXADA15M",  # ADA 15M shadow onboarding (T1 2026-05-30)
    "KXBCH15M",  # BCH 15M shadow onboarding (T1 2026-05-30)
)

# Fast incremental-discovery cadence. 10s → a 15-min (900s) window is discovered
# within ≤10s of opening, so ≥~98.9% of its orderbook is captured (avg discovery
# lag ~5s since the next poll after a :00/:15/:30/:45 boundary is ≤10s out).
# Tightened from 60s (the 86ba74hzy ship default, ~93% capture) per the
# 86ba74hzy follow-up to recover most of the opening-minute sliver.
#
# Rate safety (data-backed; CLAUDE.md "no config tuning without data"): the poll
# fires one request per crypto-15M series per tick = 7 req/tick (1 page/series —
# a crypto-15M series has only a handful of open windows at any instant, far
# under the _PAGE_LIMIT=200 cursor-page size; a series would only paginate to a
# 2nd request if >200 windows were simultaneously open, which the 15M schedule
# never produces). At 10s that is 0.7 req/s average + 7 req/s peak burst, vs
# Kalshi's READ_RATE_LIMIT=30 req/s
# (Advanced tier; the collector runs on its own KALSHI_COLLECTOR_KEY_ID, so this
# budget is independent of the bot). ~43× headroom average, ~4× on the burst.
# Pinned by tests/contracts/test_collector_incremental_subscribe.py
# ::test_incremental_poll_stays_well_under_read_rate_limit (+ the ≤10s coverage
# pin). Tunable via ``COLLECTOR_INCREMENTAL_REFRESH_SECONDS`` in main_loop.
DEFAULT_INCREMENTAL_REFRESH_SECONDS: float = 10.0


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
    bronze_writer: Optional[Any] = None,
    excluded_series: Sequence[str] = (),
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
        bronze_writer: Optional D0.3 §2 ``BronzeWriter`` for the
            ``kalshi_rest/markets`` partition (D1.9, ticket
            ``86ba0pmzz``). When non-None, one JSONL record is
            emitted per REST page (success or failure) with the
            diagnostic envelope:
              success (7 keys):
                ``{"http_status": 200, "elapsed_ms": <int>,
                   "attempts": <int>, "page_idx": <int>,
                   "cursor_in": <str|null>, "cursor_out": <str>,
                   "response": <body>}``
              failure (6 keys):
                ``{"http_status": <int|null>, "elapsed_ms": <int>,
                   "attempts": <int>, "page_idx": <int>,
                   "cursor_in": <str|null>, "error": "<reason>"}``
            ``elapsed_ms`` is per-LAST-attempt wall-clock (excludes
            backoff sleeps between retries). ``attempts`` is the count
            of HTTP attempts the inner loop made; silver D3.x can
            disaggregate first-attempt vs retried pages via
            ``(elapsed_ms, attempts)`` jointly. ``cursor_in`` is None
            on the first page; ``cursor_out`` is the empty string ``""``
            on the terminal page (Kalshi's end-of-pagination sentinel).
            Bronze write is BEST-EFFORT — any exception from
            ``write_frame`` is swallowed (logged at WARNING) so
            production subscription planning is unaffected by bronze
            failures. Default ``None`` preserves the pre-D1.9 shape.
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
    # Firehose series to drop BEFORE they enter the subscription set (ticket
    # 86ba76adw). Matched on the leading ``KX<SERIES>`` dash-segment so it is
    # an exact series match, never a substring over-match. Empty ⇒ no-op.
    excluded: set = set(excluded_series)

    tickers: set = set()
    cursor: Optional[str] = None
    page_idx = 0
    while True:
        page_idx += 1
        params: Dict[str, Any] = {"limit": _PAGE_LIMIT, "status": "open"}
        if cursor:
            params["cursor"] = cursor

        # D1.9: call the inner helper which returns the parsed body PLUS
        # per-last-attempt diagnostic fields (status, error, attempts,
        # elapsed_ms, wire_recv_ts). `elapsed_ms` is per-LAST-attempt
        # wall-clock (NOT the loop's total — backoff sleeps are excluded).
        # The single-attempt `t0`/elapsed pattern matches
        # `weather_archiver.py:_fetch_and_write` (which has no retry
        # loop); D1.9 extends it to per-LAST-attempt semantics for the
        # retry-aware REST surface. `attempts` is also surfaced so
        # silver D3.x can disaggregate retried-then-succeeded pages
        # from first-attempt successes. `wire_recv_ts` is the
        # response-receipt instant of the LAST attempt (or exception
        # fire for transport errors).
        body, status, error_reason, attempts, elapsed_ms, wire_recv_ts = (
            _do_request_with_retry_inner(
                session=session,
                url=url,
                params=params,
                api_key=api_key,
                private_key=private_key,
                max_retries=max_retries,
                skip_auth=_test_skip_auth,
                backoff_seconds=_test_backoff_seconds,
            )
        )

        # D1.9: write bronze BEFORE the parsed-body branches that might
        # return early. The diagnostic envelope is the IRREVERSIBLE
        # capture; downstream "did we capture this page" must NOT depend
        # on whether the page parsed cleanly.
        if body is None:
            # Failure path — error_reason carries the diagnostic string.
            _write_bronze_failure(
                bronze_writer=bronze_writer,
                wire_recv_ts=wire_recv_ts,
                page_idx=page_idx,
                cursor_in=cursor,
                http_status=status,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
                error_reason=error_reason or "unknown",
            )
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

        next_cursor = body.get("cursor") if isinstance(body, dict) else None
        next_cursor_str = (
            next_cursor if isinstance(next_cursor, str) else ""
        )

        # D1.9: bronze success record — captures the verbatim page body.
        _write_bronze_success(
            bronze_writer=bronze_writer,
            wire_recv_ts=wire_recv_ts,
            page_idx=page_idx,
            cursor_in=cursor,
            cursor_out=next_cursor_str,
            http_status=status if status is not None else 200,
            elapsed_ms=elapsed_ms,
            attempts=attempts,
            response_body=body,
        )

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
                if excluded and ticker.split("-", 1)[0] in excluded:
                    continue  # firehose series (e.g. esports) — ticket 86ba76adw
                tickers.add(ticker)

        if not next_cursor or not isinstance(next_cursor, str):
            break
        cursor = next_cursor

    return {TIER_ALL: sorted(tickers)}


# ─── D1.9 bronze write helpers ────────────────────────────────────────────


def _write_bronze_success(
    *,
    bronze_writer: Optional[Any],
    wire_recv_ts: _dt.datetime,
    page_idx: int,
    cursor_in: Optional[str],
    cursor_out: str,
    http_status: int,
    elapsed_ms: int,
    attempts: int,
    response_body: Dict[str, Any],
) -> None:
    """Emit one bronze record for a successful page (D1.9).

    Best-effort: any exception from ``write_frame`` is swallowed +
    logged at WARNING so production subscription planning is
    unaffected by bronze failures. Mirrors
    ``WeatherArchiver._fetch_and_write`` posture.

    `elapsed_ms` is per-LAST-attempt wall-clock (NOT inclusive of
    backoff sleeps). `attempts` is the count of HTTP attempts the
    inner loop made — silver D3.x consumers can disaggregate
    "first-attempt success at 80ms" from "third-attempt success at
    80ms" via `(elapsed_ms, attempts)` jointly.
    """
    if bronze_writer is None:
        return
    diag: Dict[str, Any] = {
        "http_status": http_status,
        "elapsed_ms": elapsed_ms,
        "attempts": attempts,
        "page_idx": page_idx,
        "cursor_in": cursor_in,
        "cursor_out": cursor_out,
        "response": response_body,
    }
    _write_bronze_diag(bronze_writer, wire_recv_ts, diag)


def _write_bronze_failure(
    *,
    bronze_writer: Optional[Any],
    wire_recv_ts: _dt.datetime,
    page_idx: int,
    cursor_in: Optional[str],
    http_status: Optional[int],
    elapsed_ms: int,
    attempts: int,
    error_reason: str,
) -> None:
    """Emit one bronze record for a failed page (D1.9).

    Failure-shape diagnostic dict (6 keys; ``response`` replaced by
    ``error``). Bronze captures the failure mode verbatim so silver
    D3.x can model REST reliability.

    `elapsed_ms` is per-LAST-attempt wall-clock (NOT inclusive of
    backoff sleeps). `attempts` is the count of HTTP attempts the
    inner loop made before giving up.
    """
    if bronze_writer is None:
        return
    diag: Dict[str, Any] = {
        "http_status": http_status,
        "elapsed_ms": elapsed_ms,
        "attempts": attempts,
        "page_idx": page_idx,
        "cursor_in": cursor_in,
        "error": error_reason,
    }
    _write_bronze_diag(bronze_writer, wire_recv_ts, diag)


def _write_bronze_diag(
    bronze_writer: Any,
    wire_recv_ts: _dt.datetime,
    diag: Dict[str, Any],
) -> None:
    """Serialize diagnostic dict + dispatch to writer with exception swallow.

    The ``write_frame`` contract handles envelope construction +
    seq increment; we only need to provide the raw payload string.
    """
    try:
        raw_str = json.dumps(diag, separators=(",", ":"), ensure_ascii=False)
        bronze_writer.write_frame(wire_recv_ts, raw_str)
    except Exception:
        logger.warning(
            "RestSnapshot bronze write_frame raised; production "
            "subscription planning continues. page_idx=%s",
            diag.get("page_idx"),
            exc_info=True,
        )


def _do_request_with_retry_inner(
    *,
    session: requests.Session,
    url: str,
    params: Dict[str, Any],
    api_key: str,
    private_key,
    max_retries: int,
    skip_auth: bool,
    backoff_seconds: float,
) -> tuple[
    Optional[Dict[str, Any]],
    Optional[int],
    Optional[str],
    int,
    int,
    _dt.datetime,
]:
    """Single-page GET with retry + backoff. Returns per-LAST-attempt
    diagnostics for bronze capture.

    Returns a 6-tuple:
      ``(body, last_status, error_reason, attempts, elapsed_ms, wire_recv_ts)``

      - On success: ``(body_dict, status_code, None, attempts, elapsed_ms, wire_recv_ts)``
      - On failure: ``(None, last_status_or_None, error_reason_str, attempts, elapsed_ms, wire_recv_ts)``

    `attempts` is the count of HTTP attempts made (1 for first-try
    success, N for N-th retry result).

    `elapsed_ms` is wall-clock-ms for the LAST attempt ONLY (the one
    whose status/body lands in bronze) — backoff sleeps between
    attempts are NOT included. This matches the
    ``weather_archiver.py:_fetch_and_write`` precedent so silver D3.x
    can use a consistent per-attempt latency unit across sources.

    `wire_recv_ts` is the UTC instant the LAST attempt's response was
    received (or the transport exception fired). Per D0.3 §2 this is
    the envelope's `_wire_recv_ts` anchor.

    Retry policy:
      - 5xx + transport errors (connect timeout / DNS) → retry with
        exponential backoff up to ``max_retries``.
      - 429 → respect ``Retry-After`` header (or fall back to backoff)
        and retry.
      - Non-429 4xx → return immediately (no retry — auth misconfig
        retrying would just spam Kalshi and burn rate-limit budget).
      - Malformed JSON → return None (treat as transient; refresh
        thread will try again next interval).
    """
    attempts = 0
    last_status: Optional[int] = None
    last_error: Optional[str] = None
    last_elapsed_ms: int = 0
    last_wire_recv_ts: _dt.datetime = _dt.datetime.now(_dt.timezone.utc)
    while True:
        attempts += 1
        t0 = time.time()
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
            last_elapsed_ms = int((time.time() - t0) * 1000)
            last_wire_recv_ts = _dt.datetime.now(_dt.timezone.utc)
            logger.warning(
                "RestSnapshot transport error (attempt %d/%d): %s",
                attempts, max_retries, exc,
            )
            last_status = None
            last_error = (
                f"request_exception: {type(exc).__name__}: {exc!r}"
            )
            if attempts >= max_retries:
                return (
                    None, last_status, last_error,
                    attempts, last_elapsed_ms, last_wire_recv_ts,
                )
            _sleep_backoff(attempts, backoff_seconds)
            continue

        # Response received — capture per-attempt elapsed + wire_recv_ts.
        last_elapsed_ms = int((time.time() - t0) * 1000)
        last_wire_recv_ts = _dt.datetime.now(_dt.timezone.utc)
        status = getattr(resp, "status_code", None)
        last_status = status
        if status == 200:
            try:
                return (
                    resp.json(), status, None,
                    attempts, last_elapsed_ms, last_wire_recv_ts,
                )
            except (ValueError, json.JSONDecodeError) as exc:
                logger.warning(
                    "RestSnapshot JSON decode failed (attempt %d/%d): %s",
                    attempts, max_retries, exc,
                )
                last_error = f"json_decode_error: {exc!r}"
                if attempts >= max_retries:
                    return (
                        None, last_status, last_error,
                        attempts, last_elapsed_ms, last_wire_recv_ts,
                    )
                _sleep_backoff(attempts, backoff_seconds)
                continue

        if status == 429:
            retry_after_s = _retry_after_seconds(resp, backoff_seconds, attempts)
            logger.warning(
                "RestSnapshot 429 rate-limited (attempt %d/%d); sleeping %.1fs.",
                attempts, max_retries, retry_after_s,
            )
            last_error = _format_http_error_reason(resp, 429)
            if attempts >= max_retries:
                return (
                    None, last_status, last_error,
                    attempts, last_elapsed_ms, last_wire_recv_ts,
                )
            time.sleep(retry_after_s)
            continue

        if status is not None and 500 <= status < 600:
            logger.warning(
                "RestSnapshot 5xx (status=%s attempt %d/%d).",
                status, attempts, max_retries,
            )
            last_error = _format_http_error_reason(resp, status)
            if attempts >= max_retries:
                return (
                    None, last_status, last_error,
                    attempts, last_elapsed_ms, last_wire_recv_ts,
                )
            _sleep_backoff(attempts, backoff_seconds)
            continue

        # Non-429 4xx / unexpected status — no retry.
        logger.warning(
            "RestSnapshot non-retryable status=%s; giving up.", status,
        )
        last_error = _format_http_error_reason(resp, status)
        return (
            None, last_status, last_error,
            attempts, last_elapsed_ms, last_wire_recv_ts,
        )


def _format_http_error_reason(resp, status: Optional[int]) -> str:
    """Build the ``error`` diagnostic string for a non-200 response.

    Tries to extract Kalshi's JSON ``reason`` field; falls back to
    bare ``http_<status>``. Mirrors WeatherArchiver._fetch_and_write
    line 343-355 pattern.
    """
    base = f"http_{status}" if status is not None else "http_unknown"
    try:
        err_payload = resp.json()
    except (ValueError, json.JSONDecodeError):
        return base
    if isinstance(err_payload, dict):
        reason = err_payload.get("reason")
        if reason:
            return f"{base}: {reason!r}"
    return base


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


# ─── Persisted ticker set (ticket 86bbvdcat, 2026-09-05) ────────────────────
#
# RCA: the synchronous boot page-through in ``collector.main_loop.run`` took
# 54.9 min on the 2026-09-05 restart (~18,500 pages, ~3.7M rows → 358,625
# tickers after exclusions), so the first WS connect landed 59.8 min after
# ActiveEnter — every restart cost an hour of orderbook bronze. The fix
# persists the last successful ticker set to disk so the NEXT boot can plan +
# start the WS conns immediately and let the refresher's first tick re-page
# in the background, replanning only if the set actually changed.

TICKER_CACHE_SCHEMA_VERSION: int = 1
DEFAULT_TICKER_CACHE_FILENAME: str = "last_tickers.json"
# Warn (still use) when the persisted set is older than this — the
# refresher's first tick corrects it, but the operator should know the
# seed was stale (e.g. the collector was stopped for days).
TICKER_CACHE_STALE_WARN_SECONDS: float = 24 * 3600.0


def utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_utc_iso(value: Any) -> Optional[float]:
    """Parse the sidecar/cache ``%Y-%m-%dT%H:%M:%S.%fZ`` shape → epoch seconds."""
    if not isinstance(value, str):
        return None
    try:
        return _dt.datetime.strptime(
            value, "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


def save_tier_map(
    path: Path,
    tier_map: Mapping[str, Sequence[str]],
) -> bool:
    """Atomically persist ``tier_map`` as JSON at ``path``.

    Shape: ``{"schema_version": 1, "saved_at": <UTC iso>, "tickers_by_tier":
    {"<tier>": ["TICKER", ...]}}``. tmp-file + ``os.replace`` so a reader
    (the next boot) never sees a torn write. Returns False (logged) on any
    OSError — persisting the cache is best-effort; the live refresh must
    not fail because the cache dir is unwritable.
    """
    payload = {
        "schema_version": TICKER_CACHE_SCHEMA_VERSION,
        "saved_at": utc_now_iso(),
        "tickers_by_tier": {str(k): list(v) for k, v in tier_map.items()},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload))
        os.replace(tmp_path, path)
        return True
    except OSError:
        logger.warning(
            "save_tier_map: could not persist ticker cache at %s; the next "
            "boot will page the REST universe synchronously.", path,
            exc_info=True,
        )
        return False


def load_tier_map(path: Path) -> Optional[Tuple[Dict[str, List[str]], float]]:
    """Load a persisted ticker set → ``(tickers_by_tier, age_seconds)``.

    ``None`` when the file is absent, unreadable, malformed, has a
    different ``schema_version``, or does not hold a ``{str: [str, ...]}``
    map — the caller then falls back to the synchronous REST fetch. Age is
    derived from ``saved_at`` (file mtime as fallback) so the boot log +
    sidecar can state how stale the seed set is.
    """
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("load_tier_map: %s is not valid JSON; ignoring cache.", path)
        return None
    if not isinstance(data, dict) or data.get("schema_version") != TICKER_CACHE_SCHEMA_VERSION:
        logger.warning("load_tier_map: %s has unexpected shape/schema; ignoring cache.", path)
        return None
    tiers = data.get("tickers_by_tier")
    if not isinstance(tiers, dict):
        logger.warning("load_tier_map: %s tickers_by_tier is not an object; ignoring cache.", path)
        return None
    out: Dict[str, List[str]] = {}
    for tier, tickers in tiers.items():
        if not isinstance(tier, str) or not isinstance(tickers, list):
            logger.warning("load_tier_map: %s tier %r malformed; ignoring cache.", path, tier)
            return None
        out[tier] = [t for t in tickers if isinstance(t, str)]
    if not any(out.values()):
        # R1-m1: a zero-ticker cache is the "empty" boot wearing a healthier
        # label; treat as no cache so the boot fetches synchronously.
        logger.warning("load_tier_map: %s holds 0 tickers; ignoring cache.", path)
        return None
    saved_epoch = _parse_utc_iso(data.get("saved_at"))
    if saved_epoch is None:
        try:
            saved_epoch = path.stat().st_mtime
        except OSError:
            saved_epoch = time.time()
    age = max(0.0, time.time() - saved_epoch)
    if age > TICKER_CACHE_STALE_WARN_SECONDS:
        logger.warning(
            "load_tier_map: %s is %.1f h old — booting from a stale seed; the "
            "refresher's first tick will re-page and replan on change.",
            path, age / 3600.0,
        )
    return out, age


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
        bronze_writer: Optional[Any] = None,
        excluded_series: Sequence[str] = (),
        initial_tier_map: Optional[Mapping[str, Sequence[str]]] = None,
        cache_path: Optional[Path] = None,
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
        # Ticket 86bbvdcat: seed the change detector with the set the boot
        # planned from (persisted cache or the synchronous first fetch) so
        # the immediate first tick does NOT fire a spurious "changed"
        # verdict (prev=0 → 7-conn reconnect storm ~1h after boot) when
        # the fresh page-through returns the same set.
        self._last_tier_map: Dict[str, List[str]] = (
            {str(k): list(v) for k, v in initial_tier_map.items()}
            if initial_tier_map else {}
        )
        # Ticket 86bbvdcat: every successful fetch is persisted here so the
        # NEXT boot can start the WS conns immediately. None = no cache.
        self._cache_path = cache_path
        # Observability for the bronze_health.json sidecar (``status()``).
        self._status_lock = threading.Lock()
        self._in_progress = False
        self._refresh_count = 0
        self._last_started_at: Optional[str] = None
        self._last_completed_at: Optional[str] = None
        self._last_duration_seconds: Optional[float] = None
        self._last_ticker_count: Optional[int] = None
        self._last_outcome: Optional[str] = None
        # D1.9 — forwarded to fetch_tickers_by_tier on every _do_refresh
        # tick. None = no bronze write (offline / test mode).
        self._bronze_writer = bronze_writer
        # Ticket 86ba76adw — firehose series dropped from every refresh's
        # subscription set (wired from COLLECTOR_EXCLUDED_SERIES in main_loop).
        self._excluded_series = tuple(excluded_series)

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

    def status(self) -> Dict[str, object]:
        """JSON-serializable refresh status for the bronze_health.json
        sidecar (ticket 86bbvdcat): whether a page-through is in flight,
        when the last one completed, how long it took, how many tickers
        it returned, and its outcome (``changed`` / ``unchanged`` /
        ``failed`` / ``empty_anomaly``)."""
        with self._status_lock:
            return {
                "in_progress": self._in_progress,
                "refresh_count": self._refresh_count,
                "last_started_at": self._last_started_at,
                "last_completed_at": self._last_completed_at,
                "last_duration_seconds": self._last_duration_seconds,
                "last_ticker_count": self._last_ticker_count,
                "last_outcome": self._last_outcome,
                # Total of the CURRENT change-detector set (the boot seed
                # until the first "changed" fetch replaces it).
                "current_ticker_count": sum(len(v) for v in self._last_tier_map.values()),
            }

    def _run(self) -> None:
        # First refresh fires immediately. Pre-86bbvdcat this was how the
        # WS conns got a populated set; post-86bbvdcat the boot already
        # planned from the persisted / synchronous set, so the immediate
        # tick's job is to re-page the universe in the background, replan
        # only on change, and recover an ``empty`` boot.
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
        """One refresh tick: fetch → (persist) → diff → callback.

        Wraps ``_do_refresh_inner`` with the status bookkeeping the sidecar
        reads; the outcome string is set by the inner body.
        """
        started = time.monotonic()
        with self._status_lock:
            self._in_progress = True
            self._last_started_at = utc_now_iso()
            self._last_outcome = None
        try:
            self._do_refresh_inner()
        finally:
            duration = time.monotonic() - started
            with self._status_lock:
                self._in_progress = False
                self._refresh_count += 1
                self._last_completed_at = utc_now_iso()
                self._last_duration_seconds = round(duration, 3)
                outcome = self._last_outcome
                count = self._last_ticker_count
            logger.info(
                "RestSnapshotRefresher: fetch completed in %.0fs "
                "(tickers=%s outcome=%s).",
                duration, count, outcome,
            )

    def _set_outcome(self, outcome: str, ticker_count: Optional[int] = None) -> None:
        with self._status_lock:
            self._last_outcome = outcome
            if ticker_count is not None:
                self._last_ticker_count = ticker_count

    def _do_refresh_inner(self) -> None:
        try:
            # Bare-name call: Python resolves ``fetch_tickers_by_tier``
            # via this function's ``__globals__`` dict (= the module's
            # namespace) at call-time, so tests that do
            # ``collector.rest_snapshot.fetch_tickers_by_tier = fake``
            # see their patch take effect (module attribute assignment
            # IS dict mutation of the same dict the function reads).
            new_map = fetch_tickers_by_tier(
                api_key=self._api_key,
                private_key=self._private_key,
                base_url=self._base_url,
                session=self._session,
                bronze_writer=self._bronze_writer,
                excluded_series=self._excluded_series,
            )
        except Exception:
            logger.exception(
                "RestSnapshotRefresher.fetch raised; keeping prior ticker "
                "set and trying again next interval."
            )
            self._set_outcome("failed")
            return
        if new_map is None:
            # R1-M2: fetch FAILED or was PARTIAL — keep prior ticker set.
            # The warning already fired from inside fetch_tickers_by_tier;
            # nothing more to do until the next interval.
            self._set_outcome("failed")
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
            self._set_outcome("empty_anomaly", new_total)
            return
        # Ticket 86bbvdcat: persist every successful, non-anomalous fetch
        # (changed OR unchanged — the saved_at freshness matters at boot).
        if self._cache_path is not None:
            save_tier_map(self._cache_path, new_map)
        if new_map == self._last_tier_map:
            # No-op refresh — ticker set unchanged. Skip the callback so
            # we don't force a WS reconnect storm when Kalshi's universe
            # is steady (the common case between trading sessions).
            logger.debug(
                "RestSnapshotRefresher: ticker set unchanged (%d tickers); "
                "skipping reconnect.",
                new_total,
            )
            self._set_outcome("unchanged", new_total)
            return
        self._set_outcome("changed", new_total)
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


# ─── Sub-hourly incremental discovery (ticket 86ba74hzy, 2026-05-30) ────────


def fetch_open_tickers_for_series(
    *,
    series_tickers: Sequence[str],
    api_key: str,
    private_key,
    base_url: str = DEFAULT_REST_BASE_URL,
    session: Optional[requests.Session] = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    _test_skip_auth: bool = False,
    _test_backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS,
) -> set:
    """Return the UNION of open tickers across the given Kalshi series.

    Issues one paginated ``/markets?series_ticker=<S>&status=open`` fetch per
    series (tiny payloads — a handful of open windows per crypto-15M series at
    any instant) and unions the results.

    Differs from ``fetch_tickers_by_tier`` in its failure posture: this feeds
    the INCREMENTAL ADD path (which only ever adds newly-seen tickers, never
    drops), so a per-series failure is BEST-EFFORT — the bad series is skipped
    this tick and retried next tick. There is no partial-set reconnect-storm
    risk (R1-M2) because nothing here triggers a reconnect; a missed series
    just delays discovery of its newest window by one ``interval_seconds``.

    NEVER raises — per-series failures are logged + skipped.
    """
    if session is None:
        session = requests.Session()
    url = base_url.rstrip("/") + _REST_PATH_MARKETS
    tickers: set = set()
    for series in series_tickers:
        cursor: Optional[str] = None
        while True:
            params: Dict[str, Any] = {
                "limit": _PAGE_LIMIT, "status": "open", "series_ticker": series,
            }
            if cursor:
                params["cursor"] = cursor
            body, _status, _err, _attempts, _elapsed, _ts = (
                _do_request_with_retry_inner(
                    session=session,
                    url=url,
                    params=params,
                    api_key=api_key,
                    private_key=private_key,
                    max_retries=max_retries,
                    skip_auth=_test_skip_auth,
                    backoff_seconds=_test_backoff_seconds,
                )
            )
            if body is None or not isinstance(body, dict):
                logger.warning(
                    "IncrementalDiscovery: series=%s page fetch failed; "
                    "skipping this series this tick (retries next interval).",
                    series,
                )
                break
            markets = body.get("markets")
            if isinstance(markets, list):
                for row in markets:
                    if not isinstance(row, dict):
                        continue
                    if row.get("status") not in ("open", "active"):
                        continue
                    ticker = row.get("ticker")
                    if isinstance(ticker, str) and ticker:
                        tickers.add(ticker)
            next_cursor = body.get("cursor")
            if not next_cursor or not isinstance(next_cursor, str):
                break
            cursor = next_cursor
    return tickers


class IncrementalDiscoveryRefresher:
    """Background thread that polls the sub-hourly (crypto-15M) series at a fast
    cadence and reports the current open ticker set to ``on_new``.

    Owned by ``collector/main_loop.py``. Like ``RestSnapshotRefresher`` the
    wiring is one-directional (refresher → callback): the refresher does NOT
    hold archiver references; main_loop's ``on_new`` callback diffs against its
    authoritative subscribed set, assigns the truly-new tickers to conns, and
    dispatches ``BronzeArchiver.add_subscriptions`` (mid-session, no reconnect).

    Lifecycle mirrors ``RestSnapshotRefresher``: first poll fires IMMEDIATELY on
    ``start()``, then every ``interval_seconds`` on a cancellable
    ``shutdown_event.wait(timeout=...)``. ``on_new`` exceptions are caught +
    logged so a callback bug cannot crash the discovery thread.
    """

    def __init__(
        self,
        *,
        api_key: str,
        private_key,
        series_tickers: Sequence[str],
        on_new: Callable[[set], None],
        shutdown_event: threading.Event,
        interval_seconds: float = DEFAULT_INCREMENTAL_REFRESH_SECONDS,
        base_url: str = DEFAULT_REST_BASE_URL,
        session: Optional[requests.Session] = None,
    ):
        if interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds must be > 0 (got {interval_seconds}); the "
                "discovery loop sleeps for this duration between polls."
            )
        self._api_key = api_key
        self._private_key = private_key
        self._series_tickers = tuple(series_tickers)
        self._on_new = on_new
        self._shutdown_event = shutdown_event
        self._interval_seconds = interval_seconds
        self._base_url = base_url
        self._session = session if session is not None else requests.Session()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="incremental-discovery-refresher",
        )
        self._thread.start()

    def stop(self) -> None:
        self._shutdown_event.set()

    def _run(self) -> None:
        self._do_poll()
        while not self._shutdown_event.is_set():
            woke_for_shutdown = self._shutdown_event.wait(
                timeout=self._interval_seconds)
            if woke_for_shutdown:
                break
            self._do_poll()

    def _do_poll(self) -> None:
        try:
            open_set = fetch_open_tickers_for_series(
                series_tickers=self._series_tickers,
                api_key=self._api_key,
                private_key=self._private_key,
                base_url=self._base_url,
                session=self._session,
            )
        except Exception:
            logger.exception(
                "IncrementalDiscoveryRefresher.fetch raised; skipping this "
                "tick and retrying next interval."
            )
            return
        try:
            self._on_new(open_set)
        except Exception:
            logger.exception(
                "IncrementalDiscoveryRefresher: on_new callback raised; the "
                "next poll will retry."
            )
