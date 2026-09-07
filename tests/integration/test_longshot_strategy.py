"""Longshot premium-harvest maker strategy — engine behavior (Bit L-1).

TDD-first scaffold (written RED before bot/longshot.py exists). Covers, per
kb/decisions/longshot-twap-live-small-plan.md + the Bit L-1 spec:

- LONGSHOT_* constants exist in bot/constants.py with the validated values
  (default LONGSHOT_ENABLED=False — flipped only by the operator at go-live).
- Condition logic: p_normal <= ask/2 boundary, price band edges (3c/4c/15c/16c),
  STC window edges (179s/180s/720s/721s).
- Sizing caps: LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE per (window, side),
  reduced by max(open longshot positions, ls- FILLED contracts per
  pending_orders.recorded_fill_count) + unfilled resting longshot quotes
  (86baf07y3 fill-ledger term).
- Collateral cap: LONGSHOT_MAX_CONCURRENT_COLLATERAL_DOLLARS across resting
  quotes + open longshot positions.
- Daily loss cap: realized live-small PnL today <= -LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS
  -> same-day auto-disable (in-memory latch + LONGSHOT_DAILY_CAP_HIT signature).
  Bit T-1 retargeted the rail to the COMBINED helper (bot/strategy_caps.py,
  strategy IN ('longshot','twaplock')); with no twaplock activity seeded, the
  combined sums reduce to the original per-strategy semantics tested here.
- Consecutive losing days: LIVE_SMALL_CONSECUTIVE_LOSING_DAYS_DISABLE completed
  losing days -> persistent (DB-derived) auto-disable;
  LIVE_SMALL_STREAK_RESET_UTC_DATE clears the latch.
- evaluated_opportunities row write with filter_stage='longshot_live' /
  'longshot_shadow' (cell-block string-literal discipline) — labeling consults
  bot.trading_mode read-only; the GATE stays at executor.execute().
- LONGSHOT_ENABLED=False -> no candidates AND no rows.
- Quote lifecycle: cancel at T-3min (tick sweep), cancel when the condition
  no longer holds (scan-driven refresh), fills recorded as positions via
  record_position_from_fill (hold to settlement, no early exit).
- Executor chokepoint: candidates route through the REAL
  OrderExecutor.execute() — the trading-mode gate at the top of execute()
  (bot/trading_mode.py, PR #158) is the single live/shadow gate; under
  GLOBAL_LIVE_TRADING=False NO order is placed even with LONGSHOT_ENABLED=True.

Real sqlite3 file via tmp_path per tests/CLAUDE.md integration-tier convention.
The integration-tier autouse fixture sets GLOBAL_LIVE_TRADING=True, so the
default label here is 'longshot_live'; the shadow-label test monkeypatches
bot.constants directly (same pattern as test_trading_mode_backstop.py).
"""
from __future__ import annotations

import datetime
from datetime import timezone
from unittest.mock import MagicMock

import pytest

import bot.constants as C
import bot.trading_mode as tm
from bot.state import StateManager

import bot.longshot as longshot_mod
from bot.longshot import LongshotEngine, compute_p_normal


TICKER = "KXBTC15M-26JUN111200-T110"
EVENT = "KXBTC15M-26JUN111200"


# ── fixtures / helpers ───────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_mark_providers():
    """R1-MN3: bot.strategy_caps._mark_providers is MODULE-GLOBAL state —
    every LongshotEngine/TwaplockEngine construction registers a mark
    provider bound to that test's StateManager. Snapshot + clear before
    each test and restore after, so a provider closed over a dead tmp-DB
    never leaks into another test's combined-cap math (or across files on
    the same xdist worker). Sister fixture in test_twaplock_strategy.py."""
    from bot import strategy_caps
    saved = dict(strategy_caps._mark_providers)
    strategy_caps._mark_providers.clear()
    yield
    strategy_caps._mark_providers.clear()
    strategy_caps._mark_providers.update(saved)


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "test_longshot.db"))
    yield s
    s.close()


@pytest.fixture
def engine(state):
    client = MagicMock()
    client.get_fills.return_value = {"fills": []}
    client.cancel_order.return_value = {"order": {"status": "canceled"}}
    return LongshotEngine(client, state)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(C, "LONGSHOT_ENABLED", True, raising=False)


def _ob(yes_ask_cents=8, yes_bid_cents=3):
    """Kalshi book: 'yes' = YES bids, 'no' = NO bids.
    YES ask = 100 - best NO bid; NO ask = 100 - best YES bid."""
    return {
        "no": [[100 - yes_ask_cents, 50]],
        "yes": [[yes_bid_cents, 50]],
    }


