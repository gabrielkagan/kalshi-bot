"""Bit L-1 adversarial-review R2 regressions (longshot premium-harvest).

One test class per R2 finding (C1 / M1 / M2 / MN3). Fixtures mirror
tests/integration/test_longshot_r1_regressions.py (real sqlite3 file via
tmp_path per tests/CLAUDE.md integration-tier convention).

The C1 class includes the first full ``OpportunityScanner.scan()``-level
longshot test in the suite — R1/R2 history proved the
timeslot-occupancy-starvation class is INVISIBLE to direct-call tests
(evaluate_market / tick() called directly never traverse the
``_get_occupied_timeslots`` window filter), so the scan-path harness here
is the durable fix, not an optional extra.
"""
from __future__ import annotations

import datetime
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
    s = StateManager(str(tmp_path / "test_longshot_r2.db"))
    yield s
    s.close()


@pytest.fixture
def client():
    cl = MagicMock()
    cl.get_fills.return_value = {"fills": []}
    cl.get_orders.return_value = {"orders": []}
    cl.cancel_order.return_value = {"order": {"status": "canceled"}}
    cl.place_order.return_value = {"order": {"order_id": "oid-r2-1"}}
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


def _register(engine, *, order_id="oid-1", ticker=TICKER, stc=600.0,
              count=3, buy_price=92, buy_side="no", sell_side="yes"):
    engine.register_resting(
        order_id=order_id, client_order_id=f"ls-{order_id}", ticker=ticker,
        event_ticker=EVENT, asset="BTC", sell_side=sell_side,
        buy_side=buy_side, buy_price_cents=buy_price, count=count,
        seconds_to_close=stc)


def _ob(yes_ask_cents=8, yes_bid_cents=3):
    return {
        "no": [[100 - yes_ask_cents, 50]],
        "yes": [[yes_bid_cents, 50]],
    }


def _eval(eng, *, yes_ask_cents=8, yes_bid_cents=3, stc=600.0, spot=100.0,
          threshold=104.0, blended_rv=0.0001, ticker=TICKER, ob=None):
    if ob is None:
        ob = _ob(yes_ask_cents, yes_bid_cents)
    return eng.evaluate_market(
        ticker=ticker, event_ticker=EVENT, asset="BTC", product_type="15m",
        spot=spot, threshold=threshold, seconds_to_close=stc,
        blended_rv=blended_rv, orderbook_fetch=lambda: ob,
        config_snapshot_id=None, balance_at_scan=50000)


def _seed_pending_resting(state, *, client_oid, order_id, ticker=TICKER,
                          event=EVENT, side="no", count=3, price=92):
    state.insert_bot_order(client_oid, ticker, event, "BTC", side, count,
                           price, False)
    state.confirm_order_submitted(client_oid, order_id)


# ── scan()-level harness (first full scan-path longshot coverage) ────────────

class _ML:
    """Minimal MainLoop stand-in for OpportunityScanner/OrderExecutor.

    Plain object (NOT MagicMock) so unexpected attribute reads fail loudly
    and config_snapshot_id binds cleanly into sqlite.
    """

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
        # NOTE: no `executor` attr by default — scan() guards on
        # hasattr(self._ml, "executor"); tests that execute wire it in.
        self._scan_iter = 0
        self._scan_loop_start = 0.0
        self._open_positions_count_cache = {}


def _vol_est():
    return {"blended_rv": 0.0001, "regime": "normal"}


def _build_scanner(state, engine, client):
    from bot.scanner import OpportunityScanner
    feed = MagicMock()
    feed.get_price_with_ts.return_value = (100.0, time.monotonic())
    feed.get_price.return_value = 100.0
    feed.get_price_trailing_avg.return_value = 100.0
    feed.get_buffer.return_value = [100.0] * 120
    vol = MagicMock()
    vol.update.return_value = _vol_est()
    ml = _ML(engine)
    scanner = OpportunityScanner(
        client, state, feed, vol, MagicMock(), MagicMock(),
        kalshi_feed=None, main_loop=ml)
    # The extended-feature provider walks unstubbed feed methods and would
    # leak MagicMocks into sqlite binds — drop it for the harness.
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


# ── C1: occupancy filter must not starve the engine after first placement ────

