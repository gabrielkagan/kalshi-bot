"""P0 collector coverage fix — sub-hourly incremental subscribe.

Ticket 86ba74hzy. Plan: kb/decisions/collector-sub-hourly-incremental-subscribe-plan.md

RCA: the collector discovers markets via a fixed HOURLY REST snapshot and
applies changes by force-reconnect. Markets whose lifespan < poll interval
(15M crypto windows, ~15 min) are caught only ~25% of the time — the rest open
AND close inside the hourly gap and are never subscribed → permanent bronze loss.

Fix: a fast incremental discovery path that dispatches subscribe frames
MID-SESSION via send_frame (no reconnect, so the D1.3-fu4 ack-flood/OOM class
can't reopen).

Ticket 86bbvdc8y (2026-09-05) GENERALIZED the discovery query. The 86ba74hzy
ship polled a hand-mirrored ``CRYPTO_15M_SERIES`` tuple (one request per
series), so every 15M family the bot did NOT trade — KXNEAR15M / KXZEC15M
(2026-06-30), KXGOLD/WTI/SILVER15M (2026-07-31), KXCOPPER/NATGAS15M
(2026-08-27), KXCRYPTOLEAD15M, FX + index 15M among others (27 fifteen_min
series on the venue, 18 untraded) — was NEVER discovered; measured
Sep-3 14Z orderbook hour: BTC 7 windows, BNB 9, NEAR 1 (stale), GOLD/ZEC/WTI/
SILVER 0. The poll is now ONE series-agnostic ``/markets?status=open&
min_close_ts=now&max_close_ts=now+horizon`` query (default horizon 1200s ≥ the
900s window lifespan + poll lag) filtered by the same ``excluded_series``
firehose list the hourly snapshot uses. No series list to maintain; a new 15M
series on the venue is collected from its first window.

These tests are TDD-first (RED before implementation):
  1. Horizon constant is ≥ a 15-min window + one poll interval; no series list.
  2. fetch_open_tickers_closing_within issues one close-window query, pages,
     filters excluded series, never raises.
  3. IncrementalDiscoveryRefresher fires on_new with the open set (immediate first tick).
  4. BronzeArchiver.add_subscriptions merges maps + dispatches mid-session, no reconnect.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest


# ─── 1. Horizon constant — series-agnostic discovery (86bbvdc8y) ────────────


def test_no_hand_mirrored_series_list_remains():
    """86bbvdc8y: the hand-mirrored ``CRYPTO_15M_SERIES`` tuple was the
    structural cause of the NEAR/ZEC/commodity bronze gap (any 15M series the
    bot did not trade was never discovered). It must be GONE so it cannot be
    re-wired as the discovery scope."""
    from collector import rest_snapshot as rs
    assert not hasattr(rs, "CRYPTO_15M_SERIES"), (
        "CRYPTO_15M_SERIES must not exist — discovery is series-agnostic "
        "(close-horizon query). Re-adding a series list reopens the "
        "new-15M-family bronze gap measured 2026-09-05."
    )
    assert not hasattr(rs, "fetch_open_tickers_for_series")


def test_horizon_covers_a_full_window_plus_poll_lag():
    """A 15M window is listed ~15 min before close. To subscribe it at (or
    before) its open, the close-horizon must be ≥ 900s + one poll interval."""
    from collector import rest_snapshot as rs
    assert isinstance(rs.DEFAULT_INCREMENTAL_HORIZON_SECONDS, float)
    assert rs.DEFAULT_INCREMENTAL_HORIZON_SECONDS >= (
        900.0 + rs.DEFAULT_INCREMENTAL_REFRESH_SECONDS
    ), "horizon must cover a full 15-min window plus one poll interval."
    # Bounded: a multi-hour horizon would sweep in hourly/daily markets that
    # the hourly snapshot already covers and inflate per-tick payload.
    assert rs.DEFAULT_INCREMENTAL_HORIZON_SECONDS <= 3600.0


# ─── 2. fetch_open_tickers_closing_within ───────────────────────────────────


def _horizon_session(pages):
    """MagicMock session returning ``pages`` (list of market-row lists) in
    order, threading a cursor between them. Records every ``params``."""
    calls = []

    def _get(url, params=None, headers=None, timeout=None):
        idx = len(calls)
        calls.append(dict(params or {}))
        resp = MagicMock()
        resp.status_code = 200
        rows = pages[idx] if idx < len(pages) else []
        resp.json.return_value = {
            "markets": rows,
            "cursor": f"c{idx + 1}" if idx + 1 < len(pages) else "",
        }
        return resp
    session = MagicMock()
    session.get.side_effect = _get
    session._calls = calls
    return session


def _row(ticker, status="open"):
    return {"ticker": ticker, "status": status}


def test_fetch_closing_within_is_one_close_window_query_no_series_param():
    from collector import rest_snapshot as rs
    session = _horizon_session([[_row("KXBTC15M-A"), _row("KXGOLD15M-B")]])
    out = rs.fetch_open_tickers_closing_within(
        horizon_seconds=1200.0, excluded_series=(),
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
        _now=1_000_000.0,
    )
    assert out == {"KXBTC15M-A", "KXGOLD15M-B"}
    assert len(session._calls) == 1, "single page → exactly one request"
    p = session._calls[0]
    assert "series_ticker" not in p, "discovery must be series-agnostic"
    assert p["status"] == "open"
    assert p["min_close_ts"] == 1_000_000
    assert p["max_close_ts"] == 1_000_000 + 1200
    assert p["limit"] == rs._HORIZON_PAGE_LIMIT


def test_fetch_closing_within_pages_until_cursor_exhausted():
    from collector import rest_snapshot as rs
    session = _horizon_session([[_row("A-1")], [_row("B-1")], [_row("C-1")]])
    out = rs.fetch_open_tickers_closing_within(
        horizon_seconds=1200.0, excluded_series=(),
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    assert out == {"A-1", "B-1", "C-1"}
    assert [c.get("cursor") for c in session._calls] == [None, "c1", "c2"]


def test_fetch_closing_within_filters_excluded_firehose_series():
    """The close-window sweep sees EVERY short-lived market, incl. the esports
    / MVE firehose the hourly snapshot excludes (measured 2026-09-05: 374 of
    388 markets closing within 16 min were KXMVECROSSCATEGORY). The same
    ``excluded_series`` prefixes MUST apply here or the incremental path would
    re-open the socket.send() reconnect-storm class (86ba76adw)."""
    from collector import rest_snapshot as rs
    session = _horizon_session([[
        _row("KXMVECROSSCATEGORY-26SEP05-X"), _row("KXNEAR15M-26SEP051600-00"),
        _row("KXMVESPORTSMULTIGAMEEXTENDED-1"), _row("KXWTI15M-26SEP051600-00"),
    ]])
    out = rs.fetch_open_tickers_closing_within(
        horizon_seconds=1200.0, excluded_series=rs.DEFAULT_EXCLUDED_SERIES,
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    assert out == {"KXNEAR15M-26SEP051600-00", "KXWTI15M-26SEP051600-00"}


def test_fetch_closing_within_excluded_match_is_series_exact():
    """``KXBTC15M`` excluded must NOT exclude a lookalike ``KXBTC15MX``; the
    match is an exact series match on the pre-``-`` segment via the shared
    ``is_excluded_series`` helper (single chokepoint with the hourly
    snapshot — R1-MN1)."""
    from collector import rest_snapshot as rs
    session = _horizon_session([[_row("KXBTC15M-A"), _row("KXBTC15MX-A")]])
    out = rs.fetch_open_tickers_closing_within(
        horizon_seconds=1200.0, excluded_series=("KXBTC15M",),
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    assert out == {"KXBTC15MX-A"}
    assert rs.is_excluded_series("KXBTC15M-A", ("KXBTC15M",)) is True
    assert rs.is_excluded_series("KXBTC15MX-A", ("KXBTC15M",)) is False
    assert rs.is_excluded_series("KXBTC15M-A", ()) is False
    # Both fetch paths must route through the one helper (source pin).
    import inspect
    assert inspect.getsource(rs.fetch_tickers_by_tier).count("is_excluded_series(") == 1
    assert inspect.getsource(rs.fetch_open_tickers_closing_within).count("is_excluded_series(") == 1


def test_main_loop_threads_excluded_series_and_horizon_into_refresher():
    """R1-MN8: the storm-reopen vector is a DROPPED kwarg at the wiring site.
    Pin that main_loop constructs IncrementalDiscoveryRefresher with the same
    ``excluded_series`` it hands the hourly refresher + the env-driven horizon."""
    import inspect
    import collector.main_loop as ml
    src = inspect.getsource(ml.run)
    i = src.index("IncrementalDiscoveryRefresher(")
    block = src[i:src.index(")", i + 1) + 1]
    assert "excluded_series=excluded_series" in block
    assert "horizon_seconds=incremental_horizon_seconds" in block


def test_fetch_closing_within_skips_non_open_rows_and_never_raises():
    from collector import rest_snapshot as rs
    session = _horizon_session([[_row("A-1", status="settled"), _row("B-1")]])
    out = rs.fetch_open_tickers_closing_within(
        horizon_seconds=1200.0, excluded_series=(),
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    assert out == {"B-1"}
    # Transport failure → empty set, no exception (best-effort ADD path).
    bad = MagicMock()
    bad.get.side_effect = RuntimeError("boom")
    out2 = rs.fetch_open_tickers_closing_within(
        horizon_seconds=1200.0, excluded_series=(),
        api_key="kid", private_key=None, session=bad, _test_skip_auth=True,
        _test_backoff_seconds=0.0,
    )
    assert out2 == set()


# ─── 3. IncrementalDiscoveryRefresher ───────────────────────────────────────


def test_incremental_refresher_fires_on_new_with_open_set(monkeypatch):
    from collector import rest_snapshot as rs

    monkeypatch.setattr(
        rs, "fetch_open_tickers_closing_within",
        lambda **kw: {"KXBTC15M-A", "KXBTC15M-B"},
    )
    seen = {}
    ev = threading.Event()

    def _on_new(open_set):
        seen["set"] = set(open_set)
        ev.set()

    shutdown = threading.Event()
    ref = rs.IncrementalDiscoveryRefresher(
        api_key="kid", private_key=None,
        excluded_series=(),
        on_new=_on_new,
        shutdown_event=shutdown,
        interval_seconds=3600.0,  # long — we only want the immediate first tick
    )
    ref.start()
    assert ev.wait(timeout=5.0), "on_new not invoked on the immediate first tick."
    shutdown.set()
    assert seen["set"] == {"KXBTC15M-A", "KXBTC15M-B"}


def test_incremental_refresher_rejects_nonpositive_interval():
    from collector import rest_snapshot as rs
    with pytest.raises(ValueError):
        rs.IncrementalDiscoveryRefresher(
            api_key="k", private_key=None, excluded_series=(),
            on_new=lambda s: None, shutdown_event=threading.Event(),
            interval_seconds=0,
        )


def test_incremental_refresher_passes_horizon_and_exclusions_to_fetch(monkeypatch):
    """The refresher is the ONLY caller of the horizon fetch; pin that it
    threads its horizon + the shared firehose exclusions through (a dropped
    kwarg would silently widen the sweep to the excluded series)."""
    from collector import rest_snapshot as rs
    seen = {}
    ev = threading.Event()

    def _fake(**kw):
        seen.update(kw)
        ev.set()
        return set()
    monkeypatch.setattr(rs, "fetch_open_tickers_closing_within", _fake)
    shutdown = threading.Event()
    ref = rs.IncrementalDiscoveryRefresher(
        api_key="kid", private_key=None,
        excluded_series=("KXMVECROSSCATEGORY",),
        on_new=lambda s: None, shutdown_event=shutdown,
        interval_seconds=3600.0, horizon_seconds=1500.0,
    )
    ref.start()
    assert ev.wait(timeout=5.0)
    shutdown.set()
    assert seen["horizon_seconds"] == 1500.0
    assert seen["excluded_series"] == ("KXMVECROSSCATEGORY",)


# ─── 4. BronzeArchiver.add_subscriptions ────────────────────────────────────


def _make_archiver(monkeypatch):
    import collector.ws_connection as wc

    monkeypatch.setattr(wc, "load_private_key", lambda _p: object())
    fake_wire = MagicMock(name="WSClient")
    fake_wire.is_connected = True
    monkeypatch.setattr(wc, "WSClient", MagicMock(return_value=fake_wire))

    writers_by_channel = {"orderbook_delta": MagicMock(), None: MagicMock()}
    archiver = wc.BronzeArchiver(
        api_key="kid",
        private_key_path="/nonexistent.pem",
        writers_by_channel=writers_by_channel,
        subscribe_frames=({"id": 1, "cmd": "subscribe",
                           "params": {"channels": ["orderbook_delta"],
                                      "market_tickers": ["OLD"]}},),
        cmd_id_to_channel={1: "orderbook_delta"},
        conn_id="A",
    )
    archiver._wire = fake_wire
    return archiver, fake_wire


def test_add_subscriptions_dispatches_mid_session_without_reconnect(monkeypatch):
    archiver, fake_wire = _make_archiver(monkeypatch)
    new_frames = [
        {"id": 900001, "cmd": "subscribe",
         "params": {"channels": ["orderbook_delta"], "market_tickers": ["NEW"]}},
    ]
    archiver.add_subscriptions(new_frames, {900001: "orderbook_delta"})

    # Dispatched the NEW frame mid-session...
    sent = [c.args[0] for c in fake_wire.send_frame.call_args_list]
    assert new_frames[0] in sent, "new subscribe frame not dispatched via send_frame."
    # ...and did NOT force a reconnect (that's the OOM class we're avoiding).
    fake_wire.request_reconnect.assert_not_called()


def test_add_subscriptions_merges_cmd_id_and_frames(monkeypatch):
    archiver, _ = _make_archiver(monkeypatch)
    archiver.add_subscriptions(
        [{"id": 900001, "cmd": "subscribe",
          "params": {"channels": ["orderbook_delta"], "market_tickers": ["NEW"]}}],
        {900001: "orderbook_delta"},
    )
    # cmd_id map keeps the old binding AND gains the new one (so the new sid
    # binds on ack).
    assert archiver._cmd_id_to_channel.get(1) == "orderbook_delta"
    assert archiver._cmd_id_to_channel.get(900001) == "orderbook_delta"
    # frames merged so a mid-cycle reconnect's on_session_start replays them.
    ids = {f["id"] for f in archiver._subscribe_frames}
    assert ids == {1, 900001}


def test_add_subscriptions_swallows_send_failure(monkeypatch):
    """send_frame raising (race with disconnect) must not propagate — the
    frame is already merged into _subscribe_frames so on_session_start replays."""
    archiver, fake_wire = _make_archiver(monkeypatch)
    fake_wire.send_frame.side_effect = ConnectionError("WS not connected")
    # Must not raise.
    archiver.add_subscriptions(
        [{"id": 900001, "cmd": "subscribe",
          "params": {"channels": ["orderbook_delta"], "market_tickers": ["NEW"]}}],
        {900001: "orderbook_delta"},
    )
    assert any(f["id"] == 900001 for f in archiver._subscribe_frames)


# ─── 5. _plan_incremental_adds — the wiring invariants (MAJOR-2, R1) ────────


def _hourly_ranges(conn_count: int):
    """The hourly planner's per-conn cmd_id ranges:
    [idx*STRIDE+1 .. (idx+1)*STRIDE] for idx in range(conn_count)."""
    from collector.main_loop import _PER_CONN_CMD_ID_STRIDE as S
    return [(i * S + 1, (i + 1) * S) for i in range(conn_count)]


def _fast_base(conn_count: int) -> int:
    from collector.main_loop import _PER_CONN_CMD_ID_STRIDE as S
    return conn_count * S + 1


def test_plan_incremental_adds_empty_when_nothing_new():
    from collector.main_loop import _plan_incremental_adds
    tracked = {"A", "B"}
    dispatch, rr, nxt, new = _plan_incremental_adds(
        open_set={"A", "B"}, tracked=tracked, rr=0,
        next_cmd_id=_fast_base(3), conn_ids=["A", "B", "C"], batch_size=1000,
    )
    assert dispatch == []
    assert new == []
    assert rr == 0 and nxt == _fast_base(3), "cursors must be unchanged on no-op."


def test_plan_incremental_adds_dedups_already_tracked():
    from collector.main_loop import _plan_incremental_adds
    dispatch, _, _, new = _plan_incremental_adds(
        open_set={"OLD", "NEW1", "NEW2"}, tracked={"OLD"}, rr=0,
        next_cmd_id=_fast_base(2), conn_ids=["A", "B"], batch_size=1000,
    )
    assert set(new) == {"NEW1", "NEW2"}, "only truly-new tickers are planned."
    planned = {t for _, frames, _ in dispatch for f in frames
               for t in f["params"]["market_tickers"]}
    assert "OLD" not in planned, "already-subscribed ticker must not be re-added."


def test_plan_incremental_adds_cmd_ids_never_collide_with_hourly_ranges():
    """The load-bearing OOM-adjacent invariant: fast cmd_ids must sit ABOVE
    every hourly per-conn range so a fast ack binds the right sid + a future
    refactor that drops the base offset fails RED here."""
    from collector.main_loop import _plan_incremental_adds
    conn_count = 4
    base = _fast_base(conn_count)
    ranges = _hourly_ranges(conn_count)
    # 50 new tickers → multiple conns, multiple frames.
    open_set = {f"KXBTC15M-{i:03d}" for i in range(50)}
    dispatch, _, nxt, _ = _plan_incremental_adds(
        open_set=open_set, tracked=set(), rr=0,
        next_cmd_id=base, conn_ids=list("ABCD"), batch_size=1000,
    )
    all_ids = [f["id"] for _, frames, _ in dispatch for f in frames]
    assert all_ids, "expected at least one frame."
    for cid in all_ids:
        assert cid >= base, f"fast cmd_id {cid} fell below the fast base {base}."
        for lo, hi in ranges:
            assert not (lo <= cid <= hi), (
                f"fast cmd_id {cid} collides with hourly range [{lo},{hi}]."
            )
    assert nxt == base + len(all_ids), "next_cmd_id must advance by frame count."


def test_plan_incremental_adds_advances_cmd_id_monotonically_across_ticks():
    from collector.main_loop import _plan_incremental_adds
    base = _fast_base(2)
    d1, rr1, nxt1, _ = _plan_incremental_adds(
        open_set={"T1"}, tracked=set(), rr=0,
        next_cmd_id=base, conn_ids=["A", "B"], batch_size=1000,
    )
    d2, _, nxt2, _ = _plan_incremental_adds(
        open_set={"T2"}, tracked={"T1"}, rr=rr1,
        next_cmd_id=nxt1, conn_ids=["A", "B"], batch_size=1000,
    )
    ids1 = {f["id"] for _, fr, _ in d1 for f in fr}
    ids2 = {f["id"] for _, fr, _ in d2 for f in fr}
    assert min(ids2) >= nxt1 > max(ids1), "second tick ids must not reuse first."


def test_plan_incremental_adds_round_robin_balances_across_conns():
    from collector.main_loop import _plan_incremental_adds
    open_set = {f"T{i:02d}" for i in range(9)}
    dispatch, rr, _, new = _plan_incremental_adds(
        open_set=open_set, tracked=set(), rr=0,
        next_cmd_id=_fast_base(3), conn_ids=["A", "B", "C"], batch_size=1000,
    )
    assert rr == 9, "round-robin cursor must advance by the new-ticker count."
    # 9 tickers over 3 conns → each conn gets 3.
    per_conn = {idx: sum(len(f["params"]["market_tickers"]) for f in frames)
                for idx, frames, _ in dispatch}
    # Each channel duplicates the ticker set, so divide by channel count.
    from collector.subscription_manager import CHANNELS_DEFAULT
    counts = {idx: c // len(CHANNELS_DEFAULT) for idx, c in per_conn.items()}
    assert sorted(counts.values()) == [3, 3, 3], f"unbalanced: {counts}"


# ─── 6. Discovery cadence — data-backed (86ba74hzy-fu, 2026-05-30) ──────────


def test_incremental_default_interval_captures_near_full_window():
    """The default discovery cadence must be fast enough to capture ~15/15 of
    a ~15-min (900s) window. At 60s the worst-case lost sliver was ≤60s (~4%);
    tightened to ≤10s (≤~1.1%) per the 86ba74hzy follow-up. Pins the coverage
    INTENT so a future bump back toward 60s fails RED."""
    from collector import rest_snapshot as rs
    assert rs.DEFAULT_INCREMENTAL_REFRESH_SECONDS <= 10.0, (
        "incremental discovery interval must be ≤10s so each ~15-min window "
        "is discovered within ≤10s of opening (captures ≥~98.9% of it)."
    )


def test_incremental_poll_stays_well_under_read_rate_limit():
    """Data-backed (CLAUDE.md 'no config tuning without data'): the close-
    horizon poll is ONE request per tick (+1 per extra 1000-row page; measured
    2026-09-05: 388 rows within 16 min incl. the MVE firehose → 1 page). At
    10s that is 0.1 req/s average, ~10× cheaper than the retired 9-series
    poll (0.9 req/s), vs Kalshi's READ_RATE_LIMIT (30 req/s, Advanced tier;
    the collector runs on its own key). Pins the page size so a future drop
    back to 200 rows/page cannot silently multiply request count."""
    from collector import rest_snapshot as rs
    from bot.constants import READ_RATE_LIMIT

    assert rs._HORIZON_PAGE_LIMIT == 1000, (
        "close-horizon poll must request 1000 rows/page so the sweep is "
        "normally a single request per tick."
    )
    # Sustained average (1 page/tick) must stay under 10% of the read budget.
    avg_rps = 1.0 / rs.DEFAULT_INCREMENTAL_REFRESH_SECONDS
    assert avg_rps < READ_RATE_LIMIT * 0.1