def _eval(eng, *, yes_ask_cents=8, yes_bid_cents=3, stc=600.0, spot=100.0,
          threshold=110.0, blended_rv=0.0001, ticker=TICKER, ob=None,
          asset="BTC", spot_staleness=0.0):
    """``spot_staleness`` seeds the scanner-owned Bit-S.1 per-asset cache
    (``state._scan_spot_staleness_cache``) that the frozen/unmeasured-spot
    gate (R1-M1 fix round, Bit V.1 — mirror of twaplock R2-MN1) reads —
    default 0.0 = fresh WS tick this tick, so every test that isn't ABOUT
    the gate sails through it. ``None`` pops the slot (warmup / scanner
    honest-NULL)."""
    if spot_staleness is None:
        eng._state._scan_spot_staleness_cache.pop(asset, None)
    else:
        eng._state._scan_spot_staleness_cache[asset] = spot_staleness
    if ob is None:
        ob = _ob(yes_ask_cents, yes_bid_cents)
    return eng.evaluate_market(
        ticker=ticker, event_ticker=EVENT, asset=asset, product_type="15m",
        spot=spot, threshold=threshold, seconds_to_close=stc,
        blended_rv=blended_rv, orderbook_fetch=lambda: ob,
        config_snapshot_id=None, balance_at_scan=50000)


def _seed_settled(state, ticker, pnl_cents, settled_date, strategy="longshot"):
    state.conn.execute(
        "INSERT INTO settled_trades (ticker, event_ticker, asset, market_result,"
        " side, count, entry_price_cents, revenue_cents, fee_cents, pnl_cents,"
        " settled_at, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (ticker, EVENT, "BTC", "no", "no", 3, 92, 0, 0, pnl_cents,
         f"{settled_date}T12:00:00.000000Z", strategy))
    state.conn.commit()


def _today_utc():
    return datetime.datetime.now(timezone.utc).date()


# ── constants ────────────────────────────────────────────────────────────────

class TestLongshotConstants:
    def test_enabled_live_since_go_live(self):
        # Flipped True at the 2026-06-12 operator go-live ($400 live-small).
        assert C.LONGSHOT_ENABLED is True

    def test_price_band(self):
        assert C.LONGSHOT_MIN_ASK_CENTS == 4
        assert C.LONGSHOT_MAX_ASK_CENTS == 15

    def test_stc_window(self):
        assert C.LONGSHOT_MIN_STC_SECONDS == 180.0
        assert C.LONGSHOT_MAX_STC_SECONDS == 720.0

    def test_edge_ratio_is_half(self):
        assert C.LONGSHOT_EDGE_RATIO == 0.5

    def test_risk_rails(self):
        assert C.LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE == 3
        assert C.LONGSHOT_MAX_CONCURRENT_COLLATERAL_DOLLARS == 150.0
        # Bit T-1: the per-strategy LONGSHOT_DAILY_LOSS_CAP_DOLLARS /
        # LONGSHOT_CONSECUTIVE_LOSING_DAYS_DISABLE constants were RETIRED
        # into the COMBINED live-small rails (bot/strategy_caps.py).
        assert C.LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS == 20.0
        assert C.LIVE_SMALL_CONSECUTIVE_LOSING_DAYS_DISABLE == 3
        assert not hasattr(C, "LONGSHOT_DAILY_LOSS_CAP_DOLLARS")
        assert not hasattr(C, "LONGSHOT_CONSECUTIVE_LOSING_DAYS_DISABLE")

    def test_streak_reset_override_exists(self):
        assert C.LIVE_SMALL_STREAK_RESET_UTC_DATE == ""
        assert not hasattr(C, "LONGSHOT_STREAK_RESET_UTC_DATE")


# ── p_normal math ────────────────────────────────────────────────────────────

class TestPNormal:
    def test_at_strike_is_half(self):
        p_yes, z = compute_p_normal(100.0, 100.0, 600.0, 0.001)
        assert z == pytest.approx(0.0)
        assert p_yes == pytest.approx(0.5)

    def test_far_below_strike_yes_improbable(self):
        p_yes, _ = compute_p_normal(100.0, 110.0, 600.0, 0.0001)
        assert p_yes < 0.001

    def test_invalid_inputs_return_none(self):
        assert compute_p_normal(0.0, 110.0, 600.0, 0.0001) == (None, None)
        assert compute_p_normal(100.0, 110.0, 0.0, 0.0001) == (None, None)
        assert compute_p_normal(100.0, 110.0, 600.0, 0.0) == (None, None)
        assert compute_p_normal(100.0, 110.0, 600.0, None) == (None, None)


