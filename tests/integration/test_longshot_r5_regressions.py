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
import sqlite3
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


# ── M2: opposite-side RESTING quote must block like an open row ─────────────

class TestM2OppositeSideRestingQuote:
    """R5-M2: the R4-M1 side-collision guard read POSITION rows only. An
    opposite-side resting quote (or a CANCEL_FILL_MISMATCH-held one whose
    fill is not yet recorded) is a FUTURE row on the same ticker-PK —
    sell-YES rests, spot crosses, quote picked off, same-tick refresh
    cancels with a fill mismatch; sell-NO qualifies and posts; the YES
    fill records next poll; the later NO fill accumulates under side
    'no'. Registry quotes must extend the one-open-longshot-row-per-
    ticker invariant."""

    def test_allowed_size_blocks_on_opposite_side_resting_quote(
            self, engine, state, client, enabled):
        _register(engine, buy_side="no", sell_side="yes", count=1)
        assert engine._allowed_size(TICKER, "no", "yes", 8) == 0, (
            "a RESTING opposite-side quote is a future positions row — "
            "_allowed_size must return 0 (R5-M2)")

    def test_allowed_size_blocks_on_mismatch_held_quote(
            self, engine, state, client, enabled):
        """A CANCEL_FILL_MISMATCH-held entry (needs_clean_poll) is still
        registered with an unrecorded fill — the most dangerous shape."""
        q = _register(engine, buy_side="no", sell_side="yes", count=1)
        q["needs_clean_poll"] = True
        assert engine._allowed_size(TICKER, "no", "yes", 8) == 0, (
            "a mismatch-held opposite-side quote must block (R5-M2)")

    def test_same_side_topup_still_allowed(self, engine, state, client,
                                           enabled):
        _register(engine, buy_side="no", sell_side="yes", count=1)
        assert engine._allowed_size(TICKER, "yes", "no", 92) == (
            C.LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE - 1), (
            "same-side top-up must stay allowed up to the per-window-side "
            "cap (R5-M2 guard is opposite-side only)")

    def test_predicate_reports_registry_conflict(self, engine, state,
                                                 client, enabled):
        _register(engine, buy_side="no", sell_side="yes", count=1)
        assert engine.has_opposite_side_resting_quote(TICKER, "yes") is True
        assert engine.has_opposite_side_resting_quote(TICKER, "no") is False
        assert engine.has_opposite_side_resting_quote(
            "KXBTC15M-26JUN111215-T104", "yes") is False

    def test_scan_overlay_filters_opposite_side_resting_quote(
            self, state, client, enabled):
        """Defensive mirror at the scanner overlay: a stale-cache
        evaluate emitting an opposite-side candidate while an
        opposite-side quote rests must be dropped at scan level too."""
        engine = LongshotEngine(client, state)
        engine._boot_reconciled = True
        _register(engine, buy_side="no", sell_side="yes", count=1)
        engine.evaluate_market = lambda **kw: [_stale_candidate("yes")]
        scanner, _ml = _build_scanner(state, engine, client)
        selected = scanner.scan([_window()]) or []
        assert [c for c in selected if c.get("strategy") == "longshot"] \
            == [], (
            "the scanner overlay must filter a candidate opposing a "
            "RESTING longshot quote (R5-M2 defensive mirror)")

    def test_scan_overlay_passes_same_side_with_resting_quote(
            self, state, client, enabled):
        engine = LongshotEngine(client, state)
        engine._boot_reconciled = True
        _register(engine, buy_side="no", sell_side="yes", count=1)
        engine.evaluate_market = lambda **kw: [_stale_candidate("no")]
        scanner, _ml = _build_scanner(state, engine, client)
        selected = scanner.scan([_window()]) or []
        same = [c for c in selected if c.get("strategy") == "longshot"]
        assert len(same) == 1, (
            "a SAME-side candidate must survive the overlay registry "
            "mirror (R5-M2 guard is opposite-side only)")


# ── M3: boot adoption must seed the REAL seconds_to_close ────────────────────

