"""Bit L-1 adversarial-review R5 regressions (longshot premium-harvest).

One test class per R5 finding (M1 / M2 / M3 / MN1 / MN2 / MN3). Fixtures
mirror tests/integration/test_longshot_r4_regressions.py (real sqlite3 file
via tmp_path per tests/CLAUDE.md integration-tier convention); the
scan()-level harness mirrors tests/integration/test_longshot_r2_regressions.py.

M1 — _apply_fills stamped seen_trade_ids (and consumed the boot skip
budget) BEFORE record_position_from_fill; a transient record failure
(database-is-locked — record_position_from_fill has no retry-on-busy)
then `continue`d with the trade_id stamped + budget consumed + filled
never bumped, so every later poll deduped the fill, the quote eventually
popped 'canceled', and the position was invisible to all rails. Fix:
the except branch un-stamps the trade_id and restores the consumed skip
budget so the next poll retries cleanly (bounded double-record in the
rare committed-despite-raise race is accepted — fail-toward-recording).

M2 — the side-collision guard (R4-M1) covered open POSITION rows only;
an opposite-side RESTING quote (or a CANCEL_FILL_MISMATCH-held one whose
fill is not yet recorded) reproduced the same corruption: sell-NO posts
while a sell-YES fill is in flight, then both fills accumulate under one
side. Fix: registry quotes ARE future rows — `_allowed_size` returns 0
when any registered quote on the ticker has a different buy_side
(inside the same lock hold as the registry sizing scan), and the scanner
overlay mirrors via the new `has_opposite_side_resting_quote` predicate.

M3 — boot step-1 adoption registered seconds_to_close=0.0, so the
stale-drop backstop (remaining < -120s) fired 120s after RESTART, not
120s after the real window close — an adopted orphan with minutes of
real life left was dropped while still live on Kalshi. Fix: seed
seconds_to_close from `_close_epoch_from_ticker` (fallback 0.0).

MN1 — the stale-drop popped the entry after `_poll_fills` regardless of
poll completeness — a terminal decision on a failed/partial snapshot
could orphan a last-moment fill. Fix: only pop when the final poll
returned complete=True; otherwise leave the entry (the stale condition
re-fires next tick).

MN2 — step-1's remaining-count fallback used the ORIGINAL count when
both remaining fields were absent, overstating the registered cumulative
count so a fully-recorded orphan ended 'canceled' instead of 'filled'.
Fix: derive remaining = max(0, original_count - skip).

MN3 — boot step-2 queried status='resting' only, so an ls- row stranded
in 'pending' (crash between insert_bot_order and confirm_order_submitted)
was invisible forever. Fix: query status IN ('resting','pending'); step-1
additionally repairs an API-present pending row via
confirm_order_submitted (no-op for already-confirmed rows) so its
lifecycle marks land on the row.
"""
from __future__ import annotations

import datetime
import time
from datetime import timezone
from unittest.mock import MagicMock

import pytest

import bot.constants as C
from bot.state import StateManager

from bot.longshot import LongshotEngine, _close_epoch_from_ticker

TICKER = "KXBTC15M-26JUN111200-T104"
EVENT = "KXBTC15M-26JUN111200"

_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "test_longshot_r5.db"))
    yield s
    s.close()


@pytest.fixture
def client():
    cl = MagicMock()
    cl.get_fills.return_value = {"fills": []}
    cl.get_orders.return_value = {"orders": []}
    cl.get_positions.return_value = {"market_positions": []}
    cl.cancel_order.return_value = {"order": {"status": "canceled"}}
    cl.place_order.return_value = {"order": {"order_id": "oid-r5-1"}}
    cl.get_balance.return_value = {"balance": 50000}
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


def _ticker_closing_at(close_epoch, asset="BTC"):
    """15M ticker whose encoded close time is `close_epoch` (ET = UTC-4h
    encoding convention, mirrors _close_epoch_from_ticker)."""
    et = datetime.datetime.fromtimestamp(
        close_epoch, timezone.utc) - datetime.timedelta(hours=4)
    return (f"KX{asset}15M-{et:%y}{_MONTHS[et.month - 1]}"
            f"{et:%d%H%M}-T104")


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


def _pending_status(state, key):
    return state.conn.execute(
        "SELECT status FROM pending_orders "
        "WHERE order_id=? OR client_order_id=?",
        (key, key)).fetchone()["status"]


def _record_longshot(state, *, ticker=TICKER, event=EVENT, side="no",
                     count=1, price=92):
    state.record_position_from_fill(
        ticker, event, "BTC", side, count, price, strategy="longshot",
        is_taker=False, fill_source="longshot_maker")