# ── condition logic ──────────────────────────────────────────────────────────

class TestConditionLogic:
    def test_disabled_no_candidates_no_rows(self, engine, state,
                                            monkeypatch):
        # Explicit OFF baseline (shipped config is LIVE since 2026-06-12)
        monkeypatch.setattr(C, "LONGSHOT_ENABLED", False, raising=False)
        assert _eval(engine) == []
        rows = state.conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities").fetchone()[0]
        assert rows == 0

    def test_yes_sell_candidate_emitted(self, engine, enabled):
        cands = _eval(engine, yes_ask_cents=8)
        assert len(cands) == 1
        c = cands[0]
        assert c["strategy"] == "longshot"
        assert c["longshot_sell_side"] == "yes"
        assert c["longshot_ask_cents"] == 8
        # selling YES at ask 8c == posting a NO bid at 92c
        assert c["side"] == "no"
        assert c["longshot_buy_side"] == "no"
        assert c["longshot_buy_price_cents"] == 92
        assert c["best_yes_ask"] == 92  # per-contract cost convention (bracket_no pattern)
        assert c["position_size"] == 3

    def test_no_sell_candidate_emitted(self, engine, enabled):
        # spot far ABOVE threshold -> NO improbable; NO ask = 100 - yes_bid = 8c
        cands = _eval(engine, spot=110.0, threshold=100.0,
                      yes_ask_cents=97, yes_bid_cents=92)
        assert len(cands) == 1
        c = cands[0]
        assert c["longshot_sell_side"] == "no"
        assert c["longshot_ask_cents"] == 8
        assert c["side"] == "yes"
        assert c["longshot_buy_price_cents"] == 92

    @pytest.mark.parametrize("ask,expected", [(3, 0), (4, 1), (15, 1), (16, 0)])
    def test_price_band_edges(self, engine, enabled, ask, expected):
        cands = _eval(engine, yes_ask_cents=ask)
        assert len(cands) == expected

    @pytest.mark.parametrize("stc,expected", [
        (179.0, 0), (180.0, 1), (720.0, 1), (721.0, 0)])
    def test_stc_window_edges(self, engine, enabled, stc, expected):
        cands = _eval(engine, stc=stc)
        assert len(cands) == expected

    def test_edge_condition_boundary(self, engine, enabled, monkeypatch):
        # ask=8c -> threshold p = 0.04. p == ask/2 qualifies; p above does not.
        monkeypatch.setattr(longshot_mod, "compute_p_normal",
                            lambda *a, **k: (0.04, 5.0))
        assert len(_eval(engine, yes_ask_cents=8)) == 1
        monkeypatch.setattr(longshot_mod, "compute_p_normal",
                            lambda *a, **k: (0.0401, 5.0))
        assert len(_eval(engine, yes_ask_cents=8)) == 0

    def test_no_orderbook_no_candidate(self, engine, enabled):
        # fresh staleness — isolate the orderbook branch from the
        # frozen-spot gate (same idiom as test_twaplock_strategy.py:359).
        engine._state._scan_spot_staleness_cache["BTC"] = 0.0
        out = engine.evaluate_market(
            ticker=TICKER, event_ticker=EVENT, asset="BTC", product_type="15m",
            spot=100.0, threshold=110.0, seconds_to_close=600.0,
            blended_rv=0.0001, orderbook_fetch=lambda: None,
            config_snapshot_id=None, balance_at_scan=50000)
        assert out == []


# ── frozen/unmeasured-spot gate (R1-M1 fix round, Bit V.1) ───────────────────