class TestM3BootAdoptionSeedsRealClose:
    """R5-M3: step-1 adoption registered seconds_to_close=0.0, so the
    stale-drop backstop (remaining < -120s) measured 120s from RESTART,
    not from the real window close — an adopted orphan with minutes of
    real life left was dropped (and its entry's caps/fill-polling
    abandoned) while the order could still be live and filling on
    Kalshi."""

    def _adopt_orphan(self, state, client, ticker, *, order_id="oid-m3",
                      client_oid="ls-m3", created_epoch=None):
        now = time.time()
        _seed_pending_resting(
            state, client_oid=client_oid, order_id=order_id, ticker=ticker,
            event=ticker.rsplit("-", 1)[0],
            created_epoch=created_epoch or (now - 300))
        client.get_orders.return_value = {"orders": [
            {"order_id": order_id, "client_order_id": client_oid,
             "ticker": ticker, "side": "no", "action": "buy",
             "no_price": 92, "count": 3, "remaining_count": 3,
             "status": "resting",
             "created_time": _rfc3339(created_epoch or (now - 300))},
        ]}
        client.cancel_order.return_value = None  # cancel keeps failing
        engine = LongshotEngine(client, state)
        engine.tick()
        return engine

    def test_adopted_orphan_with_real_life_not_stale_dropped_at_120s(
            self, state, client, enabled):
        now = time.time()
        ticker = _ticker_closing_at(now + 600)
        engine = self._adopt_orphan(state, client, ticker)
        assert engine.resting_count() == 1
        with engine._lock:
            q = next(iter(engine._resting.values()))
        # The encoded close is minute-truncated; ~9-10 min of life left.
        assert q["stc_at_register"] > 400.0, (
            "adoption must seed seconds_to_close from the ticker's real "
            "close epoch, not 0.0 (R5-M3)")
        # 200s after restart: window still has minutes left — the
        # stale-drop backstop must NOT fire (pre-fix: stc=0.0 made
        # remaining = -200 < -120 and dropped the live order's entry).
        engine.tick(now=now + 200)
        assert engine.resting_count() == 1, (
            "an adopted orphan with real window life left was "
            "stale-dropped 120s after RESTART instead of 120s after the "
            "real close (R5-M3)")

    def test_adopted_orphan_still_stale_drops_past_real_close(
            self, state, client, enabled):
        """The backstop must still fire once the REAL close + grace has
        passed (cancel kept failing -> Kalshi auto-canceled at close)."""
        now = time.time()
        ticker = _ticker_closing_at(now + 600)
        engine = self._adopt_orphan(state, client, ticker)
        engine.tick(now=now + 600 + 200)  # > close + 120s grace
        assert engine.resting_count() == 0, (
            "the stale-drop backstop must still fire past the real "
            "close + grace (R5-M3 keeps the backstop, just re-anchors it)")

    def test_unparseable_ticker_falls_back_to_zero(self, state, client,
                                                   enabled):
        ticker = "KXWEIRDSERIES-NOTADATE-T104"
        engine = self._adopt_orphan(state, client, ticker)
        assert engine.resting_count() == 1
        with engine._lock:
            q = next(iter(engine._resting.values()))
        assert q["stc_at_register"] == 0.0, (
            "an unparseable ticker must fall back to 0.0 — adoption must "
            "never crash on it (R5-M3)")


# ── MN1: stale-drop is terminal — its final poll must be COMPLETE ───────────

