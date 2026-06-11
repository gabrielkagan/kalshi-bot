"""Bit L-1 adversarial-review R1 regressions (longshot premium-harvest).

One test class per R1 finding. Each class docstring carries the finding id;
fixtures mirror tests/integration/test_longshot_strategy.py (real sqlite3
file via tmp_path per tests/CLAUDE.md integration-tier convention).
"""
from __future__ import annotations

import datetime
from datetime import timezone
from unittest.mock import MagicMock

import pytest

import bot.constants as C
from bot.state import StateManager

import bot.longshot as longshot_mod
from bot.longshot import LongshotEngine

TICKER = "KXBTC15M-26JUN111200-T110"
EVENT = "KXBTC15M-26JUN111200"


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "test_longshot_r1.db"))
    yield s
    s.close()


@pytest.fixture
def engine(state):
    client = MagicMock()
    client.get_fills.return_value = {"fills": []}
    client.get_orders.return_value = {"orders": []}
    client.cancel_order.return_value = {"order": {"status": "canceled"}}
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
          threshold=110.0, blended_rv=0.0001, ticker=TICKER, ob=None):
    if ob is None:
        ob = _ob(yes_ask_cents, yes_bid_cents)
    return eng.evaluate_market(
        ticker=ticker, event_ticker=EVENT, asset="BTC", product_type="15m",
        spot=spot, threshold=threshold, seconds_to_close=stc,
        blended_rv=blended_rv, orderbook_fetch=lambda: ob,
        config_snapshot_id=None, balance_at_scan=50000)


# ── C1: fills must be polled before / at cancel, never dropped ───────────────

