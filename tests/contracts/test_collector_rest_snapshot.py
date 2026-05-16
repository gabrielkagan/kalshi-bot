"""D1.4 — REST snapshot contract (ticket 86b9ypn8r, 2026-05-16).

D1.4 wires the live catalog fetch that replaces D1.3's file-based
``COLLECTOR_TICKERS_FILE`` seam. The collector now pulls the current
universe of open markets directly from Kalshi's REST ``/markets``
endpoint and re-plans subscriptions on a periodic cadence.

What this file pins:

  1. Public surface — ``fetch_tickers_by_tier`` + ``RestSnapshotRefresher``
     + ``DEFAULT_REST_BASE_URL`` + ``DEFAULT_REFRESH_INTERVAL_SECONDS``
     + ``TIER_ALL`` are importable from ``collector.rest_snapshot``.
  2. URL + auth wiring — ``fetch_tickers_by_tier`` GETs
     ``{base_url}/trade-api/v2/markets`` with ``status=open`` and
     pages via ``cursor``. Auth headers come from
     ``kalshi_wire.auth.make_rest_headers``; collector MUST NOT
     re-implement RSA-PSS (D1.1.5 AMENDMENT).
  3. Tier classification — until a measurement-driven split is
     justified, every open market lands in a single tier keyed by
     ``TIER_ALL``. The return shape is the dict consumed by
     ``SubscriptionManager(tickers_by_tier=...)``.
  4. Empty / partial / malformed JSON tolerance — a transient 5xx
     that exhausts retries, a missing ``markets`` key, partial
     pagination, or a non-429 4xx returns ``None`` (NOT a partial
     dict). Callers (``_do_refresh``) treat ``None`` as a no-op and
     keep the prior subscription map — they MUST NOT zero out a live
     deploy on a single bad poll. Returning a partial set would
     propagate a reconnect storm now + a recovery reconnect on the
     next successful tick (D1.4 R1-M2 class).
  5. Retry policy — transient HTTP errors retry up to ``max_retries``
     with exponential backoff. 4xx (non-429) does NOT retry. A 429
     respects ``Retry-After``.
  6. Determinism — identical REST responses yield identical
     ``tickers_by_tier`` output (sorted tickers within each tier),
     so the SubscriptionManager planner produces identical
     ConnPlans on replay.
  7. Zero ``bot.*`` imports — pinned by import-linter contract
     ``collector-no-bot`` + AST defense-in-depth in
     ``tests/contracts/test_collector_no_bot_imports.py``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from collector import rest_snapshot
from collector.rest_snapshot import (
    DEFAULT_REFRESH_INTERVAL_SECONDS,
    DEFAULT_REST_BASE_URL,
    RestSnapshotRefresher,
    TIER_ALL,
    fetch_tickers_by_tier,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
REST_SNAPSHOT_SRC = REPO_ROOT / "collector" / "rest_snapshot.py"


# ─── 1. Public surface ─────────────────────────────────────────────────────


def test_tier_all_is_a_string_constant():
    """TIER_ALL is the single tier key used until a measurement-driven
    split is justified (D0.2 scope-map: all 59,904 non-MVE markets in
    scope, no per-tier breakdown). Stable string identity matters because
    SubscriptionManager treats the tier key as opaque and downstream
    observability surfaces (logs, S3 partitions if a future Bit shards
    by tier) will reference it."""
    assert isinstance(TIER_ALL, str)
    assert TIER_ALL == "1", (
        "TIER_ALL convention is the string '1' — matches the existing "
        "COLLECTOR_TICKERS_FILE example shape ({\"1\": [...], ...}) and "
        "the JSON-string-tier-key pattern used by the test fixtures in "
        "tests/integration/test_collector_main_loop_wireup.py."
    )


def test_default_refresh_interval_is_hourly():
    """Per the D1.4 pickup prompt: hourly refresh cadence is the cost /
    freshness tradeoff (Kalshi REST rate limit + bronze-side cost vs.
    surfacing new markets that appear every 5 min during the trading
    day). Tunable via env in production but the default is locked here."""
    assert DEFAULT_REFRESH_INTERVAL_SECONDS == 3600.0


def test_default_rest_base_url_is_kalshi_production():
    """REST host MUST point at Kalshi's production REST API."""
    assert DEFAULT_REST_BASE_URL.startswith("https://"), (
        f"DEFAULT_REST_BASE_URL={DEFAULT_REST_BASE_URL!r} not https — TLS "
        "is required for Kalshi auth."
    )
    assert "kalshi" in DEFAULT_REST_BASE_URL.lower()


# ─── 2. fetch_tickers_by_tier — single-page happy path ─────────────────────


