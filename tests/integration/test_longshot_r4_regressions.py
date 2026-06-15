"""Bit L-1 adversarial-review R4 regressions (longshot premium-harvest).

One test class per R4 finding (M1 / M2 / MN1 / MN2 / MN3). Fixtures mirror
tests/integration/test_longshot_r3_regressions.py (real sqlite3 file via
tmp_path per tests/CLAUDE.md integration-tier convention); the scan()-level
harness mirrors tests/integration/test_longshot_r2_regressions.py.

M1 — longshot self-collision: a sell-YES fill creates the (ticker,
side='no', strategy_group='longshot') positions row; when spot crosses the
strike the OPPOSITE side (sell-NO -> buy YES) qualifies, and
record_position_from_fill matches WHERE ticker+strategy_group with NO side
predicate — the opposite-side fill ACCUMULATES under the OLD side, so
settlement books winners as losers and caps/marks/streaks corrupt. The
invariant is ONE open longshot row per ticker (ticker-PK reality,
86badbf9t): _allowed_size returns 0 on any open opposite-side longshot row,
and the scanner overlay mirrors the guard defensively.

M2 — boot delta-apply skip seed misattributed across SEQUENTIAL
same-(ticker, side) orders: order-1 partially fills 1 (recorded) ->
canceled; order-2 quotes the remainder; crash with order-2's fills
unpolled; restart seeded boot_skip_remaining from the (ticker, side)
AGGREGATE (=1 from order-1) and skipped a REAL order-2 fill — permanent
under-record. Fix: per-order `recorded_fill_count` on the pending_orders
row seeds each order's OWN skip; aggregate is the NULL-legacy fallback.

MN1 — cleanup_expired_resting_orders lacked the ls- carve-out that
_reconcile_orders has (R3-M1): the settlement daemon calls it concurrently
and flipping a past-close ls- row to 'expired' hides it from the engine's
boot step-2 query (status='resting').

MN2 — the ls--history stamp in _reconcile_positions was
existence-not-recency: ANY old ls- row claimed the import as longshot even
when the main pipeline most recently traded the ticker.

MN3 — boot step-1 adoption mixed units: registered count=REMAINING while
q["filled"] accumulates CUMULATIVE fetched fills (skip included), so a
2-of-3-prefilled orphan popped as 'filled' (2 >= 1) with a (2/1)
LONGSHOT_FILL log while the remainder was actually canceled.
"""
from __future__ import annotations

import datetime
import logging
import re
import time
from datetime import timezone
from unittest.mock import MagicMock

import pytest

import bot.constants as C
from bot.state import StateManager

from bot.longshot import LongshotEngine

TICKER = "KXBTC15M-26JUN111200-T104"
EVENT = "KXBTC15M-26JUN111200"
TICKER2 = "KXBTC15M-26JUN111215-T104"
EVENT2 = "KXBTC15M-26JUN111215"


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "test_longshot_r4.db"))
    yield s
    s.close()


@pytest.fixture
def client():
    cl = MagicMock()
    cl.get_fills.return_value = {"fills": []}
    cl.get_orders.return_value = {"orders": []}
    cl.get_positions.return_value = {"market_positions": []}
    cl.cancel_order.return_value = {"order": {"status": "canceled"}}
    cl.place_order.return_value = {"order": {"order_id": "oid-r4-1"}}
    cl.get_balance.return_value = {"balance": 50000}
    # REST orderbook: YES bids at 3c, NO bids at 92c -> yes_ask=8c, no_ask=97c
    cl.get_orderbook.return_value = {
        "orderbook": {"yes": [[3, 50]], "no": [[92, 50]]}}
    return cl


@pytest.fixture
def engine(state, client):
    return LongshotEngine(client, state)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(C, "LONGSHOT_ENABLED", True, raising=False)