class TestC1FillsNotDroppedAtCancel:
    """R1-C1: tick() ran the T-3min cancel sweep BEFORE fill polling and
    _cancel_quote popped the entry without a final poll — a fill landing
    between the last poll and the cancel was silently dropped (position
    held to settlement with NO local position row)."""

    def test_fill_arriving_at_t3_cancel_is_recorded(self, engine, state,
                                                    enabled):
        # Quote past T-3min: the sweep will cancel it on this tick. The
        # fill arrived after the previous poll — it MUST still be recorded.
        _register(engine, order_id="oid-c1", stc=170.0)
        engine._client.get_fills.return_value = {
            "fills": [{"order_id": "oid-c1", "trade_id": "t-c1", "count": 2}]}
        engine.tick()
        row = state.conn.execute(
            "SELECT count, strategy_group FROM positions "
            "WHERE ticker=? AND status='open'", (TICKER,)).fetchone()
        assert row is not None, "fill dropped at cancel (R1-C1)"
        assert row["count"] == 2
        assert row["strategy_group"] == "longshot"
        assert engine.resting_count() == 0

    def test_fill_arriving_at_condition_flip_cancel_is_recorded(
            self, engine, state, enabled):
        _register(engine, order_id="oid-c1b", stc=600.0)
        engine._client.get_fills.return_value = {
            "fills": [{"order_id": "oid-c1b", "trade_id": "t-c1b",
                       "count": 1}]}
        # ask moved out of band -> condition-flip cancel path
        cands = _eval(engine, yes_ask_cents=20)
        assert cands == []
        row = state.conn.execute(
            "SELECT count FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row is not None, "fill dropped at condition-flip cancel"
        assert row["count"] == 1

    def test_cancel_response_fill_count_mismatch_keeps_entry(
            self, engine, state, enabled, caplog):
        # DELETE response says 2 contracts filled but the fills API hasn't
        # surfaced them yet — the entry must stay registered for a retry
        # instead of being popped (which would orphan the fill forever).
        _register(engine, order_id="oid-c1c", stc=170.0)
        engine._client.get_fills.return_value = {"fills": []}
        engine._client.cancel_order.return_value = {
            "order": {"status": "canceled", "fill_count": 2}}
        with caplog.at_level("WARNING"):
            engine.tick()
        assert engine.resting_count() == 1
        assert "LONGSHOT_CANCEL_FILL_MISMATCH" in caplog.text
        # next tick the fills API catches up -> recorded, entry dropped
        engine._client.get_fills.return_value = {
            "fills": [{"order_id": "oid-c1c", "trade_id": "t-c1c",
                       "count": 2}]}
        engine.tick()
        row = state.conn.execute(
            "SELECT count FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row is not None and row["count"] == 2
        assert engine.resting_count() == 0


# ── M1: restart orphans — boot reconciliation via ls- client_oid prefix ──────

class TestM1BootOrphanReconciliation:
    """R1-M1: a restart wiped the in-memory _resting registry, leaving real
    resting longshot orders on Kalshi with NO lifecycle owner (no T-3min
    cancel, no fill recording). Boot reconciliation lists open orders,
    identifies longshot's by the ls- client_order_id prefix, fill-polls,
    then cancels."""

    def test_prefix_constant_exists(self):
        assert C.LONGSHOT_CLIENT_OID_PREFIX == "ls-"

    def _orders(self):
        return {"orders": [
            {"order_id": "oid-orph", "client_order_id": "ls-orph-1",
             "ticker": TICKER, "side": "no", "no_price": 92, "count": 3,
             "status": "resting"},
            {"order_id": "oid-main", "client_order_id": "b2c3d4-main",
             "ticker": TICKER, "side": "yes", "yes_price": 95, "count": 1,
             "status": "resting"},
        ]}

    def test_orphan_polled_then_cancelled_on_first_tick(self, engine, state,
                                                        enabled):
        engine._client.get_orders.return_value = self._orders()
        engine._client.get_fills.return_value = {
            "fills": [{"order_id": "oid-orph", "trade_id": "t-orph",
                       "count": 1}]}
        engine.tick()
        cancelled = [c.args[0] for c in
                     engine._client.cancel_order.call_args_list]
        assert "oid-orph" in cancelled
        assert "oid-main" not in cancelled  # main-pipeline order untouched
        row = state.conn.execute(
            "SELECT side, count, avg_price_cents, strategy_group "
            "FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row is not None, "orphan fill not recorded at boot"
        assert row["side"] == "no"
        assert row["count"] == 1
        assert row["avg_price_cents"] == 92
        assert row["strategy_group"] == "longshot"
        assert engine.resting_count() == 0

    def test_reconcile_runs_once(self, engine, enabled):
        engine._client.get_orders.return_value = {"orders": []}
        engine.tick()
        engine.tick()
        assert engine._client.get_orders.call_count == 1

    def test_reconcile_retries_after_api_failure(self, engine, enabled):
        engine._client.get_orders.return_value = None
        engine.tick()
        engine._client.get_orders.return_value = self._orders()
        engine.tick()
        assert engine._client.get_orders.call_count == 2
        cancelled = [c.args[0] for c in
                     engine._client.cancel_order.call_args_list]
        assert "oid-orph" in cancelled

    def test_reconcile_runs_even_when_disabled(self, engine, state):
        # LONGSHOT_ENABLED stays False (shipped default): orphans from a
        # pre-restart enabled run must still be cancelled (reduces exposure).
        engine._client.get_orders.return_value = self._orders()
        engine.tick()
        cancelled = [c.args[0] for c in
                     engine._client.cancel_order.call_args_list]
        assert "oid-orph" in cancelled

    def test_placement_client_oid_carries_prefix(self, state, enabled):
        from bot.executor import OrderExecutor
        client = MagicMock()
        client.get_fills.return_value = {"fills": []}
        client.get_orders.return_value = {"orders": []}
        client.place_order.return_value = {"order": {"order_id": "oid-x"}}
        eng = LongshotEngine(client, state)
        ml = MagicMock()
        ml.longshot_engine = eng
        executor = OrderExecutor(client, state, MagicMock(),
                                 main_loop=ml, kalshi_feed=None)
        cands = _eval(eng)
        assert len(cands) == 1
        assert executor.execute(cands[0]) is not None
        coid = client.place_order.call_args.kwargs["client_order_id"]
        assert coid.startswith(C.LONGSHOT_CLIENT_OID_PREFIX)
        row = state.conn.execute(
            "SELECT client_order_id FROM pending_orders WHERE ticker=?",
            (TICKER,)).fetchone()
        assert row["client_order_id"] == coid


# ── C2: positions ticker-PK collision stopgap ────────────────────────────────

@pytest.fixture
def wired(state):
    from bot.executor import OrderExecutor
    client = MagicMock()
    client.get_fills.return_value = {"fills": []}
    client.get_orders.return_value = {"orders": []}
    client.cancel_order.return_value = {"order": {"status": "canceled"}}
    client.place_order.return_value = {"order": {"order_id": "oid-live-1"}}
    eng = LongshotEngine(client, state)
    ml = MagicMock()
    ml.longshot_engine = eng
    executor = OrderExecutor(client, state, MagicMock(),
                             main_loop=ml, kalshi_feed=None)
    return executor, eng, client


class TestC2PositionsPKCollisionStopgap:
    """R1-C2: positions PK is (ticker) and record_position_from_fill uses
    INSERT OR REPLACE — a longshot fill on a ticker the main pipeline also
    holds clobbers the main row (and vice versa). L-1-scope STOPGAP keeps
    longshot off any ticker with main-pipeline flow; the durable composite-
    PK rebuild is ticketed 86badbf9t."""

    def test_no_quote_when_main_position_open(self, engine, state, enabled):
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "yes", 2, 95, strategy="MAKER_PATIENT",
            is_taker=True)
        assert _eval(engine) == []

    def test_no_quote_when_null_strategy_group_position_open(self, engine,
                                                             state, enabled):
        # legacy rows can carry NULL strategy_group — must count as main
        state.conn.execute(
            "INSERT INTO positions (ticker, event_ticker, asset, side, count,"
            " avg_price_cents, total_cost_cents, opened_at, updated_at,"
            " status) VALUES (?,?,?,?,?,?,?,?,?,'open')",
            (TICKER, EVENT, "BTC", "yes", 1, 95, 95,
             "2026-06-11T12:00:00Z", "2026-06-11T12:00:00Z"))
        state.conn.commit()
        assert _eval(engine) == []

    def test_authorize_blocks_when_main_position_appears_after_scan(
            self, engine, state, enabled):
        cands = _eval(engine)
        assert len(cands) == 1
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "yes", 2, 95, strategy="MAKER_PATIENT",
            is_taker=True)
        assert engine.authorize(cands[0]) == 0

    def test_executor_blocks_when_main_maker_active_on_ticker(self, wired,
                                                              enabled):
        executor, eng, client = wired
        cands = _eval(eng)
        assert len(cands) == 1
        executor._active_orders["BTC"] = {"ticker": TICKER,
                                          "order_id": "oid-main"}
        assert executor.execute(cands[0]) is None
        client.place_order.assert_not_called()

    def test_executor_blocks_when_main_resting_order_on_ticker(self, wired,
                                                               state,
                                                               enabled):
        executor, eng, client = wired
        cands = _eval(eng)
        assert len(cands) == 1
        state.insert_bot_order("coid-main-1", TICKER, EVENT, "BTC", "yes",
                               1, 95, False)
        state.confirm_order_submitted("coid-main-1", "oid-main-1")
        assert executor.execute(cands[0]) is None
        client.place_order.assert_not_called()

    def test_executor_allows_when_only_own_ls_order_on_ticker(self, wired,
                                                              state,
                                                              enabled):
        # longshot's OWN resting order (ls- prefix) must not self-block
        executor, eng, client = wired
        cands = _eval(eng)
        assert len(cands) == 1
        state.insert_bot_order("ls-own-1", TICKER, EVENT, "BTC", "no",
                               1, 92, False)
        state.confirm_order_submitted("ls-own-1", "oid-ls-1")
        assert executor.execute(cands[0]) is not None
        client.place_order.assert_called_once()

    def test_scanner_overlay_guards_on_main_position(self):
        # source pin: the scanner longshot overlay must consult
        # has_open_main_pipeline_position before evaluate_market
        # (defense-in-depth at the scan layer; same C2 stopgap).
        import pathlib
        src = pathlib.Path("bot/scanner/__init__.py").read_text()
        start = src.index("Longshot premium-harvest overlay")
        block = src[start:start + 3500]
        guard = block.index("has_open_main_pipeline_position")
        call = block.index("evaluate_market")
        assert guard < call, (
            "scanner overlay must check has_open_main_pipeline_position "
            "BEFORE calling evaluate_market (R1-C2)")


# ── M2: immortal cancel-failure entries ──────────────────────────────────────

class TestM2ImmortalCancelEntries:
    """R1-M2: a quote whose cancel kept failing (API None / order already
    gone) stayed in _resting forever — re-cancelled every tick, polluting
    caps and REST budget. Order-not-found (DELETE 404 sentinel) is
    terminal; anything else is dropped once the window is 120s past close
    after one final fill poll."""

    def test_cancel_404_is_terminal_with_final_poll(self, engine, state,
                                                    enabled):
        _register(engine, order_id="oid-m2", stc=170.0)
        engine._client.cancel_order.return_value = {
            "_error": True, "_status_code": 404}
        engine._client.get_fills.return_value = {
            "fills": [{"order_id": "oid-m2", "trade_id": "t-m2",
                       "count": 1}]}
        engine.tick()
        assert engine.resting_count() == 0
        row = state.conn.execute(
            "SELECT count FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row is not None and row["count"] == 1

    def test_persistent_cancel_failure_dropped_past_grace(self, engine,
                                                          state, enabled,
                                                          caplog):
        _register(engine, order_id="oid-m2b", stc=600.0)
        engine._client.cancel_order.return_value = None  # always fails
        with engine._lock:
            t0 = engine._resting["oid-m2b"]["registered_ts"]
        engine.tick(now=t0 + 500)  # remaining=100 -> cancel fails, stays
        assert engine.resting_count() == 1
        # fills API surfaces a fill just before the stale drop
        engine._client.get_fills.return_value = {
            "fills": [{"order_id": "oid-m2b", "trade_id": "t-m2b",
                       "count": 2}]}
        with caplog.at_level("WARNING"):
            engine.tick(now=t0 + 721)  # remaining=-121 < -120 -> drop
        assert engine.resting_count() == 0
        assert "LONGSHOT_STALE_DROP" in caplog.text
        row = state.conn.execute(
            "SELECT count FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row is not None and row["count"] == 2

    def test_not_dropped_inside_grace(self, engine, enabled):
        _register(engine, order_id="oid-m2c", stc=600.0)
        engine._client.cancel_order.return_value = None
        with engine._lock:
            t0 = engine._resting["oid-m2c"]["registered_ts"]
        engine.tick(now=t0 + 700)  # remaining=-100 > -120 -> keep retrying
        assert engine.resting_count() == 1
