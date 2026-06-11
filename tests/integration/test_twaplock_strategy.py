"""TWAP-lock endgame taker strategy — engine behavior (Bit T-1).

TDD-first scaffold (written RED before bot/twaplock.py exists). Covers, per
kb/decisions/longshot-twap-live-small-plan.md + the Bit T-1 spec
(validated via scripts/research/genhunt/01b_twap_lock_validation.py:
+14.4c/ct, CI [+11.1, +17.7], n=359/12d, print cross-check 99.2%):

- TWAPLOCK_* constants exist in bot/constants.py (default
  TWAPLOCK_ENABLED=False; live threshold 0.99 — STRICTER than the
  validated 0.95 because the Coinbase-anchored MVP index adds proxy error
  vs the honest 4-venue index; undercounting costs frequency, not
  correctness).
- p_lock math (compute_p_lock): accrued-TWAP + remaining-variance normal
  model; zero/missing vol -> no signal (None, None) — NEVER 0 or 0.5;
  in-window variance < pre-window variance; missing accrued mean inside
  the TWAP window -> no signal.
- Accrued ring-buffer math: time-weighted (step-hold) mean over the
  elapsed portion of the final-60s TWAP window; requires a sample at or
  before window start.
- Entry window edges: only TWAPLOCK_MIN_SUBMIT_STC_SECONDS <= stc <=
  TWAPLOCK_ENTRY_WINDOW_SECONDS (final 120s).
- Fee + margin gate: locked-side executable ask must satisfy
  ask <= 100 - taker_fee(1ct, ask) - TWAPLOCK_MIN_EDGE_CENTS.
- One entry per window per asset (TWAPLOCK_MAX_ENTRIES_PER_WINDOW=1):
  in-memory one-shot latch + DB-derived (tw- pending_orders rows) so the
  latch survives a restart.
- Cross-strategy ticker exclusion: ANY open position (main, longshot,
  twaplock) or ANY pending/resting order row on the ticker blocks entry
  (positions PK is still single-ticker until 86badbf9t).
- COMBINED daily loss cap (bot/strategy_caps.py): realized+marked summed
  across strategy IN ('longshot','twaplock') vs
  LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS=20 — BOTH engines latch; each
  strategy alone under the cap trips NEITHER.
- Combined consecutive-losing-days disable with
  LIVE_SMALL_STREAK_RESET_UTC_DATE operator reset.
- evaluated_opportunities rows: filter_stage 'twaplock_live' /
  'twaplock_shadow' (labeling consults bot.trading_mode READ-ONLY; the
  GATE stays at executor.execute()).
- Executor chokepoint truth table (ENABLED x GLOBAL x ASSET x OVERRIDE)
  through the REAL OrderExecutor.execute(); taker IOC placement with
  'tw-' client_oid prefix; FP-primary fill_count_fp read from day 1
  (the L-1 R7 lesson pre-learned).
- tw- reconciler carve-outs in bot/state.py generalized to a prefix
  tuple (ls-, tw-): _reconcile_orders cancel/flip sweeps,
  cleanup_expired_resting_orders, RECONCILE_IMPORT strategy stamping.
- Engine boot sweep: stranded tw- pending/resting rows flipped to
  'canceled' on first tick (positions-API reconcile owns the money side;
  IOC orders never rest so there is no orphan-quote lifecycle).

Real sqlite3 file via tmp_path per tests/CLAUDE.md integration-tier
convention. The integration-tier autouse fixture sets
GLOBAL_LIVE_TRADING=True, so the default label here is 'twaplock_live';
shadow-label tests monkeypatch bot.constants directly (same pattern as
test_longshot_strategy.py / test_trading_mode_backstop.py).
"""
from __future__ import annotations

import datetime
from datetime import timezone
from unittest.mock import MagicMock

import pytest

import bot.constants as C
import bot.trading_mode as tm
from bot.state import StateManager

import bot.twaplock as twaplock_mod
from bot.twaplock import TwaplockEngine, compute_p_lock
from bot import strategy_caps


TICKER = "KXBTC15M-26JUN111200-T110"
EVENT = "KXBTC15M-26JUN111200"
TICKER2 = "KXETH15M-26JUN111200-T3500"
EVENT2 = "KXETH15M-26JUN111200"


# ── fixtures / helpers ───────────────────────────────────────────────────────

@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "test_twaplock.db"))
    yield s
    s.close()


@pytest.fixture
def engine(state):
    client = MagicMock()
    return TwaplockEngine(client, state)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(C, "TWAPLOCK_ENABLED", True, raising=False)