def _build_session(pages: List[Dict[str, Any]]) -> MagicMock:
    """Build a fake requests.Session that returns ``pages`` in order
    from ``session.get``. Each page is a dict that will be the parsed
    JSON body. Returns ``MagicMock`` so call_args_list can be inspected.
    """
    responses = []
    for page in pages:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = page
        resp.content = json.dumps(page).encode()
        resp.raise_for_status = MagicMock()
        responses.append(resp)
    session = MagicMock()
    session.get.side_effect = responses
    return session


def test_fetch_single_page_returns_tickers_under_tier_all():
    """One-page response with N open markets ⇒ ``{TIER_ALL: [t1, t2, ...]}``.
    Tickers are sorted lexicographically to make the output deterministic
    on identical input (SubscriptionManager assigns round-robin, so
    re-ordering inputs would scramble per-conn ticker sets across
    refresh cycles unnecessarily)."""
    session = _build_session([{
        "markets": [
            {"ticker": "KXETH-Z", "status": "open"},
            {"ticker": "KXBTC-A", "status": "open"},
            {"ticker": "KXSOL-M", "status": "open"},
        ],
        "cursor": "",
    }])
    out = fetch_tickers_by_tier(
        api_key="kid",
        private_key=None,
        session=session,
        _test_skip_auth=True,
    )
    assert out == {TIER_ALL: ["KXBTC-A", "KXETH-Z", "KXSOL-M"]}


def test_fetch_paginates_until_empty_cursor():
    """The Kalshi /markets endpoint paginates via a ``cursor`` returned
    in the response. Empty cursor signals end-of-data. ``fetch_tickers_by_tier``
    follows the cursor chain and concatenates all pages."""
    session = _build_session([
        {"markets": [{"ticker": "A", "status": "open"}], "cursor": "p2"},
        {"markets": [{"ticker": "B", "status": "open"}], "cursor": "p3"},
        {"markets": [{"ticker": "C", "status": "open"}], "cursor": ""},
    ])
    out = fetch_tickers_by_tier(
        api_key="kid",
        private_key=None,
        session=session,
        _test_skip_auth=True,
    )
    assert out == {TIER_ALL: ["A", "B", "C"]}
    # Second + third requests must carry the cursor param.
    second_call = session.get.call_args_list[1]
    third_call = session.get.call_args_list[2]
    assert second_call.kwargs["params"].get("cursor") == "p2"
    assert third_call.kwargs["params"].get("cursor") == "p3"


def test_fetch_url_is_kalshi_markets_with_status_open():
    """Path must be ``/trade-api/v2/markets`` and the first-page query
    must include ``status=open`` so we only subscribe to active markets
    (closed/settled markets have no orderbook updates left)."""
    session = _build_session([{"markets": [], "cursor": ""}])
    fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    first_call = session.get.call_args_list[0]
    url = first_call.args[0] if first_call.args else first_call.kwargs.get("url")
    assert url.endswith("/trade-api/v2/markets"), (
        f"unexpected REST path: {url!r}"
    )
    params = first_call.kwargs.get("params") or {}
    assert params.get("status") == "open"


def test_fetch_passes_kalshi_auth_headers():
    """Real (non-test-skip) call must include the 4 KALSHI-ACCESS-*
    headers from ``kalshi_wire.auth.make_rest_headers``. The auth path
    is exercised end-to-end here — if a future change drops the
    delegation, this test fires."""
    captured_headers: Dict[str, str] = {}

    class _CapturingSession:
        def get(self, url, **kwargs):
            captured_headers.update(kwargs.get("headers") or {})
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {}
            resp.json.return_value = {"markets": [], "cursor": ""}
            resp.content = b"{}"
            resp.raise_for_status = MagicMock()
            return resp

    # Generate a throwaway private key via the same primitive the
    # production path uses; tests must not bundle a real Kalshi key.
    from cryptography.hazmat.primitives.asymmetric import rsa
    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    fetch_tickers_by_tier(
        api_key="test-kid", private_key=pk, session=_CapturingSession(),
    )
    assert "KALSHI-ACCESS-KEY" in captured_headers
    assert captured_headers["KALSHI-ACCESS-KEY"] == "test-kid"
    assert "KALSHI-ACCESS-TIMESTAMP" in captured_headers
    assert "KALSHI-ACCESS-SIGNATURE" in captured_headers


# ─── 3. Empty / malformed / partial response tolerance ─────────────────────


def test_fetch_missing_markets_key_returns_none():
    """R1-M1/M2: a malformed page (no ``markets`` field) returns ``None``
    so the caller (``_do_refresh``) keeps the prior subscription set.
    Previously this returned ``{TIER_ALL: []}`` which would wipe a live
    deploy's subscription map on a single corrupted REST response — a
    production-data-loss class flagged in D1.4 R1-M1."""
    session = _build_session([{"cursor": ""}])
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    assert out is None