class TestSpotStalenessGate:
    """Mirror of twaplock's R2-MN1 frozen-spot gate (TWAPLOCK_SPOT_STALE).
    The CoinbaseFeed sampler re-stamps the last-known price with fresh
    timestamps every 1s, so a frozen WS feed is invisible to buffer-shape
    guards — only the Bit-S.1 EVENT-time staleness cache
    (state._scan_spot_staleness_cache) can see the freeze, and longshot's
    p_normal would otherwise be priced off a stale spot AND a deflated
    tape rv. The validated backtest (02_longshot_tick_floor STALE_S=30.0)
    ABSTAINED at every decision point whose spot was >30s event-stale;
    pre-fix the live engine had NO such gate (twaplock had its 5s gate,
    longshot none — R1-M1)."""

    def test_constant_exists(self):
        assert C.LONGSHOT_MAX_SPOT_STALENESS_SECONDS == 30.0

    def test_stale_reading_blocks_and_writes_no_row(self, engine, state,
                                                    enabled, caplog):
        import logging as _logging
        with caplog.at_level(_logging.INFO):
            cands = _eval(
                engine,
                spot_staleness=C.LONGSHOT_MAX_SPOT_STALENESS_SECONDS + 1.0)
        assert cands == []
        assert "LONGSHOT_SPOT_STALE" in caplog.text
        n = state.conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities WHERE "
            "filter_stage LIKE 'longshot%'").fetchone()[0]
        assert n == 0

    def test_missing_cache_entry_blocks(self, engine, enabled):
        """Warmup / scanner honest-NULL pop: no reading = no signal."""
        assert _eval(engine, spot_staleness=None) == []

    def test_fresh_reading_passes(self, engine, enabled):
        assert len(_eval(engine, spot_staleness=0.5)) == 1

    def test_boundary_at_threshold_passes(self, engine, enabled):
        """Gate is strict-greater-than: exactly the constant still trades
        (same boundary semantics as twaplock's gate)."""
        assert len(_eval(
            engine,
            spot_staleness=C.LONGSHOT_MAX_SPOT_STALENESS_SECONDS)) == 1

    def test_stale_log_throttled_per_asset(self, engine, enabled, caplog):
        """Longshot evaluates every 15M ticker in the STC band every tick
        — on gapped feeds (BNB: 34% of 1-min intervals stale) an
        unthrottled log would spam per ticker. 60s/asset throttle (the
        TAPE_RV_NONE idiom); candidates still blocked on every stale
        call."""
        import logging as _logging
        with caplog.at_level(_logging.INFO):
            assert _eval(engine, spot_staleness=31.0) == []
            assert _eval(engine, spot_staleness=31.0,
                         ticker=TICKER + "X") == []
        assert caplog.text.count("LONGSHOT_SPOT_STALE") == 1


# ── stale-episode quote-down (R2-M1 fix round, Bit V.1) ──────────────────────