def _ob(yes_ask_cents=95, yes_bid_cents=90):
    """Kalshi book: 'yes' = YES bids, 'no' = NO bids.
    YES ask = 100 - best NO bid; NO ask = 100 - best YES bid."""
    return {
        "no": [[100 - yes_ask_cents, 50]],
        "yes": [[yes_bid_cents, 50]],
    }


def _eval(eng, *, yes_ask_cents=95, yes_bid_cents=90, stc=90.0, spot=110.0,
          threshold=100.0, blended_rv=0.00001, ticker=TICKER, ob=None,
          now=None, asset="BTC", event=EVENT):
    """Default geometry: spot WAY above strike with tiny vol at stc=90s
    (pre-TWAP-window branch — no accrued buffer needed) -> p_lock ~ 1.0
    -> locked side YES at executable ask 95c (fee 1c; 95 <= 100-1-3)."""
    if ob is None:
        ob = _ob(yes_ask_cents, yes_bid_cents)
    return eng.evaluate_market(
        ticker=ticker, event_ticker=event, asset=asset, product_type="15m",
        spot=spot, threshold=threshold, seconds_to_close=stc,
        blended_rv=blended_rv, orderbook_fetch=lambda: ob,
        config_snapshot_id=None, balance_at_scan=50000, now=now)


def _seed_settled(state, ticker, pnl_cents, settled_date, strategy="twaplock"):
    state.conn.execute(
        "INSERT INTO settled_trades (ticker, event_ticker, asset, market_result,"
        " side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents,"
        " settled_at, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ticker, EVENT, "BTC", "yes", "yes", 2, 95, 0, 0, pnl_cents,
         f"{settled_date}T12:00:00.000000Z", strategy))
    state.conn.commit()


def _today_utc():
    return datetime.datetime.now(timezone.utc).date()


# ── constants ────────────────────────────────────────────────────────────────

class TestTwaplockConstants:
    def test_enabled_defaults_off(self):
        assert C.TWAPLOCK_ENABLED is False

    def test_p_lock_threshold_stricter_than_validated(self):
        # Validation used 0.95 on the honest 4-venue index; the
        # Coinbase-anchored live MVP uses 0.99 (degraded-index lesson).
        assert C.TWAPLOCK_P_LOCK_THRESHOLD == 0.99

    def test_sizing_rails(self):
        assert C.TWAPLOCK_MAX_CONTRACTS_PER_ENTRY == 2
        assert C.TWAPLOCK_MAX_ENTRIES_PER_WINDOW == 1
        assert C.TWAPLOCK_MIN_EDGE_CENTS == 3

    def test_entry_window(self):
        assert C.TWAPLOCK_ENTRY_WINDOW_SECONDS == 120.0
        assert C.TWAPLOCK_TWAP_WINDOW_SECONDS == 60.0

    def test_live_override_defaults_off(self):
        assert C.TWAPLOCK_LIVE_OVERRIDE is False

    def test_client_oid_prefix(self):
        assert C.TWAPLOCK_CLIENT_OID_PREFIX == "tw-"

    def test_combined_live_small_rails(self):
        assert C.LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS == 20.0
        assert C.LIVE_SMALL_CONSECUTIVE_LOSING_DAYS_DISABLE == 3
        assert C.LIVE_SMALL_STREAK_RESET_UTC_DATE == ""

    def test_engine_owned_prefix_tuple(self):
        assert C.ENGINE_OWNED_OID_PREFIX_TO_STRATEGY == {
            "ls-": "longshot", "tw-": "twaplock"}
        assert set(C.ENGINE_OWNED_CLIENT_OID_PREFIXES) == {"ls-", "tw-"}


# ── p_lock math ──────────────────────────────────────────────────────────────

