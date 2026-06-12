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
- Entry window edges: only when the module-private
  bot/twaplock.py::_MIN_SUBMIT_STC_SECONDS <= stc <=
  C.TWAPLOCK_ENTRY_WINDOW_SECONDS (the validated decision grid).
- Fee + margin gate: locked-side executable ask must satisfy
  ask <= 100 - taker_fee(1ct, ask) - TWAPLOCK_MIN_EDGE_CENTS.
- One entry per window per asset (STRUCTURAL — no knob; the retired
  TWAPLOCK_MAX_ENTRIES_PER_WINDOW constant is pinned absent): in-memory
  one-shot latch + DB-derived (tw- pending_orders rows) so the latch
  survives a restart.
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

@pytest.fixture(autouse=True)
def _isolate_mark_providers():
    """R1-MN3: bot.strategy_caps._mark_providers is MODULE-GLOBAL state —
    every TwaplockEngine/LongshotEngine construction registers a mark
    provider bound to that test's StateManager. Snapshot + clear before
    each test and restore after, so a provider closed over a dead tmp-DB
    never leaks into another test's combined-cap math (or across files on
    the same xdist worker). Sister fixture in test_longshot_strategy.py."""
    saved = dict(strategy_caps._mark_providers)
    strategy_caps._mark_providers.clear()
    yield
    strategy_caps._mark_providers.clear()
    strategy_caps._mark_providers.update(saved)


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
          now=None, asset="BTC", event=EVENT, spot_staleness=0.0):
    """Default geometry: spot WAY above strike with tiny vol at stc=90s
    (pre-TWAP-window branch — no accrued buffer needed) -> p_lock ~ 1.0
    -> locked side YES at executable ask 95c (fee 1c; 95 <= 100-1-3).

    ``spot_staleness`` seeds the scanner-owned Bit-S.1 per-asset cache
    (``state._scan_spot_staleness_cache``) that the frozen-spot gate
    (R2-MN1) reads — default 0.0 = fresh WS tick this tick, so every
    test that isn't ABOUT the gate sails through it. ``None`` pops the
    slot (warmup / scanner honest-NULL)."""
    if spot_staleness is None:
        eng._state._scan_spot_staleness_cache.pop(asset, None)
    else:
        eng._state._scan_spot_staleness_cache[asset] = spot_staleness
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
    def test_enabled_live_since_go_live(self):
        # Flipped True at the 2026-06-12 operator go-live ($400 live-small).
        assert C.TWAPLOCK_ENABLED is True

    def test_p_lock_threshold_stricter_than_validated(self):
        # Validation used 0.95 on the honest 4-venue index; the
        # Coinbase-anchored live MVP uses 0.99 (degraded-index lesson).
        assert C.TWAPLOCK_P_LOCK_THRESHOLD == 0.99

    def test_sizing_rails(self):
        assert C.TWAPLOCK_MAX_CONTRACTS_PER_ENTRY == 2
        assert C.TWAPLOCK_MIN_EDGE_CENTS == 3
        # R1-MN4: one-entry-per-window is STRUCTURAL (binary in-memory
        # latch + ANY tw- pending_orders row on the ticker), not a knob —
        # the TWAPLOCK_MAX_ENTRIES_PER_WINDOW constant was RETIRED because
        # the latch could never honor a value other than 1.
        assert not hasattr(C, "TWAPLOCK_MAX_ENTRIES_PER_WINDOW")

    def test_entry_window(self):
        # Validated decision grid (01b_twap_lock_validation.py
        # DEC_FROM/DEC_TO = 90..10): no backtest evidence outside [10, 90].
        assert C.TWAPLOCK_ENTRY_WINDOW_SECONDS == 90.0
        assert twaplock_mod._MIN_SUBMIT_STC_SECONDS == 10.0
        assert C.TWAPLOCK_TWAP_WINDOW_SECONDS == 60.0

    def test_live_override_paused_after_go_live(self):
        # True at the 2026-06-12 go-live; paused same day: the longshot
        # autopsy found blended_rv (which p_lock also consumes) running
        # 1.4-4x below tape vol. Engine stays ENABLED (shadow rows).
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
    def test_disabled_no_candidates_no_rows(self, engine, state,
                                            monkeypatch):
        # Explicit OFF baseline (shipped config is LIVE since 2026-06-12)
        monkeypatch.setattr(C, "TWAPLOCK_ENABLED", False, raising=False)
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
        # Validated grid edges (DEC_FROM=90 / DEC_TO=10): no evidence for
        # (90, 120] or [5, 10) — both former edges are now OUTSIDE.
        (120.0, 0), (90.1, 0), (90.0, 1), (45.0, 1), (10.0, 1), (9.9, 0),
        (5.0, 0), (0.0, 0)])
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
        engine._state._scan_spot_staleness_cache["BTC"] = 0.0  # fresh — isolate the orderbook branch from the R2-MN1 gate
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