class TestSpotStaleQuoteDown:
    """R2-M1 (Bit V.1 R2 fix round): the staleness gate returns [] BEFORE
    the condition-refresh pass, so pre-fix a resting quote stayed up
    UN-refreshed for the whole stale episode (observed up to ~12 min) —
    but the validated economics excluded those fills: the 02b fill
    model's ``zscore`` goes None on a >30s-stale spot at print time
    (``02b_longshot_fillable_validation.py::_at``/``_rv_pure``,
    ``STALE_S=30.0``), and the only measured tolerance for quotes
    lingering past signal death is the 10s cancel-latency arm
    (``LATENCY_S=10.0``, pickoff -0.16c/ct). Fix: staleness persisting
    past ``LONGSHOT_STALE_CANCEL_GRACE_SECONDS`` cancels this ticker's
    resting quotes (reason ``spot_stale``; cancel_order is intentionally
    ungated — cancels only reduce exposure). Grace > 0 absorbs flickery
    staleness (no cancel churn); a fresh eval clears the episode clock."""

    @pytest.fixture
    def clock(self, monkeypatch):
        """Deterministic wall clock for bot.longshot's module-level
        ``time`` name (the module only calls ``time.time()``)."""
        import types
        c = {"t": 1_750_000_000.0}
        monkeypatch.setattr(
            longshot_mod, "time",
            types.SimpleNamespace(time=lambda: c["t"]))
        return c

    @staticmethod
    def _register_with_ledger(engine, state):
        """Resting quote + its pending_orders ledger row (R2-C1 pattern
        from test_longshot_r2_regressions.py::_seed_pending_resting)."""
        state.insert_bot_order("ls-ss1", TICKER, EVENT, "BTC", "no", 3, 92,
                               False)
        state.confirm_order_submitted("ls-ss1", "oid-ss1")
        engine.register_resting(
            order_id="oid-ss1", client_order_id="ls-ss1", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)

    def test_grace_constant_within_validated_latency_arm(self):
        assert C.LONGSHOT_STALE_CANCEL_GRACE_SECONDS == 10.0
        # The 02b LATENCY_S=10.0 arm is the ONLY measured tolerance for a
        # quote lingering after the signal dies — the grace must never
        # exceed it.
        assert C.LONGSHOT_STALE_CANCEL_GRACE_SECONDS <= 10.0

    def test_stale_past_grace_cancels_with_spot_stale_reason(
            self, engine, state, enabled, clock, caplog):
        import logging as _logging
        self._register_with_ledger(engine, state)
        with caplog.at_level(_logging.INFO):
            # first stale eval latches the episode clock — within grace,
            # the quote stays (churn protection)
            assert _eval(engine, spot_staleness=31.0) == []
            engine._client.cancel_order.assert_not_called()
            clock["t"] += C.LONGSHOT_STALE_CANCEL_GRACE_SECONDS + 0.1
            assert _eval(engine, spot_staleness=31.0) == []
        engine._client.cancel_order.assert_called_once_with(
            "oid-ss1", ticker=TICKER)
        assert engine.resting_count() == 0
        assert "reason=spot_stale" in caplog.text
        row = state.conn.execute(
            "SELECT status FROM pending_orders WHERE order_id='oid-ss1'"
        ).fetchone()
        assert row["status"] == "canceled", (
            "the spot_stale pop must flip the ledger row off 'resting' "
            "(R2-C1 — otherwise the timeslot stays occupied forever)")

    def test_unmeasured_reading_past_grace_also_cancels(
            self, engine, state, enabled, clock):
        """``None`` staleness (warmup / scanner honest-NULL pop) is the
        same no-signal episode as a stale reading — quote-down applies."""
        self._register_with_ledger(engine, state)
        assert _eval(engine, spot_staleness=None) == []
        clock["t"] += C.LONGSHOT_STALE_CANCEL_GRACE_SECONDS + 0.1
        assert _eval(engine, spot_staleness=None) == []
        engine._client.cancel_order.assert_called_once_with(
            "oid-ss1", ticker=TICKER)
        assert engine.resting_count() == 0

    def test_stale_within_grace_quote_stays(self, engine, state, enabled,
                                            clock):
        self._register_with_ledger(engine, state)
        assert _eval(engine, spot_staleness=31.0) == []
        clock["t"] += C.LONGSHOT_STALE_CANCEL_GRACE_SECONDS - 5.0
        assert _eval(engine, spot_staleness=31.0) == []
        engine._client.cancel_order.assert_not_called()
        assert engine.resting_count() == 1

    def test_recovery_within_grace_no_churn(self, engine, state, enabled,
                                            clock):
        """Flickery staleness: a fresh eval inside the grace clears the
        episode clock, and a NEW stale episode restarts it — 13s of
        cumulative wall time across two separate sub-grace episodes must
        NOT cancel (no churn on gapped-but-recovering feeds)."""
        self._register_with_ledger(engine, state)
        assert _eval(engine, spot_staleness=31.0) == []
        clock["t"] += 5.0
        # fresh tick (condition still holds: in-band ask, deep-OTM p) —
        # clears the episode; cap math returns [] (3 resting = cap)
        assert _eval(engine, spot_staleness=0.5) == []
        clock["t"] += 8.0
        assert _eval(engine, spot_staleness=31.0) == []
        engine._client.cancel_order.assert_not_called()
        assert engine.resting_count() == 1

    def test_twaplock_untouched(self):
        """Twaplock is a TAKER (IOC) overlay — it rests no quotes, so its
        abstain-only stale gate (TWAPLOCK_SPOT_STALE) is already the
        complete fix on that side. The grace-cancel machinery must stay
        longshot-only."""
        import inspect
        import bot.twaplock as tw
        src = inspect.getsource(tw)
        assert "LONGSHOT_STALE_CANCEL_GRACE_SECONDS" not in src
        assert '"spot_stale"' not in src


# ── evaluated_opportunities rows ─────────────────────────────────────────────

class TestEvalRows:
    def test_live_row_written(self, engine, state, enabled):
        _eval(engine)
        row = state.conn.execute(
            "SELECT * FROM evaluated_opportunities WHERE filter_stage='longshot_live'"
        ).fetchone()
        assert row is not None
        assert row["ticker"] == TICKER
        assert row["strategy"] == "longshot"
        assert row["side"] == "no"
        assert row["market_price"] == 8

    def test_shadow_row_when_global_shadow(self, engine, state, enabled,
                                           monkeypatch):
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        # shipped override is LIVE since 2026-06-12 — this test pins the
        # NON-override global-shadow path, so force it off
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", False, raising=False)
        cands = _eval(engine)
        # candidate still emitted — the GATE lives at executor.execute()
        assert len(cands) == 1
        row = state.conn.execute(
            "SELECT * FROM evaluated_opportunities WHERE filter_stage='longshot_shadow'"
        ).fetchone()
        assert row is not None
        live = state.conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities WHERE filter_stage='longshot_live'"
        ).fetchone()[0]
        assert live == 0