class TestMN1StaleDropRequiresCompletePoll:
    """R5-MN1: the stale-drop popped the entry after _poll_fills
    regardless of completeness — a failed/partial final poll could
    orphan a last-moment fill forever (the pop is terminal; the entry's
    seen_trade_ids/dedup state dies with it). One-retry-per-tick fix:
    pop only when the final poll returned complete=True; the stale
    condition re-fires next tick. Bounded worst case: the entry persists
    one tick per failed poll — the window is already closed (no NEW
    fills accrue) and trade_id dedup keeps re-polls idempotent."""

    def _stale_quote(self, engine, state, *, order_id="oid-mn1",
                     client_oid="ls-mn1", count=3):
        _seed_pending_resting(state, client_oid=client_oid,
                              order_id=order_id, count=count)
        engine._boot_reconciled = True
        return _register(engine, order_id=order_id, client_oid=client_oid,
                         count=count, stc=0.0)

    def test_failed_final_poll_defers_the_drop(self, engine, state, client,
                                               enabled):
        self._stale_quote(engine, state)
        client.cancel_order.return_value = None     # cancel keeps failing
        client.get_fills.return_value = None        # poll fails outright
        engine.tick(now=time.time() + 300)          # > 120s grace
        assert engine.resting_count() == 1, (
            "a stale-drop on a FAILED final poll is a terminal decision "
            "on missing data — the entry must survive to next tick "
            "(R5-MN1)")
        assert _pending_status(state, "oid-mn1") == "resting"
        # Next tick the poll succeeds -> drop proceeds.
        client.get_fills.return_value = {"fills": []}
        engine.tick(now=time.time() + 301)
        assert engine.resting_count() == 0
        assert _pending_status(state, "oid-mn1") == "canceled"

    def test_partial_final_poll_records_but_defers_the_drop(
            self, engine, state, client, enabled):
        """Page-cap exhaustion (cursor never drains) = partial snapshot:
        the partial page's fills are still recorded, but the drop waits
        for a COMPLETE poll."""
        self._stale_quote(engine, state, count=3)
        client.cancel_order.return_value = None
        client.get_fills.return_value = {
            "fills": [{"order_id": "oid-mn1", "trade_id": "t-mn1",
                       "count": 1}],
            "cursor": "never-drains"}
        engine.tick(now=time.time() + 300)
        assert engine.resting_count() == 1, (
            "a PARTIAL final poll must not drive the terminal stale-drop "
            "(R5-MN1)")
        row = _positions_row(state)
        assert row is not None and row["count"] == 1, (
            "the partial page's fills must still be recorded (R3-MN1 "
            "behavior preserved)")
        client.get_fills.return_value = {"fills": []}
        engine.tick(now=time.time() + 301)
        assert engine.resting_count() == 0
        assert _pending_status(state, "oid-mn1") == "canceled"
        assert _positions_row(state)["count"] == 1  # no double-record


# ── MN2: absent remaining fields must not overstate the count ───────────────

class TestMN2RemainingFallbackUsesCumulativeTruth:
    """R5-MN2: when BOTH remaining_count_fp and remaining_count were
    absent from the API order, step-1 fell back to the ORIGINAL count —
    but the registered count is CUMULATIVE (remaining + skip), so the
    fallback double-counted the already-recorded contracts
    (count = original + skip) and a fully-recorded orphan could never
    reach filled >= count: it ended 'canceled' instead of 'filled'.
    Fix: derive remaining = max(0, original_count - skip), consistent
    with the cumulative-truth units of R4-MN3."""

    def _boot_with_api_order(self, state, client, api_order, fills):
        now = time.time()
        _seed_pending_resting(state, client_oid="ls-mn2", order_id="oid-mn2",
                              count=3, created_epoch=now - 600)
        state.conn.execute(
            "UPDATE pending_orders SET recorded_fill_count=? "
            "WHERE order_id='oid-mn2'", (len(fills) and sum(
                f["count"] for f in fills),))
        state.conn.commit()
        client.get_orders.return_value = {"orders": [api_order]}
        client.get_fills.side_effect = _min_ts_respecting_fills(fills)
        engine = LongshotEngine(client, state)
        engine.tick()
        return engine

    def test_fully_recorded_orphan_absent_remaining_ends_filled(
            self, state, client, enabled):
        now = time.time()
        # 3-of-3 filled AND recorded pre-restart (counter=3); the API
        # order carries NO remaining fields at all.
        _record_longshot(state, side="no", count=3)
        engine = self._boot_with_api_order(
            state, client,
            {"order_id": "oid-mn2", "client_order_id": "ls-mn2",
             "ticker": TICKER, "side": "no", "action": "buy",
             "no_price": 92, "count": 3, "status": "resting",
             "created_time": _rfc3339(now - 600)},
            [{"order_id": "oid-mn2", "trade_id": "t-mn2", "count": 3,
              "ts": now - 500, "created_time": _rfc3339(now - 500)}])
        assert _pending_status(state, "oid-mn2") == "filled", (
            "a fully-recorded orphan with ABSENT remaining fields must "
            "end 'filled' — the original-count fallback overstated the "
            "cumulative count to original+skip (R5-MN2)")
        assert engine.resting_count() == 0
        # Money behavior: nothing re-recorded.
        assert _positions_row(state)["count"] == 3

    def test_partially_recorded_orphan_absent_remaining_counts_cumulative(
            self, state, client, enabled):
        now = time.time()
        _record_longshot(state, side="no", count=2)
        client.cancel_order.return_value = None  # keep the entry alive
        engine = self._boot_with_api_order(
            state, client,
            {"order_id": "oid-mn2", "client_order_id": "ls-mn2",
             "ticker": TICKER, "side": "no", "action": "buy",
             "no_price": 92, "count": 3, "status": "resting",
             "created_time": _rfc3339(now - 600)},
            [{"order_id": "oid-mn2", "trade_id": "t-mn2", "count": 2,
              "ts": now - 500, "created_time": _rfc3339(now - 500)}])
        with engine._lock:
            q = next(iter(engine._resting.values()))
        assert q["count"] == 3, (
            "remaining = max(0, original - skip) keeps the registered "
            "count in cumulative units == the original size (R5-MN2)")
        assert q["filled"] == 2