class TestPLockMath:
    def test_deep_itm_pre_window_locks(self):
        # stc=90s > 60s TWAP window: accrued not yet relevant.
        p_yes, z = compute_p_lock(110.0, 100.0, 90.0, 0.00001)
        assert p_yes is not None
        assert p_yes > 0.999
        assert z > 3.0

    def test_deep_otm_pre_window_locks_no(self):
        p_yes, _ = compute_p_lock(90.0, 100.0, 90.0, 0.00001)
        assert p_yes is not None
        assert p_yes < 0.001

    def test_zero_vol_is_no_signal(self):
        assert compute_p_lock(110.0, 100.0, 90.0, 0.0) == (None, None)
        assert compute_p_lock(110.0, 100.0, 90.0, None) == (None, None)

    def test_invalid_inputs_no_signal(self):
        assert compute_p_lock(0.0, 100.0, 90.0, 0.0001) == (None, None)
        assert compute_p_lock(110.0, 0.0, 90.0, 0.0001) == (None, None)
        assert compute_p_lock(110.0, 100.0, 0.0, 0.0001) == (None, None)
        assert compute_p_lock(110.0, 100.0, None, 0.0001) == (None, None)

    def test_in_window_missing_accrued_is_no_signal(self):
        # stc=30s < 60s window but no accrued mean available (e.g. restart
        # mid-window): conservative no-signal, never assume A=spot.
        assert compute_p_lock(110.0, 100.0, 30.0, 0.0001,
                              accrued_mean=None) == (None, None)

    def test_in_window_uses_accrued_mean(self):
        # Accrued half the window ABOVE strike + spot above strike -> lock
        # YES even with moderate vol; accrued BELOW strike with the same
        # spot pulls p down.
        p_hi, _ = compute_p_lock(101.0, 100.0, 30.0, 0.00001,
                                 accrued_mean=105.0)
        p_lo, _ = compute_p_lock(101.0, 100.0, 30.0, 0.00001,
                                 accrued_mean=95.0)
        assert p_hi is not None and p_lo is not None
        assert p_hi > 0.999
        assert p_lo < 0.001

    def test_in_window_variance_below_pre_window_variance(self):
        # Same spot/strike/vol: deeper into the TWAP window (with accrued
        # at spot) the remaining variance shrinks -> p closer to 1.
        p_pre, _ = compute_p_lock(100.5, 100.0, 90.0, 0.0005)
        p_in, _ = compute_p_lock(100.5, 100.0, 20.0, 0.0005,
                                 accrued_mean=100.5)
        assert p_pre is not None and p_in is not None
        assert p_in > p_pre


# ── accrued ring buffer ──────────────────────────────────────────────────────

class TestAccruedBuffer:
    def test_time_weighted_mean(self, engine):
        engine.record_spot("BTC", 100.0, ts=1000.0)
        engine.record_spot("BTC", 110.0, ts=1010.0)
        # window [1000, 1020]: 10s at 100 + 10s at 110 -> 105
        assert engine._accrued_mean("BTC", 1000.0, 1020.0) == \
            pytest.approx(105.0)

    def test_sample_before_window_start_holds(self, engine):
        engine.record_spot("BTC", 100.0, ts=990.0)
        engine.record_spot("BTC", 102.0, ts=1010.0)
        # window [1000, 1020]: 10s held at 100 (sample from 990) + 10s at 102
        assert engine._accrued_mean("BTC", 1000.0, 1020.0) == \
            pytest.approx(101.0)

    def test_no_sample_at_or_before_window_start_is_none(self, engine):
        engine.record_spot("BTC", 100.0, ts=1005.0)
        assert engine._accrued_mean("BTC", 1000.0, 1020.0) is None

    def test_empty_buffer_is_none(self, engine):
        assert engine._accrued_mean("BTC", 1000.0, 1020.0) is None


# ── entry window + condition logic ───────────────────────────────────────────