def _rfc3339(epoch):
    return datetime.datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _seed_pending_resting(state, *, client_oid, order_id, ticker=TICKER,
                          event=EVENT, side="no", count=3, price=92,
                          created_epoch=None):
    state.insert_bot_order(client_oid, ticker, event, "BTC", side, count,
                           price, False)
    state.confirm_order_submitted(client_oid, order_id)
    if created_epoch is not None:
        state.conn.execute(
            "UPDATE pending_orders SET created_at=? WHERE order_id=?",
            (_rfc3339(created_epoch), order_id))
        state.conn.commit()


def _min_ts_respecting_fills(fills):
    """get_fills side_effect that filters like the real API: only fills
    with ts >= min_ts are returned."""
    def _side(min_ts=None, cursor=None, **kw):
        out = [f for f in fills
               if min_ts is None or f.get("ts", 0) >= min_ts]
        return {"fills": out}
    return _side


def _positions_row(state, ticker=TICKER):
    return state.conn.execute(
        "SELECT side, count, total_cost_cents, strategy, strategy_group "
        "FROM positions WHERE ticker=? AND status='open'",
        (ticker,)).fetchone()


def _pending_status(state, order_id):
    return state.conn.execute(
        "SELECT status FROM pending_orders WHERE order_id=?",
        (order_id,)).fetchone()["status"]


def _record_longshot(state, *, ticker=TICKER, event=EVENT, side="no",
                     count=1, price=92):
    state.record_position_from_fill(
        ticker, event, "BTC", side, count, price, strategy="longshot",
        is_taker=False, fill_source="longshot_maker")


# ── scan()-level harness (mirrors test_longshot_r2_regressions.py) ──────────

class _ML:
    """Minimal MainLoop stand-in for OpportunityScanner (plain object, NOT
    MagicMock, so unexpected attribute reads fail loudly)."""

    def __init__(self, longshot_engine):
        self.longshot_engine = longshot_engine
        self.config_snapshot_id = None
        self.cross_feed = None
        self.synthetic_rti_feed = None
        self.spx_engine = None
        self.weather_engine = None
        self.fifteenm_shadow = None
        self.hourly_alt_shadow = None
        self.spx_harrv_shadow = None
        self.capital_allocator = None
        self._scan_iter = 0
        self._scan_loop_start = 0.0
        self._open_positions_count_cache = {}


def _build_scanner(state, engine, client):
    from bot.scanner import OpportunityScanner
    feed = MagicMock()
    feed.get_price_with_ts.return_value = (100.0, time.monotonic())
    feed.get_price.return_value = 100.0
    feed.get_price_trailing_avg.return_value = 100.0
    feed.get_buffer.return_value = [100.0] * 120
    vol = MagicMock()
    vol.update.return_value = {"blended_rv": 0.0001, "regime": "normal"}
    ml = _ML(engine)
    scanner = OpportunityScanner(
        client, state, feed, vol, MagicMock(), MagicMock(),
        kalshi_feed=None, main_loop=ml)
    state._extended_feature_provider = None
    return scanner, ml


def _window(event_ticker=EVENT, ticker=TICKER, stc=600.0):
    return {
        "asset": "BTC",
        "event_ticker": event_ticker,
        "product_type": "15m",
        "seconds_to_close": stc,
        "markets": [{"ticker": ticker, "floor_strike": 104.0,
                     "yes_ask": 8}],
    }


def _stale_candidate(buy_side):
    """Forged longshot candidate as if evaluate_market emitted it from a
    stale cache (M1: the scanner overlay must filter it independently of
    the engine's own _allowed_size guard)."""
    sell_side = "no" if buy_side == "yes" else "yes"
    return {
        "ticker": TICKER,
        "event_ticker": EVENT,
        "asset": "BTC",
        "product_type": "15m",
        "spot": 100.0,
        "threshold": 104.0,
        "seconds_to_close": 600.0,
        "blended_rv": 0.0001,
        "strategy": "longshot",
        "side": buy_side,
        "longshot_sell_side": sell_side,
        "longshot_ask_cents": 8,
        "longshot_buy_side": buy_side,
        "longshot_buy_price_cents": 92,
        "best_yes_ask": 92,
        "position_size": 1,
        "calibrated_prob": 0.99,
        "edge": 0.07,
        "z_score": -36.5,
        "balance_at_scan": 50000,
    }