class TestC1OccupancyStarvation:
    """R2-C1: `_execute_longshot_maker` persists a pending_orders row with
    status='resting'; `scan()` drops the whole (timeslot, asset) window via
    `_get_occupied_timeslots` BEFORE the market loop, but the longshot
    overlay lives INSIDE the market loop. From the next tick the engine's
    condition-flip cancel, _mark_inputs, and the MAIN pipeline's evaluation
    of that window all starve — and the block outlived the quote because
    `_cancel_quote` never marked the pending_orders row."""

    def test_pending_row_marked_canceled_on_cancel_pop(self, engine, state,
                                                       client, enabled):
        _seed_pending_resting(state, client_oid="ls-c1a", order_id="oid-c1a")
        _register(engine, order_id="oid-c1a", stc=170.0)
        engine.tick()  # T-3min sweep cancels + pops
        assert engine.resting_count() == 0
        row = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-c1a'"
        ).fetchone()
        assert row["status"] == "canceled", (
            "pending_orders row must be marked canceled when the quote is "
            "popped — otherwise the timeslot stays occupied forever (R2-C1)")

    def test_pending_row_marked_filled_on_full_fill_pop(self, engine, state,
                                                        client, enabled):
        _seed_pending_resting(state, client_oid="ls-c1b", order_id="oid-c1b")
        _register(engine, order_id="oid-c1b", stc=600.0)
        client.get_fills.return_value = {
            "fills": [{"order_id": "oid-c1b", "trade_id": "t-c1b",
                       "count": 3}]}
        engine.tick()
        assert engine.resting_count() == 0
        row = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-c1b'"
        ).fetchone()
        assert row["status"] == "filled"

    def test_pending_row_marked_canceled_on_stale_drop(self, engine, state,
                                                       client, enabled):
        _seed_pending_resting(state, client_oid="ls-c1c", order_id="oid-c1c")
        _register(engine, order_id="oid-c1c", stc=600.0)
        client.cancel_order.return_value = None  # cancel keeps failing
        with engine._lock:
            t0 = engine._resting["oid-c1c"]["registered_ts"]
        engine.tick(now=t0 + 721)  # stale-drop path pops the entry
        assert engine.resting_count() == 0
        row = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-c1c'"
        ).fetchone()
        assert row["status"] == "canceled"

    def test_occupied_timeslots_exclude_longshot_rows(self, engine, state,
                                                      client, enabled):
        scanner, _ml = _build_scanner(state, engine, client)
        # longshot resting order + longshot open position on EVENT's slot
        _seed_pending_resting(state, client_oid="ls-occ-1",
                              order_id="oid-occ-1")
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "no", 1, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        # main-pipeline position on EVENT2's slot must still occupy
        state.record_position_from_fill(
            TICKER2, EVENT2, "BTC", "yes", 1, 95, strategy="MAKER_PATIENT",
            is_taker=True)
        occupied = scanner._get_occupied_timeslots()
        ls_slot = scanner._window_timeslot(EVENT)
        main_slot = scanner._window_timeslot(EVENT2)
        assert "BTC" not in occupied.get(ls_slot, set()), (
            "longshot rows (ls- resting order + strategy_group='longshot' "
            "position) must NOT occupy the timeslot (R2-C1)")
        assert "BTC" in occupied.get(main_slot, set())

    def test_scan_continues_after_longshot_quote_posted(self, state, client,
                                                        enabled):
        """THE durable regression: post a longshot quote through the real
        scan()+execute() path, then assert the NEXT tick still (a) reaches
        LongshotEngine.evaluate_market and (b) runs the main-pipeline
        evaluation of the window. Pre-fix the occupied-timeslot filter
        dropped the window before the market loop, starving both."""
        from bot.executor import OrderExecutor
        engine = LongshotEngine(client, state)
        eval_calls = []
        orig_eval = engine.evaluate_market

        def _spy(**kwargs):
            out = orig_eval(**kwargs)
            eval_calls.append(out)
            return out

        engine.evaluate_market = _spy
        scanner, ml = _build_scanner(state, engine, client)
        executor = OrderExecutor(client, state, MagicMock(),
                                 main_loop=ml, kalshi_feed=None)
        ml.executor = executor

        # tick 1: scan emits the longshot candidate
        selected = scanner.scan([_window()])
        assert len(eval_calls) == 1
        ls_cands = [c for c in (selected or [])
                    if c.get("strategy") == "longshot"]
        assert len(ls_cands) == 1, "longshot candidate must survive scan tail"
        scanned_tick1 = scanner._session_total_scanned
        assert scanned_tick1 >= 1  # main pipeline evaluated the market

        # place through the real executor chokepoint -> pending_orders row
        # status='resting' with the ls- client_oid prefix
        assert executor.execute(ls_cands[0]) is not None
        row = state.conn.execute(
            "SELECT status, client_order_id FROM pending_orders "
            "WHERE ticker=?", (TICKER,)).fetchone()
        assert row["status"] == "resting"
        assert row["client_order_id"].startswith(
            C.LONGSHOT_CLIENT_OID_PREFIX)

        # tick 2: the window must NOT be starved by its own resting quote
        scanner.scan([_window()])
        assert len(eval_calls) == 2, (
            "evaluate_market must be reached on the tick AFTER a longshot "
            "quote is posted — occupied-timeslot filter starved the engine "
            "(R2-C1)")
        assert scanner._session_total_scanned > scanned_tick1, (
            "main-pipeline evaluation of the window must continue on the "
            "tick after a longshot quote is posted (R2-C1)")