class TestConditionLogic:
    def test_disabled_no_candidates_no_rows(self, engine, state):
        # TWAPLOCK_ENABLED is False by default (shipped config)
        assert _eval(engine) == []
        rows = state.conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities").fetchone()[0]
        assert rows == 0

    def test_locked_yes_candidate_emitted(self, engine, enabled):
        cands = _eval(engine)
        assert len(cands) == 1
        c = cands[0]
        assert c["strategy"] == "twaplock"
        assert c["side"] == "yes"
        assert c["twaplock_ask_cents"] == 95
        assert c["best_yes_ask"] == 95  # cents-at-risk per contract (taker)
        assert c["position_size"] == 2
        assert c["calibrated_prob"] >= C.TWAPLOCK_P_LOCK_THRESHOLD
        assert c["twaplock_p_lock"] >= C.TWAPLOCK_P_LOCK_THRESHOLD

    def test_locked_no_candidate_emitted(self, engine, enabled):
        # spot far BELOW strike -> NO locks; NO ask = 100 - yes_bid = 95c
        cands = _eval(engine, spot=90.0, threshold=100.0,
                      yes_ask_cents=10, yes_bid_cents=5)
        assert len(cands) == 1
        c = cands[0]
        assert c["side"] == "no"
        assert c["twaplock_ask_cents"] == 95

    def test_unlocked_emits_nothing(self, engine, enabled, monkeypatch):
        monkeypatch.setattr(twaplock_mod, "compute_p_lock",
                            lambda *a, **k: (0.90, 1.5))
        assert _eval(engine) == []

    def test_p_lock_threshold_boundary(self, engine, enabled, monkeypatch):
        # exactly AT the threshold qualifies; just below does not.
        monkeypatch.setattr(twaplock_mod, "compute_p_lock",
                            lambda *a, **k: (0.99, 3.0))
        assert len(_eval(engine)) == 1
        monkeypatch.setattr(twaplock_mod, "compute_p_lock",
                            lambda *a, **k: (0.9899, 3.0))
        assert _eval(engine) == []

    @pytest.mark.parametrize("stc,expected", [
        (121.0, 0), (120.0, 1), (90.0, 1), (5.0, 1), (4.9, 0), (0.0, 0)])
    def test_entry_window_edges(self, engine, enabled, stc, expected,
                                monkeypatch):
        # pin the lock signal so only the STC gate varies
        monkeypatch.setattr(twaplock_mod, "compute_p_lock",
                            lambda *a, **k: (0.999, 5.0))
        cands = _eval(engine, stc=stc)
        assert len(cands) == expected

    @pytest.mark.parametrize("ask,expected", [
        # fee(1ct) = ceil(0.07*ask*(100-ask)/100) = 1c for 95..97:
        # max allowed ask = 100 - 1 - 3 = 96.
        (95, 1), (96, 1), (97, 0)])
    def test_fee_plus_margin_gate(self, engine, enabled, ask, expected):
        cands = _eval(engine, yes_ask_cents=ask)
        assert len(cands) == expected

    def test_no_orderbook_no_candidate(self, engine, enabled):
        out = engine.evaluate_market(
            ticker=TICKER, event_ticker=EVENT, asset="BTC",
            product_type="15m", spot=110.0, threshold=100.0,
            seconds_to_close=90.0, blended_rv=0.00001,
            orderbook_fetch=lambda: None,
            config_snapshot_id=None, balance_at_scan=50000)
        assert out == []

    def test_zero_vol_no_candidate(self, engine, enabled):
        assert _eval(engine, blended_rv=0.0) == []
        assert _eval(engine, blended_rv=None) == []


# ── one entry per window per asset ───────────────────────────────────────────

class TestOneEntryPerWindow:
    def test_register_entry_blocks_second_shot(self, engine, enabled):
        assert len(_eval(engine)) == 1
        engine.register_entry(TICKER)
        assert _eval(engine) == []

    def test_other_window_unaffected(self, engine, enabled):
        engine.register_entry(TICKER)
        cands = _eval(engine, ticker=TICKER2, event=EVENT2, asset="ETH")
        assert len(cands) == 1

    def test_db_tw_row_blocks_across_restart(self, engine, state, enabled):
        # A tw- order row on the ticker (ANY status — even a zero-fill
        # canceled IOC consumed the one shot) blocks a FRESH engine.
        state.insert_bot_order("tw-prior", TICKER, EVENT, "BTC",
                               "yes", 2, 95, True)
        state.mark_order_status("tw-prior", "canceled")
        fresh = TwaplockEngine(MagicMock(), state)
        assert _eval(fresh) == []

    def test_authorize_recheck_blocks_after_entry(self, engine, enabled):
        cands = _eval(engine)
        assert len(cands) == 1
        engine.register_entry(TICKER)
        assert engine.authorize(cands[0]) == 0

    def test_authorize_passes_when_room(self, engine, enabled):
        cands = _eval(engine)
        assert engine.authorize(cands[0]) == 2


# ── cross-strategy ticker exclusion ──────────────────────────────────────────

class TestCrossStrategyExclusion:
    """twaplock must not enter a ticker with ANY open position or
    pending/resting order from ANY strategy (positions PK is still
    single-ticker until 86badbf9t — same class as longshot's R1-C2)."""

    def test_open_main_position_blocks(self, engine, state, enabled):
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "yes", 1, 90, strategy="TAKER_NOW",
            is_taker=True)
        assert _eval(engine) == []

    def test_open_longshot_position_blocks(self, engine, state, enabled):
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "no", 1, 8, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        assert _eval(engine) == []

    def test_resting_order_any_strategy_blocks(self, engine, state, enabled):
        state.insert_bot_order("mk-abc", TICKER, EVENT, "BTC",
                               "yes", 1, 90, False)
        state.confirm_order_submitted("mk-abc", "oid-mk-1")
        assert _eval(engine) == []

    def test_resting_longshot_quote_blocks(self, engine, state, enabled):
        state.insert_bot_order("ls-abc", TICKER, EVENT, "BTC",
                               "no", 3, 8, False)
        state.confirm_order_submitted("ls-abc", "oid-ls-1")
        assert _eval(engine) == []

    def test_clean_ticker_passes(self, engine, state, enabled):
        # exposure on a DIFFERENT ticker does not block this one
        state.record_position_from_fill(
            TICKER2, EVENT2, "ETH", "yes", 1, 90, strategy="TAKER_NOW",
            is_taker=True)
        assert len(_eval(engine)) == 1