# ── M1: opposite-side self-collision on the same ticker ─────────────────────

class TestM1OppositeSideSelfCollision:
    """R4-M1: one open longshot row per ticker. An opposite-side fill
    would ACCUMULATE into the existing row under the wrong side
    (record_position_from_fill has no side predicate; ticker-PK reality,
    86badbf9t)."""

    def test_allowed_size_blocks_opposite_side(self, engine, state, client,
                                               enabled):
        # Sell-YES filled -> positions (ticker, side='no', sg='longshot').
        _record_longshot(state, side="no")
        # Spot crossed the strike: sell-NO (buy YES) now qualifies — must
        # be blocked outright at the sizing chokepoint.
        assert engine._allowed_size(TICKER, "no", "yes", 8) == 0, (
            "an open longshot row on the OPPOSITE side must zero the "
            "allowed size — the fill would accumulate under side='no' "
            "and settle inverted (R4-M1)")

    def test_predicate_reports_opposite_side_conflict(self, engine, state,
                                                      client, enabled):
        _record_longshot(state, side="no")
        assert engine.has_opposite_side_longshot_position(
            TICKER, "yes") is True
        assert engine.has_opposite_side_longshot_position(
            TICKER, "no") is False
        assert engine.has_opposite_side_longshot_position(
            TICKER2, "yes") is False

    def test_same_side_requote_allowed_up_to_cap(self, engine, state,
                                                 client, enabled):
        _record_longshot(state, side="no", count=1)
        assert engine._allowed_size(TICKER, "yes", "no", 92) == (
            C.LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE - 1), (
            "same-side re-quote up to the per-window-side cap must stay "
            "allowed (R4-M1 guard is opposite-side only)")

    def test_opposite_side_allowed_after_settle(self, engine, state,
                                                client, enabled):
        _record_longshot(state, side="no")
        state.conn.execute(
            "UPDATE positions SET status='settled' WHERE ticker=?",
            (TICKER,))
        state.conn.commit()
        assert engine._allowed_size(TICKER, "no", "yes", 8) == (
            C.LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE), (
            "the guard scopes to OPEN rows — a settled position must not "
            "block the opposite side (R4-M1)")

    def test_scan_overlay_filters_stale_opposite_side_candidate(
            self, state, client, enabled):
        """Defensive mirror at the scanner overlay: even if a stale-cache
        evaluate emits an opposite-side candidate, scan() must drop it."""
        engine = LongshotEngine(client, state)
        engine._boot_reconciled = True
        _record_longshot(state, side="no")  # open longshot row, side='no'
        engine.evaluate_market = lambda **kw: [_stale_candidate("yes")]
        scanner, _ml = _build_scanner(state, engine, client)
        selected = scanner.scan([_window()]) or []
        assert [c for c in selected if c.get("strategy") == "longshot"] \
            == [], (
            "the scanner overlay must filter a stale opposite-side "
            "longshot candidate (R4-M1 defensive mirror)")

    def test_scan_overlay_passes_same_side_candidate(self, state, client,
                                                     enabled):
        engine = LongshotEngine(client, state)
        engine._boot_reconciled = True
        _record_longshot(state, side="no")
        engine.evaluate_market = lambda **kw: [_stale_candidate("no")]
        scanner, _ml = _build_scanner(state, engine, client)
        selected = scanner.scan([_window()]) or []
        same = [c for c in selected if c.get("strategy") == "longshot"]
        assert len(same) == 1, (
            "a SAME-side candidate must survive the overlay filter — the "
            "guard is opposite-side only (R4-M1)")