# ── M1: boot-orphan fills must be recoverable (min_ts bound) ─────────────────

def _rfc3339(epoch):
    return datetime.datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _min_ts_respecting_fills(fills):
    """get_fills side_effect that filters like the real API: only fills
    with ts >= min_ts are returned. This is what makes the pre-fix
    min_ts=now-60 bound a real data loss instead of a mock artifact."""
    def _side(min_ts=None, cursor=None, **kw):
        out = [f for f in fills
               if min_ts is None or f.get("ts", 0) >= min_ts]
        return {"fills": out}
    return _side


class TestM1BootOrphanFillRecovery:
    """R2-M1: adopted orphans derived the fills min_ts from registered_ts
    (= restart time), so any fill landed BEFORE the restart was outside
    the fetch bound and never recorded; and ls- orders that were no
    longer resting at boot (fully filled / expired pre-restart) were
    never reconciled at all."""

    def test_fill_landed_5min_before_restart_recorded(self, engine, state,
                                                      client, enabled):
        now = time.time()
        client.get_orders.return_value = {"orders": [
            {"order_id": "oid-orph-r2", "client_order_id": "ls-orph-r2",
             "ticker": TICKER, "side": "no", "no_price": 92, "count": 3,
             "remaining_count": 2, "status": "resting",
             "created_time": _rfc3339(now - 600)},
        ]}
        client.get_fills.side_effect = _min_ts_respecting_fills([
            {"order_id": "oid-orph-r2", "trade_id": "t-pre-restart",
             "count": 1, "ts": now - 300},
        ])
        engine.tick()
        row = state.conn.execute(
            "SELECT side, count FROM positions WHERE ticker=? "
            "AND status='open'", (TICKER,)).fetchone()
        assert row is not None, (
            "fill landed 5 min before restart must be recorded — min_ts "
            "bound from registered_ts loses pre-restart fills (R2-M1)")
        assert row["side"] == "no"
        assert row["count"] == 1

    def test_fully_filled_pre_restart_order_recorded(self, engine, state,
                                                     client, enabled):
        now = time.time()
        # ls- order persisted pre-restart, fully filled before the restart:
        # Kalshi no longer lists it as resting, only the local row remains.
        _seed_pending_resting(state, client_oid="ls-gone-1",
                              order_id="oid-gone-1", count=3)
        state.conn.execute(
            "UPDATE pending_orders SET created_at=? WHERE order_id=?",
            (_rfc3339(now - 600), "oid-gone-1"))
        state.conn.commit()
        # main-pipeline resting row must NOT be touched by the reconcile
        _seed_pending_resting(state, client_oid="coid-main-r2",
                              order_id="oid-main-r2", ticker=TICKER2,
                              event=EVENT2, side="yes", price=95)
        client.get_orders.return_value = {"orders": []}
        client.get_fills.side_effect = _min_ts_respecting_fills([
            {"order_id": "oid-gone-1", "trade_id": "t-gone-1",
             "count": 3, "ts": now - 300},
        ])
        engine.tick()
        row = state.conn.execute(
            "SELECT side, count, strategy_group FROM positions "
            "WHERE ticker=? AND status='open'", (TICKER,)).fetchone()
        assert row is not None, (
            "fully-filled-pre-restart ls- order must be reconciled against "
            "fills at boot (R2-M1)")
        assert row["count"] == 3
        assert row["strategy_group"] == "longshot"
        status = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-gone-1'"
        ).fetchone()["status"]
        assert status == "filled"
        main_status = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-main-r2'"
        ).fetchone()["status"]
        assert main_status == "resting", "main-pipeline rows untouched"

    def test_expired_unfilled_pre_restart_row_marked_canceled(
            self, engine, state, client, enabled):
        now = time.time()
        _seed_pending_resting(state, client_oid="ls-exp-1",
                              order_id="oid-exp-1", count=3)
        state.conn.execute(
            "UPDATE pending_orders SET created_at=? WHERE order_id=?",
            (_rfc3339(now - 600), "oid-exp-1"))
        state.conn.commit()
        client.get_orders.return_value = {"orders": []}
        client.get_fills.side_effect = _min_ts_respecting_fills([])
        engine.tick()
        status = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-exp-1'"
        ).fetchone()["status"]
        assert status == "canceled"
        assert state.conn.execute(
            "SELECT COUNT(*) FROM positions").fetchone()[0] == 0