# ── COMBINED daily loss cap + streak (bot/strategy_caps.py) ─────────────────

class TestCombinedDailyCap:
    def test_combined_losses_trip_both_engines(self, engine, state, enabled,
                                               monkeypatch, caplog):
        from bot.longshot import LongshotEngine
        monkeypatch.setattr(C, "LONGSHOT_ENABLED", True, raising=False)
        today = _today_utc().isoformat()
        # each alone under $20; combined -$21 over the cap
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -1200, today,
                      strategy="longshot")
        _seed_settled(state, "KXBTC15M-26JUN110915-T99", -900, today,
                      strategy="twaplock")
        ls_engine = LongshotEngine(MagicMock(), state)
        with caplog.at_level("WARNING"):
            assert _eval(engine) == []
        assert engine.disabled_reason() == "daily_cap"
        assert "TWAPLOCK_DAILY_CAP_HIT" in caplog.text
        ls_engine._refresh_disabled()
        assert ls_engine.disabled_reason() == "daily_cap"

    def test_each_alone_under_cap_trips_neither(self, engine, state, enabled,
                                                monkeypatch):
        from bot.longshot import LongshotEngine
        monkeypatch.setattr(C, "LONGSHOT_ENABLED", True, raising=False)
        today = _today_utc().isoformat()
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -1200, today,
                      strategy="longshot")
        ls_engine = LongshotEngine(MagicMock(), state)
        assert len(_eval(engine)) == 1
        assert engine.disabled_reason() is None
        ls_engine._refresh_disabled()
        assert ls_engine.disabled_reason() is None

    def test_twaplock_alone_under_cap_no_trip(self, engine, state, enabled):
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -1900,
                      _today_utc().isoformat(), strategy="twaplock")
        assert len(_eval(engine)) == 1

    def test_other_strategy_losses_dont_count(self, engine, state, enabled):
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -5000,
                      _today_utc().isoformat(), strategy="above")
        assert len(_eval(engine)) == 1

    def test_twaplock_marked_loss_counts_into_longshot_latch(
            self, engine, state, enabled, monkeypatch):
        """Marks cross engines: an open twaplock position whose BOUGHT side
        is currently OTM (full-loss mark) plus realized longshot losses
        must trip the COMBINED cap on the LONGSHOT engine too."""
        from bot.longshot import LongshotEngine
        monkeypatch.setattr(C, "LONGSHOT_ENABLED", True, raising=False)
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -1900,
                      _today_utc().isoformat(), strategy="longshot")
        # open twaplock position: bought YES 2ct @ 95c = 190c at risk
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "yes", 2, 95, strategy="twaplock",
            is_taker=True, fill_source="twaplock_taker")
        # latest mark: spot BELOW strike -> bought YES currently losing
        engine._mark_inputs[TICKER] = (90.0, 100.0, 9e12)
        ls_engine = LongshotEngine(MagicMock(), state)
        ls_engine._refresh_disabled()
        # -1900 realized - 190 marked = -2090 <= -2000
        assert ls_engine.disabled_reason() == "daily_cap"

    def test_combined_consecutive_losing_days_disable(self, engine, state,
                                                      enabled, caplog):
        today = _today_utc()
        # alternate strategies across the 3 losing days — COMBINED streak
        for i, strat in ((1, "longshot"), (2, "twaplock"), (3, "longshot")):
            d = (today - datetime.timedelta(days=i)).isoformat()
            _seed_settled(state, f"KXBTC15M-26JUN{i:02d}0900-T99", -100, d,
                          strategy=strat)
        with caplog.at_level("WARNING"):
            assert _eval(engine) == []
        assert engine.disabled_reason() == "consec_days"

    def test_streak_reset_override_clears_latch(self, engine, state, enabled,
                                                monkeypatch):
        today = _today_utc()
        for i in (1, 2, 3):
            d = (today - datetime.timedelta(days=i)).isoformat()
            _seed_settled(state, f"KXBTC15M-26JUN{i:02d}0900-T99", -100, d)
        monkeypatch.setattr(C, "LIVE_SMALL_STREAK_RESET_UTC_DATE",
                            today.isoformat(), raising=False)
        assert len(_eval(engine)) == 1