# ── M2: per-order recorded-fill counter for the boot skip seed ───────────────

def _recorded_fill_count(state, order_id):
    return state.conn.execute(
        "SELECT recorded_fill_count FROM pending_orders WHERE order_id=?",
        (order_id,)).fetchone()["recorded_fill_count"]


class TestM2PerOrderRecordedFillCounter:
    """R4-M2: the boot delta-apply skip must seed from EACH ORDER'S OWN
    recorded-fill counter — the (ticker, side) aggregate misattributes
    across sequential same-(ticker, side) orders and silently drops a
    real fill of the later order. Aggregate remains the NULL-legacy
    fallback only."""

    def test_fresh_insert_initializes_counter_to_zero(self, state):
        _seed_pending_resting(state, client_oid="ls-m2z", order_id="oid-m2z")
        assert _recorded_fill_count(state, "oid-m2z") == 0, (
            "fresh pending_orders rows must start at 0 (non-NULL) so boot "
            "seeds the order's OWN counter, never the aggregate (R4-M2)")

    def test_counter_increments_on_record_and_survives_calls(
            self, engine, state, client, enabled):
        engine._boot_reconciled = True
        _seed_pending_resting(state, client_oid="ls-m2c", order_id="oid-m2c",
                              count=3)
        engine.register_resting(
            order_id="oid-m2c", client_order_id="ls-m2c", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)
        with engine._lock:
            q = engine._resting["oid-m2c"]
        engine._apply_fills(q, [{"order_id": "oid-m2c", "trade_id": "t-m2c1",
                                 "count": 1}])
        assert _recorded_fill_count(state, "oid-m2c") == 1
        engine._apply_fills(q, [{"order_id": "oid-m2c", "trade_id": "t-m2c2",
                                 "count": 2}])
        assert _recorded_fill_count(state, "oid-m2c") == 3, (
            "the counter must accumulate across _apply_fills calls (R4-M2)")

    def test_delta_skip_branch_does_not_increment(self, engine, state,
                                                  client, enabled):
        engine._boot_reconciled = True
        _record_longshot(state, side="no", count=2)
        _seed_pending_resting(state, client_oid="ls-m2s", order_id="oid-m2s",
                              count=2)
        engine.register_resting(
            order_id="oid-m2s", client_order_id="ls-m2s", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=2, seconds_to_close=600.0,
            boot_fill_skip=2)
        with engine._lock:
            q = engine._resting["oid-m2s"]
        engine._apply_fills(q, [{"order_id": "oid-m2s", "trade_id": "t-m2s1",
                                 "count": 2}])
        assert _recorded_fill_count(state, "oid-m2s") == 0, (
            "skipped (already-recorded) contracts must NOT bump the "
            "counter — they were never recorded by THIS pass (R4-M2)")

    def test_two_sequential_orders_same_ticker_side(self, state, client,
                                                    enabled):
        """THE R4-M2 scenario: order-1 partially fills 1 (recorded) ->
        canceled; order-2 quotes the remainder; crash with order-2's fills
        unpolled; restart must record order-2's fills IN FULL (the
        aggregate seed =1 from order-1 skipped a real order-2 fill)."""
        now = time.time()
        # order-1: 1 of 3 fills pre-crash (recorded via the engine path,
        # which bumps its per-order counter), then canceled.
        engine1 = LongshotEngine(client, state)
        engine1._boot_reconciled = True
        _seed_pending_resting(state, client_oid="ls-m2o1",
                              order_id="oid-m2o1", count=3,
                              created_epoch=now - 700)
        engine1.register_resting(
            order_id="oid-m2o1", client_order_id="ls-m2o1", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)
        # ts must be >= the live quote's fill_min_ts (registration time,
        # i.e. ~now) minus the fetch slack for the pre-crash poll to see it.
        fill_o1 = {"order_id": "oid-m2o1", "trade_id": "t-m2o1", "count": 1,
                   "ts": now - 30, "created_time": _rfc3339(now - 30)}
        client.get_fills.side_effect = _min_ts_respecting_fills([fill_o1])
        engine1.tick()
        engine1._cancel_quote("oid-m2o1", "test_requote")
        assert _pending_status(state, "oid-m2o1") == "canceled"
        assert _positions_row(state)["count"] == 1

        # order-2: quotes the remainder (2). Crash before any fill poll.
        _seed_pending_resting(state, client_oid="ls-m2o2",
                              order_id="oid-m2o2", count=2,
                              created_epoch=now - 400)

        # Restart: order-2 fully filled pre-crash (absent from API list).
        fill_o2 = {"order_id": "oid-m2o2", "trade_id": "t-m2o2", "count": 2,
                   "ts": now - 300, "created_time": _rfc3339(now - 300)}
        client.get_fills.side_effect = _min_ts_respecting_fills(
            [fill_o1, fill_o2])
        engine2 = LongshotEngine(client, state)
        engine2.tick()
        row = _positions_row(state)
        assert row["count"] == 3, (
            "order-2's REAL fills must be recorded in full — the "
            "(ticker, side) aggregate seed (=1 from order-1) skipped one "
            "of them, a permanent under-record (R4-M2)")
        assert _pending_status(state, "oid-m2o2") == "filled"

    def test_single_order_case_unchanged(self, state, client, enabled):
        """Single order whose fills were recorded pre-crash (counter
        incremented by _apply_fills): restart must not double-count."""
        now = time.time()
        engine1 = LongshotEngine(client, state)
        engine1._boot_reconciled = True
        _seed_pending_resting(state, client_oid="ls-m2u", order_id="oid-m2u",
                              count=2, created_epoch=now - 600)
        engine1.register_resting(
            order_id="oid-m2u", client_order_id="ls-m2u", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=2, seconds_to_close=600.0)
        fill = {"order_id": "oid-m2u", "trade_id": "t-m2u", "count": 2,
                "ts": now - 300, "created_time": _rfc3339(now - 300)}
        client.get_fills.side_effect = _min_ts_respecting_fills([fill])
        engine1.tick()  # records 2, counter=2, quote popped as filled
        # Crash erased the pop's row flip? No — simulate the worst case:
        # the row is still 'resting' at restart (pop never committed).
        state.conn.execute(
            "UPDATE pending_orders SET status='resting' "
            "WHERE order_id='oid-m2u'")
        state.conn.commit()
        engine2 = LongshotEngine(client, state)
        engine2.tick()
        assert _positions_row(state)["count"] == 2, (
            "single-order restart must not double-count (own counter =2 "
            "absorbs the refetch) (R4-M2)")
        assert _pending_status(state, "oid-m2u") == "filled"

    def test_null_legacy_row_falls_back_to_aggregate(self, state, client,
                                                     enabled):
        """Pre-R4 rows have recorded_fill_count=NULL — the (ticker, side)
        aggregate fallback must still absorb the refetch."""
        now = time.time()
        _record_longshot(state, side="no", count=2)
        _seed_pending_resting(state, client_oid="ls-m2l", order_id="oid-m2l",
                              count=2, created_epoch=now - 600)
        state.conn.execute(
            "UPDATE pending_orders SET recorded_fill_count=NULL "
            "WHERE order_id='oid-m2l'")
        state.conn.commit()
        client.get_fills.side_effect = _min_ts_respecting_fills([
            {"order_id": "oid-m2l", "trade_id": "t-m2l", "count": 2,
             "ts": now - 300, "created_time": _rfc3339(now - 300)},
        ])
        engine = LongshotEngine(client, state)
        engine.tick()
        assert _positions_row(state)["count"] == 2, (
            "NULL-legacy rows must fall back to the (ticker, side) "
            "aggregate seed (R4-M2)")
        assert _pending_status(state, "oid-m2l") == "filled"

    def test_reconcile_import_attributes_to_most_recent_ls_order(
            self, state, client, enabled):
        """RECONCILE_IMPORT_LONGSHOT contracts are 'already embodied'
        truth the engine never counter-attributed — the import must bump
        the most recent ls- order's counter so its own-row seed absorbs
        the boot refetch."""
        now = time.time()
        _seed_pending_resting(state, client_oid="ls-m2r", order_id="oid-m2r",
                              side="yes", count=2, price=10,
                              created_epoch=now - 600)
        client.get_positions.return_value = {"market_positions": [
            {"ticker": TICKER, "position": 2, "market_exposure": 20},
        ]}
        state.reconcile_with_api(client)
        assert _positions_row(state)["strategy_group"] == "longshot"
        assert _recorded_fill_count(state, "oid-m2r") == 2, (
            "the import must attribute the embodied contracts to the most "
            "recent ls- order's recorded_fill_count (R4-M2)")
        client.get_fills.side_effect = _min_ts_respecting_fills([
            {"order_id": "oid-m2r", "trade_id": "t-m2r", "count": 2,
             "ts": now - 300, "created_time": _rfc3339(now - 300)},
        ])
        engine = LongshotEngine(client, state)
        engine.tick()
        assert _positions_row(state)["count"] == 2, (
            "boot refetch after a reconcile import must be a no-op")