# ── M2: FP-primary field extraction + dedup-stamp-after-validation ───────────

class TestM2FpFieldsAndDedupStamp:
    """R2-M2: three sites read only the legacy non-_fp fields (count /
    fill_count / count) instead of the canonical FP-primary extraction
    chains (executor.py _on_fill / executor.py taker-submit /
    state.py:1622 patterns), and _apply_fills stamped seen_trade_ids
    BEFORE the fill_count<=0 validation — a zero-parse fill was
    permanently blacklisted instead of retried."""

    def test_count_fp_only_fill_recorded(self, engine, state, client,
                                         enabled):
        _register(engine, order_id="oid-m2fp", stc=600.0)
        client.get_fills.return_value = {
            "fills": [{"order_id": "oid-m2fp", "trade_id": "t-fp",
                       "count_fp": "2"}]}  # NO legacy count field
        engine.tick()
        row = state.conn.execute(
            "SELECT count FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row is not None, (
            "count_fp-only shaped fill must be recorded (R2-M2)")
        assert row["count"] == 2

    def test_zero_parse_fill_not_permanently_blacklisted(self, engine, state,
                                                         client, enabled):
        _register(engine, order_id="oid-m2z", stc=600.0)
        # malformed snapshot: count fields missing/unparsable -> 0 contracts
        client.get_fills.return_value = {
            "fills": [{"order_id": "oid-m2z", "trade_id": "t-z"}]}
        engine.tick(now=1000.0)
        assert state.conn.execute(
            "SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        # next snapshot carries the count -> must be recorded, not skipped
        # via a stale seen_trade_ids stamp
        client.get_fills.return_value = {
            "fills": [{"order_id": "oid-m2z", "trade_id": "t-z",
                       "count": 2}]}
        engine.tick(now=2000.0)
        row = state.conn.execute(
            "SELECT count FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row is not None, (
            "zero-parse fill must be retried on the next snapshot — "
            "dedup stamp must come AFTER the count validation (R2-M2)")
        assert row["count"] == 2

    def test_cancel_reconcile_reads_fill_count_fp(self, engine, state, client,
                                                  enabled, caplog):
        _register(engine, order_id="oid-m2c", stc=170.0)
        client.get_fills.return_value = {"fills": []}
        # DELETE response carries ONLY the FP field — legacy fill_count absent
        client.cancel_order.return_value = {
            "order": {"status": "canceled", "fill_count_fp": "2"}}
        with caplog.at_level("WARNING"):
            engine.tick()
        assert engine.resting_count() == 1, (
            "fill_count_fp on the cancel response must keep the entry "
            "registered for a fill-poll retry (R2-M2)")
        assert "LONGSHOT_CANCEL_FILL_MISMATCH" in caplog.text

    def test_boot_adoption_uses_remaining_count_fp(self, engine, state,
                                                   client, enabled):
        now = time.time()
        client.get_orders.return_value = {"orders": [
            {"order_id": "oid-m2b", "client_order_id": "ls-m2b",
             "ticker": TICKER, "side": "no", "no_price": 92,
             # FP-primary remaining; legacy fields absent (state.py:1622
             # extraction pattern). Stale `count` would be the ORIGINAL
             # size, not what's still resting.
             "remaining_count_fp": "2", "status": "resting",
             "created_time": _rfc3339(now - 300)},
        ]}
        # cancel fails so the adopted entry stays registered and inspectable
        client.cancel_order.return_value = None
        engine.tick()
        assert engine.resting_count() == 1
        with engine._lock:
            q = engine._resting["oid-m2b"]
        assert q["count"] == 2