# ── sizing + collateral caps ─────────────────────────────────────────────────

class TestSizingCaps:
    def test_window_side_cap_full(self, engine, state, enabled):
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "no", 3, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        assert _eval(engine) == []

    def test_window_side_cap_partial(self, engine, state, enabled):
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "no", 2, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        cands = _eval(engine)
        assert len(cands) == 1
        assert cands[0]["position_size"] == 1

    def test_window_side_cap_counts_resting(self, engine, enabled):
        engine.register_resting(
            order_id="oid-r1", client_order_id="c-r1", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)
        assert _eval(engine) == []

    def test_collateral_cap_reduces(self, engine, enabled, monkeypatch):
        # cap $1.00 -> 100c // 92c = 1 contract
        monkeypatch.setattr(
            C, "LONGSHOT_MAX_CONCURRENT_COLLATERAL_DOLLARS", 1.00, raising=False)
        cands = _eval(engine)
        assert len(cands) == 1
        assert cands[0]["position_size"] == 1

    def test_collateral_cap_blocks(self, engine, enabled, monkeypatch):
        monkeypatch.setattr(
            C, "LONGSHOT_MAX_CONCURRENT_COLLATERAL_DOLLARS", 0.50, raising=False)
        assert _eval(engine) == []

    def test_authorize_recheck_blocks_when_cap_consumed(self, engine, state,
                                                        enabled):
        cands = _eval(engine)
        assert len(cands) == 1
        # cap consumed between scan and execute (e.g. sister candidate filled)
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "no", 3, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        assert engine.authorize(cands[0]) == 0

    def test_authorize_passes_when_room(self, engine, enabled):
        cands = _eval(engine)
        assert engine.authorize(cands[0]) == 3


# ── auto-disable rails ───────────────────────────────────────────────────────

class TestAutoDisable:
    def test_daily_loss_cap_disables_same_day(self, engine, state, enabled,
                                              caplog):
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -2100,
                      _today_utc().isoformat())
        with caplog.at_level("WARNING"):
            assert _eval(engine) == []
        assert engine.disabled_reason() == "daily_cap"
        assert "LONGSHOT_DAILY_CAP_HIT" in caplog.text

    def test_daily_loss_under_cap_stays_enabled(self, engine, state, enabled):
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -1900,
                      _today_utc().isoformat())
        assert len(_eval(engine)) == 1

    def test_other_strategy_losses_dont_count(self, engine, state, enabled):
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -5000,
                      _today_utc().isoformat(), strategy="above")
        assert len(_eval(engine)) == 1

    def test_consecutive_losing_days_disable(self, engine, state, enabled,
                                             caplog):
        today = _today_utc()
        for i in (1, 2, 3):
            d = (today - datetime.timedelta(days=i)).isoformat()
            _seed_settled(state, f"KXBTC15M-26JUN{i:02d}0900-T99", -100, d)
        with caplog.at_level("WARNING"):
            assert _eval(engine) == []
        assert engine.disabled_reason() == "consec_days"

    def test_two_losing_days_stays_enabled(self, engine, state, enabled):
        today = _today_utc()
        for i in (1, 2):
            d = (today - datetime.timedelta(days=i)).isoformat()
            _seed_settled(state, f"KXBTC15M-26JUN{i:02d}0900-T99", -100, d)
        assert len(_eval(engine)) == 1

    def test_streak_reset_override_clears_latch(self, engine, state, enabled,
                                                monkeypatch):
        today = _today_utc()
        for i in (1, 2, 3):
            d = (today - datetime.timedelta(days=i)).isoformat()
            _seed_settled(state, f"KXBTC15M-26JUN{i:02d}0900-T99", -100, d)
        monkeypatch.setattr(C, "LIVE_SMALL_STREAK_RESET_UTC_DATE",
                            today.isoformat(), raising=False)
        assert len(_eval(engine)) == 1

    def test_daily_cap_cancels_resting_on_tick(self, engine, state, enabled):
        engine.register_resting(
            order_id="oid-d1", client_order_id="c-d1", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)
        _seed_settled(state, "KXBTC15M-26JUN110900-T99", -2100,
                      _today_utc().isoformat())
        engine.tick()
        engine._client.cancel_order.assert_called_once_with(
            "oid-d1", ticker=TICKER)
        assert engine.resting_count() == 0