# ── MN1: cleanup_expired_resting_orders needs the ls- carve-out ──────────────

# A clearly past-close 15M ticker (Jan 1 2026 12:00 ET).
PAST_TICKER = "KXBTC15M-26JAN011200-T104"
PAST_EVENT = "KXBTC15M-26JAN011200"


class TestMN1CleanupExpiredCarveOut:
    """R4-MN1: the settlement daemon calls cleanup_expired_resting_orders
    concurrently with the engine's boot reconcile; flipping a past-close
    ls- row to 'expired' hides it from boot step 2 (which queries
    status='resting') — same engine-owned carve-out as _reconcile_orders
    (R3-M1)."""

    def test_past_close_ls_row_stays_resting(self, state):
        _seed_pending_resting(state, client_oid="ls-mn1", order_id="oid-mn1",
                              ticker=PAST_TICKER, event=PAST_EVENT)
        _seed_pending_resting(state, client_oid="mk-mn1", order_id="oid-mn1m",
                              ticker=PAST_TICKER, event=PAST_EVENT,
                              side="yes", price=95)
        state.cleanup_expired_resting_orders()
        assert _pending_status(state, "oid-mn1") == "resting", (
            "a past-close ls- row must stay 'resting' for the engine's "
            "boot step-2 query — the LongshotEngine owns the ls- "
            "lifecycle (R4-MN1)")
        assert _pending_status(state, "oid-mn1m") == "expired", (
            "non-longshot past-close rows must still expire (R4-MN1 "
            "carve-out is ls- only)")


