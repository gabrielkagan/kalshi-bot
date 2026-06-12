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
    # Fresh Bit-S.1 staleness reading so the frozen/unmeasured-spot gate
    # (R1-M1 fix round, Bit V.1) doesn't mask the surfaces under test —
    # same idiom as test_twaplock_strategy.py::_eval.
    eng._state._scan_spot_staleness_cache["BTC"] = 0.0
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


# ── M3: daily cap must include marked (sold-side-ITM) open positions ─────────

TICKER2 = "KXBTC15M-26JUN111215-T110"
EVENT2 = "KXBTC15M-26JUN111215"


def _seed_settled(state, ticker, pnl_cents, settled_date,
                  strategy="longshot"):
    state.conn.execute(
        "INSERT INTO settled_trades (ticker, event_ticker, asset,"
        " market_result, side, count, entry_price_cents, revenue_cents,"
        " fee_cents, pnl_cents, settled_at, strategy)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ticker, EVENT, "BTC", "no", "no", 3, 92, 0, 0, pnl_cents,
         f"{settled_date}T12:00:00.000000Z", strategy))
    state.conn.commit()


def _today_iso():
    return datetime.datetime.now(timezone.utc).date().isoformat()


class TestM3DailyCapMarkedTerm:
    """R1-M3: the $20 daily cap counted REALIZED PnL only — open longshot
    positions whose sold side is currently ITM (a near-certain full loss
    at settlement) didn't count, so the cap could be blown by multiples
    before settlement realized it. Plan doc says 'realized+marked'."""

    def test_itm_sold_side_marks_toward_cap(self, engine, state, enabled):
        # realized -$18 (under the $20 cap) ...
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -1800, _today_iso())
        # ... plus an open position on TICKER2 (bought NO 3 @ 92c = 276c)
        # whose SOLD side (YES) is ITM at the latest mark: 1800+276 >= 2000.
        state.record_position_from_fill(
            TICKER2, EVENT2, "BTC", "no", 3, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        out = _eval(engine, ticker=TICKER2, spot=120.0, threshold=110.0)
        assert out == []
        assert engine.disabled_reason() == "daily_cap"

    def test_otm_sold_side_does_not_mark(self, engine, state, enabled):
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -1800, _today_iso())
        state.record_position_from_fill(
            TICKER2, EVENT2, "BTC", "no", 3, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        # sold YES still OTM (spot below strike) -> no marked loss
        _eval(engine, ticker=TICKER2, spot=100.0, threshold=110.0)
        assert engine.disabled_reason() is None
        assert len(_eval(engine, ticker=TICKER)) == 1

    def test_marked_only_can_trip_cap(self, engine, state, enabled,
                                      monkeypatch, caplog):
        # zero realized; cap shrunk to $2 so the 276c marked loss trips it
        # (Bit T-1: cap constant is the COMBINED live-small rail now)
        monkeypatch.setattr(C, "LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS", 2.0,
                            raising=False)
        state.record_position_from_fill(
            TICKER2, EVENT2, "BTC", "no", 3, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        with caplog.at_level("WARNING"):
            out = _eval(engine, ticker=TICKER2, spot=120.0, threshold=110.0)
        assert out == []
        assert engine.disabled_reason() == "daily_cap"
        assert "LONGSHOT_DAILY_CAP_HIT" in caplog.text

    def test_sold_no_side_itm_when_spot_below_strike(self, engine, state,
                                                     enabled, monkeypatch):
        monkeypatch.setattr(C, "LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS", 2.0,
                            raising=False)
        # bought YES (sold NO); NO is ITM when spot < threshold
        state.record_position_from_fill(
            TICKER2, EVENT2, "BTC", "yes", 3, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        out = _eval(engine, ticker=TICKER2, spot=100.0, threshold=110.0)
        assert out == []
        assert engine.disabled_reason() == "daily_cap"


# ── M6: ONE paginated unfiltered get_fills per tick ──────────────────────────

class TestM6SingleFillsFetchPerTick:
    """R1-M6: fill polling issued one get_fills REST call PER resting
    quote per tick. One unfiltered, cursor-paginated call per tick is
    dispatched across all resting quotes by order_id instead."""

    def test_one_unfiltered_call_dispatched_across_quotes(self, engine,
                                                          state, enabled):
        _register(engine, order_id="oid-a", ticker=TICKER, stc=600.0)
        _register(engine, order_id="oid-b", ticker=TICKER2, stc=600.0)
        engine._client.get_fills.return_value = {"fills": [
            {"order_id": "oid-a", "trade_id": "t-a", "count": 2},
            {"order_id": "oid-b", "trade_id": "t-b", "count": 1},
        ]}
        engine.tick()
        engine._client.get_fills.assert_called_once()
        kwargs = engine._client.get_fills.call_args.kwargs
        assert kwargs.get("ticker") is None, "must be UNFILTERED (one call)"
        rows = {r["ticker"]: r["count"] for r in state.conn.execute(
            "SELECT ticker, count FROM positions WHERE status='open'")}
        assert rows == {TICKER: 2, TICKER2: 1}

    def test_pagination_follows_cursor(self, engine, state, enabled):
        _register(engine, order_id="oid-p", ticker=TICKER, stc=600.0)
        engine._client.get_fills.side_effect = [
            {"fills": [{"order_id": "oid-p", "trade_id": "t-p1",
                        "count": 1}], "cursor": "cur-1"},
            {"fills": [{"order_id": "oid-p", "trade_id": "t-p2",
                        "count": 2}]},
        ]
        engine.tick()
        assert engine._client.get_fills.call_count == 2
        second = engine._client.get_fills.call_args_list[1].kwargs
        assert second.get("cursor") == "cur-1"
        row = state.conn.execute(
            "SELECT count FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row is not None and row["count"] == 3
        assert engine.resting_count() == 0  # 3/3 filled -> popped

    def test_no_fetch_when_no_quotes_resting(self, engine, enabled):
        engine.tick()
        engine._client.get_fills.assert_not_called()


# ── MN1: eval-row write dedup per (ticker, side) ─────────────────────────────

class TestMN1EvalRowDedup:
    """R1-MN1: evaluate_market wrote an evaluated_opportunities row every
    tick while the condition held (~once per scan tick per side). Dedup
    per (ticker, side) mirrors the scanner's _eval_opp_seen pattern —
    candidates still emit every tick; only the DB write is once."""

    def test_row_written_once_per_ticker_side(self, engine, state, enabled,
                                              monkeypatch):
        calls = []
        orig = state.insert_evaluated_opportunity

        def _spy(*a, **k):
            calls.append(1)
            return orig(*a, **k)

        monkeypatch.setattr(state, "insert_evaluated_opportunity", _spy)
        assert len(_eval(engine)) == 1
        assert len(_eval(engine)) == 1  # candidate still emitted
        assert len(calls) == 1, "eval row must be written once per "\
                                "(ticker, side), not per tick"

    def test_other_ticker_still_writes(self, engine, state, enabled,
                                       monkeypatch):
        calls = []
        orig = state.insert_evaluated_opportunity

        def _spy(*a, **k):
            calls.append(1)
            return orig(*a, **k)

        monkeypatch.setattr(state, "insert_evaluated_opportunity", _spy)
        _eval(engine, ticker=TICKER)
        _eval(engine, ticker=TICKER2)
        assert len(calls) == 2


# ── MN4: mid-flight trading-mode flip cancels resting quotes ─────────────────

class TestMN4ModeFlipCancelsResting:
    """R1-MN4: the trading-mode gate only protects NEW placements; a
    live->shadow flip mid-flight left already-resting longshot quotes
    working (cancel_order is intentionally ungated, so the engine must
    cancel them itself on tick)."""

    def test_mode_flip_cancels_resting(self, engine, enabled, monkeypatch):
        _register(engine, order_id="oid-mn4", stc=600.0)
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", False,
                            raising=False)
        engine.tick()
        cancelled = [c.args[0] for c in
                     engine._client.cancel_order.call_args_list]
        assert "oid-mn4" in cancelled
        assert engine.resting_count() == 0

    def test_no_cancel_when_override_keeps_longshot_live(self, engine,
                                                         enabled,
                                                         monkeypatch):
        _register(engine, order_id="oid-mn4b", stc=600.0)
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", True, raising=False)
        engine.tick()
        engine._client.cancel_order.assert_not_called()
        assert engine.resting_count() == 1


# ── M4: per-strategy live override (longshot-only go-live) ──────────────────

def _main_candidate(**overrides):
    base = {
        "ticker": TICKER, "event_ticker": EVENT, "asset": "BTC",
        "best_yes_ask": 92, "position_size": 5, "calibrated_prob": 0.96,
        "edge": 0.03, "seconds_to_close": 400, "strategy": "above",
        "balance_at_scan": 50000, "spot": 68500.0, "threshold": 68000.0,
        "blended_rv": 0.0004, "z_score": 2.5, "vol_regime": "normal",
        "kelly_f": 0.15, "product_type": "15m", "ofa_adjustment": 0.0,
        "ob_snapshot": {"ask_depth": 10}, "calibrated_prob_raw": 0.95,
        "drawdown_scaler": 1.0,
    }
    base.update(overrides)
    return base


def _mocked_main_executor():
    from bot.executor import OrderExecutor
    client = MagicMock()
    client.get_orderbook.return_value = None
    client.place_order.return_value = {"order": {"order_id": "ord-m"}}
    return OrderExecutor(client=client, state=MagicMock(),
                         logger=MagicMock(), main_loop=MagicMock(),
                         kalshi_feed=None), client


class TestM4PerStrategyLiveOverride:
    """R1-M4: there was no way to go live with longshot ONLY — flipping
    GLOBAL_LIVE_TRADING would wake the whole main pipeline. Fix:
    LONGSHOT_LIVE_OVERRIDE constant + trading_mode.strategy_is_live
    (single-chokepoint design per bot/trading_mode.py / PR #158),
    consulted at executor.execute() and at the place_order backstop via
    the ls- client_oid prefix. Main pipeline behavior UNCHANGED."""

    def test_override_paused_after_go_live(self):
        # True at the 2026-06-12 go-live; flipped back False same day at the
        # operator pause (live loss rate 4/11 vs ~6% backtest — adverse
        # selection autopsy). Engine stays ENABLED (shadow rows continue).
        assert C.LONGSHOT_LIVE_OVERRIDE is False

    def test_strategy_is_live_truth_table(self, monkeypatch):
        from bot import trading_mode as tm
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", False,
                            raising=False)
        assert tm.strategy_is_live("longshot", "BTC") is False
        assert tm.strategy_is_live("above", "BTC") is False
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", True, raising=False)
        assert tm.strategy_is_live("longshot", "BTC") is True
        assert tm.strategy_is_live("above", "BTC") is False  # main UNCHANGED
        # M1 fix round (Bit T-1): override scoped to LONGSHOT_LIVE_ASSETS —
        # ADA/BCH excluded (Kalshi 15M series not yet listed + T1 shadow);
        # BNB INCLUDED per the 2026-06-12 operator directive (see the
        # LONGSHOT_LIVE_ASSETS constants comment).
        assert tm.strategy_is_live("longshot", "ADA") is False
        assert tm.strategy_is_live("longshot", "BCH") is False
        assert tm.strategy_is_live("longshot", "BNB") is True
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", True)
        monkeypatch.setattr(C, "ASSET_LIVE_TRADING", {"BTC": True})
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", False,
                            raising=False)
        assert tm.strategy_is_live("longshot", "BTC") is True
        assert tm.strategy_is_live("above", "BTC") is True

    def test_override_on_global_shadow_longshot_places_main_does_not(
            self, wired, enabled, monkeypatch):
        from unittest.mock import patch
        executor, eng, client = wired
        cands = _eval(eng)
        assert len(cands) == 1
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", True, raising=False)
        assert executor.execute(cands[0]) is not None
        client.place_order.assert_called_once()
        assert client.place_order.call_args.kwargs["post_only"] is True
        # main pipeline stays shadow under the same flags
        main_ex, main_client = _mocked_main_executor()
        with patch("bot.executor.OBSERVATION_MODE", False), \
             patch("bot.executor.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False,
                                              min_entry_price=86)
            assert main_ex.execute(_main_candidate()) is None
        main_client.place_order.assert_not_called()

    def test_override_off_global_shadow_nothing_places(self, wired, enabled,
                                                       monkeypatch):
        from unittest.mock import patch
        executor, eng, client = wired
        cands = _eval(eng)
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", False,
                            raising=False)
        assert executor.execute(cands[0]) is None
        client.place_order.assert_not_called()
        main_ex, main_client = _mocked_main_executor()
        with patch("bot.executor.OBSERVATION_MODE", False), \
             patch("bot.executor.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False,
                                              min_entry_price=86)
            assert main_ex.execute(_main_candidate()) is None
        main_client.place_order.assert_not_called()

    def test_both_live_both_place(self, wired, enabled):
        from unittest.mock import patch
        # integration conftest sets GLOBAL + all assets live
        executor, eng, client = wired
        cands = _eval(eng)
        assert executor.execute(cands[0]) is not None
        client.place_order.assert_called_once()
        main_ex, main_client = _mocked_main_executor()
        with patch("bot.executor.OBSERVATION_MODE", False), \
             patch("bot.executor.get_market_config") as mock_cfg:
            mock_cfg.return_value = MagicMock(observation_only=False,
                                              min_entry_price=86)
            main_ex.execute(_main_candidate())
        main_client.place_order.assert_called_once()

    def test_place_order_backstop_recognizes_ls_prefix(self, monkeypatch):
        from bot.kalshi_client import KalshiClient
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "ASSET_LIVE_TRADING", {"BTC": False})
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", True, raising=False)
        client = MagicMock()
        client._request.return_value = {"order": {"order_id": "ok"}}
        # ls- prefixed order passes the backstop under the override
        result = KalshiClient.place_order(
            client, TICKER, "no", "buy", 1, no_price=92,
            client_order_id="ls-abc123", post_only=True)
        client._request.assert_called_once()
        assert result == {"order": {"order_id": "ok"}}
        # non-prefixed (main pipeline) order is still blocked
        client._request.reset_mock()
        result = KalshiClient.place_order(
            client, TICKER, "yes", "buy", 1, yes_price=95,
            client_order_id="b2c3-main")
        assert result is None
        client._request.assert_not_called()

    def test_place_order_backstop_blocks_ls_prefix_without_override(
            self, monkeypatch):
        from bot.kalshi_client import KalshiClient
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "ASSET_LIVE_TRADING", {"BTC": False})
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", False,
                            raising=False)
        client = MagicMock()
        result = KalshiClient.place_order(
            client, TICKER, "no", "buy", 1, no_price=92,
            client_order_id="ls-abc123", post_only=True)
        assert result is None
        client._request.assert_not_called()