# ── frozen-spot false-lock gate (R2-MN1) ─────────────────────────────────────

class TestSpotStalenessGate:
    """A frozen Coinbase WS price keeps feeding record_spot with FRESH
    receive timestamps, so the accrued TWAP freezes at a stale price and
    p_lock can clear 0.99 spuriously — the absent-sample -> None layer in
    _accrued_mean never fires because samples keep arriving. Layer 2:
    evaluate_market reads the scanner's per-asset Bit-S.1 staleness cache
    (state._scan_spot_staleness_cache) and emits NO SIGNAL when the
    reading is missing or > TWAPLOCK_MAX_SPOT_STALENESS_SECONDS."""

    def test_constant_exists(self):
        assert C.TWAPLOCK_MAX_SPOT_STALENESS_SECONDS == 5.0

    def test_stale_reading_blocks_and_writes_no_row(self, engine, state,
                                                    enabled, caplog):
        import logging as _logging
        with caplog.at_level(_logging.INFO):
            cands = _eval(
                engine,
                spot_staleness=C.TWAPLOCK_MAX_SPOT_STALENESS_SECONDS + 1.0)
        assert cands == []
        assert "TWAPLOCK_SPOT_STALE" in caplog.text
        n = state.conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities WHERE "
            "filter_stage LIKE 'twaplock%'").fetchone()[0]
        assert n == 0

    def test_missing_cache_entry_blocks(self, engine, enabled):
        """Warmup / scanner honest-NULL pop: no reading = no signal."""
        assert _eval(engine, spot_staleness=None) == []

    def test_fresh_reading_passes(self, engine, enabled):
        assert len(_eval(engine, spot_staleness=0.5)) == 1

    def test_boundary_at_threshold_passes(self, engine, enabled):
        """Gate is strict-greater-than: exactly the constant still trades."""
        assert len(_eval(
            engine,
            spot_staleness=C.TWAPLOCK_MAX_SPOT_STALENESS_SECONDS)) == 1

    def test_absent_sample_layer_still_holds(self, engine, enabled):
        """Layer 1 (docstring-claimed absent-sample path) survives the
        Layer-2 addition: inside the TWAP window (stc < 60) with no ring-
        buffer sample at-or-before window start, a FRESH staleness reading
        still emits nothing (accrued mean is None -> no signal)."""
        assert _eval(engine, stc=30.0, spot_staleness=0.0) == []


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
        monkeypatch.setattr(C, "TWAPLOCK_LIVE_OVERRIDE", False,
                            raising=False)  # pin the non-override path
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
        # shipped overrides are LIVE since 2026-06-12 — pin the OFF baseline
        # explicitly before walking the table
        monkeypatch.setattr(C, "TWAPLOCK_LIVE_OVERRIDE", False, raising=False)
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", False, raising=False)
        assert tm.strategy_is_live("twaplock", "BTC") is False
        assert tm.strategy_is_live("above", "BTC") is False
        monkeypatch.setattr(C, "TWAPLOCK_LIVE_OVERRIDE", True, raising=False)
        assert tm.strategy_is_live("twaplock", "BTC") is True
        assert tm.strategy_is_live("above", "BTC") is False  # main UNCHANGED
        assert tm.strategy_is_live("longshot", "BTC") is False  # ls UNCHANGED
        # M1 fix round: the override is scoped to the VALIDATED universe
        # (TWAPLOCK_LIVE_ASSETS mirrors the 01b TRACKED tuple — 7 assets).
        # ADA/BCH are T1 zero-live-orders shadow + outside the 01b corpus.
        assert tm.strategy_is_live("twaplock", "ADA") is False
        assert tm.strategy_is_live("twaplock", "BCH") is False
        assert tm.strategy_is_live("twaplock", "BNB") is True  # in TRACKED
        assert tm.strategy_is_live("longshot", "BNB") is False  # override OFF here

    def test_backstop_blocks_tw_order_on_shadow_asset(self, monkeypatch):
        """M1 fix round: the place_order backstop must refuse a tw- order on
        an asset outside TWAPLOCK_LIVE_ASSETS even with the override ON —
        ADA/BCH carry the T1 zero-live-orders shadow designation
        (ADA_15M_SHADOW/BCH_15M_SHADOW) and have no 01b validation rows."""
        from bot.kalshi_client import KalshiClient
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "TWAPLOCK_LIVE_OVERRIDE", True, raising=False)
        for shadow_ticker in ("KXADA15M-26JUN111200-T1",
                              "KXBCH15M-26JUN111200-T500"):
            client = MagicMock()
            result = KalshiClient.place_order(
                client, shadow_ticker, "yes", "buy", 1, yes_price=95,
                client_order_id="tw-x1")
            assert result is None
            client._request.assert_not_called()
        # BNB IS in the twaplock validated set -> the override passes it
        client = MagicMock()
        client._request.return_value = {"order": {"order_id": "ok"}}
        KalshiClient.place_order(
            client, "KXBNB15M-26JUN111200-T700", "yes", "buy", 1,
            yes_price=95, client_order_id="tw-x2")
        client._request.assert_called_once()

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

    def test_override_shadow_asset_places_nothing(self, wired, enabled,
                                                  monkeypatch):
        """M1 fix round: TWAPLOCK_LIVE_OVERRIDE must not arm ADA/BCH at the
        executor chokepoint (T1 zero-live-orders shadow + outside the 01b
        validated universe) — feedback_shadow_flag_comprehensive_may10."""
        executor, engine, client = wired
        cands = _eval(engine)
        assert len(cands) == 1
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "TWAPLOCK_LIVE_OVERRIDE", True, raising=False)
        for shadow_asset, shadow_ticker in (
                ("ADA", "KXADA15M-26JUN111200-T1"),
                ("BCH", "KXBCH15M-26JUN111200-T500")):
            cand = dict(cands[0], ticker=shadow_ticker, asset=shadow_asset)
            assert executor.execute(cand) is None
        client.place_order.assert_not_called()

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

    def test_place_malformed_no_order_id_marks_api_error(self, wired, state,
                                                         enabled):
        """TWAPLOCK_PLACE_MALFORMED: a place response without an order_id
        keys nothing — ledger row marked api_error, NO position recorded,
        one-shot consumed (positions-API reconcile owns any hidden fill).
        Mirrors longshot's LONGSHOT_PLACE_MALFORMED R2 defense."""
        executor, engine, client = wired
        client.place_order.return_value = {"order": {}}
        cands = _eval(engine)
        assert executor.execute(cands[0]) is None
        row = state.conn.execute(
            "SELECT status FROM pending_orders "
            "WHERE client_order_id LIKE 'tw-%'").fetchone()
        assert row["status"] == "api_error"
        pos = state.conn.execute(
            "SELECT 1 FROM positions WHERE ticker=?", (TICKER,)).fetchone()
        assert pos is None
        assert _eval(engine) == []  # shot consumed

    @pytest.mark.parametrize("order_fields", [
        {"fill_count_fp": "garbage"},                  # unparseable FP str
        {"fill_count": "garbage"},                     # unparseable legacy
        {"fill_count_fp": {"nested": "junk"}},         # wrong type entirely
    ])
    def test_malformed_fill_count_degrades_to_zero_fill(
            self, wired, state, enabled, order_fields):
        """R1-MN5: a malformed fill-count field on an otherwise-valid IOC
        response must DEGRADE to the 0-fill path (row canceled, no
        position, shot consumed) — never raise past
        confirm_order_submitted (which would strand the row 'resting' and
        crash the scan tick)."""
        executor, engine, client = wired
        client.place_order.return_value = {
            "order": {"order_id": "oid-tw-m", **order_fields}}
        cands = _eval(engine)
        assert executor.execute(cands[0]) is None  # must not raise
        st = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-tw-m'"
        ).fetchone()["status"]
        assert st == "canceled"
        pos = state.conn.execute(
            "SELECT 1 FROM positions WHERE ticker=?", (TICKER,)).fetchone()
        assert pos is None
        assert _eval(engine) == []  # shot consumed — no hot retry

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