# ── MN2: ls--history stamp must be recency, not existence ───────────────────

class TestMN2LsHistoryStampIsRecency:
    """R4-MN2: _reconcile_positions stamped strategy_group='longshot' on
    ANY ls- pending history. A ticker longshot quoted weeks ago that the
    main pipeline traded since would have its unknown-position import
    claimed by longshot. The stamp must check the MOST RECENT
    pending_orders row for the ticker (any prefix) and fire only when
    THAT row is ls-prefixed."""

    def test_old_ls_history_newer_main_order_imports_main(
            self, state, client, enabled):
        now = time.time()
        # Old ls- order, long settled.
        _seed_pending_resting(state, client_oid="ls-mn2a",
                              order_id="oid-mn2a", side="yes", count=2,
                              price=10, created_epoch=now - 86400)
        state.mark_order_status("oid-mn2a", "filled")
        # Newer MAIN order on the same ticker.
        _seed_pending_resting(state, client_oid="mk-mn2a",
                              order_id="oid-mn2am", side="yes", count=2,
                              price=80, created_epoch=now - 300)
        state.mark_order_status("oid-mn2am", "filled")
        client.get_positions.return_value = {"market_positions": [
            {"ticker": TICKER, "position": 2, "market_exposure": 160},
        ]}
        state.reconcile_with_api(client)
        row = _positions_row(state)
        assert row is not None
        assert row["strategy_group"] == "main", (
            "the MAIN pipeline most recently traded this ticker — the "
            "import must stay 'main'; existence of OLD ls- history must "
            "not claim it for longshot (R4-MN2)")

    def test_recent_ls_after_old_main_imports_longshot(
            self, state, client, enabled):
        now = time.time()
        _seed_pending_resting(state, client_oid="mk-mn2b",
                              order_id="oid-mn2bm", side="yes", count=2,
                              price=80, created_epoch=now - 86400)
        state.mark_order_status("oid-mn2bm", "filled")
        _seed_pending_resting(state, client_oid="ls-mn2b",
                              order_id="oid-mn2b", side="yes", count=2,
                              price=10, created_epoch=now - 300)
        state.mark_order_status("oid-mn2b", "filled")
        client.get_positions.return_value = {"market_positions": [
            {"ticker": TICKER, "position": 2, "market_exposure": 20},
        ]}
        state.reconcile_with_api(client)
        row = _positions_row(state)
        assert row is not None
        assert row["strategy_group"] == "longshot", (
            "longshot most recently traded this ticker — the import must "
            "land inside the longshot rails (R4-MN2)")