# ── evaluated_opportunities rows ─────────────────────────────────────────────

class TestEvalRows:
    def test_live_row_written(self, engine, state, enabled):
        _eval(engine)
        row = state.conn.execute(
            "SELECT * FROM evaluated_opportunities "
            "WHERE filter_stage='twaplock_live'").fetchone()
        assert row is not None
        assert row["ticker"] == TICKER
        assert row["strategy"] == "twaplock"
        assert row["side"] == "yes"
        assert row["market_price"] == 95

    def test_shadow_row_when_global_shadow(self, engine, state, enabled,
                                           monkeypatch):
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        cands = _eval(engine)
        # candidate still emitted — the GATE lives at executor.execute()
        assert len(cands) == 1
        row = state.conn.execute(
            "SELECT * FROM evaluated_opportunities "
            "WHERE filter_stage='twaplock_shadow'").fetchone()
        assert row is not None
        live = state.conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities "
            "WHERE filter_stage='twaplock_live'").fetchone()[0]
        assert live == 0

    def test_row_deduped_per_ticker_side(self, engine, state, enabled):
        _eval(engine)
        _eval(engine)
        n = state.conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities "
            "WHERE filter_stage LIKE 'twaplock_%'").fetchone()[0]
        assert n == 1


# ── trading-mode strategy override + tw- recognition ─────────────────────────

class TestTradingModeTwaplock:
    def test_strategy_is_live_truth_table(self, monkeypatch):
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        assert tm.strategy_is_live("twaplock", "BTC") is False
        assert tm.strategy_is_live("above", "BTC") is False
        monkeypatch.setattr(C, "TWAPLOCK_LIVE_OVERRIDE", True, raising=False)
        assert tm.strategy_is_live("twaplock", "BTC") is True
        assert tm.strategy_is_live("above", "BTC") is False  # main UNCHANGED
        assert tm.strategy_is_live("longshot", "BTC") is False  # ls UNCHANGED

    def test_strategy_from_client_order_id(self):
        assert tm.strategy_from_client_order_id("tw-abc") == "twaplock"
        assert tm.strategy_from_client_order_id("ls-abc") == "longshot"
        assert tm.strategy_from_client_order_id("mk-abc") is None
        assert tm.strategy_from_client_order_id(None) is None


# ── executor chokepoint (single live/shadow gate at execute()) ───────────────