# ── quote lifecycle ──────────────────────────────────────────────────────────

class TestQuoteLifecycle:
    def test_t3_cancel_on_tick(self, engine, enabled):
        # registered inside the window, but the clock has run down past T-3min
        engine.register_resting(
            order_id="oid-t3", client_order_id="c-t3", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=170.0)
        engine.tick()
        engine._client.cancel_order.assert_called_once_with(
            "oid-t3", ticker=TICKER)
        assert engine.resting_count() == 0

    def test_no_cancel_inside_window(self, engine, enabled):
        engine.register_resting(
            order_id="oid-ok", client_order_id="c-ok", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)
        engine.tick()
        engine._client.cancel_order.assert_not_called()
        assert engine.resting_count() == 1

    def test_condition_flip_cancels_resting(self, engine, enabled):
        engine.register_resting(
            order_id="oid-cf", client_order_id="c-cf", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)
        # ask moved out of band (20c) -> condition no longer holds -> cancel
        cands = _eval(engine, yes_ask_cents=20)
        assert cands == []
        engine._client.cancel_order.assert_called_once_with(
            "oid-cf", ticker=TICKER)
        assert engine.resting_count() == 0

    def test_kill_switch_flip_cancels_resting_on_tick(self, engine, enabled,
                                                      monkeypatch):
        engine.register_resting(
            order_id="oid-ks", client_order_id="c-ks", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)
        monkeypatch.setattr(C, "LONGSHOT_ENABLED", False, raising=False)
        engine.tick()
        engine._client.cancel_order.assert_called_once_with(
            "oid-ks", ticker=TICKER)
        assert engine.resting_count() == 0

    def test_fill_recorded_as_position(self, engine, state, enabled):
        engine.register_resting(
            order_id="oid-f1", client_order_id="c-f1", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)
        engine._client.get_fills.return_value = {
            "fills": [{"order_id": "oid-f1", "trade_id": "t-1", "count": 2}]}
        engine.tick()
        row = state.conn.execute(
            "SELECT side, count, avg_price_cents, strategy_group FROM positions "
            "WHERE ticker=? AND status='open'", (TICKER,)).fetchone()
        assert row is not None
        assert row["side"] == "no"
        assert row["count"] == 2
        assert row["avg_price_cents"] == 92
        assert row["strategy_group"] == "longshot"
        # partial fill: quote stays resting for the remaining contract
        assert engine.resting_count() == 1

    def test_fill_dedup_by_trade_id(self, engine, state, enabled):
        engine.register_resting(
            order_id="oid-f2", client_order_id="c-f2", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)
        engine._client.get_fills.return_value = {
            "fills": [{"order_id": "oid-f2", "trade_id": "t-x", "count": 1}]}
        engine.tick(now=1000.0)
        engine.tick(now=2000.0)  # same fill returned again — must not double-count
        row = state.conn.execute(
            "SELECT count FROM positions WHERE ticker=? AND status='open'",
            (TICKER,)).fetchone()
        assert row["count"] == 1

    def test_full_fill_removes_resting(self, engine, state, enabled):
        engine.register_resting(
            order_id="oid-f3", client_order_id="c-f3", ticker=TICKER,
            event_ticker=EVENT, asset="BTC", sell_side="yes", buy_side="no",
            buy_price_cents=92, count=3, seconds_to_close=600.0)
        engine._client.get_fills.return_value = {
            "fills": [{"order_id": "oid-f3", "trade_id": "t-3", "count": 3}]}
        engine.tick()
        assert engine.resting_count() == 0
        engine._client.cancel_order.assert_not_called()


# ── executor chokepoint (single live/shadow gate at execute()) ───────────────