# ── MN3: boot step-1 adoption must compare like units ────────────────────────

class TestMN3BootAdoptionUnitMix:
    """R4-MN3: step-1 adoption registered count=REMAINING while
    q['filled'] accumulates CUMULATIVE fetched fills (skipped pre-restart
    contracts included), so a 2-of-3-prefilled orphan popped as 'filled'
    (2 >= 1) with a (2/1) LONGSHOT_FILL log even though the remainder was
    actually canceled. Fix: register count = remaining + skip (cumulative
    units on both sides of the pop condition); money behavior unchanged
    (the skip budget already prevented re-recording)."""

    def test_two_of_three_prefilled_orphan_ends_canceled(
            self, state, client, enabled, caplog):
        now = time.time()
        # Pre-restart: 2 of 3 contracts filled AND recorded (counter=2).
        _record_longshot(state, side="no", count=2)
        _seed_pending_resting(state, client_oid="ls-mn3", order_id="oid-mn3",
                              count=3, created_epoch=now - 600)
        state.conn.execute(
            "UPDATE pending_orders SET recorded_fill_count=2 "
            "WHERE order_id='oid-mn3'")
        state.conn.commit()
        # Still resting on Kalshi with 1 remaining; the 2 old fills come
        # back on the boot poll (created_time-bounded fetch).
        client.get_orders.return_value = {"orders": [
            {"order_id": "oid-mn3", "client_order_id": "ls-mn3",
             "ticker": TICKER, "side": "no", "action": "buy",
             "no_price": 92, "count": 3, "remaining_count": 1,
             "status": "resting", "created_time": _rfc3339(now - 600)},
        ]}
        client.get_fills.side_effect = _min_ts_respecting_fills([
            {"order_id": "oid-mn3", "trade_id": "t-mn3", "count": 2,
             "ts": now - 500, "created_time": _rfc3339(now - 500)},
        ])
        engine = LongshotEngine(client, state)
        with caplog.at_level(logging.INFO):
            engine.tick()
        # The 1-contract remainder was CANCELED (boot_orphan path), not
        # filled — the row's terminal status must say so.
        assert _pending_status(state, "oid-mn3") == "canceled", (
            "a partially-prefilled boot orphan whose remainder is "
            "canceled must end 'canceled' — the unit mix (filled "
            "cumulative vs count remaining) marked it 'filled' (R4-MN3)")
        # Money behavior identical: nothing re-recorded.
        assert _positions_row(state)["count"] == 2
        # No (filled/count) log drift: filled must never exceed count.
        for rec in caplog.records:
            msg = rec.getMessage()
            if "LONGSHOT_FILL:" not in msg:
                continue
            m = re.search(r"\((\d+)/(\d+)\)", msg)
            assert m is not None
            assert int(m.group(1)) <= int(m.group(2)), (
                f"LONGSHOT_FILL log drift (filled > count): {msg!r} "
                "(R4-MN3)")


