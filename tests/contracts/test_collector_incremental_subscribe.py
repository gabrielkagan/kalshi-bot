"""P0 collector coverage fix — sub-hourly incremental subscribe.

Ticket 86ba74hzy. Plan: kb/decisions/collector-sub-hourly-incremental-subscribe-plan.md

RCA: the collector discovers markets via a fixed HOURLY REST snapshot and
applies changes by force-reconnect. Markets whose lifespan < poll interval
(15M crypto windows, ~15 min) are caught only ~25% of the time — the rest open
AND close inside the hourly gap and are never subscribed → permanent bronze loss.

Fix: a fast incremental discovery path scoped to the sub-hourly crypto-15M
series that dispatches subscribe frames MID-SESSION via send_frame (no reconnect,
so the D1.3-fu4 ack-flood/OOM class can't reopen).

These tests are TDD-first (RED before implementation):
  1. CRYPTO_15M_SERIES exists + drift-pins against bot.constants.SERIES_TICKERS.
  2. fetch_open_tickers_for_series queries per-series + unions.
  3. IncrementalDiscoveryRefresher fires on_new with the open set (immediate first tick).
  4. BronzeArchiver.add_subscriptions merges maps + dispatches mid-session, no reconnect.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest


# ─── 1. CRYPTO_15M_SERIES constant + drift-pin ──────────────────────────────


def test_crypto_15m_series_constant_exists():
    from collector import rest_snapshot as rs
    assert isinstance(rs.CRYPTO_15M_SERIES, tuple), (
        "CRYPTO_15M_SERIES must be a tuple of Kalshi series_ticker strings."
    )
    assert rs.CRYPTO_15M_SERIES, "CRYPTO_15M_SERIES must be non-empty."
    assert all(isinstance(s, str) and s for s in rs.CRYPTO_15M_SERIES)


def test_crypto_15m_series_mirrors_bot_series_tickers():
    """Drift-pin (mirrors the LEAGUES_ESPN pattern): collector cannot import
    bot.* (collector-no-bot contract), so it mirrors the 15M series list. This
    test fails RED if the bot's SERIES_TICKERS drifts from the collector mirror,
    forcing the operator to update both sides + restart kalshi-collector."""
    from collector import rest_snapshot as rs
    from bot.constants import SERIES_TICKERS

    assert set(rs.CRYPTO_15M_SERIES) == set(SERIES_TICKERS.values()), (
        "collector.rest_snapshot.CRYPTO_15M_SERIES drifted from "
        "bot.constants.SERIES_TICKERS. Update the collector mirror + restart "
        "kalshi-collector to pick up the new 15M series."
    )


# ─── 2. fetch_open_tickers_for_series ───────────────────────────────────────


def _series_session(by_series: dict):
    """MagicMock session whose .get returns a single open page per series_ticker
    param. ``by_series`` maps series_ticker -> [tickers]."""
    def _get(url, params=None, headers=None, timeout=None):
        series = (params or {}).get("series_ticker")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "markets": [
                {"ticker": t, "status": "active"}
                for t in by_series.get(series, [])
            ],
            "cursor": "",
        }
        return resp
    session = MagicMock()
    session.get.side_effect = _get
    return session


def test_fetch_open_tickers_for_series_unions_across_series():
    from collector import rest_snapshot as rs
    session = _series_session({
        "KXBTC15M": ["KXBTC15M-A", "KXBTC15M-B"],
        "KXETH15M": ["KXETH15M-A"],
    })
    out = rs.fetch_open_tickers_for_series(
        series_tickers=("KXBTC15M", "KXETH15M"),
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    assert out == {"KXBTC15M-A", "KXBTC15M-B", "KXETH15M-A"}


def test_fetch_open_tickers_for_series_passes_series_param():
    from collector import rest_snapshot as rs
    session = _series_session({"KXBTC15M": ["KXBTC15M-A"]})
    rs.fetch_open_tickers_for_series(
        series_tickers=("KXBTC15M",),
        api_key="kid", private_key=None, session=session, _test_skip_auth=True,
    )
    call = session.get.call_args_list[0]
    params = call.kwargs.get("params") or call.args[1]
    assert params.get("series_ticker") == "KXBTC15M"
    assert params.get("status") == "open"


# ─── 3. IncrementalDiscoveryRefresher ───────────────────────────────────────


def test_incremental_refresher_fires_on_new_with_open_set(monkeypatch):
    from collector import rest_snapshot as rs

    monkeypatch.setattr(
        rs, "fetch_open_tickers_for_series",
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
        series_tickers=("KXBTC15M",),
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
            api_key="k", private_key=None, series_tickers=("X",),
            on_new=lambda s: None, shutdown_event=threading.Event(),
            interval_seconds=0,
        )


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
    """Data-backed (CLAUDE.md 'no config tuning without data'): the scoped poll
    fires one request per crypto-15M series per tick. Even as a same-instant
    burst, that must stay well under Kalshi's READ_RATE_LIMIT (30 req/s,
    Advanced tier; collector runs on its own key). At 7 series / 10s = 0.7
    req/s avg, 7 req/s peak — comfortable headroom. This guards against a
    future interval drop + series-count growth jointly breaching the budget."""
    from collector import rest_snapshot as rs
    from bot.constants import READ_RATE_LIMIT

    n_series = len(rs.CRYPTO_15M_SERIES)
    # Peak burst (all series fired same instant) must keep ≥2× headroom.
    assert n_series <= READ_RATE_LIMIT / 2, (
        f"peak burst {n_series} req would exceed half the {READ_RATE_LIMIT} "
        "req/s read budget — add a per-request spacing or split the poll."
    )
    # Sustained average must stay under 10% of the read budget.
    avg_rps = n_series / rs.DEFAULT_INCREMENTAL_REFRESH_SECONDS
    assert avg_rps < READ_RATE_LIMIT * 0.1, (
        f"avg {avg_rps:.2f} req/s exceeds 10% of the {READ_RATE_LIMIT} req/s "
        "read budget; raise the interval or reduce the series scope."
    )