# ── scanner overlay + main_loop wiring (source pins) ─────────────────────────

class TestScannerAndMainLoopWiring:
    """Source pins for the Bit T-1 wiring outside the engine (mirrors the
    longshot R1 source-pin pattern in test_longshot_r1_regressions.py)."""

    def _scanner_src(self):
        import pathlib
        return pathlib.Path("bot/scanner/__init__.py").read_text()

    def test_scanner_overlay_calls_engine(self):
        src = self._scanner_src()
        start = src.index("TWAP-lock endgame overlay")
        block = src[start:start + 3000]
        assert "twaplock_engine" in block
        assert "evaluate_market" in block
        # kill switch read live at the scan layer (cheap no-op when off)
        assert "TWAPLOCK_ENABLED" in block

    def test_scanner_partitions_twaplock_as_overlay(self):
        src = self._scanner_src()
        # tail partition (bracket_no/longshot pattern): twaplock candidates
        # bypass the single-asset filter and re-join via selected.extend
        assert '_twaplock_candidates = [c for c in candidates' in src
        assert 'c.get("strategy") == "twaplock"' in src
        assert "selected.extend(_twaplock_candidates)" in src
        # excluded from the main-pipeline list
        _main_start = src.index("_main_candidates = [c for c in candidates")
        _main_block = src[_main_start:_main_start + 600]
        assert '"twaplock"' in _main_block

    def test_occupied_timeslots_have_no_twaplock_carveout(self):
        """INTENTIONAL asymmetry vs longshot R2-C1: a twaplock position /
        tw- order DOES occupy its (timeslot, asset) slot, so the main
        pipeline skips the window for its final ~2min — the cheap reverse-
        direction defense for the single-ticker positions PK (86badbf9t).
        Twaplock needs no further evaluation of an entered window (one
        shot, hold to settlement; marks go stale but settlement realizes
        within minutes)."""
        import pathlib
        src = pathlib.Path("bot/scanner/__init__.py").read_text()
        start = src.index("def _get_occupied_timeslots")
        block = src[start:start + 2500]
        assert "TWAPLOCK_CLIENT_OID_PREFIX" not in block
        assert '"twaplock"' not in block.replace(
            "twaplock rows DO occupy", "")  # carve-out absent by design

    def test_main_loop_constructs_and_ticks_engine(self):
        import pathlib
        src = pathlib.Path("bot/main_loop.py").read_text()
        assert "from bot.twaplock import TwaplockEngine" in src
        assert "self.twaplock_engine = TwaplockEngine(" in src
        assert "self.twaplock_engine.tick()" in src