# ── MN3: 'pending' ls- rows must be visible to boot reconciliation ──────────

class TestMN3PendingRowsReconciledAtBoot:
    """R5-MN3: boot step-2 queried status='resting' only. A crash
    between insert_bot_order (status='pending', order_id=client_oid) and
    confirm_order_submitted stranded the row in 'pending' forever —
    invisible to step 2, never terminally marked, leaking into
    dashboards and the executor's pending-order conflict check. Fix:
    step-2 queries status IN ('resting','pending') and marks pending
    rows by client_order_id (the row never received a server order_id);
    step-1 additionally REPAIRS an API-present pending row via
    confirm_order_submitted (no-op for confirmed rows) so the adopted
    quote's lifecycle marks land on the row."""

    def test_pending_row_never_placed_is_terminally_marked(
            self, state, client, enabled):
        # Crash after the ledger insert, before (or during) place_order:
        # no API order, no server order_id, row status='pending'.
        state.insert_bot_order("ls-mn3a", TICKER, EVENT, "BTC", "no", 2,
                               92, False)
        assert _pending_status(state, "ls-mn3a") == "pending"
        engine = LongshotEngine(client, state)
        engine.tick()
        assert engine._boot_reconciled is True
        assert _pending_status(state, "ls-mn3a") == "canceled", (
            "a crash-between-place-and-confirm ls- row must be "
            "reconciled and terminally marked at boot — 'pending' rows "
            "were invisible to step 2 (R5-MN3)")
        assert _positions_row(state) is None  # no fills -> nothing booked

    def test_pending_row_still_resting_on_api_is_repaired_then_lifecycled(
            self, state, client, enabled):
        """Crash AFTER place succeeded but before confirm: the order IS
        on the API under a server order_id the local row never learned.
        Step-1 must repair the row (confirm_order_submitted) so its
        adopt-and-kill lifecycle marks land on the row instead of
        matching nothing."""
        now = time.time()
        state.insert_bot_order("ls-mn3b", TICKER, EVENT, "BTC", "no", 2,
                               92, False)
        client.get_orders.return_value = {"orders": [
            {"order_id": "oid-mn3b", "client_order_id": "ls-mn3b",
             "ticker": TICKER, "side": "no", "action": "buy",
             "no_price": 92, "count": 2, "remaining_count": 2,
             "status": "resting", "created_time": _rfc3339(now - 120)},
        ]}
        engine = LongshotEngine(client, state)
        engine.tick()  # adopt + repair + cancel (mock cancel succeeds)
        row = state.conn.execute(
            "SELECT order_id, status FROM pending_orders "
            "WHERE client_order_id='ls-mn3b'").fetchone()
        assert row["order_id"] == "oid-mn3b", (
            "step-1 must repair the pending row with the server "
            "order_id (R5-MN3)")
        assert row["status"] == "canceled", (
            "the adopted quote's lifecycle mark must land on the "
            "repaired row — pre-fix the server-id mark matched nothing "
            "and the row stayed 'pending' forever (R5-MN3)")

    def test_main_pipeline_pending_rows_untouched(self, state, client,
                                                  enabled):
        state.insert_bot_order("mk-mn3c", TICKER, EVENT, "BTC", "yes", 2,
                               80, False)
        engine = LongshotEngine(client, state)
        engine.tick()
        assert _pending_status(state, "mk-mn3c") == "pending", (
            "boot step-2 is scoped to ls- rows — main-pipeline pending "
            "rows are owned by the executor/reconciler (R5-MN3)")

    def test_pending_row_filled_matches_fill_by_client_order_id(
            self, state, client, enabled):
        """Crash after place+fill, before confirm: order is gone from
        GET /orders; Kalshi fills carry the SERVER order_id. The pending
        row's order_id is still the client_oid. _apply_fills must match
        fill.client_order_id == q.client_order_id or the fill is lost
        and the row is marked canceled with 0 contracts booked.
        """
        now = time.time()
        state.insert_bot_order("ls-mn3d", TICKER, EVENT, "BTC", "no", 2,
                               92, False)
        client.get_orders.return_value = {"orders": []}
        client.get_fills.return_value = {"fills": [
            {"order_id": "oid-mn3d-server",
             "client_order_id": "ls-mn3d",
             "trade_id": "t-mn3d",
             "count": 2,
             "ts": now - 5,
             "created_time": _rfc3339(now - 5)},
        ]}
        engine = LongshotEngine(client, state)
        engine.tick()
        pos = _positions_row(state)
        assert pos is not None and pos["count"] == 2, (
            "boot step-2 must book the fill keyed on client_order_id "
            "when q.order_id is still the client_oid (R5-MN3 fill match)")
        assert pos["side"] == "no"
        assert _pending_status(state, "ls-mn3d") == "filled"

    def test_fill_with_other_client_oid_is_not_stolen(
            self, state, client, enabled):
        """Matching on client_order_id must not book another order's fill."""
        now = time.time()
        state.insert_bot_order("ls-mn3e", TICKER, EVENT, "BTC", "no", 2,
                               92, False)
        client.get_orders.return_value = {"orders": []}
        client.get_fills.return_value = {"fills": [
            {"order_id": "oid-other",
             "client_order_id": "ls-someone-else",
             "trade_id": "t-other",
             "count": 2,
             "ts": now - 5,
             "created_time": _rfc3339(now - 5)},
        ]}
        engine = LongshotEngine(client, state)
        engine.tick()
        assert _positions_row(state) is None
        assert _pending_status(state, "ls-mn3e") == "canceled"

    def test_step2_skips_api_present_pending_if_confirm_fails(
            self, state, client, enabled):
        """confirm BUSY must not let step 2 double-book the same fills."""
        now = time.time()
        state.insert_bot_order("ls-mn3f", TICKER, EVENT, "BTC", "no", 2,
                               92, False)
        client.get_orders.return_value = {"orders": [
            {"order_id": "oid-mn3f", "client_order_id": "ls-mn3f",
             "ticker": TICKER, "side": "no", "action": "buy",
             "no_price": 92, "count": 2, "remaining_count": 2,
             "status": "resting", "created_time": _rfc3339(now - 120)},
        ]}
        client.get_fills.return_value = {"fills": [
            {"order_id": "oid-mn3f", "client_order_id": "ls-mn3f",
             "trade_id": "t-mn3f", "count": 2,
             "ts": now - 30, "created_time": _rfc3339(now - 30)},
        ]}
        client.cancel_order.return_value = {"order": {"status": "canceled"}}

        def boom(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        state.confirm_order_submitted = boom
        engine = LongshotEngine(client, state)
        engine.tick()
        pos = _positions_row(state)
        assert pos is not None and pos["count"] == 2, (
            "step 1 adopts once; step 2 must skip API-present coid")
        # Next boot: order gone from API, pending row unrepaired.
        # recorded_fill_count must have been bumped via coid so step 2
        # skip-seeds and does not double-book.
        client.get_orders.return_value = {"orders": []}
        engine2 = LongshotEngine(client, state)
        engine2.tick()
        pos2 = _positions_row(state)
        assert pos2 is not None and pos2["count"] == 2, (
            "second boot must not re-record fills after confirm-fail")

    def test_step2_still_books_when_api_order_lacks_order_id(
            self, state, client, enabled):
        """api_coids must not include bodies step 1 continues past."""
        now = time.time()
        state.insert_bot_order("ls-mn3g", TICKER, EVENT, "BTC", "no", 2,
                               92, False)
        state.confirm_order_submitted("ls-mn3g", "oid-mn3g")
        client.get_orders.return_value = {"orders": [
            {"client_order_id": "ls-mn3g", "ticker": TICKER,
             "side": "no", "action": "buy", "status": "resting"},
        ]}
        client.get_fills.return_value = {"fills": [
            {"order_id": "oid-mn3g", "client_order_id": "ls-mn3g",
             "trade_id": "t-mn3g", "count": 2,
             "ts": now - 30, "created_time": _rfc3339(now - 30)},
        ]}
        engine = LongshotEngine(client, state)
        engine.tick()
        pos = _positions_row(state)
        assert pos is not None and pos["count"] == 2, (
            "step 1 cannot adopt an order_id-less body; step 2 must still book")