class TestExecutorChokepoint:
    """Twaplock candidates route through the REAL OrderExecutor.execute().
    The trading-mode gate at the top of execute() stays the SINGLE
    live/shadow chokepoint — the twaplock path sits BELOW it."""

    @pytest.fixture
    def wired(self, state):
        from bot.executor import OrderExecutor
        client = MagicMock()
        client.place_order.return_value = {
            "order": {"order_id": "oid-tw-1", "fill_count_fp": "2.00"}}
        engine = TwaplockEngine(client, state)
        ml = MagicMock()
        ml.twaplock_engine = engine
        executor = OrderExecutor(client, state, MagicMock(),
                                 main_loop=ml, kalshi_feed=None)
        return executor, engine, client

    def test_live_routes_to_ioc_taker(self, wired, enabled):
        executor, engine, client = wired
        cands = _eval(engine)
        assert len(cands) == 1
        result = executor.execute(cands[0])
        assert result is not None
        client.place_order.assert_called_once()
        kwargs = client.place_order.call_args.kwargs
        assert kwargs["ticker"] == TICKER
        assert kwargs["side"] == "yes"
        assert kwargs["action"] == "buy"
        assert kwargs["count"] == 2
        assert kwargs["time_in_force"] == "immediate_or_cancel"
        assert kwargs["yes_price"] == 95
        assert "post_only" not in kwargs or not kwargs.get("post_only")
        assert kwargs["client_order_id"].startswith("tw-")

    def test_fill_recorded_fp_primary(self, wired, state, enabled):
        """FP-primary from day 1 (L-1 R7 lesson): a response carrying ONLY
        fill_count_fp (no legacy fill_count) must record the position."""
        executor, engine, client = wired
        cands = _eval(engine)
        executor.execute(cands[0])
        row = state.conn.execute(
            "SELECT side, count, avg_price_cents, strategy_group, is_taker "
            "FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row is not None
        assert row["side"] == "yes"
        assert row["count"] == 2
        assert row["avg_price_cents"] == 95
        assert row["strategy_group"] == "twaplock"
        assert row["is_taker"] == 1
        # ledger row marked off the placeable path
        st = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-tw-1'"
        ).fetchone()["status"]
        assert st == "filled"

    def test_zero_fill_marks_canceled_no_position(self, wired, state,
                                                  enabled):
        executor, engine, client = wired
        client.place_order.return_value = {
            "order": {"order_id": "oid-tw-z", "fill_count": 0}}
        cands = _eval(engine)
        executor.execute(cands[0])
        pos = state.conn.execute(
            "SELECT 1 FROM positions WHERE ticker=?", (TICKER,)).fetchone()
        assert pos is None
        st = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-tw-z'"
        ).fetchone()["status"]
        assert st == "canceled"
        # one shot CONSUMED even on zero fill (no hammering)
        assert _eval(engine) == []

    def test_partial_fill_recorded(self, wired, state, enabled):
        executor, engine, client = wired
        client.place_order.return_value = {
            "order": {"order_id": "oid-tw-p", "fill_count_fp": "1.00"}}
        cands = _eval(engine)
        executor.execute(cands[0])
        row = state.conn.execute(
            "SELECT count FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row["count"] == 1

    def test_api_none_marks_api_error(self, wired, state, enabled):
        executor, engine, client = wired
        client.place_order.return_value = None
        cands = _eval(engine)
        assert executor.execute(cands[0]) is None
        row = state.conn.execute(
            "SELECT status FROM pending_orders "
            "WHERE client_order_id LIKE 'tw-%'").fetchone()
        assert row["status"] == "api_error"

    def test_kill_switch_midflight_places_nothing(self, wired, enabled,
                                                  monkeypatch):
        executor, engine, client = wired
        cands = _eval(engine)
        monkeypatch.setattr(C, "TWAPLOCK_ENABLED", False, raising=False)
        assert executor.execute(cands[0]) is None
        client.place_order.assert_not_called()

    def test_inflight_main_order_blocks_at_executor(self, wired, state,
                                                    enabled):
        executor, engine, client = wired
        cands = _eval(engine)
        # a main-pipeline order lands between scan and execute
        state.insert_bot_order("mk-race", TICKER, EVENT, "BTC",
                               "yes", 1, 90, False)
        state.confirm_order_submitted("mk-race", "oid-mk-race")
        assert executor.execute(cands[0]) is None
        client.place_order.assert_not_called()

    # truth table: (global, asset_live, override) -> placed?
    @pytest.mark.parametrize("global_live,asset_live,override,placed", [
        (True, True, False, True),
        (True, False, False, False),   # asset shadow, no override
        (True, False, True, True),     # override rescues
        (False, True, False, False),   # global shadow
        (False, True, True, True),     # override rescues global shadow
        (False, False, False, False),
        (False, False, True, True),
    ])
    def test_gate_truth_table(self, wired, enabled, monkeypatch,
                              global_live, asset_live, override, placed):
        executor, engine, client = wired
        cands = _eval(engine)
        assert len(cands) == 1
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", global_live)
        monkeypatch.setitem(C.ASSET_LIVE_TRADING, "BTC", asset_live)
        monkeypatch.setattr(C, "TWAPLOCK_LIVE_OVERRIDE", override,
                            raising=False)
        result = executor.execute(cands[0])
        if placed:
            assert result is not None
            client.place_order.assert_called_once()
        else:
            assert result is None
            client.place_order.assert_not_called()


# ── tw- reconciler carve-outs (bot/state.py prefix-tuple generalization) ─────

class TestReconcilerCarveOuts:
    @pytest.fixture
    def client(self):
        c = MagicMock()
        c.get_orders.return_value = {"orders": []}
        c.get_positions.return_value = {"market_positions": []}
        c.get_settlements.return_value = {"settlements": []}
        c.get_balance.return_value = {"balance": 50000}
        c.get_fills.return_value = {"fills": []}
        return c

    def test_reconcile_skips_tw_orders_in_cancel_sweep(self, state, client):
        # a tw- row stranded 'resting' locally + (hypothetically) on the API
        state.insert_bot_order("tw-r1", TICKER, EVENT, "BTC",
                               "yes", 2, 95, True)
        state.confirm_order_submitted("tw-r1", "oid-tw-r1")
        client.get_orders.return_value = {"orders": [
            {"order_id": "oid-tw-r1", "client_order_id": "tw-r1",
             "ticker": TICKER, "side": "yes", "action": "buy",
             "yes_price": 95, "count": 2, "remaining_count": 2,
             "status": "resting"},
        ]}
        state.reconcile_with_api(client)
        client.cancel_order.assert_not_called()
        st = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-tw-r1'"
        ).fetchone()["status"]
        assert st == "resting"  # engine boot sweep owns the flip

    def test_reconcile_does_not_flip_local_tw_row_absent_from_api(
            self, state, client):
        state.insert_bot_order("tw-r2", TICKER, EVENT, "BTC",
                               "yes", 2, 95, True)
        state.confirm_order_submitted("tw-r2", "oid-tw-r2")
        state.reconcile_with_api(client)
        st = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-tw-r2'"
        ).fetchone()["status"]
        assert st == "resting"

    def test_cleanup_expired_skips_tw_rows(self, state):
        # past-close 15M ticker on a tw- row stays 'resting' for the engine
        past_ticker = "KXBTC15M-20JAN011200-T110"
        state.insert_bot_order("tw-r3", past_ticker, "KXBTC15M-20JAN011200",
                               "BTC", "yes", 2, 95, True)
        state.confirm_order_submitted("tw-r3", "oid-tw-r3")
        state.cleanup_expired_resting_orders()
        st = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-tw-r3'"
        ).fetchone()["status"]
        assert st == "resting"

    def test_unknown_position_with_tw_history_imported_as_twaplock(
            self, state, client):
        state.insert_bot_order("tw-r4", TICKER, EVENT, "BTC",
                               "yes", 2, 95, True)
        state.mark_order_status("tw-r4", "filled")
        client.get_positions.return_value = {"market_positions": [
            {"ticker": TICKER, "position": 2, "market_exposure": 190},
        ]}
        state.reconcile_with_api(client)
        row = state.conn.execute(
            "SELECT side, count, strategy, strategy_group FROM positions "
            "WHERE ticker=? AND status='open'", (TICKER,)).fetchone()
        assert row is not None
        assert row["strategy_group"] == "twaplock"
        assert row["strategy"] == "twaplock"

    def test_unknown_position_with_ls_history_still_longshot(
            self, state, client):
        # the generalization must not break the ls- stamp
        state.insert_bot_order("ls-r5", TICKER, EVENT, "BTC",
                               "no", 3, 8, False)
        state.mark_order_status("ls-r5", "filled")
        client.get_positions.return_value = {"market_positions": [
            {"ticker": TICKER, "position": -3, "market_exposure": 24},
        ]}
        state.reconcile_with_api(client)
        row = state.conn.execute(
            "SELECT strategy_group FROM positions "
            "WHERE ticker=? AND status='open'", (TICKER,)).fetchone()
        assert row is not None
        assert row["strategy_group"] == "longshot"


