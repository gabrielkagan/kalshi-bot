"""Bit L-1 adversarial-review R7 regression (longshot premium-harvest).

R7-M1: boot step-1 adoption read only the DEPRECATED integer price fields
(`yes_price`/`no_price`) off the GET /orders response. Post-FP-transition
order objects carry `*_price_dollars` (the deprecated fields read as None —
see kb/failures/ppo-monitor-bugs.md, Feb 26 FP transition), so an adopted
orphan registered `buy_price_cents=0` and any fill recovered through the
boot path was recorded with a ZERO cost basis: settlement would book a 96c
loss as 0 PnL and the daily-loss/streak rails went blind to exactly the
fills the boot machinery exists to capture. Adjacent micro-edge folded in:
the R5-MN2 count derive read bare `o.get("count")` with no `count_fp` twin.

Observation surface: boot adoption immediately final-polls and cancels the
orphan (the entry is popped before tick() returns), so these tests spy on
`register_resting` kwargs (wrapping the REAL method) and on the recorded
positions row — not on post-tick registry state.

Fixtures mirror test_longshot_r1_regressions.py (real sqlite3 file per
tests/CLAUDE.md integration-tier convention).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import bot.constants as C
from bot.state import StateManager

from bot.longshot import LongshotEngine

TICKER = "KXBTC15M-26JUN111200-T110"


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "test_longshot_r7.db"))
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


@pytest.fixture
def reg_spy(engine, monkeypatch):
    """Capture register_resting kwargs while still running the real method."""
    calls = []
    real = engine.register_resting

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(engine, "register_resting", spy)
    return calls


class TestR7M1FpPriceFieldsAtBootAdoption:
    """R7-M1: adopted orphans must extract price dollars-first (FP shape)."""

    def _orders_fp_only(self):
        # Post-FP-transition shape: ONLY *_price_dollars + *_count_fp.
        # No legacy integer price/count fields at all.
        return {"orders": [
            {"order_id": "oid-fp", "client_order_id": "ls-fp-1",
             "ticker": TICKER, "side": "no",
             "no_price_dollars": "0.9200", "yes_price_dollars": "0.0800",
             "remaining_count_fp": "3", "status": "resting"},
        ]}

    def test_fp_only_order_adopts_nonzero_price(self, engine, state, enabled,
                                                reg_spy):
        engine._client.get_orders.return_value = self._orders_fp_only()
        engine.tick()
        assert reg_spy, "FP-shaped orphan not adopted"
        assert reg_spy[0]["buy_price_cents"] == 92, (
            f"adopted buy_price_cents={reg_spy[0]['buy_price_cents']} — "
            "deprecated integer field read None -> 0 cost basis")

    def test_fp_only_fill_records_nonzero_cost_basis(self, engine, state,
                                                     enabled):
        engine._client.get_orders.return_value = self._orders_fp_only()
        engine._client.get_fills.return_value = {
            "fills": [{"order_id": "oid-fp", "trade_id": "t-fp-1",
                       "count_fp": "1"}]}
        engine.tick()
        row = state.conn.execute(
            "SELECT count, avg_price_cents, total_cost_cents "
            "FROM positions WHERE ticker=? AND strategy_group='longshot' "
            "AND status='open'", (TICKER,)).fetchone()
        assert row is not None, "FP-shaped orphan fill not recorded"
        assert row["avg_price_cents"] == 92
        assert row["total_cost_cents"] == 92

    def test_legacy_fields_still_work(self, engine, state, enabled, reg_spy):
        engine._client.get_orders.return_value = {"orders": [
            {"order_id": "oid-legacy", "client_order_id": "ls-leg-1",
             "ticker": TICKER, "side": "no", "no_price": 92, "count": 3,
             "status": "resting"},
        ]}
        engine.tick()
        assert reg_spy
        assert reg_spy[0]["buy_price_cents"] == 92

    def test_fp_count_derive_fallback(self, engine, state, enabled, reg_spy):
        # R5-MN2 derive path: remaining fields ABSENT, count present only
        # as count_fp — the bare o.get("count") read must not zero it.
        engine._client.get_orders.return_value = {"orders": [
            {"order_id": "oid-cfp", "client_order_id": "ls-cfp-1",
             "ticker": TICKER, "side": "no",
             "no_price_dollars": "0.9200", "count_fp": "3",
             "status": "resting"},
        ]}
        engine.tick()
        assert reg_spy
        assert reg_spy[0]["count"] >= 3, (
            f"count={reg_spy[0]['count']} — bare o.get('count') zeroed "
            "the R5-MN2 derive")

    def test_legacy_cents_unparseable_uses_zero_without_dollars_fallback_log(
            self, engine, state, enabled, reg_spy, caplog):
        """When *_price_dollars is absent and the integer cents field is
        itself unparseable, do not re-run the same int() and do not log
        'falling back to integer cents' with dollars=None.
        """
        import logging
        caplog.set_level(logging.WARNING)
        engine._client.get_orders.return_value = {"orders": [
            {"order_id": "oid-badcents", "client_order_id": "ls-badc-1",
             "ticker": TICKER, "side": "no", "no_price": "garbage",
             "remaining_count": 1, "status": "resting"},
        ]}
        engine.tick()
        assert reg_spy, "orphan must still be adopted"
        assert reg_spy[0]["buy_price_cents"] == 0
        assert "falling back to integer cents" not in caplog.text
        assert "legacy" in caplog.text.lower() or "unparseable" in caplog.text.lower()
