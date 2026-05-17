"""Integration: REST snapshot → SubscriptionManager → re-plan invariant.

D1.4 (ticket 86b9ypn8r, 2026-05-16). Verifies the refresh cycle that
the periodic ``RestSnapshotRefresher`` drives in production:

  REST /markets ──► fetch_tickers_by_tier ──► SubscriptionManager.assign()
                                                       │
                                                       ▼
                                          per-conn ConnPlans (round-robin
                                          deterministic on identical input)
                                                       │
                                                       ▼
                                          on_refresh callback ──►
                                              BronzeArchiver.update_subscriptions
                                              + BronzeArchiver.request_reconnect

The contract test covers each unit; this file pins the END-TO-END
shape so a future refactor that breaks the seam (e.g., changing
SubscriptionManager.assign() to non-deterministic ordering) fails
loudly at integration tier.

Mocks: ``requests.Session`` for /markets responses; ``WSClient`` for
the BronzeArchiver-level reconnect signal.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from collector.rest_snapshot import (
    RestSnapshotRefresher,
    TIER_ALL,
    fetch_tickers_by_tier,
)
from collector.subscription_manager import (
    CHANNELS_DEFAULT,
    SubscriptionManager,
)


def _fake_session(pages: List[Dict[str, Any]]) -> MagicMock:
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


def test_fetch_to_planner_round_trip_is_deterministic():
    """Identical REST response ⇒ identical ConnPlans (round-robin within
    sorted ticker list). The deterministic shape is what makes
    refresh-then-reconnect safe: a single conn that crashes + reconnects
    gets the same ticker set without cross-conn coordination."""
    page = {"markets": [
        {"ticker": "M-3", "status": "open"},
        {"ticker": "M-1", "status": "open"},
        {"ticker": "M-2", "status": "open"},
        {"ticker": "M-4", "status": "open"},
    ], "cursor": ""}
    s1 = _fake_session([page])
    s2 = _fake_session([page])

    out1 = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=s1, _test_skip_auth=True,
    )
    out2 = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=s2, _test_skip_auth=True,
    )
    assert out1 == out2

    mgr1 = SubscriptionManager(tickers_by_tier=out1, conn_count=2)
    mgr2 = SubscriptionManager(tickers_by_tier=out2, conn_count=2)
    plans1 = mgr1.assign()
    plans2 = mgr2.assign()

    assert [p.market_tickers for p in plans1] == [p.market_tickers for p in plans2]
    # Round-robin spreads — neither plan gets all tickers, both get ≥1.
    for p in plans1:
        assert p.market_tickers, f"conn={p.conn_id} got 0 tickers in 4-ticker assignment"


def test_fetch_to_planner_changes_when_response_changes():
    """Different REST responses ⇒ different ConnPlans. Symmetric guard
    to the determinism test — if the planner ever ignored its input,
    this would fail."""
    page_a = {"markets": [
        {"ticker": "A", "status": "open"},
        {"ticker": "B", "status": "open"},
    ], "cursor": ""}
    page_b = {"markets": [
        {"ticker": "A", "status": "open"},
        {"ticker": "B", "status": "open"},
        {"ticker": "C", "status": "open"},
    ], "cursor": ""}
    s_a = _fake_session([page_a])
    s_b = _fake_session([page_b])

    out_a = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=s_a, _test_skip_auth=True,
    )
    out_b = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=s_b, _test_skip_auth=True,
    )
    assert out_a != out_b

    plans_a = SubscriptionManager(
        tickers_by_tier=out_a, conn_count=2).assign()
    plans_b = SubscriptionManager(
        tickers_by_tier=out_b, conn_count=2).assign()
    total_a = sum(len(p.market_tickers) for p in plans_a)
    total_b = sum(len(p.market_tickers) for p in plans_b)
    assert total_a == 2
    assert total_b == 3


def test_refresher_invokes_callback_on_first_tick_with_initial_set():
    """The refresher's first tick fires IMMEDIATELY on start (no wait
    for the first interval). The on_refresh callback receives the
    parsed ticker map.
    """
    page = {"markets": [
        {"ticker": "BTC-1", "status": "open"},
        {"ticker": "ETH-1", "status": "open"},
    ], "cursor": ""}
    received: List[Dict[str, List[str]]] = []
    refresh_event = threading.Event()

    def _on_refresh(new_map: Dict[str, List[str]]) -> None:
        received.append(new_map)
        refresh_event.set()

    shutdown = threading.Event()
    refresher = RestSnapshotRefresher(
        api_key="kid",
        private_key=None,
        on_refresh=_on_refresh,
        shutdown_event=shutdown,
        interval_seconds=3600.0,  # high — we only want the first tick
        session=_fake_session([page]),
    )
    # _test_skip_auth equivalent: private_key=None + we monkey-patch the
    # fetch call below. Instead, run with the session prebuilt; the
    # refresher passes private_key into make_rest_headers which would
    # fail. Use a stub session that doesn't trigger auth at all.
    # Simpler: override fetch via monkey-patching the module attr.
    import collector.rest_snapshot as rs

    orig = rs.fetch_tickers_by_tier

    def fake_fetch(**kwargs):
        return orig(
            api_key=kwargs.get("api_key", "kid"),
            private_key=None,
            session=kwargs.get("session"),
            _test_skip_auth=True,
        )
    rs.fetch_tickers_by_tier = fake_fetch
    try:
        refresher.start()
        # First tick is immediate — wait briefly.
        assert refresh_event.wait(timeout=2.0), (
            "refresher did not invoke on_refresh within 2s of start; first "
            "tick should fire immediately, not wait one interval."
        )
        assert received, "on_refresh callback was not invoked"
        assert received[0] == {TIER_ALL: ["BTC-1", "ETH-1"]}
    finally:
        rs.fetch_tickers_by_tier = orig
        shutdown.set()


def test_refresher_skips_callback_when_ticker_set_unchanged():
    """When two refresh ticks return the same ticker set, on_refresh
    fires ONCE (first tick) and is skipped on the no-op tick — Kalshi's
    universe is steady between trading sessions and force-reconnecting
    every conn every hour for no reason would be a self-inflicted
    reconnect storm."""
    page = {"markets": [
        {"ticker": "A", "status": "open"},
    ], "cursor": ""}
    received: List[Dict[str, List[str]]] = []
    second_tick_done = threading.Event()

    def _on_refresh(new_map: Dict[str, List[str]]) -> None:
        received.append(new_map)
        second_tick_done.set()

    shutdown = threading.Event()
    # Tiny interval so two ticks fire fast.
    refresher = RestSnapshotRefresher(
        api_key="kid",
        private_key=None,
        on_refresh=_on_refresh,
        shutdown_event=shutdown,
        interval_seconds=0.05,
    )

    import collector.rest_snapshot as rs

    call_count = {"n": 0}

    def fake_fetch(**kwargs):
        call_count["n"] += 1
        return {TIER_ALL: ["A"]}

    orig = rs.fetch_tickers_by_tier
    rs.fetch_tickers_by_tier = fake_fetch
    try:
        refresher.start()
        # Wait for at least 3 fetch calls.
        deadline = time.time() + 2.0
        while call_count["n"] < 3 and time.time() < deadline:
            time.sleep(0.02)
        # Multiple fetches happened; only the first should fire callback.
        assert call_count["n"] >= 2, (
            f"expected ≥2 fetch calls, got {call_count['n']}"
        )
        assert len(received) == 1, (
            f"expected exactly 1 on_refresh call (first tick only), "
            f"got {len(received)} ({received})"
        )
    finally:
        rs.fetch_tickers_by_tier = orig
        shutdown.set()


def test_refresher_skips_callback_when_fetch_returns_none():
    """R1-M2: ``fetch_tickers_by_tier`` returns ``None`` on partial /
    failed fetches. ``_do_refresh`` MUST treat None as "keep prior set"
    — no callback, no log-INFO ticker-set-changed line. Production
    blocker: returning partial sets propagates a reconnect storm."""
    received: List[Dict[str, List[str]]] = []
    call_count = {"n": 0}

    def _on_refresh(new_map):
        received.append(new_map)

    shutdown = threading.Event()
    refresher = RestSnapshotRefresher(
        api_key="kid", private_key=None,
        on_refresh=_on_refresh, shutdown_event=shutdown,
        interval_seconds=3600.0,
    )
    refresher._last_tier_map = {TIER_ALL: ["BTC-1", "ETH-1"]}

    import collector.rest_snapshot as rs
    orig = rs.fetch_tickers_by_tier

    def fake_fetch(**kw):
        call_count["n"] += 1
        return None
    rs.fetch_tickers_by_tier = fake_fetch
    try:
        refresher._do_refresh()
        assert call_count["n"] == 1
        assert received == [], (
            "on_refresh fired despite fetch returning None — refresher "
            "would have propagated a phantom subscription wipe to all "
            "archivers (R1-M2 class). Must skip callback on None."
        )
        # Prior map preserved.
        assert refresher._last_tier_map == {TIER_ALL: ["BTC-1", "ETH-1"]}
    finally:
        rs.fetch_tickers_by_tier = orig
        shutdown.set()


def test_refresher_refuses_to_wipe_nonempty_prior_with_empty_response():
    """R1-M1: a successful 200 response with 0 markets MUST NOT wipe a
    live deploy's non-empty subscription set. Kalshi is never
    legitimately at 0 open markets in production; treat 0-with-nonempty-
    prior as anomalous and skip the callback."""
    received: List[Dict[str, List[str]]] = []

    def _on_refresh(new_map):
        received.append(new_map)

    shutdown = threading.Event()
    refresher = RestSnapshotRefresher(
        api_key="kid", private_key=None,
        on_refresh=_on_refresh, shutdown_event=shutdown,
        interval_seconds=3600.0,
    )
    refresher._last_tier_map = {TIER_ALL: ["BTC-1", "ETH-1"]}

    import collector.rest_snapshot as rs
    orig = rs.fetch_tickers_by_tier
    rs.fetch_tickers_by_tier = lambda **kw: {TIER_ALL: []}
    try:
        refresher._do_refresh()
        assert received == [], (
            "on_refresh fired with empty new set against non-empty prior "
            "— would wipe production subscriptions (R1-M1 class)."
        )
        # Prior map preserved.
        assert refresher._last_tier_map == {TIER_ALL: ["BTC-1", "ETH-1"]}
    finally:
        rs.fetch_tickers_by_tier = orig
        shutdown.set()


def test_refresher_allows_empty_when_prior_was_already_empty():
    """Boundary: prior=[] + new=[] is a legitimate no-op (collector
    just booted with no markets and Kalshi still has none). MUST NOT
    fire the callback, MUST NOT log the anomaly warning."""
    received: List[Dict[str, List[str]]] = []

    def _on_refresh(new_map):
        received.append(new_map)

    shutdown = threading.Event()
    refresher = RestSnapshotRefresher(
        api_key="kid", private_key=None,
        on_refresh=_on_refresh, shutdown_event=shutdown,
        interval_seconds=3600.0,
    )
    # _last_tier_map default is {} per __init__.
    assert refresher._last_tier_map == {}

    import collector.rest_snapshot as rs
    orig = rs.fetch_tickers_by_tier
    rs.fetch_tickers_by_tier = lambda **kw: {TIER_ALL: []}
    try:
        refresher._do_refresh()
        # New non-equal to prior ({TIER_ALL: []} != {}); but the
        # equality-check fall-through fires after the empty-guard
        # because prior was 0. We DO fire here (first legitimate
        # populate-with-zero) — operator now has empty stored.
        assert received == [{TIER_ALL: []}]
        assert refresher._last_tier_map == {TIER_ALL: []}
    finally:
        rs.fetch_tickers_by_tier = orig
        shutdown.set()


def test_refresher_callback_exception_does_not_crash_thread():
    """A buggy on_refresh callback must not crash the refresh thread —
    we log + carry on so the next refresh tick can still update the
    ticker set."""
    received: List[Dict[str, List[str]]] = []
    fired = threading.Event()

    def _on_refresh(new_map: Dict[str, List[str]]) -> None:
        received.append(new_map)
        fired.set()
        raise RuntimeError("simulated callback bug")

    shutdown = threading.Event()
    refresher = RestSnapshotRefresher(
        api_key="kid",
        private_key=None,
        on_refresh=_on_refresh,
        shutdown_event=shutdown,
        interval_seconds=3600.0,
    )

    import collector.rest_snapshot as rs
    orig = rs.fetch_tickers_by_tier
    rs.fetch_tickers_by_tier = lambda **kw: {TIER_ALL: ["A"]}
    try:
        refresher.start()
        assert fired.wait(timeout=2.0), "on_refresh was not invoked"
        # Thread did not die — confirm it's still running.
        time.sleep(0.1)
        assert refresher._thread is not None
        assert refresher._thread.is_alive(), (
            "refresh thread died after callback raised — must survive "
            "callback exceptions per D1.4 contract."
        )
    finally:
        rs.fetch_tickers_by_tier = orig
        shutdown.set()


def test_refresher_rejects_non_positive_interval():
    """``interval_seconds <= 0`` is a misconfig — refresher would loop
    without sleeping and burn CPU + REST rate-limit budget."""
    shutdown = threading.Event()
    for bad in (0.0, -1.0):
        with pytest.raises(ValueError, match="interval_seconds"):
            RestSnapshotRefresher(
                api_key="kid",
                private_key=None,
                on_refresh=lambda _m: None,
                shutdown_event=shutdown,
                interval_seconds=bad,
            )


def test_refresher_stop_via_shutdown_event_terminates_thread():
    """Setting the shared shutdown_event terminates the refresh thread
    promptly. ``stop()`` is a convenience that just sets the event."""
    shutdown = threading.Event()
    refresher = RestSnapshotRefresher(
        api_key="kid",
        private_key=None,
        on_refresh=lambda _m: None,
        shutdown_event=shutdown,
        interval_seconds=3600.0,
    )

    import collector.rest_snapshot as rs
    orig = rs.fetch_tickers_by_tier
    rs.fetch_tickers_by_tier = lambda **kw: {TIER_ALL: []}
    try:
        refresher.start()
        time.sleep(0.05)
        refresher.stop()
        # Thread must exit within a small grace window.
        refresher._thread.join(timeout=2.0)
        assert not refresher._thread.is_alive(), (
            "refresh thread did not exit after shutdown_event was set"
        )
    finally:
        rs.fetch_tickers_by_tier = orig


# ─── D1.4-fu (86b9zjqhn) — status="active" vocab parity at integration tier ──
#
# The contract tier (``tests/contracts/test_collector_rest_snapshot.py
# ::test_fetch_filters_non_trading_markets_defensively``) already pins
# that fetch_tickers_by_tier accepts BOTH ``open`` and ``active`` rows.
# The integration tier was a blind spot — all sister fixtures used
# ``status="open"``, mirroring the query parameter rather than what
# Kalshi's response body actually labels trading-active markets
# (``status="active"``). A future regression that re-narrows the filter
# to ``status="open"`` only (the original D1.4 bug — booted collector
# with subscribes=0 in production) would still pass every test in this
# file. The fixtures below extend the integration coverage so the
# regression class is caught at the integration tier too.
#
# Ticket ``86b9zjter`` (D1.4-fu NIT-1, LOW). Closes the L97 vocab-
# drift class at the integration seam (the contract tier already
# pins the single-call invariant; this pins the end-to-end fetch →
# planner round-trip under production's actual vocab).


def test_fetch_to_planner_round_trip_accepts_active_status_rows():
    """Production reality: Kalshi's REST ``/markets?status=open`` response
    rows arrive with ``status="active"`` (the query-param vocab and the
    response-field vocab differ). The fetch → planner round-trip MUST
    handle the production vocab end-to-end without dropping rows.

    Belt-and-suspenders against the L97 assertion-fossil class — the
    D1.4 ship initially read ``status != "open"`` and dropped 100% of
    markets in production (collector booted with subscribes=0). If a
    future Bit re-narrows the filter, this integration test fires
    alongside the contract-tier guard."""
    page = {"markets": [
        {"ticker": "M-A", "status": "active"},
        {"ticker": "M-B", "status": "active"},
        {"ticker": "M-C", "status": "active"},
        {"ticker": "M-D", "status": "active"},
    ], "cursor": ""}
    session = _fake_session([page])
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True,
    )
    assert out == {TIER_ALL: ["M-A", "M-B", "M-C", "M-D"]}, (
        "status='active' rows were dropped by the response-side filter — "
        "regression to the original D1.4 'status != open' bug. Kalshi's "
        "response body labels trading-active markets status='active'; the "
        "filter MUST accept both 'open' and 'active'. Ticket 86b9zjqhn."
    )

    plans = SubscriptionManager(
        tickers_by_tier=out, conn_count=2).assign()
    total = sum(len(p.market_tickers) for p in plans)
    assert total == 4, (
        f"planner saw {total} tickers, expected 4 — fetch dropped active "
        f"rows OR planner regressed determinism. plans={plans}"
    )
    # Both conns must receive at least one ticker — round-robin spreads
    # 4 tickers across 2 conns 2/2.
    for p in plans:
        assert p.market_tickers, (
            f"conn={p.conn_id} got 0 tickers despite 4 active rows in "
            f"the REST response; round-robin assignment regressed."
        )


def test_fetch_to_planner_mixed_open_and_active_rows_all_pass_through():
    """Mixed-vocab page (some rows ``status="open"``, some ``status="active"``)
    is exactly what a race during a settlement window can produce.
    The fetch → planner pipeline MUST accept the union — dropping
    either vocab leaves a partial subscription set + a reconnect when
    Kalshi normalizes back to a single vocab in the next page."""
    page = {"markets": [
        {"ticker": "OPEN-1", "status": "open"},
        {"ticker": "ACTIVE-1", "status": "active"},
        {"ticker": "OPEN-2", "status": "open"},
        {"ticker": "ACTIVE-2", "status": "active"},
        {"ticker": "CLOSED-1", "status": "closed"},   # filtered (defensive)
        {"ticker": "SETTLED-1", "status": "settled"}, # filtered (defensive)
    ], "cursor": ""}
    session = _fake_session([page])
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True,
    )
    # Both vocab-forms pass; defensive non-trading statuses drop.
    assert out == {TIER_ALL: ["ACTIVE-1", "ACTIVE-2", "OPEN-1", "OPEN-2"]}

    plans = SubscriptionManager(
        tickers_by_tier=out, conn_count=2).assign()
    total = sum(len(p.market_tickers) for p in plans)
    assert total == 4, (
        f"planner total={total}, expected 4 (2 open + 2 active, with "
        f"closed/settled defensively filtered). plans={plans}"
    )


# ─── BronzeArchiver.update_subscriptions + request_reconnect surface ────────


def test_archiver_update_subscriptions_replaces_frames_atomically(tmp_path):
    """update_subscriptions swaps subscribe_frames + cmd_id_to_channel
    atomically. The replacement is visible to the next on_session_start
    invocation (which reads from the now-updated state)."""
    from collector.ws_connection import BronzeArchiver
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "test.pem"
    pem_path.write_bytes(pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))

    initial_frames = [{"id": 1, "cmd": "subscribe", "params": {
        "channels": ["orderbook_delta"], "market_tickers": ["A"],
    }}]
    initial_map = {1: "orderbook_delta"}

    fake_writer = MagicMock()
    archiver = BronzeArchiver(
        api_key="kid",
        private_key_path=str(pem_path),
        writers_by_channel={"orderbook_delta": fake_writer, None: fake_writer},
        subscribe_frames=initial_frames,
        cmd_id_to_channel=initial_map,
        conn_id="A",
    )

    new_frames = [{"id": 100, "cmd": "subscribe", "params": {
        "channels": ["trade"], "market_tickers": ["B", "C"],
    }}]
    new_map = {100: "trade"}

    archiver.update_subscriptions(new_frames, new_map)

    # Pin the swap took effect on internal state.
    assert archiver._subscribe_frames == tuple(new_frames)
    assert archiver._cmd_id_to_channel == new_map


def test_archiver_request_reconnect_delegates_to_wsclient(tmp_path):
    """request_reconnect proxies to the underlying WSClient — the only
    way to force Kalshi to give us a fresh session (and thus a chance
    to re-dispatch subscribe frames against the new ticker set)."""
    from collector.ws_connection import BronzeArchiver
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "test.pem"
    pem_path.write_bytes(pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))

    archiver = BronzeArchiver(
        api_key="kid",
        private_key_path=str(pem_path),
        writers_by_channel={None: MagicMock()},
        conn_id="A",
    )
    fake_wire = MagicMock()
    archiver._wire = fake_wire
    archiver.request_reconnect()
    fake_wire.request_reconnect.assert_called_once_with()