# ── engine boot sweep ────────────────────────────────────────────────────────

class TestBootSweep:
    def test_stranded_tw_rows_flipped_on_first_tick(self, state, enabled):
        state.insert_bot_order("tw-b1", TICKER, EVENT, "BTC",
                               "yes", 2, 95, True)  # stays 'pending'
        state.insert_bot_order("tw-b2", TICKER2, EVENT2, "ETH",
                               "yes", 2, 95, True)
        state.confirm_order_submitted("tw-b2", "oid-tw-b2")  # 'resting'
        # non-tw rows untouched
        state.insert_bot_order("mk-b3", TICKER2, EVENT2, "ETH",
                               "yes", 1, 90, False)
        state.confirm_order_submitted("mk-b3", "oid-mk-b3")
        eng = TwaplockEngine(MagicMock(), state)
        eng.tick()
        st1 = state.conn.execute(
            "SELECT status FROM pending_orders WHERE client_order_id='tw-b1'"
        ).fetchone()["status"]
        st2 = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-tw-b2'"
        ).fetchone()["status"]
        st3 = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-mk-b3'"
        ).fetchone()["status"]
        assert st1 == "canceled"
        assert st2 == "canceled"
        assert st3 == "resting"

    def test_boot_sweep_runs_once(self, state, enabled):
        eng = TwaplockEngine(MagicMock(), state)
        eng.tick()
        state.insert_bot_order("tw-late", TICKER, EVENT, "BTC",
                               "yes", 2, 95, True)
        eng.tick()  # latch set — late rows are NOT the boot sweep's problem
        st = state.conn.execute(
            "SELECT status FROM pending_orders WHERE client_order_id='tw-late'"
        ).fetchone()["status"]
        assert st == "pending"
