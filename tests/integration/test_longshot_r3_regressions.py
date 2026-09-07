"""Bit L-1 adversarial-review R3 regressions (longshot premium-harvest).

One test class per R3 finding (M1 / M2 / MN1 / MN2). Fixtures mirror
tests/integration/test_longshot_r2_regressions.py (real sqlite3 file via
tmp_path per tests/CLAUDE.md integration-tier convention).

M1 — StateManager.reconcile_with_api ran BEFORE the engine's first tick
and (a) cancel-all swept ls- resting orders out from under the
LongshotEngine's boot-orphan machinery while flipping their local rows
off 'resting' (so engine boot step 2 could never find them), and
(b) imported unknown longshot positions with the DDL-default
strategy_group='main' — outside every longshot rail (caps, marks,
streaks) and triggering the R1-C2 main-conflict stopgap against the
strategy's own inventory.

M2 — boot-path fill application double-recorded pre-restart fills:
record_position_from_fill ACCUMULATES, seen_trade_ids is in-memory and
reborn empty at restart, and the R2-M1 created_time fill bound
re-fetches fills that were already recorded pre-restart (or imported
by _reconcile_positions).

MN1 — a partial fills snapshot (mid-pagination failure / page-cap
exhaustion) was treated as complete by boot step 2's TERMINAL-marking
decision.

MN2 — after LONGSHOT_CANCEL_FILL_MISMATCH kept an entry for a fill-poll
retry, a 404 on the NEXT cancel attempt popped the entry without any
successful re-poll having happened.
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
    s = StateManager(str(tmp_path / "test_longshot_r3.db"))
    yield s
    s.close()


@pytest.fixture
def client():
    cl = MagicMock()
    cl.get_fills.return_value = {"fills": []}
    cl.get_orders.return_value = {"orders": []}
    cl.get_positions.return_value = {"market_positions": []}
    cl.cancel_order.return_value = {"order": {"status": "canceled"}}
    cl.place_order.return_value = {"order": {"order_id": "oid-r3-1"}}
    cl.get_balance.return_value = {"balance": 50000}
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


# ── M1: startup reconciler must respect engine-owned ls- flow ────────────────

class TestM1ReconcilerRespectsLongshotFlow:
    """R3-M1: _reconcile_orders cancel-all swept ls- orders + flipped
    their local rows off 'resting' (neutralizing the engine's boot-orphan
    machinery), and _reconcile_positions imported unknown longshot
    positions as strategy_group='main' (outside all longshot rails)."""

    def test_reconcile_skips_ls_orders_and_engine_adopts(
            self, state, client, enabled):
        now = time.time()
        _seed_pending_resting(state, client_oid="ls-r3a", order_id="oid-ls",
                              created_epoch=now - 300)
        _seed_pending_resting(state, client_oid="mk-r3a",
                              order_id="oid-main", ticker=TICKER2,
                              event=EVENT2, side="yes", price=95)
        client.get_orders.return_value = {"orders": [
            {"order_id": "oid-ls", "client_order_id": "ls-r3a",
             "ticker": TICKER, "side": "no", "action": "buy",
             "no_price": 92, "count": 3, "remaining_count": 3,
             "status": "resting", "created_time": _rfc3339(now - 300)},
            {"order_id": "oid-main", "client_order_id": "mk-r3a",
             "ticker": TICKER2, "side": "yes", "action": "buy",
             "yes_price": 95, "count": 1, "remaining_count": 1,
             "status": "resting", "created_time": _rfc3339(now - 300)},
        ]}

        state.reconcile_with_api(client)

        # Main resting order: canceled on Kalshi + local row flipped.
        canceled_ids = [c.args[0] for c in client.cancel_order.call_args_list]
        assert canceled_ids == ["oid-main"], (
            "reconcile must cancel ONLY non-longshot resting orders — the "
            "LongshotEngine owns the ls- lifecycle (R3-M1)")
        assert _pending_status(state, "oid-main") == "canceled"
        # ls- order: untouched on Kalshi, local row still 'resting' so the
        # engine boot step can find it.
        assert _pending_status(state, "oid-ls") == "resting", (
            "reconcile must not flip ls- rows off 'resting' — engine boot "
            "step 2 finds them via status='resting' (R3-M1)")

        # Engine boot step then adopts the still-resting ls- orphan.
        client.cancel_order.reset_mock()
        engine = LongshotEngine(client, state)
        engine.tick()
        adopted_cancels = [c.args[0]
                           for c in client.cancel_order.call_args_list]
        assert "oid-ls" in adopted_cancels, (
            "engine boot step must adopt + cancel the ls- orphan that the "
            "reconciler left alone (R3-M1)")
        assert _pending_status(state, "oid-ls") == "canceled"

    def test_reconcile_does_not_import_canceled_history_for_ls_order(
            self, state, client, enabled):
        """An API ls- resting order with NO local row must not get a
        synthetic 'canceled' history INSERT (the engine adopts from the
        API list directly)."""
        now = time.time()
        client.get_orders.return_value = {"orders": [
            {"order_id": "oid-ls-nolocal", "client_order_id": "ls-r3b",
             "ticker": TICKER, "side": "no", "action": "buy",
             "no_price": 92, "count": 3, "remaining_count": 3,
             "status": "resting", "created_time": _rfc3339(now - 60)},
        ]}
        state.reconcile_with_api(client)
        client.cancel_order.assert_not_called()
        row = state.conn.execute(
            "SELECT 1 FROM pending_orders WHERE order_id='oid-ls-nolocal'"
        ).fetchone()
        assert row is None, (
            "reconcile must not INSERT a 'canceled' history row for an "
            "ls- order it did not cancel (R3-M1)")

    def test_unknown_position_with_ls_history_imported_as_longshot(
            self, state, client, enabled):
        now = time.time()
        # ls- pending history on this ticker (any status — here 'filled').
        _seed_pending_resting(state, client_oid="ls-r3c", order_id="oid-lsf",
                              side="yes", count=2, price=10,
                              created_epoch=now - 300)
        state.mark_order_status("oid-lsf", "filled")
        client.get_positions.return_value = {"market_positions": [
            {"ticker": TICKER, "position": 2, "market_exposure": 20},
        ]}
        state.reconcile_with_api(client)

        row = _positions_row(state)
        assert row is not None
        assert row["strategy_group"] == "longshot", (
            "unknown position on a ticker with ls- pending history must be "
            "imported with strategy_group='longshot' — DDL-default 'main' "
            "leaks it outside all longshot rails (R3-M1)")
        assert row["strategy"] == "longshot"
        assert row["side"] == "yes"
        assert row["count"] == 2

        # Visible to the longshot rails:
        engine = LongshotEngine(client, state)
        # _allowed_size: 2 open longshot contracts on (ticker, yes) leave
        # cap 3 - 2 = 1 (and the row must NOT trip the R1-C2 main-conflict
        # stopgap, which would return 0).
        assert engine._allowed_size(TICKER, "no", "yes", 10) == 1
        # _marked_open_loss_cents: bought YES -> sold NO, ITM when spot is
        # below strike.
        engine._mark_inputs[TICKER] = (100.0, 104.0, time.time())
        assert engine._marked_open_loss_cents() == row["total_cost_cents"]

    def test_unknown_position_without_ls_history_stays_main(
            self, state, client, enabled):
        client.get_positions.return_value = {"market_positions": [
            {"ticker": TICKER, "position": 2, "market_exposure": 20},
        ]}
        state.reconcile_with_api(client)
        row = _positions_row(state)
        assert row is not None
        assert row["strategy_group"] == "main", (
            "non-longshot reconcile import behavior must stay unchanged")


# ── M2: boot path must not double-record pre-restart fills ───────────────────

class TestM2BootFillDeltaApply:
    """R3-M2: boot fill application is RECONCILE-AWARE — existing open
    longshot rows already embody recorded/imported truth, so only
    max(0, fetched - existing) contracts are applied (oldest first)."""

    def test_pre_restart_recorded_fill_not_double_counted(
            self, engine, state, client, enabled):
        now = time.time()
        # Pre-restart: the fill was recorded before the process died.
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "no", 2, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        _seed_pending_resting(state, client_oid="ls-r3d", order_id="oid-d",
                              count=2, created_epoch=now - 600)
        # R4-M2: production recording goes through _apply_fills, which
        # bumps the order's per-order recorded_fill_count — mirror that
        # here since this test records directly via the StateManager.
        state.conn.execute(
            "UPDATE pending_orders SET recorded_fill_count=2 "
            "WHERE order_id='oid-d'")
        state.conn.commit()
        client.get_fills.side_effect = _min_ts_respecting_fills([
            {"order_id": "oid-d", "trade_id": "t-d1", "count": 2,
             "ts": now - 300, "created_time": _rfc3339(now - 300)},
        ])
        engine.tick()
        row = _positions_row(state)
        assert row["count"] == 2, (
            "boot refetch of a pre-restart-recorded fill must not "
            "double-count: record_position_from_fill accumulates and "
            "seen_trade_ids is reborn empty (R3-M2)")
        assert _pending_status(state, "oid-d") == "filled"

    def test_reconcile_imported_position_boot_increment_zero(
            self, state, client, enabled):
        now = time.time()
        _seed_pending_resting(state, client_oid="ls-r3e", order_id="oid-e",
                              side="yes", count=2, price=10,
                              created_epoch=now - 600)
        client.get_positions.return_value = {"market_positions": [
            {"ticker": TICKER, "position": 2, "market_exposure": 20},
        ]}
        state.reconcile_with_api(client)  # imports as longshot (R3-M1)
        client.get_fills.side_effect = _min_ts_respecting_fills([
            {"order_id": "oid-e", "trade_id": "t-e1", "count": 2,
             "ts": now - 300, "created_time": _rfc3339(now - 300)},
        ])
        engine = LongshotEngine(client, state)
        engine.tick()
        row = _positions_row(state)
        assert row["count"] == 2, (
            "_reconcile_positions already imported this position — the "
            "boot fill increment must be zero (R3-M2)")
        assert _pending_status(state, "oid-e") == "filled"

    def test_genuinely_unrecorded_fill_recorded_once(
            self, engine, state, client, enabled):
        now = time.time()
        _seed_pending_resting(state, client_oid="ls-r3f", order_id="oid-f",
                              count=2, created_epoch=now - 600)
        client.get_fills.side_effect = _min_ts_respecting_fills([
            {"order_id": "oid-f", "trade_id": "t-f1", "count": 2,
             "ts": now - 300, "created_time": _rfc3339(now - 300)},
        ])
        engine.tick()
        row = _positions_row(state)
        assert row is not None, "genuinely unrecorded fill must be recorded"
        assert row["count"] == 2
        assert _pending_status(state, "oid-f") == "filled"

    def test_partial_overlap_applies_delta_oldest_first(
            self, engine, state, client, enabled):
        now = time.time()
        # 1 of 3 contracts was recorded pre-restart.
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "no", 1, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        _seed_pending_resting(state, client_oid="ls-r3g", order_id="oid-g",
                              count=3, created_epoch=now - 600)
        # R4-M2: mirror the _apply_fills counter bump (see comment in
        # test_pre_restart_recorded_fill_not_double_counted).
        state.conn.execute(
            "UPDATE pending_orders SET recorded_fill_count=1 "
            "WHERE order_id='oid-g'")
        state.conn.commit()
        client.get_fills.side_effect = _min_ts_respecting_fills([
            {"order_id": "oid-g", "trade_id": "t-g2", "count": 2,
             "ts": now - 200, "created_time": _rfc3339(now - 200)},
            {"order_id": "oid-g", "trade_id": "t-g1", "count": 1,
             "ts": now - 400, "created_time": _rfc3339(now - 400)},
        ])
        engine.tick()
        row = _positions_row(state)
        assert row["count"] == 3, (
            "delta-apply: existing 1 + fetched 3 must land on 3, skipping "
            "the OLDEST 1 contract (R3-M2)")
        assert row["total_cost_cents"] == 3 * 92
        assert _pending_status(state, "oid-g") == "filled"


# ── MN1: partial fills snapshot is not complete ──────────────────────────────

class TestMN1PartialSnapshotNotTerminal:
    """R3-MN1: a mid-pagination failure must record page 1's fills but
    count as a fetch FAILURE for terminal-marking decisions (row stays
    'resting', boot latch unset, retried next tick)."""

    def test_page2_failure_records_page1_but_retries(
            self, engine, state, client, enabled):
        now = time.time()
        _seed_pending_resting(state, client_oid="ls-r3h", order_id="oid-h",
                              count=3, created_epoch=now - 600)
        fill_p1 = {"order_id": "oid-h", "trade_id": "t-h1", "count": 2,
                   "ts": now - 400, "created_time": _rfc3339(now - 400)}
        fill_p2 = {"order_id": "oid-h", "trade_id": "t-h2", "count": 1,
                   "ts": now - 200, "created_time": _rfc3339(now - 200)}
        # Page 1 succeeds with a cursor; page 2 fails.
        client.get_fills.side_effect = [
            {"fills": [fill_p1], "cursor": "c1"},
            None,
        ]
        engine.tick()
        row = _positions_row(state)
        assert row is not None and row["count"] == 2, (
            "page-1 fills must still be recorded on a partial snapshot")
        assert _pending_status(state, "oid-h") == "resting", (
            "a PARTIAL snapshot must not terminally mark the row — page 2 "
            "could hold the missing fills (R3-MN1)")
        assert engine._boot_reconciled is False, (
            "boot latch must stay unset so the next tick retries")

        # Next tick: complete snapshot -> delta-applied + terminal-marked.
        client.get_fills.side_effect = None
        client.get_fills.return_value = {"fills": [fill_p1, fill_p2]}
        engine.tick()
        row = _positions_row(state)
        assert row["count"] == 3, (
            "retry must apply only the missing contract (delta-apply)")
        assert _pending_status(state, "oid-h") == "filled"
        assert engine._boot_reconciled is True

    def test_page_cap_exhaustion_counts_as_partial(self, engine, client):
        from bot.longshot import _MAX_FILL_PAGES
        client.get_fills.return_value = {"fills": [], "cursor": "more"}
        fills, complete = engine._fetch_fills_snapshot(min_ts=time.time())
        assert fills == []
        assert complete is False, (
            "cursor still present after the page cap means the snapshot "
            "is incomplete (R3-MN1)")
        assert client.get_fills.call_count == _MAX_FILL_PAGES


# ── MN2: 404-after-mismatch needs a clean re-poll first ──────────────────────

class TestMN2CleanPollBefore404Pop:
    """R3-MN2: when a cancel kept the entry via
    LONGSHOT_CANCEL_FILL_MISMATCH, a subsequent 404-path terminal pop
    must wait for one clean (complete) fills poll on a LATER tick."""

    def _register(self, engine, order_id="oid-mn2"):
        engine.register_resting(
            order_id=order_id, client_order_id=f"ls-{order_id}",
            ticker=TICKER, event_ticker=EVENT, asset="BTC",
            sell_side="yes", buy_side="no", buy_price_cents=92,
            count=3, seconds_to_close=600.0)

    def test_404_after_mismatch_waits_for_clean_poll(
            self, engine, state, client, enabled):
        engine._boot_reconciled = True
        _seed_pending_resting(state, client_oid="ls-oid-mn2",
                              order_id="oid-mn2", count=3)
        self._register(engine)

        # Cancel response says 2 filled; fills API still shows nothing ->
        # mismatch keeps the entry for a re-poll.
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.return_value = {
            "order": {"reduced_by": 1, "reduced_by_fp": "1.00"}}
        engine._cancel_quote("oid-mn2", "t_minus_3min")
        assert "oid-mn2" in engine._resting

        # Retry cancel now 404s (order gone on Kalshi) and the fills API
        # is STILL not showing the fills — the entry must NOT pop yet.
        client.cancel_order.return_value = {"_error": True,
                                            "_status_code": 404}
        engine._cancel_quote("oid-mn2", "t_minus_3min")
        assert "oid-mn2" in engine._resting, (
            "404-path pop without any successful re-poll loses the "
            "mismatched fills forever (R3-MN2)")
        assert _positions_row(state) is None

        # Next tick: the bulk poll is clean (complete) and carries the
        # fills -> recorded; the NEXT 404 cancel attempt may now pop.
        client.get_fills.return_value = {"fills": [
            {"order_id": "oid-mn2", "trade_id": "t-mn2", "count": 2},
        ]}
        engine.tick()
        row = _positions_row(state)
        assert row is not None and row["count"] == 2, (
            "the mismatched fills must be recorded by the clean re-poll")
        engine._cancel_quote("oid-mn2", "t_minus_3min")
        assert "oid-mn2" not in engine._resting, (
            "after a clean re-poll on a subsequent tick the 404 path "
            "pops normally")

    def test_retry_cancel_reduced_by_zero_holds_until_local_filled_catches_up(
            self, engine, state, client, enabled):
        """Persist count-reduced_by from the first cancel. A retry with
        reduced_by=0 must not pop while local filled lags that number
        (tick() clears needs_clean_poll in the same tick as re-cancel).
        """
        engine._boot_reconciled = True
        _seed_pending_resting(state, client_oid="ls-oid-z",
                              order_id="oid-z", count=3)
        self._register(engine, order_id="oid-z")
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.return_value = {
            "order": {"reduced_by": 1, "reduced_by_fp": "1.00"}}
        engine._cancel_quote("oid-z", "t_minus_3min")
        assert "oid-z" in engine._resting
        with engine._lock:
            engine._resting["oid-z"]["filled"] = 1
            engine._resting["oid-z"].pop("needs_clean_poll", None)
        client.cancel_order.return_value = {
            "order": {"reduced_by": 0, "reduced_by_fp": "0.00"}}
        engine._cancel_quote("oid-z", "t_minus_3min")
        assert "oid-z" in engine._resting, (
            "reduced_by=0 retry must hold while filled < first-cancel "
            "api_filled (same-tick needs_clean_poll clear is not enough)")
        with engine._lock:
            engine._resting["oid-z"]["filled"] = 2
        engine._cancel_quote("oid-z", "t_minus_3min")
        assert "oid-z" not in engine._resting, (
            "pop only after local filled catches Kalshi's first-cancel "
            "fill count")

    def test_first_cancel_reduced_by_zero_still_holds_for_fill_lag(
            self, engine, state, client, enabled, caplog):
        """reduced_by=0 on the FIRST cancel is a full fill with
        nothing left to cancel — still hold for fills-API lag.
        """
        engine._boot_reconciled = True
        _seed_pending_resting(state, client_oid="ls-oid-full",
                              order_id="oid-full", count=3)
        self._register(engine, order_id="oid-full")
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.return_value = {
            "order": {"reduced_by": 0, "reduced_by_fp": "0.00"}}
        with caplog.at_level("WARNING"):
            engine._cancel_quote("oid-full", "t_minus_3min")
        assert "oid-full" in engine._resting
        assert "LONGSHOT_CANCEL_FILL_MISMATCH" in caplog.text

    def test_first_cancel_reduced_by_zero_with_partial_fills_still_holds(
            self, engine, state, client, enabled, caplog):
        """First DELETE reduced_by=0 with filled>0 is full-fill +
        fills-API lag, not 'already gone'. Must keep the entry.
        """
        engine._boot_reconciled = True
        _seed_pending_resting(state, client_oid="ls-oid-p",
                              order_id="oid-p", count=3)
        self._register(engine, order_id="oid-p")
        with engine._lock:
            engine._resting["oid-p"]["filled"] = 1
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.return_value = {
            "order": {"reduced_by": 0, "reduced_by_fp": "0.00"}}
        with caplog.at_level("WARNING"):
            engine._cancel_quote("oid-p", "t_minus_3min")
        assert "oid-p" in engine._resting, (
            "first cancel reduced_by=0 with a partial local fill must "
            "hold for the lagged fills, not pop")
        assert "LONGSHOT_CANCEL_FILL_MISMATCH" in caplog.text

    def test_first_cancel_404_with_failed_fills_poll_holds(
            self, engine, state, client, enabled, caplog):
        """A first-attempt 404 with a failed fills poll must not pop —
        no _cancel_api_filled yet, and the poll is the only fill net.
        """
        engine._boot_reconciled = True
        _seed_pending_resting(state, client_oid="ls-oid-pf",
                              order_id="oid-pf", count=3)
        self._register(engine, order_id="oid-pf")
        client.get_fills.return_value = None
        client.cancel_order.return_value = {
            "_error": True, "_status_code": 404}
        with caplog.at_level("WARNING"):
            engine._cancel_quote("oid-pf", "t_minus_3min")
        assert "oid-pf" in engine._resting, (
            "404 + failed fills poll must keep the entry (R1-C1)")
        assert "LONGSHOT_CANCEL_POLL_DEFERRED" in caplog.text

    def test_partial_poll_does_not_clear_mismatch_hold(
            self, engine, state, client, enabled):
        engine._boot_reconciled = True
        _seed_pending_resting(state, client_oid="ls-oid-mn2b",
                              order_id="oid-mn2b", count=3)
        self._register(engine, order_id="oid-mn2b")
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.return_value = {
            "order": {"reduced_by": 1, "reduced_by_fp": "1.00"}}
        engine._cancel_quote("oid-mn2b", "t_minus_3min")
        assert "oid-mn2b" in engine._resting

        # Tick whose bulk poll is PARTIAL (page-2 failure every time) —
        # must NOT count as the clean poll; the 404 pop stays deferred.
        client.get_fills.side_effect = lambda **kw: (
            {"fills": [], "cursor": "c1"} if kw.get("cursor") is None
            else None)
        engine.tick()
        client.cancel_order.return_value = {"_error": True,
                                            "_status_code": 404}
        engine._cancel_quote("oid-mn2b", "t_minus_3min")
        assert "oid-mn2b" in engine._resting, (
            "a PARTIAL bulk poll must not satisfy the clean-poll "
            "requirement (R3-MN2)")