# ── SAME-side re-quote-after-fill stacking (live bug 86baf07y3) ──────────────


def _seed_filled_ls(state, *, client_oid, order_id, side, count, price,
                    recorded_fills):
    """A FILLED ls- pending_orders row whose fills are recorded in the
    per-order ledger (recorded_fill_count) but NOT (yet / any longer) in a
    positions row — the live race: on fill the registry entry is popped and
    the positions write lags / is clobbered by a later same-ticker order
    (ticker-PK). Reproduces KXDOGE15M-26JUN151515-15, 2026-06-15."""
    _seed_pending_resting(state, client_oid=client_oid, order_id=order_id,
                          side=side, count=count, price=price)
    state.conn.execute(
        "UPDATE pending_orders SET status='filled', recorded_fill_count=? "
        "WHERE order_id=?", (recorded_fills, order_id))
    state.conn.commit()


class TestSameSideRequoteStacking:
    """86baf07y3: the per-(window, side) cap in _allowed_size counted open
    positions + unfilled resting registry contracts, but NOT contracts that
    are FILLED yet not in a positions row — popped from the registry on
    fill, with the positions write lagging or clobbered by a later
    same-ticker order (single-ticker PK, 86badbf9t). A re-quote then sized a
    fresh full cap on a window-side already at the cap. Fix: the cap also
    subtracts SUM(recorded_fill_count) over ls- pending_orders on
    (ticker, buy_side) — the per-order ledger is current on fill detection
    and immune to the ticker-PK overwrite."""

    def test_filled_order_with_no_position_row_zeroes_cap(
            self, engine, state, client, enabled):
        # 3ct already filled on (ticker, yes) but NO positions row (race /
        # clobber). A same-side re-quote (sell-NO -> buy YES) must size 0.
        _seed_filled_ls(state, client_oid="ls-fill1", order_id="oid-f1",
                        side="yes", count=3, price=86, recorded_fills=3)
        assert _positions_row(state) is None  # the race state: no open row
        assert engine._allowed_size(TICKER, "no", "yes", 86) == 0, (
            "a window-side already filled to the cap must zero the allowed "
            "size even when the positions row is missing — the cap must "
            "consult the per-order fill ledger (86baf07y3)")

    def test_partial_filled_order_leaves_remainder(
            self, engine, state, client, enabled):
        # 1 of 3 filled on (ticker, yes), no positions row -> 2 remain.
        _seed_filled_ls(state, client_oid="ls-fill2", order_id="oid-f2",
                        side="yes", count=3, price=86, recorded_fills=1)
        assert engine._allowed_size(TICKER, "no", "yes", 86) == (
            C.LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE - 1), (
            "only the FILLED contracts consume cap; the remainder stays "
            "available (86baf07y3)")

    def test_filled_other_ticker_does_not_block(
            self, engine, state, client, enabled):
        # Filled cap on a DIFFERENT window must not block this one.
        _seed_filled_ls(state, client_oid="ls-fill3", order_id="oid-f3",
                        side="yes", count=3, price=86, recorded_fills=3)
        assert engine._allowed_size(TICKER2, "no", "yes", 86) == (
            C.LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE), (
            "the cap is per (window, side) — a filled row on another ticker "
            "must not reduce it (86baf07y3)")
