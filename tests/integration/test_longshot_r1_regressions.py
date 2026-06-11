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