def _register(engine, *, order_id="oid-r5", client_oid="ls-r5",
              ticker=TICKER, event=EVENT, sell_side="yes", buy_side="no",
              price=92, count=3, stc=600.0, boot_fill_skip=0):
    engine.register_resting(
        order_id=order_id, client_order_id=client_oid, ticker=ticker,
        event_ticker=event, asset="BTC", sell_side=sell_side,
        buy_side=buy_side, buy_price_cents=price, count=count,
        seconds_to_close=stc, boot_fill_skip=boot_fill_skip)
    with engine._lock:
        return engine._resting[order_id]


class _FailOnce:
    """record_position_from_fill stand-in: raises on the first call
    (transient database-is-locked shape), delegates afterwards."""

    def __init__(self, real):
        self._real = real
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        if self.calls == 1:
            raise Exception("database is locked")
        return self._real(*a, **kw)


# ── scan()-level harness (mirrors test_longshot_r2/r4_regressions.py) ───────

class _ML:
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


# ── M1: record failure must not permanently blacklist the fill ──────────────

class TestM1RecordFailureRetried:
    """R5-M1: seen_trade_ids was stamped (and the boot skip budget
    consumed) BEFORE record_position_from_fill; a transient record
    failure then deduped the fill forever — quote popped 'canceled',
    position invisible to caps/marks/streaks."""

    def test_record_failure_retried_and_recorded_exactly_once(
            self, engine, state, client, enabled, monkeypatch):
        q = _register(engine, count=3)
        failer = _FailOnce(state.record_position_from_fill)
        monkeypatch.setattr(state, "record_position_from_fill", failer)
        fill = {"order_id": "oid-r5", "trade_id": "t-m1", "count": 1}
        engine._apply_fills(q, [fill])
        # Failed record: nothing recorded, fill NOT blacklisted, filled
        # NOT bumped (the quote must not pop as fully filled).
        assert _positions_row(state) is None
        assert "t-m1" not in q["seen_trade_ids"], (
            "a failed record must un-stamp the trade_id so the next poll "
            "retries (R5-M1)")
        assert q["filled"] == 0
        # Next poll re-delivers the same fill: recorded exactly once.
        engine._apply_fills(q, [fill])
        row = _positions_row(state)
        assert row is not None and row["count"] == 1, (
            "the retried fill must be recorded exactly once (R5-M1)")
        assert q["filled"] == 1
        assert failer.calls == 2
        # Idempotent thereafter.
        engine._apply_fills(q, [fill])
        assert _positions_row(state)["count"] == 1

    def test_skip_budget_restored_when_failed_fill_inside_skip_window(
            self, engine, state, client, enabled, monkeypatch):
        """A fill that straddles the boot skip budget (skip 1, record 2)
        must restore the consumed budget on record failure so the retry
        skips the SAME already-recorded contract, not a fresh one."""
        q = _register(engine, count=3, boot_fill_skip=1)
        failer = _FailOnce(state.record_position_from_fill)
        monkeypatch.setattr(state, "record_position_from_fill", failer)
        fill = {"order_id": "oid-r5", "trade_id": "t-m1s", "count": 3}
        engine._apply_fills(q, [fill])
        assert q["boot_skip_remaining"] == 1, (
            "the consumed skip budget must be restored on record failure "
            "(R5-M1) — otherwise the retry records the already-recorded "
            "contract too")
        assert _positions_row(state) is None
        # Retry: skip 1, record 2.
        engine._apply_fills(q, [fill])
        assert q["boot_skip_remaining"] == 0
        assert _positions_row(state)["count"] == 2
        assert q["filled"] == 3

    def test_record_failure_does_not_mark_quote_filled(
            self, engine, state, client, enabled, monkeypatch):
        """A record failure on the LAST outstanding contract must keep
        the quote registered (filled < count) so the next tick retries
        instead of popping the entry."""
        _seed_pending_resting(state, client_oid="ls-m1p",
                              order_id="oid-m1p", count=1)
        q = _register(engine, order_id="oid-m1p", client_oid="ls-m1p",
                      count=1)
        failer = _FailOnce(state.record_position_from_fill)
        monkeypatch.setattr(state, "record_position_from_fill", failer)
        fill = {"order_id": "oid-m1p", "trade_id": "t-m1p", "count": 1}
        engine._apply_fills(q, [fill])
        assert engine.resting_count() == 1, (
            "a quote whose only fill failed to record must stay "
            "registered for the retry (R5-M1)")
        assert _pending_status(state, "oid-m1p") == "resting"
        engine._apply_fills(q, [fill])
        assert engine.resting_count() == 0
        assert _pending_status(state, "oid-m1p") == "filled"
        assert _positions_row(state)["count"] == 1