def test_fetch_partial_pagination_returns_none_not_partial():
    """R1-M2: when pagination breaks mid-stream (transient 5xx exhausted
    on page 2 after page 1 succeeded), the fetcher returns ``None`` —
    not a partial set. A partial set would trigger a reconnect storm
    (subs drop to partial), then a RECOVERY reconnect storm on the next
    refresh tick when the full set returns. Two reconnects per single
    REST hiccup is unacceptable for production deploys with 7-8 conns."""
    page1 = MagicMock()
    page1.status_code = 200
    page1.headers = {}
    page1.json.return_value = {"markets": [
        {"ticker": "M-1", "status": "open"},
        {"ticker": "M-2", "status": "open"},
    ], "cursor": "p2"}
    page1.content = b"{}"
    page1.raise_for_status = MagicMock()
    page2_bad = MagicMock()
    page2_bad.status_code = 503
    page2_bad.headers = {}
    page2_bad.json.return_value = {}
    page2_bad.content = b""
    page2_bad.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.side_effect = [page1, page2_bad, page2_bad, page2_bad]
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        max_retries=2, _test_skip_auth=True, _test_backoff_seconds=0.0,
    )
    assert out is None, (
        f"partial-pagination must return None, not partial. Got {out}. "
        "Returning partial causes reconnect-storm class R1-M2."
    )


def test_fetch_filters_non_open_markets_defensively():
    """Even though we send ``status=open``, defensive double-filter on
    the response in case Kalshi ever returns mixed statuses (e.g.,
    races during settlement). Only ``status=open`` rows produce
    subscribes."""
    session = _build_session([{
        "markets": [
            {"ticker": "OPEN-1", "status": "open"},
            {"ticker": "CLOSED-1", "status": "closed"},
            {"ticker": "SETTLED-1", "status": "settled"},
            {"ticker": "OPEN-2", "status": "open"},
        ],
        "cursor": "",
    }])
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    assert out == {TIER_ALL: ["OPEN-1", "OPEN-2"]}


def test_fetch_skips_rows_missing_ticker_field():
    """Malformed row (no ``ticker``) gets skipped; the remaining rows
    still flow. Bronze fidelity does not depend on REST snapshot
    being lossless — bronze captures the WS frames directly."""
    session = _build_session([{
        "markets": [
            {"ticker": "GOOD-1", "status": "open"},
            {"status": "open"},  # missing ticker
            {"ticker": "GOOD-2", "status": "open"},
        ],
        "cursor": "",
    }])
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    assert out == {TIER_ALL: ["GOOD-1", "GOOD-2"]}


def test_fetch_dedupes_tickers_across_pages():
    """If Kalshi returns the same ticker on multiple pages (race during
    pagination, or response retried mid-stream), the output dedupes so
    SubscriptionManager doesn't double-subscribe."""
    session = _build_session([
        {"markets": [
            {"ticker": "A", "status": "open"},
            {"ticker": "B", "status": "open"},
        ], "cursor": "p2"},
        {"markets": [
            {"ticker": "B", "status": "open"},  # duplicate
            {"ticker": "C", "status": "open"},
        ], "cursor": ""},
    ])
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    assert out == {TIER_ALL: ["A", "B", "C"]}


# ─── 4. Retry / error tolerance ────────────────────────────────────────────


def test_fetch_retries_on_transient_5xx():
    """Single 503 retries and succeeds. Backoff is short for tests via
    ``_test_backoff_seconds=0.0``."""
    bad = MagicMock()
    bad.status_code = 503
    bad.headers = {}
    bad.json.return_value = {}
    bad.content = b""
    bad.raise_for_status = MagicMock()
    good = MagicMock()
    good.status_code = 200
    good.headers = {}
    good.json.return_value = {"markets": [{"ticker": "OK", "status": "open"}], "cursor": ""}
    good.content = b"{}"
    good.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.side_effect = [bad, good]
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True, _test_backoff_seconds=0.0,
    )
    assert out == {TIER_ALL: ["OK"]}


def test_fetch_gives_up_after_max_retries_returns_none():
    """R1-M2: all retries exhausted ⇒ ``None`` (caller keeps existing
    subscriptions). Never raises out into the refresh thread —
    transient network outages MUST NOT crash the collector. Previously
    returned empty dict; the change to None prevents the subscription-
    wipe class flagged at R1-M1."""
    bad = MagicMock()
    bad.status_code = 503
    bad.headers = {}
    bad.json.return_value = {}
    bad.content = b""
    bad.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.return_value = bad
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        max_retries=2, _test_skip_auth=True, _test_backoff_seconds=0.0,
    )
    assert out is None