class TestExecutorChokepoint:
    """Longshot candidates route through the REAL OrderExecutor.execute().

    The trading-mode gate at the top of execute() (bot/trading_mode.py,
    PR #158) is the SINGLE live/shadow chokepoint — the longshot path must
    sit BELOW it and never duplicate it.
    """

    @pytest.fixture
    def wired(self, state):
        from bot.executor import OrderExecutor
        client = MagicMock()
        client.get_fills.return_value = {"fills": []}
        client.cancel_order.return_value = {"order": {"status": "canceled"}}
        client.place_order.return_value = {"order": {"order_id": "oid-live-1"}}
        engine = LongshotEngine(client, state)
        ml = MagicMock()
        ml.longshot_engine = engine
        executor = OrderExecutor(client, state, MagicMock(),
                                 main_loop=ml, kalshi_feed=None)
        return executor, engine, client

    def test_live_routes_to_post_only_maker(self, wired, enabled):
        executor, engine, client = wired
        cands = _eval(engine)
        assert len(cands) == 1
        result = executor.execute(cands[0])
        assert result is not None
        client.place_order.assert_called_once()
        kwargs = client.place_order.call_args.kwargs
        assert kwargs["ticker"] == TICKER
        assert kwargs["side"] == "no"          # buy NO == sell YES
        assert kwargs["action"] == "buy"
        assert kwargs["count"] == 3
        assert kwargs["post_only"] is True
        assert kwargs["no_price"] == 92        # 100 - 8c sold ask
        assert "yes_price" not in kwargs
        # resting quote registered with the engine for lifecycle management
        assert engine.resting_count() == 1

    def test_global_shadow_places_nothing_even_when_enabled(
            self, wired, enabled, monkeypatch):
        executor, engine, client = wired
        cands = _eval(engine)
        assert len(cands) == 1                  # scanner side still evaluates
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        # shipped override is LIVE since 2026-06-12; this test pins the
        # global-shadow path WITHOUT the override
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", False, raising=False)
        assert executor.execute(cands[0]) is None
        client.place_order.assert_not_called()
        assert engine.resting_count() == 0

    def test_kill_switch_midflight_places_nothing(self, wired, enabled,
                                                  monkeypatch):
        executor, engine, client = wired
        cands = _eval(engine)
        monkeypatch.setattr(C, "LONGSHOT_ENABLED", False, raising=False)
        assert executor.execute(cands[0]) is None
        client.place_order.assert_not_called()

    def test_authorize_recheck_at_execute_blocks(self, wired, state, enabled):
        executor, engine, client = wired
        cands = _eval(engine)
        # cap consumed between scan and execute
        state.record_position_from_fill(
            TICKER, EVENT, "BTC", "no", 3, 92, strategy="longshot",
            is_taker=False, fill_source="longshot_maker")
        assert executor.execute(cands[0]) is None
        client.place_order.assert_not_called()

    def test_override_unvalidated_asset_places_nothing(self, wired, enabled,
                                                       monkeypatch):
        """M1 fix round: LONGSHOT_LIVE_OVERRIDE must not arm assets outside
        LONGSHOT_LIVE_ASSETS at the executor chokepoint — ADA/BCH (Kalshi
        15M series not yet listed; zero corpus windows; T1 shadow
        designation). BNB passes per the 2026-06-12 operator directive.
        feedback_shadow_flag_comprehensive_may10 class."""
        executor, engine, client = wired
        cands = _eval(engine)
        assert len(cands) == 1
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", True, raising=False)
        for shadow_asset, shadow_ticker in (
                ("ADA", "KXADA15M-26JUN111200-T1"),
                ("BCH", "KXBCH15M-26JUN111200-T500")):
            cand = dict(cands[0], ticker=shadow_ticker, asset=shadow_asset)
            assert executor.execute(cand) is None
        client.place_order.assert_not_called()
        # the 7 live-universe assets DO pass strategy_is_live under the
        # override (BNB included per the 2026-06-12 operator directive —
        # see the LONGSHOT_LIVE_ASSETS constants comment)
        for a in ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"):
            assert tm.strategy_is_live("longshot", a) is True, a

    def test_backstop_blocks_ls_order_on_unvalidated_asset(self,
                                                           monkeypatch):
        """M1 fix round: the place_order backstop refuses an ls- order on
        any asset outside LONGSHOT_LIVE_ASSETS even with the override ON."""
        from bot.kalshi_client import KalshiClient
        monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
        monkeypatch.setattr(C, "LONGSHOT_LIVE_OVERRIDE", True, raising=False)
        for shadow_ticker in ("KXADA15M-26JUN111200-T1",
                              "KXBCH15M-26JUN111200-T500"):
            client = MagicMock()
            result = KalshiClient.place_order(
                client, shadow_ticker, "no", "buy", 1, no_price=92,
                client_order_id="ls-x1")
            assert result is None
            client._request.assert_not_called()
        # a validated asset passes through under the override
        client = MagicMock()
        client._request.return_value = {"order": {"order_id": "ok"}}
        KalshiClient.place_order(
            client, "KXBTC15M-26JUN111200-T110", "no", "buy", 1,
            no_price=92, client_order_id="ls-x2")
        client._request.assert_called_once()