def test_fetch_does_not_retry_on_non_429_4xx():
    """A 401/403 indicates an auth misconfig — retrying spams Kalshi
    with bad-auth requests and wastes the rate-limit budget. Return
    ``None`` immediately (R1-M2: None means "failed", not partial)."""
    bad = MagicMock()
    bad.status_code = 401
    bad.headers = {}
    bad.json.return_value = {}
    bad.content = b""
    bad.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.return_value = bad
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        max_retries=5, _test_skip_auth=True, _test_backoff_seconds=0.0,
    )
    assert out is None
    # Only one call — no retry.
    assert session.get.call_count == 1


# ─── 5. Determinism ────────────────────────────────────────────────────────


def test_fetch_is_deterministic_on_identical_response():
    """Replaying the same response yields identical output."""
    page = {"markets": [
        {"ticker": "Z", "status": "open"},
        {"ticker": "A", "status": "open"},
        {"ticker": "M", "status": "open"},
    ], "cursor": ""}
    s1 = _build_session([page])
    s2 = _build_session([page])
    out1 = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=s1, _test_skip_auth=True,
    )
    out2 = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=s2, _test_skip_auth=True,
    )
    assert out1 == out2
    assert out1[TIER_ALL] == sorted(out1[TIER_ALL])


# ─── 6. RestSnapshotRefresher class shape ──────────────────────────────────


def test_refresher_signature_documented_kwargs():
    """Constructor accepts the documented kwargs and exposes start/stop.

    Pinning the signature here means a future signature drift fails at
    the contract tier (~5s budget) instead of being caught at integration
    or — worse — at deploy time."""
    import inspect

    sig = inspect.signature(RestSnapshotRefresher.__init__)
    params = set(sig.parameters)
    for required in (
        "api_key", "private_key", "interval_seconds",
        "on_refresh", "shutdown_event",
    ):
        assert required in params, (
            f"RestSnapshotRefresher.__init__ missing kwarg {required!r}; "
            f"got {sorted(params)}"
        )
    # Public lifecycle methods.
    assert callable(getattr(RestSnapshotRefresher, "start", None))
    assert callable(getattr(RestSnapshotRefresher, "stop", None))


# ─── 7. AST / structural pins ──────────────────────────────────────────────


def test_rest_snapshot_uses_kalshi_wire_auth_not_inline_rsa():
    """D1.1.5 AMENDMENT: collector/auth.py was DELETED. All auth flows
    through kalshi_wire.auth. The rest_snapshot module MUST NOT re-
    implement RSA-PSS or import cryptography.* at top level.

    AST check: there is an ``import`` line referencing
    ``kalshi_wire.auth`` AND no ``from cryptography`` or
    ``import cryptography`` at module level.
    """
    src = REST_SNAPSHOT_SRC.read_text()
    tree = ast.parse(src)
    saw_kalshi_wire_auth = False
    crypto_violations: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("kalshi_wire.auth") or mod == "kalshi_wire":
                saw_kalshi_wire_auth = True
            if mod.startswith("cryptography"):
                crypto_violations.append(f"line {node.lineno}: from {mod} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("kalshi_wire.auth") or alias.name == "kalshi_wire":
                    saw_kalshi_wire_auth = True
                if alias.name.startswith("cryptography"):
                    crypto_violations.append(f"line {node.lineno}: import {alias.name}")
    assert saw_kalshi_wire_auth, (
        "collector/rest_snapshot.py MUST import from kalshi_wire.auth — "
        "duplicating RSA-PSS in collector/ regresses the D1.1.5 AMENDMENT."
    )
    assert not crypto_violations, (
        "collector/rest_snapshot.py MUST NOT import cryptography.* directly "
        "(auth lives in kalshi_wire.auth). Violations:\n  "
        + "\n  ".join(crypto_violations)
    )


def test_rest_snapshot_has_no_bot_imports():
    """Defense-in-depth: import-linter contract collector-no-bot is the
    primary enforcement, but a per-file AST pin gives a sharper error
    message + protects against ignore_imports drift."""
    src = REST_SNAPSHOT_SRC.read_text()
    tree = ast.parse(src)
    violations: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "bot" or mod.startswith("bot."):
                violations.append(f"line {node.lineno}: from {mod}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bot" or alias.name.startswith("bot."):
                    violations.append(f"line {node.lineno}: import {alias.name}")
    assert not violations, (
        "collector/rest_snapshot.py has bot.* imports — violates "
        "collector-no-bot isolation contract:\n  " + "\n  ".join(violations)
    )
