"""OpportunityScanner — Bit 8.1 extraction (2026-05-10).

Evaluates all markets across active windows and returns the best trade
candidate. Filters by time-to-close, one-asset-per-window, probability
pre-filter, orderbook price range, edge threshold, and position sizing.

Path-A++ extraction (NOT byte-for-byte): in-Bit refactor of bot/_impl.py
to relocate the `_TELEGRAM` module-level singleton to bot/notifier.py
(where it logically belongs since Bit 4.2). The laundered-namespace
coupling smell is fixed in-Bit per the modularization strategic goal.
All five of bot/orphan_db_watchdog.py (for the `_alert_orphan_db_holder`
orphan-DB Layer-3 helper + the `detect_orphan_db_holders` lsof-not-found
alert branch — clean leaf relocated from bot/_impl.py at Bit 9.3-ii
REPLACING that slot, 2026-05-10), bot/main_loop.py (MainLoop reads + the
singleton WRITE post-Bit-9.3 — `_telegram_state._TELEGRAM = self.telegram`
in `MainLoop.__init__`), bot/scanner/__init__.py (this module),
bot/executor.py (Bit 9.1, 2026-05-10), and bot/settlement.py (Bit 9.2,
2026-05-10) reach `_TELEGRAM` via the `_telegram_state`
module-attribute access pattern (mirrors Bit 6.3 path-B
`_cal_state._CALIBRATION_ENGINE`); writes by `MainLoop.__init__`
propagate to all readers without alias-import freshness loss.

Cross-class coupling (post-Sprint-9-Bit-9.1):
- OrderExecutor lives in bot.executor (Bit 9.1, 2026-05-10) — direct
  top-level import; the previous `_get_order_executor()` late-binding
  helper retired in this Bit and the `scanner-no-impl-toplevel`
  `.importlinter` contract dropped in the same atomic commit (net
  contracts: 6 → 5).

Mutable-singleton coupling (preserved via aliased module-attribute access):
- `_cal_state._CALIBRATION_ENGINE` / `_cal_state._resolve_cal_engine`
  (Bit 6.3 path-B precedent; module-attribute access preserves mutation
  freshness without late-binding)
- `_telegram_state._TELEGRAM` (Bit 8.1 path-A++; same pattern)

`main_loop=None` constructor arg → 11 self._ml.X sub-attribute accesses
are constructor-injected references (NOT bare-name lookups). Construction
order in `MainLoop.__init__` guarantees the dependencies are populated
before any scanner method runs. Locked by tests/test_scanner_extraction.py.

Re-imported into bot/_impl.py via `from bot.scanner import OpportunityScanner`
so the runtime construction in `MainLoop.__init__` (search "self.scanner = OpportunityScanner")
+ test instantiation sites + 13 OrderExecutor + MainLoop static-method
call sites resolve. Post-Bit-9.3-iii.b (2026-05-11) the `_BotProxy` is retired;
callers reach `OpportunityScanner` via `from bot.scanner import OpportunityScanner`
directly (or `bot.scanner.OpportunityScanner`). The bot/_impl.py re-export
persists as the residual shim until Bit 9.3-iii.c deletes bot/_impl.py.
"""
import datetime
import inspect
import json
import logging
import math
import os
import random
import re
import sys
import threading
import time
from collections import deque
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# scripts/cal_mlp/ on sys.path so `from integration import ...` resolves —
# mirrors bot/_impl.py:15 + bot/state.py.
sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent.parent / "scripts" / "cal_mlp"),
)
from integration import (  # noqa: E402
    _calmlp_predictors,
    annotate_evaluation_async_enqueue as _calmlp_annotate_async,  # mirror bot/_impl.py:67 alias
)

from market_config import get_market_config, validate_market_configs  # noqa: E402
from bot.models import PositionSizer, calculate_fee, calculate_taker_fee, strategy_to_group  # noqa: E402

from bot.constants import (
    BALANCE_CACHE_TTL,
    BRACKET_NO_ASSUMED_PROB,
    BRACKET_NO_ENABLED,
    BRACKET_NO_FIXED_CONTRACTS,
    BRACKET_NO_KILL_THRESHOLD,
    BRACKET_NO_MAX_CONCURRENT,
    BRACKET_NO_MIN_STC,
    BRACKET_NO_YES_MAX,
    BRACKET_NO_YES_MIN,
    BTC_MAX_RISK_PER_TRADE,
    BTC_MIN_ENTRY_PRICE,
    BUFFER_SIZING_ENABLED,
    CONVERGENCE_WINDOW_SECONDS,
    DB_PATH,
    DC_T2_Z2_PHASE1_RISK,
    DECIDED_CONTRACT_MAX_STC,
    DECIDED_CONTRACT_MAX_WINDOW_RISK,
    DECIDED_CONTRACT_MIN_PRICE,
    DECIDED_CONTRACT_RISK,
    DECIDED_CONTRACT_SHADOW,
    DECIDED_CONTRACT_T1B_MIN_PRICE,
    DECIDED_CONTRACT_T2_MAX_PRICE,
    DECIDED_CONTRACT_T2_Z25_RISK,
    DECIDED_CONTRACT_T2_Z2_RISK,
    DECIDED_CONTRACT_Z_T1,
    DECIDED_CONTRACT_Z_T1B,
    DECIDED_CONTRACT_Z_T2,
    DECIDED_CONTRACT_Z_T2_Z2,
    DECIDED_CONTRACT_Z_T2_Z25,
    DECIDED_T1B_ENABLED,
    DECIDED_T1_ENABLED,
    DECIDED_T2_ENABLED,
    DECIDED_T2_Z25_ENABLED,
    DECIDED_T2_Z2_ENABLED,
    DIP_ADDON_ENABLED,
    DIP_ADDON_MAX_TOTAL_RISK,
    DIP_ADDON_MIN_DROP_CENTS,
    DIP_ADDON_MIN_ENTRY_PRICE,
    DIP_ADDON_MIN_STC_REMAINING,
    DIP_ADDON_SHADOW_MODE,
    DOGE_15M_SHADOW,
    ENDGAME_BLEND_PRICE,
    ETH_MAX_RISK_PER_TRADE,
    ETH_MIN_ENTRY_PRICE,
    ETH_SUB80_POSITION_CAP,
    HIGH_PRICE_STC_BLOCK_ENABLED,
    HIGH_PRICE_STC_BLOCK_FILTER_STAGE,
    HOURLY_BANKROLL_FRACTION,
    HOURLY_CONFIG_A_EXCLUDED,
    HOURLY_CONFIG_A_MAX_EDGE,
    HOURLY_CONFIG_B_ASSET,
    HOURLY_CONFIG_B_MAX_PER_WINDOW,
    HOURLY_CONFIG_B_MAX_PRICE,
    HOURLY_CONFIG_B_MIN_PRICE,
    HOURLY_DC_ASSETS,
    HOURLY_DC_ASSUMED_PROB,
    HOURLY_DC_CONTRACTS,
    HOURLY_DC_ENABLED,
    HOURLY_DC_MAX_PRICE,
    HOURLY_DC_MIN_PRICE,
    HOURLY_DC_MIN_SIGMA,
    HOURLY_DC_Z_THRESHOLD,
    HOURLY_FIXED_CONTRACTS,
    HOURLY_KELLY_FRACTION,
    HOURLY_MARKET_BLEND_W,
    HOURLY_MAX_EDGE,
    HOURLY_MAX_POSITIONS_PER_WINDOW,
    HOURLY_MAX_RISK_PER_TRADE,
    HOURLY_MAX_SECONDS_BEFORE_CLOSE,
    HOURLY_MAX_STC_ENTRY,
    HOURLY_MIN_EDGE_PCT,
    HOURLY_MIN_ENTRY_PRICE,
    HOURLY_MIN_STC_ENTRY,
    HOURLY_NO_EXCLUDED_ASSETS,
    HOURLY_NO_FIXED_CONTRACTS,
    HOURLY_NO_KILL_THRESHOLD,
    HOURLY_NO_MAX_PRICE,
    HOURLY_NO_MIN_PRICE,
    HOURLY_NO_SIDE_LIVE,
    HOURLY_OBSERVATION_ENABLED,
    HOURLY_OBSERVATION_ONLY,
    HOURLY_SERIES_TICKERS,
    HOURLY_SHADOW_CONFIGS,
    HOURLY_TEMPERATURE_T,
    HYPE_15M_SHADOW,
    KALSHI_OFT_SHADOW_MODE,
    LOSS_COOLDOWN_ENABLED,
    LOSS_COOLDOWN_SECONDS,
    LOW_PRICE_SHADOW_ENABLED,
    LOW_PRICE_SHADOW_MAX_PRICE,
    LOW_PRICE_SHADOW_MAX_STC,
    LOW_PRICE_SHADOW_MIN_PRICE,
    LOW_STC_SIZING_CAP,
    LOW_STC_SIZING_CAP_THRESHOLD,
    LPNE_ASSETS,
    LPNE_ENABLED,
    LPNE_FIXED_CONTRACTS,
    LPNE_MAX_CONCURRENT,
    LPNE_MAX_PRICE,
    LPNE_MAX_STC,
    LPNE_MIN_PRICE,
    LPNE_MIN_STC,
    LP_KELLY_FRACTION,
    LP_MAX_RISK_PER_TRADE,
    MAKER_ONLY_THRESHOLD,
    MARKET_BLEND_W,
    MAX_ENTRY_PRICE,
    MAX_OB_FETCHES_PER_TICK,
    MAX_SECONDS_BEFORE_CLOSE,
    MIN_EDGE_PCT,
    MIN_ENTRY_PRICE,
    NO_SIDE_MIN_ENTRY_PRICE,
    OBSERVATION_MODE,
    ONE_ASSET_PER_WINDOW,
    ORDERBOOK_CACHE_TTL,
    OVERNIGHT_DISCOUNT_LIVE,
    OVERNIGHT_DISCOUNT_MAX_STC,
    OVERNIGHT_DISCOUNT_MIN_PRICE,
    OVERNIGHT_EDGE_DISCOUNT,
    OVERNIGHT_LP_HOURS_END,
    OVERNIGHT_LP_HOURS_START,
    OVERNIGHT_LP_KELLY_FRACTION,
    OVERNIGHT_LP_MAX_ENTRY_PRICE,
    OVERNIGHT_LP_MAX_RISK_PER_TRADE,
    OVERNIGHT_LP_MAX_STC,
    OVERNIGHT_LP_MIN_CAL_PROB,
    OVERNIGHT_LP_MIN_EDGE_PCT,
    OVERNIGHT_LP_MIN_ENTRY_PRICE,
    OVERNIGHT_LP_MIN_STC,
    OVERNIGHT_LP_SHADOW,
    OVERNIGHT_LP_VOL_HISTORY_DAYS,
    OVERNIGHT_LP_VOL_SPIKE_MULT,
    OVERNIGHT_QUIET_END,
    OVERNIGHT_QUIET_START,
    PRICE_SHADOW_ENABLED,
    PRICE_SHADOW_FLOOR,
    RELAXED_EDGE_DISCOUNT,
    RELAXED_EDGE_MAX_PRICE,
    RELAXED_EDGE_MIN_PRICE,
    RELAXED_EDGE_SHADOW,
    RK_TV_SHADOW_MODE,
    SHADOW_CAL_PIPELINE,
    SOL_DC_RISK_TIERS,
    SOL_HIGH_EDGE_SHADOW,
    SOL_LOW_ENTRY_STC_GATE,
    SOL_MAX_RISK_PER_TRADE,
    SOL_MIN_EDGE,
    SOL_MIN_ENTRY_PRICE,
    SOL_BLEED_V2_BLOCK_FILTER_STAGE,
    SOL_RESCUE_CONTRACT_CAP,
    SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE,
    SPORTS_ENABLED,
    SPORTS_OBSERVATION_ONLY,
    SPX_DC_MAX_PRICE,
    SPX_DC_MIN_PRICE,
    SPX_DC_SHADOW_ENABLED,
    SPX_DC_VALID_DAYS,
    SPX_DC_Z_THRESHOLD,
    SPX_HOURLY_BANKROLL_FRACTION,
    STACKING_ENABLED,
    STC_EXTENDED_BTC_MIN_PRICE,
    STC_EXTENDED_BUFFER_RESCUE,
    STC_EXTENDED_ETH_MIN_PRICE,
    STC_EXTENDED_LIVE_FLOOR,
    STC_EXTENDED_SOL_MIN_PRICE,
    STC_EXTENDED_XRP_MIN_PRICE,
    STC_SHADOW_THRESHOLD,
    STC_SIZING_SCALER_ENABLED,
    STC_SIZING_SCALER_KNEE,
    STRATEGY_MAKER_AGGRESSIVE,
    STRATEGY_MAKER_PATIENT,
    STRATEGY_PANIC_CAPTURE,
    STRATEGY_TAKER_NOW,
    STRATEGY_WAIT,
    TERMINAL_MOMENTUM_ENABLED,
    TM96_CALMLP_GATE_ENABLED,
    TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE,
    TM_MAX_CONCURRENT,
    TM_MAX_STC,
    TM_MIN_PROB,
    TM_MIN_STC,
    TM_NBBO_BLOCKED_PRICES,
    TM_NBBO_MIN_BUFFER_PCT,
    TM_PRICE_SET,
    TM_STC_DANGER_HI,
    TM_STC_SAFE_THRESHOLD,
    TM_SWEEP_LIVE_ENABLED,
    WEATHER_MIN_EDGE_PCT,
    WEATHER_NO_ASSUMED_PROB,
    WEATHER_NO_CONTRACT_COUNT,
    WEATHER_NO_KILL_THRESHOLD,
    WEATHER_NO_MAX_PRICE,
    WEATHER_NO_MIN_PRICE,
    WEATHER_NO_SHADOW_MIN_YES_PROB,
    WEATHER_NO_SIDE_LIVE,
    WEATHER_NO_SIDE_MIN_STC,
    WEATHER_SHADOW_CONFIGS,
    WEEKEND_DISCOUNT_LIVE,
    WEEKEND_DISCOUNT_MAX_STC,
    WEEKEND_DISCOUNT_MIN_PRICE,
    WEEKEND_EDGE_DISCOUNT,
    WEEKEND_EDGE_FLOOR,
    WEEKEND_FIXED_RISK,
    XRP_15M_SHADOW,
    XRP_MAX_RISK_PER_TRADE,
    XRP_MIN_ENTRY_PRICE,
    XRP_SHADOW_MIN_PRICE,
)
from config import (
    ASSETS,
    DRAWDOWN_HALF_THRESHOLD,
    DRAWDOWN_HALT_THRESHOLD,
    DRAWDOWN_QUARTER_THRESHOLD,
    EGARCH_BLEND_SHADOW_MODE,
    MAX_RISK_PER_TRADE,
    NUMERICAL_SAFETY_CEILING,
    SIZING_TIERS,
)
from bot.helpers import (
    best_yes_ask_cents,
    buffer_sizing_multiplier,
    convert_orderbook_fp,
    dollars_str_to_cents,
    evaluate_execution_strategy,
    get_min_edge,
    should_block_high_price_stc_candidate,
    should_block_sol_bleed_v2_candidate,
    should_block_sol_taker_lowprice_bleed_candidate,
    should_block_tm98_highprice_bleed_candidate,
    should_exclude_weather_no_ticker,
    tm_compute_contracts,
)
from bot.kalshi_client import KalshiClient
from bot.state import StateManager
from bot.feeds import CoinbaseFeed
from bot.engines import VolatilityEngine, ProbabilityEngine
from bot.engines import calibration as _cal_state  # Bit 6.3 path-B alias
from bot.logger import Logger
import bot.notifier as _telegram_state  # Bit 8.1 path-A++ alias — explicit submodule import bypasses _BotProxy.__getattr__ (the `from bot import notifier` form would go through the proxy and trigger a circular `import bot._impl`)
from bot.db_writer_registry import tracked_write


from bot.executor import OrderExecutor  # Bit 9.1 (2026-05-10): direct top-level import — replaces the `_get_order_executor()` late-binding helper retired here. Works because bot.executor breaks the cycle from its side via a `_get_opportunity_scanner()` method-body helper (bot.executor has NO top-level bot.scanner import). The `scanner-no-impl-toplevel` `.importlinter` contract dropped in the same atomic commit (net contracts: 6 → 5).
from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker  # Bit 9.3.5 (2026-05-10): direct top-level import unquotes the Optional["OrderFlowEngine"] / Optional["KalshiOrderFlowTracker"] forward-refs below. Safe — bot.order_flow has zero bot.scanner edges (clean leaf, stdlib + bot.constants only).


class OpportunityScanner:
    """Evaluate all markets across active windows and return the best trade candidate.

    Filters by: time-to-close, one-asset-per-window, probability pre-filter,
    orderbook price range, edge threshold, and position sizing.
    """

    def __init__(self, client: KalshiClient, state: StateManager,
                 feed: CoinbaseFeed, vol: VolatilityEngine, logger: Logger,
                 sizer: PositionSizer,
                 # Bit 9.3.5 (2026-05-10): both classes live in bot/order_flow.py
                 # post-extraction. The annotations are UNQUOTED post-Bit-9.3.5 —
                 # bot.order_flow is a clean leaf (stdlib + bot.constants only)
                 # and has zero bot.scanner edges, so the top-level
                 # `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker`
                 # above resolves cleanly at scanner load time.
                 order_flow: Optional[OrderFlowEngine] = None,
                 kalshi_oft: Optional[KalshiOrderFlowTracker] = None,
                 kalshi_feed=None, main_loop=None):
        self._client = client
        self._state = state
        self._feed = feed
        self._vol = vol
        self._logger = logger
        self._sizer = sizer
        self._order_flow = order_flow
        self._kalshi_oft = kalshi_oft
        self._kalshi_feed = kalshi_feed
        self._ml = main_loop
        # Orderbook cache: ticker -> (data, fetch_time)
        self._ob_cache: Dict[str, Tuple[Optional[Dict], float]] = {}
        # WS-bypass cooldown: ticker -> expiry_unix_ts. During cooldown,
        # _get_orderbook_cached skips WS and goes straight to REST. Set by
        # flag_ticker_drifted when scan silent-bails. Fix #1a from
        # kb/failures/ws-cache-drift-silent-scan-2026-04-24.md.
        self._ws_drift_cooldown: Dict[str, float] = {}
        # Balance cache: (balance_cents, fetch_time)
        self._balance_cache: Tuple[Optional[int], float] = (None, 0.0)
        # Scan stats from last scan() call
        self._last_scan_stats: Optional[Dict] = None
        # Session-level counters for dashboard
        self._session_strategy_counts: Dict[str, int] = {
            STRATEGY_WAIT: 0, STRATEGY_MAKER_PATIENT: 0,
            STRATEGY_MAKER_AGGRESSIVE: 0, STRATEGY_TAKER_NOW: 0,
            STRATEGY_PANIC_CAPTURE: 0,
        }
        self._session_asset_perf: Dict[str, Dict[str, int]] = {
            a: {"opportunities_found": 0, "times_selected": 0, "times_rejected": 0}
            for a in ASSETS
        }
        self._session_total_scanned: int = 0
        self._session_total_candidates: int = 0
        self._last_opportunity_ts: Optional[str] = None
        self._recent_opportunities: deque = deque(maxlen=20)
        self._ticker_ask_history: Dict[str, deque] = {}
        self._eval_opp_seen: set = set()  # 2-tuples (ticker, stage) or 3-tuples (ticker, stage, side)
        # Scan-productivity heartbeat — updated each tick when scan() actually
        # iterates a 15M window body. Watchdog reads this instead of DB row
        # count: dedup at insert sites can suppress writes for a window's
        # entire 15-min lifetime once each (ticker, stage) tuple is seen,
        # producing watchdog false positives even though scan is healthy.
        # See ws-cache-drift-silent-scan-2026-04-24 PM Apr 24 incident #3.
        self._scan_15m_iter_heartbeat_ts: float = 0.0
        self._dc_skip_cooldown: Dict[str, float] = {}  # ticker → expiry timestamp (60s after "no asks" skip)
        self._shadow_cal_last_log: Dict[str, float] = {}
        # Hourly per-window tracking (reset each scan tick)
        self._hourly_window_counts: Dict[str, int] = {}
        self._hourly_window_risk: Dict[str, float] = {}

        # Overnight LP shadow: per-asset vol history for circuit breaker
        # Stores (timestamp, blended_rv) tuples during overnight hours
        # ~8640 obs/night at 5s ticks × 7 days ≈ 60K max
        self._overnight_rv_history: Dict[str, deque] = {
            a: deque(maxlen=70000) for a in ASSETS
        }
        self._overnight_lp_vol_skip_count: int = 0  # session counter for dashboard

        # Low-price shadow: per-window and per-hour correlation tracking (reset each tick)
        self._lp_window_counts: Dict[str, int] = {}
        self._lp_hour_signals: Dict[str, int] = {}  # key = "HH" UTC hour string

        # ── Phase 2 extended feature state ──
        # Per-ticker window state: {ticker: {first_above_since, max_buf, min_buf,
        #   crossings_deque, spot_at_open, last_above_strike}}.
        # Bounded: cleaned up when ticker is beyond scan window (hook in scan()).
        # Max memory: ~100 tickers × ~200 bytes = 20KB.
        self._window_states: Dict[str, Dict[str, Any]] = {}
        # Bot state cache: 1-min TTL to avoid SQL contention on every insert.
        # Keyed by asset for active_positions / bot_pnl / drawdown / ioc_fill_rate.
        self._bot_state_cache: Dict[str, Any] = {"ts": 0.0, "features_by_asset": {}}
        # WS vs REST drift probe (H-NEW diagnostic). Once per minute, pick a
        # random subscribed 15M ticker, fetch REST /orderbook depth=100, diff
        # against WS cache. Measures the cache-vs-truth gap that produces
        # WS delta underflow warnings. Observation-only; no state mutation.
        # See kb/failures/kalshi-ws-schema-drift.md § "WS delta underflow".
        self._drift_probe_last_run: float = 0.0
        # Attach the extended feature provider callback to StateManager so
        # insert_evaluated_opportunity auto-populates Tier 1/2/3/6 for 15M rows.
        self._state._extended_feature_provider = self._get_extended_features_for_ticker

        # ── Startup assertion: _shadow_diag keys must be accepted by DB insert fns ──
        # Prevents the bug class where a new key in _shadow_diag causes a crash
        # at every **_shadow_diag splat into insert_rejection/insert_evaluated_opportunity.
        _SHADOW_DIAG_KEYS = {
            "egarch_sigma", "egarch_blend_sigma", "egarch_blend_weight",
            "mz_r_squared", "shadow_tv_blend_rv", "mz_shadow_sigmoid_w",
            "mz_baseline_qlike", "mz_qlike", "no_ask_cents",
            # Phase 7 cal_mlp audit fields (added by Edit 4 hook to _shadow_diag).
            # insert_rejection's signature does NOT receive cal_mlp_* params,
            # so the assertion only enforces acceptance on
            # insert_evaluated_opportunity. Phase D (2026-05-02) moved the
            # hook earlier in the scan iteration; the one post-annotate
            # insert_rejection site (tradeable_false with_market) strips
            # cal_mlp_* inline at its splat to satisfy this constraint.
        }
        _SHADOW_DIAG_KEYS_EVAL_OPP_ONLY = _SHADOW_DIAG_KEYS | {
            "cal_mlp_p_mean", "cal_mlp_p_std", "cal_mlp_final_lo",
            "cal_mlp_final_hi", "cal_mlp_train_id", "cal_mlp_skipped_reason",
            "cal_mlp_request_id",  # R-p7-deploy-r8: async-predict uuid
        }
        for _fn_name, _fn, _expected in [
            ("insert_rejection", self._state.insert_rejection, _SHADOW_DIAG_KEYS),
            ("insert_evaluated_opportunity", self._state.insert_evaluated_opportunity, _SHADOW_DIAG_KEYS_EVAL_OPP_ONLY),
        ]:
            _accepted = set(inspect.signature(_fn).parameters.keys())
            _unknown = _expected - _accepted
            assert not _unknown, (
                f"_shadow_diag keys {_unknown} not accepted by {_fn_name}(). "
                f"Add them to the function signature + SQL or remove from _shadow_diag."
            )

        # ── Startup assertion: DB busy_timeout must be set ──
        # Prevents the bug class where a new sqlite3.connect() call forgets
        # PRAGMA busy_timeout, causing "database is locked" under contention.
        # (Learned: sports_engine.py missing busy_timeout → ~2000 errors/8hr, Mar 2 2026)
        _bt = self._state.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert _bt >= 5000, (
            f"StateManager busy_timeout={_bt}ms is too low (need ≥5000). "
            f"Add: conn.execute('PRAGMA busy_timeout=30000')"
        )

        # ── Startup assertion: critical config values ──
        assert MARKET_BLEND_W == 0.40, f"MARKET_BLEND_W misconfigured: {MARKET_BLEND_W}"
        assert SHADOW_CAL_PIPELINE is True, "SHADOW_CAL_PIPELINE should be True"
        assert MAX_RISK_PER_TRADE == 0.25, f"MAX_RISK_PER_TRADE misconfigured: {MAX_RISK_PER_TRADE}"
        assert XRP_MAX_RISK_PER_TRADE <= MAX_RISK_PER_TRADE, (
            f"XRP risk {XRP_MAX_RISK_PER_TRADE} > 15M risk {MAX_RISK_PER_TRADE}")
        assert XRP_MAX_RISK_PER_TRADE >= 0.05, f"XRP_MAX_RISK_PER_TRADE too low: {XRP_MAX_RISK_PER_TRADE}"
        assert BTC_MAX_RISK_PER_TRADE <= MAX_RISK_PER_TRADE, (
            f"BTC risk {BTC_MAX_RISK_PER_TRADE} > 15M risk {MAX_RISK_PER_TRADE}")
        assert BTC_MAX_RISK_PER_TRADE >= 0.05, f"BTC_MAX_RISK_PER_TRADE too low: {BTC_MAX_RISK_PER_TRADE}"
        logging.info(
            "CONFIG_VERIFY: MARKET_BLEND_W=%.2f SHADOW_CAL_PIPELINE=%s "
            "MAX_RISK=%s XRP_MAX_RISK=%s BTC_MAX_RISK=%s XRP_15M_SHADOW=%s SIZING_TIERS=%s DRAWDOWN_HALF=%.2f DRAWDOWN_QUARTER=%.2f "
            "DRAWDOWN_HALT=%.2f MAKER_ONLY_THRESHOLD=%.0f",
            MARKET_BLEND_W, SHADOW_CAL_PIPELINE, MAX_RISK_PER_TRADE, XRP_MAX_RISK_PER_TRADE, BTC_MAX_RISK_PER_TRADE,
            XRP_15M_SHADOW, SIZING_TIERS, DRAWDOWN_HALF_THRESHOLD, DRAWDOWN_QUARTER_THRESHOLD,
            DRAWDOWN_HALT_THRESHOLD, MAKER_ONLY_THRESHOLD)

        # ── Hourly config verify ──
        if HOURLY_OBSERVATION_ENABLED:
            assert HOURLY_MARKET_BLEND_W >= 0.30, (
                f"HOURLY_MARKET_BLEND_W={HOURLY_MARKET_BLEND_W} too low")
            assert HOURLY_MIN_ENTRY_PRICE >= 50, (
                f"HOURLY_MIN_ENTRY_PRICE={HOURLY_MIN_ENTRY_PRICE} too low")
            assert HOURLY_MIN_ENTRY_PRICE <= MIN_ENTRY_PRICE, (
                f"HOURLY floor {HOURLY_MIN_ENTRY_PRICE} > 15M floor {MIN_ENTRY_PRICE}")
            assert HOURLY_MAX_RISK_PER_TRADE <= MAX_RISK_PER_TRADE, (
                f"HOURLY risk {HOURLY_MAX_RISK_PER_TRADE} > 15M risk {MAX_RISK_PER_TRADE}")
            assert HOURLY_TEMPERATURE_T > 0, "HOURLY_TEMPERATURE_T must be positive"
            assert 0 < HOURLY_KELLY_FRACTION <= 1.0, "HOURLY_KELLY_FRACTION must be in (0, 1]"
            assert HOURLY_MIN_STC_ENTRY < HOURLY_MAX_STC_ENTRY, "STC entry window invalid"
            assert HOURLY_MAX_POSITIONS_PER_WINDOW >= 1, "Must allow at least 1 position per window"
            logging.info(
                "CONFIG_VERIFY (hourly): ENABLED=%s OBS_ONLY=%s BLEND_W=%.2f "
                "MIN_ENTRY=%dc MAX_RISK=%.2f MAX_STC=%ds MIN_STC=%ds "
                "TEMP_T=%.2f KELLY_F=%.2f MIN_EDGE=%.3f%%",
                HOURLY_OBSERVATION_ENABLED, HOURLY_OBSERVATION_ONLY,
                HOURLY_MARKET_BLEND_W, HOURLY_MIN_ENTRY_PRICE,
                HOURLY_MAX_RISK_PER_TRADE, HOURLY_MAX_SECONDS_BEFORE_CLOSE,
                HOURLY_MIN_STC_ENTRY,
                HOURLY_TEMPERATURE_T, HOURLY_KELLY_FRACTION,
                HOURLY_MIN_EDGE_PCT * 100)

        # ── Dip addon config verify ──
        if DIP_ADDON_ENABLED:
            assert DIP_ADDON_MIN_DROP_CENTS >= 2, "dip addon drop too small"
            assert DIP_ADDON_MAX_TOTAL_RISK <= MAX_RISK_PER_TRADE * 2, (
                "dip addon risk too high")
            logging.info(
                "CONFIG_VERIFY (dip_addon): ENABLED=%s SHADOW=%s "
                "DROP=%dc STC=%.0fs RISK=%.2f FLOOR=%dc",
                DIP_ADDON_ENABLED, DIP_ADDON_SHADOW_MODE,
                DIP_ADDON_MIN_DROP_CENTS, DIP_ADDON_MIN_STC_REMAINING,
                DIP_ADDON_MAX_TOTAL_RISK, DIP_ADDON_MIN_ENTRY_PRICE)

        # ── Sports config verify ──
        if SPORTS_ENABLED:
            assert SPORTS_OBSERVATION_ONLY is True, (
                "SPORTS_OBSERVATION_ONLY must be True — never live without explicit promotion")
            logging.info(
                "CONFIG_VERIFY (sports): ENABLED=%s OBS_ONLY=%s",
                SPORTS_ENABLED, SPORTS_OBSERVATION_ONLY)

        # ── Validate market_config.py matches bot/_impl.py constants ──
        validate_market_configs()

    # ── V2 variant helper (shadow cal pipeline: temperature + no blend) ──

    def _insert_hourly_v2_variant(
        self, ticker: str, window: Dict, asset: str,
        raw_prob: Optional[float], best_ask: int, seconds_remaining: float,
        spot: float, threshold: float, blended_rv: float,
        ofa_adjustment: float, z_score: float, vol_est: Dict,
        calibrated_prob_raw: float, est_fee_1c: float,
        ask_depth: Optional[int], best_ask_source: Optional[str],
        _cf: Dict, _shadow_diag: Dict,
    ):
        """Insert a V2 variant row for hourly signals using the shadow cal pipeline.

        V2 uses temperature scaling + no market blend (vs V1's beta cal + 40% blend).
        Gets its own edge, sizing, and filter_stage for independent PnL simulation.
        Settlement works automatically since it shares the same table + ticker.
        """
        _v2_data = _cf.get("cal_pipeline") or _cf.get("old_cal_system")
        if not _v2_data:
            return
        _v2_prob = _v2_data.get("prob")
        if _v2_prob is None:
            return
        _v2_dedup = (ticker, "hourly_observation_v2")
        if _v2_dedup in self._eval_opp_seen:
            return
        self._eval_opp_seen.add(_v2_dedup)

        _v2_edge = _v2_prob - best_ask / 100.0
        _v2_fee_edge = _v2_data.get("fee_edge")
        if _v2_fee_edge is None:
            _v2_fee_edge = _v2_edge - est_fee_1c / 100.0
        _v2_ev = (_v2_prob * (100 - best_ask)) - ((1 - _v2_prob) * best_ask) - est_fee_1c

        # V2 sizing (Kelly with V2 probability)
        _v2_contracts = 0
        _v2_kelly_f = None
        _v2_drawdown = None
        _v2_balance = self._get_balance_cached()
        if _v2_balance and _v2_balance > 0:
            _v2_sizing = self._sizer.compute(_v2_prob, best_ask, _v2_balance)
            _v2_kelly_f = _v2_sizing["kelly_f"]
            _v2_contracts = _v2_sizing["contracts"]
            _v2_drawdown = _v2_sizing["drawdown_scaler"]
            _v2_scfg = get_market_config("hourly")
            if _v2_scfg.kelly_fraction < 1.0:
                _v2_contracts = max(1, int(_v2_contracts * _v2_scfg.kelly_fraction))
            _v2_max = int((_v2_balance * _v2_scfg.max_risk_per_trade) / best_ask)
            if _v2_contracts > _v2_max:
                _v2_contracts = max(1, _v2_max)

        try:
            self._state.insert_evaluated_opportunity(
                ticker, window["event_ticker"], asset,
                "hourly_observation_v2",
                spot_price=spot, threshold=threshold,
                volatility=blended_rv, market_price=best_ask,
                seconds_to_close=seconds_remaining,
                calibrated_prob=_v2_prob, edge=_v2_edge,
                ofa_adjustment=ofa_adjustment,
                z_score=z_score,
                vol_regime=vol_est["regime"],
                calibrated_prob_raw=calibrated_prob_raw,
                kelly_f=_v2_kelly_f,
                position_size=_v2_contracts,
                drawdown_scaler=_v2_drawdown,
                breakeven_wr=best_ask / 100.0,
                expected_value=round(_v2_ev, 2),
                ask_depth=ask_depth,
                best_ask_source=best_ask_source,
                raw_prob=raw_prob,
                calibration_method="shadow_cal_v2",
                fee_adjusted_edge=_v2_fee_edge,
                product_type="hourly",
                shadow_cal_temperature=_v2_data.get("temperature"),
                **_shadow_diag)
        except Exception:
            logging.warning("insert_evaluated_opportunity failed (hourly_observation_v2)", exc_info=True)

    # ── Phase 2: Extended Feature Computation ─────────────────────────────
    # These methods populate Tier 1/2/3/6 features on the insert callback.
    # Called from self._get_extended_features_for_ticker() which is hooked
    # into StateManager.insert_evaluated_opportunity via _extended_feature_provider.

    def _update_window_state(self, ticker: str, spot: Optional[float],
                              threshold: Optional[float]) -> None:
        """Update per-window spot-path state. Call once per scan tick per ticker.

        Tracks: first_above_strike timestamp, max/min buffer seen, crossings
        in last 5 min, spot at window open, last above/below state.
        """
        if spot is None or threshold is None or threshold <= 0:
            return
        now = time.time()
        state = self._window_states.get(ticker)
        if state is None:
            # Bound dict size — evict oldest if we hit cap
            if len(self._window_states) >= 100:
                oldest = min(self._window_states,
                             key=lambda k: self._window_states[k].get("last_ts", 0))
                self._window_states.pop(oldest, None)
            state = {
                "spot_at_open": spot,
                "first_above_since": None,
                "max_buf": None,
                "min_buf": None,
                "crossings": deque(maxlen=50),  # (timestamp,) per crossing event
                "was_above": None,  # last observed state
                "last_ts": now,
                # Phase F (shadow coverage expansion 2026-05-02): cumulative
                # time-above/below accumulators + threshold for excursion
                # price-unit conversion. See
                # kb/decisions/shadow-coverage-expansion-may01.md.
                "time_above_total_s": 0.0,
                "time_below_total_s": 0.0,
                "threshold": threshold,
                # Phase F-3: window-open timestamp pinned ONCE on creation —
                # never overwritten on subsequent ticks. Used by
                # _compute_knockout_time_relative to normalize "time decided"
                # by total observation window elapsed.
                "window_open_ts": now,
            }
            self._window_states[ticker] = state

        buf_pct = (spot - threshold) / threshold * 100
        is_above = spot >= threshold

        # Update max/min
        if state["max_buf"] is None or buf_pct > state["max_buf"]:
            state["max_buf"] = buf_pct
        if state["min_buf"] is None or buf_pct < state["min_buf"]:
            state["min_buf"] = buf_pct

        # Detect crossings (transition between above/below)
        if state["was_above"] is not None and is_above != state["was_above"]:
            state["crossings"].append(now)

        # Phase F: accumulate time above/below using prior was_above and
        # time delta since last_ts. Skips the first call (was_above=None);
        # subsequent calls add the dt to whichever bucket the spot was in
        # PRIOR to this update.
        if state["was_above"] is not None:
            dt = max(0.0, now - state["last_ts"])
            if state["was_above"]:
                state["time_above_total_s"] += dt
            else:
                state["time_below_total_s"] += dt
        state["was_above"] = is_above

        # Track contiguous time above strike
        if is_above:
            if state["first_above_since"] is None:
                state["first_above_since"] = now
        else:
            state["first_above_since"] = None

        # Phase F: refresh threshold so the excursion calc uses the most
        # current strike (15M strikes are static per window — this is a
        # no-op for the second+ tick — but defensive against future
        # multi-strike paths).
        state["threshold"] = threshold
        state["last_ts"] = now

    def _compute_window_features(self, ticker: str) -> Dict[str, Any]:
        """Tier 1 features from _window_states."""
        state = self._window_states.get(ticker)
        if state is None:
            return {}
        now = time.time()
        minutes_above = None
        if state.get("first_above_since") is not None:
            minutes_above = (now - state["first_above_since"]) / 60.0
        cutoff = now - 300  # 5 min
        recent_crossings = sum(1 for t in state["crossings"] if t >= cutoff)
        # Phase F (shadow coverage expansion 2026-05-02): resolution
        # metadata. max_excursion is signed price (max above wins if its
        # |buf_pct| > |min_buf|; otherwise min_buf wins with negative sign).
        # See kb/decisions/shadow-coverage-expansion-may01.md.
        max_excursion_from_strike = None
        max_buf = state.get("max_buf")
        min_buf = state.get("min_buf")
        threshold = state.get("threshold")
        if threshold is not None and threshold > 0 and (
            max_buf is not None or min_buf is not None
        ):
            mb = max_buf if max_buf is not None else 0.0
            nb = min_buf if min_buf is not None else 0.0
            picked = mb if abs(mb) >= abs(nb) else nb
            max_excursion_from_strike = picked * threshold / 100.0
        return {
            "minutes_above_strike": minutes_above,
            "window_max_buf_pct": max_buf,
            "window_min_buf_pct": min_buf,
            "recent_crossings_5m": recent_crossings,
            "spot_at_window_open": state.get("spot_at_open"),
            "time_above_strike_seconds": state.get("time_above_total_s"),
            "time_below_strike_seconds": state.get("time_below_total_s"),
            "max_excursion_from_strike": max_excursion_from_strike,
            # Phase F-3: decision-time approximation of knockout-time-relative.
            "knockout_time_relative": self._compute_knockout_time_relative(state, now),
        }

    def _compute_momentum_features(self, asset: str) -> Dict[str, Any]:
        """Tier 2: spot momentum from CoinbaseFeed buffer."""
        try:
            buf = self._feed.get_buffer(asset)
        except Exception:
            return {}
        if not buf or len(buf) < 2:
            return {}
        now = time.time()
        current_price = buf[-1][1]

        def _price_at(seconds_ago: float) -> Optional[float]:
            """Find closest buffer entry to (now - seconds_ago)."""
            target = now - seconds_ago
            best = None
            best_dt = float("inf")
            for ts, price in buf:
                if ts > now:
                    continue
                dt = abs(ts - target)
                if dt < best_dt:
                    best_dt = dt
                    best = price
                if ts >= target:  # passed target, early exit
                    break
            # Tolerate up to 30s mismatch on lookup
            if best_dt > 30:
                return None
            return best

        def _pct_bps(current: Optional[float], past: Optional[float]) -> Optional[float]:
            if current is None or past is None or past == 0:
                return None
            return (current - past) / past * 10000.0

        mom_60s = _pct_bps(current_price, _price_at(60))
        mom_5m = _pct_bps(current_price, _price_at(300))

        # 15-min range: iterate buffer once
        cutoff_15m = now - 900
        prices_15m = [p for t, p in buf if t >= cutoff_15m]
        range_15m_bps = None
        if len(prices_15m) >= 2:
            hi = max(prices_15m)
            lo = min(prices_15m)
            mid = (hi + lo) / 2
            if mid > 0:
                range_15m_bps = (hi - lo) / mid * 10000.0

        return {
            "spot_momentum_60s_bps": mom_60s,
            "spot_momentum_5m_bps": mom_5m,
            "spot_realized_range_15m_bps": range_15m_bps,
        }

    def _compute_cross_asset_features(self, asset: str) -> Dict[str, Any]:
        """Tier 3: BTC momentum + relative return vs BTC.

        For BTC itself, returns BTC self-momentum (still useful). For non-BTC,
        also computes relative return vs BTC over 30m.
        """
        result: Dict[str, Any] = {}
        try:
            btc_buf = self._feed.get_buffer("BTC")
        except Exception:
            return result
        if not btc_buf or len(btc_buf) < 2:
            return result
        now = time.time()
        btc_now = btc_buf[-1][1]

        def _price_at(buf_list, seconds_ago: float) -> Optional[float]:
            target = now - seconds_ago
            best = None
            best_dt = float("inf")
            for ts, price in buf_list:
                if ts > now:
                    continue
                dt = abs(ts - target)
                if dt < best_dt:
                    best_dt = dt
                    best = price
            if best_dt > 60:
                return None
            return best

        def _pct_bps(cur, past):
            if cur is None or past is None or past == 0:
                return None
            return (cur - past) / past * 10000.0

        btc_30m_ago = _price_at(btc_buf, 1800)
        btc_5m_ago = _price_at(btc_buf, 300)
        btc_15m_ago = _price_at(btc_buf, 900)
        result["btc_spot_change_30m_bps"] = _pct_bps(btc_now, btc_30m_ago)
        result["btc_spot_change_5m_bps"] = _pct_bps(btc_now, btc_5m_ago)

        # BTC realized vol (15m): std of 60s log returns within the window
        try:
            cutoff = now - 900
            recent = [(t, p) for t, p in btc_buf if t >= cutoff]
            if len(recent) >= 10:
                # Sample at ~60s intervals by taking every Nth
                step = max(1, len(recent) // 15)
                sampled = recent[::step]
                import math as _math
                returns = []
                for i in range(1, len(sampled)):
                    p0 = sampled[i - 1][1]
                    p1 = sampled[i][1]
                    if p0 > 0 and p1 > 0:
                        returns.append(_math.log(p1 / p0))
                if len(returns) >= 3:
                    mean = sum(returns) / len(returns)
                    var = sum((r - mean) ** 2 for r in returns) / len(returns)
                    result["btc_realized_vol_15m"] = _math.sqrt(var)
        except Exception:
            pass

        # Relative return: non-BTC assets only
        if asset != "BTC":
            try:
                asset_buf = self._feed.get_buffer(asset)
                if asset_buf and len(asset_buf) >= 2:
                    asset_now = asset_buf[-1][1]
                    asset_30m_ago = _price_at(asset_buf, 1800)
                    asset_ret = _pct_bps(asset_now, asset_30m_ago)
                    btc_ret = result.get("btc_spot_change_30m_bps")
                    if asset_ret is not None and btc_ret is not None:
                        result["sol_btc_relative_return_30m_bps"] = asset_ret - btc_ret
            except Exception:
                pass

        return result

    def _compute_knockout_time_relative(
        self, state: Dict[str, Any], now: float,
    ) -> Optional[float]:
        """Phase F-3 (shadow coverage expansion 2026-05-02): approximate
        decision-time knockout-time-relative from a window state.

        Formula: (now - last_crossing_ts) / (now - window_open_ts).
        Bounded [0, 1]. Higher = market has been on one side longer
        relative to total observation window.
          - 1.0 = no crossings since window open (perfectly decided)
          - ~0.0 = crossing JUST happened (still in flux)
          - intermediate = X% of window-elapsed has been "decided"

        Returns None if `window_open_ts` is missing (pre-Phase-F-3 state)
        or in the future (clock skew).

        DEFERRED to a Phase F-3b: a settlement-time backfill via
        SettlementTracker would produce a more accurate value relative to
        the FULL window (including post-decision-tick activity). Phase F-3
        ships the decision-tick approximation. Master plan:
        kb/decisions/shadow-coverage-expansion-may01.md.
        """
        window_open_ts = state.get("window_open_ts")
        if window_open_ts is None:
            return None
        elapsed = now - window_open_ts
        if elapsed <= 0:
            return None
        crossings = state.get("crossings") or ()
        # Pull the most recent crossing — `crossings` is a deque appended
        # in chronological order, so [-1] is the latest.
        try:
            last_crossing = crossings[-1] if len(crossings) > 0 else None
        except Exception:
            last_crossing = None
        if last_crossing is None:
            # Never crossed since window open → fully decided since open.
            return 1.0
        decided_seconds = now - last_crossing
        # Clamp into [0, 1] — last_crossing < window_open_ts shouldn't
        # happen in well-formed state but defends against legacy data.
        return max(0.0, min(1.0, decided_seconds / elapsed))

    @staticmethod
    def _compute_maker_counterfactual(
        best_yes_bid: Optional[int],
        best_yes_ask: Optional[int],
        ladder_json: Optional[str],
    ) -> Dict[str, Any]:
        """Phase F-2 (shadow coverage expansion 2026-05-02): snapshot half
        of the maker-counterfactual fields. Computes:

          - `maker_price_cents` = best_yes_bid + 1 (a "1-cent improve"
            maker post that becomes the new top of book). NULL if either
            best_yes_bid or best_yes_ask is unavailable, OR if the implied
            maker price would cross the ask (in which case a "maker post"
            is mathematically a taker — better to mark NULL than mislabel).

          - `maker_depth_at_post` = sum of YES-bid quantities at the
            target maker price level in the current ladder. 0 if no level
            (typical for an improve maker) or ladder unparseable; ladder
            None / non-JSON also returns 0 (the conservative answer for
            "level didn't exist" — distinct from NULL price which signals
            "the question itself doesn't apply").

        DEFERRED to a future Phase F-2b: `maker_would_fill_within_30s`
        requires a post-hoc fillability daemon tracking ask depletion +
        trade prints over 30s. Master plan:
        kb/decisions/shadow-coverage-expansion-may01.md.
        """
        out: Dict[str, Any] = {
            "maker_price_cents": None,
            "maker_depth_at_post": None,
        }
        if best_yes_bid is None or best_yes_ask is None:
            return out
        maker_price = best_yes_bid + 1
        if maker_price >= best_yes_ask:
            # Post would cross the ask → not a maker at all.
            return out
        out["maker_price_cents"] = maker_price
        # Depth lookup. Default 0 (the answer for "level not in ladder").
        depth = 0
        if ladder_json:
            try:
                ladder = json.loads(ladder_json)
                for entry in (ladder.get("yes_bids") or []):
                    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        if int(entry[0]) == maker_price:
                            depth += int(entry[1])
            except Exception:
                # Malformed JSON → treat as level-absent (depth 0).
                pass
        out["maker_depth_at_post"] = depth
        return out

    def _compute_cross_asset_spot_snapshot(self) -> Dict[str, Any]:
        """Phase F (shadow coverage expansion 2026-05-02): absolute spot
        snapshot of all 4 crypto assets at the current decision tick.

        Distinct from `_compute_cross_asset_features` (which returns
        relative `*_bps` changes) — these are LEVELS. Used to enable
        cross-asset interaction analysis on shadow rows post-hoc, where
        the relative-change features alone are insufficient (e.g.,
        "what was BTC spot when this XRP signal fired?"). One feed lookup
        per asset (cheap; CoinbaseFeed caches in-memory). Missing prices
        return None — caller will write NULL.

        Master plan: kb/decisions/shadow-coverage-expansion-may01.md.
        """
        # Single get_all_prices() acquires the feed lock once vs 4
        # acquires for per-asset get_price (Phase F adversarial round 2 LOW-2).
        # ASSETS is the module-level constant from config.py (NOT an
        # OpportunityScanner attribute) — Phase F adversarial round 4
        # caught a `self.ASSETS` typo that would have silently NULL'd
        # all 4 cross-asset spot columns in production (the per-helper
        # try/except in _get_extended_features_for_ticker would swallow
        # the AttributeError).
        try:
            prices = self._feed.get_all_prices()
        except Exception:
            prices = {}
        return {f"{a.lower()}_spot_at_decision": prices.get(a) for a in ASSETS}

    def _compute_bot_state_features(self, asset: str) -> Dict[str, Any]:
        """Tier 6: active positions, recent PnL, drawdown, IOC fill rate.

        Cached at 1-min resolution to avoid SQL contention on every insert.
        """
        now = time.time()
        cache = self._bot_state_cache
        if now - cache.get("ts", 0) < 60:
            return cache.get("features_by_asset", {}).get(asset, {})
        try:
            features_by_asset = {}
            conn = self._state.conn
            # Active positions per asset
            pos_rows = conn.execute(
                "SELECT asset, COUNT(*) FROM positions "
                "WHERE status IN ('open', 'pending') GROUP BY asset"
            ).fetchall()
            pos_counts = {r[0]: r[1] for r in pos_rows}

            # Recent PnL last 30m
            pnl_row = conn.execute(
                "SELECT SUM(pnl_cents - COALESCE(fee_cents, 0)) FROM settled_trades "
                "WHERE settled_at > datetime('now', '-30 minutes')"
            ).fetchone()
            recent_pnl = pnl_row[0] if pnl_row and pnl_row[0] is not None else 0

            # Drawdown: current_balance vs _session_hwm_balance if tracked, else 0
            # Use main loop balance tracking if available
            drawdown_pct = 0.0
            if self._ml is not None:
                cur = getattr(self._ml, "_last_known_balance", None)
                hwm = getattr(self._ml, "_session_hwm_balance", None)
                if cur is not None and hwm is not None and hwm > 0:
                    drawdown_pct = max(0.0, (1 - cur / hwm) * 100)

            # IOC fill success rate last 1h: evaluated_opportunities with order_outcome
            ioc_row = conn.execute(
                "SELECT SUM(CASE WHEN order_outcome='filled' THEN 1 ELSE 0 END), COUNT(*) "
                "FROM evaluated_opportunities "
                "WHERE order_submitted_at > datetime('now', '-1 hour') "
                "AND order_outcome IS NOT NULL"
            ).fetchone()
            ioc_rate = None
            if ioc_row and ioc_row[1] and ioc_row[1] > 0:
                ioc_rate = ioc_row[0] / ioc_row[1]

            # Phase E (shadow coverage expansion 2026-05-02): state-at-
            # decision-time fields. All three are GLOBAL (not per-asset)
            # but stored uniformly in the per-asset cache for reuse.
            # Master plan: kb/decisions/shadow-coverage-expansion-may01.md.
            #
            # n_open_positions: total open positions across ALL assets
            # (distinct from active_positions_same_asset which is
            # same-asset only — already exposed above).
            n_open_positions = sum(pos_counts.values())

            # Phase H-2: also publish to MainLoop._open_positions_count_cache
            # so the bot microstate snapshot avoids per-insert SQL hits.
            # 60s cache (this whole block is gated by `now - cache.get('ts',
            # 0) < 60` above) — approximate, NOT decision-tick-exact. v2
            # training treats `open_positions_count` as approximate.
            if self._ml is not None:
                try:
                    self._ml._open_positions_count_cache = int(n_open_positions)
                except (TypeError, ValueError):
                    pass

            # time_since_last_fill_s: seconds since the most recent fill
            # event. Uses MAX across positions.opened_at AND
            # settled_trades.settled_at because (a) `positions` is
            # mutated by reconciliation — bot/_impl.py issues `DELETE FROM
            # positions WHERE ticker=?` when Kalshi REST reports
            # position_count=0 — so a quiet-period reconciliation can
            # drop all rows and turn `MAX(opened_at)` into NULL despite
            # recent activity; (b) `settled_trades` is append-only, so
            # `MAX(settled_at)` survives reconciliation. settled_at
            # slightly UNDERSTATES the actual fill-to-now duration (the
            # fill happened earlier than settlement) but that's a small
            # bias < the position's hold time. NULL only if there has
            # never been a fill OR settlement on this DB.
            tslf_row = conn.execute(
                "SELECT MAX(t) FROM ("
                "  SELECT MAX(opened_at) AS t FROM positions"
                "  UNION ALL"
                "  SELECT MAX(settled_at) AS t FROM settled_trades"
                ") WHERE t IS NOT NULL"
            ).fetchone()
            time_since_last_fill_s = None
            if tslf_row and tslf_row[0] is not None:
                # Compute via julianday so SQLite's ISO-8601 parser handles
                # the same timestamp formats (`datetime('now')` writes UTC).
                tslf_calc = conn.execute(
                    "SELECT (julianday('now') - julianday(?)) * 86400.0",
                    (tslf_row[0],),
                ).fetchone()
                if tslf_calc and tslf_calc[0] is not None:
                    # max(0,) defends against clock skew producing tiny negatives.
                    time_since_last_fill_s = max(0.0, float(tslf_calc[0]))

            # recent_n_outcome_streak: signed streak count from the most
            # recent settlement. Outcome semantics on net PnL
            # (pnl_cents - COALESCE(fee_cents,0)):
            #   net > 0 → WIN  (contributes +1)
            #   net < 0 → LOSS (contributes -1)
            #   net = 0 → PUSH (breaks streak; contributes 0)
            # Returns: positive int = consecutive wins, negative int =
            # consecutive losses, 0 = no trades OR most-recent is a push.
            # Bound to last 50 trades to limit sort cost (no index on
            # settled_at). 50 covers any operationally-relevant streak;
            # streaks longer than 50 would warrant a dedicated "regime
            # alert" signal, not just a count.
            streak = 0
            try:
                streak_rows = conn.execute(
                    "SELECT pnl_cents - COALESCE(fee_cents, 0) AS net_pnl "
                    "FROM settled_trades "
                    "ORDER BY settled_at DESC LIMIT 50"
                ).fetchall()
                if streak_rows:
                    first_net = streak_rows[0][0] or 0
                    if first_net != 0:
                        sign = 1 if first_net > 0 else -1
                        for r in streak_rows:
                            net = r[0] or 0
                            if (sign > 0 and net > 0) or (sign < 0 and net < 0):
                                streak += sign
                            else:
                                # PUSH (net=0) or sign change → break.
                                break
            except Exception:
                pass

            for a in ASSETS:
                features_by_asset[a] = {
                    "active_positions_same_asset": pos_counts.get(a, 0),
                    "recent_bot_pnl_30m_cents": int(recent_pnl),
                    "current_drawdown_pct": drawdown_pct,
                    "recent_ioc_fill_success_rate_1h": ioc_rate,
                    "n_open_positions": n_open_positions,
                    "time_since_last_fill_s": time_since_last_fill_s,
                    "recent_n_outcome_streak": streak,
                }
            cache["features_by_asset"] = features_by_asset
            cache["ts"] = now
            return features_by_asset.get(asset, {})
        except Exception as e:
            logging.debug("bot_state_features compute failed: %s", e)
            return {}

    def _get_extended_features_for_ticker(
        self, ticker: str, asset: Optional[str],
        spot_price: Optional[float], threshold: Optional[float],
        product_type: Optional[str],
    ) -> Dict[str, Any]:
        """Provider callback for StateManager.insert_evaluated_opportunity.

        Returns Tier 1/2/3/6 feature dict for 15M rows only. Fast (no SQL
        except for bot state which is cached at 1-min). Must not raise.
        """
        if product_type not in (None, "15m"):
            return {}
        if asset is None:
            return {}
        out: Dict[str, Any] = {}
        # Phase F (shadow coverage expansion 2026-05-02): each helper
        # gets its own try/except so a failure in one (e.g. bot state
        # SQL hits a transient DB lock) doesn't drop the others. Pre-
        # Phase-F a single shared try/except meant a bot_state DB
        # error wiped all 4 helpers' output. See
        # kb/decisions/shadow-coverage-expansion-may01.md.
        for _helper in (
            lambda: self._compute_window_features(ticker),
            lambda: self._compute_momentum_features(asset),
            lambda: self._compute_cross_asset_features(asset),
            lambda: self._compute_bot_state_features(asset),
            lambda: self._compute_cross_asset_spot_snapshot(),
        ):
            try:
                out.update(_helper())
            except Exception as e:
                logging.debug("extended_features helper failed for %s: %s", ticker, e)
        return out

    # ── Public entry point ────────────────────────────────────────────────

    def scan(self, active_windows: List[Dict]) -> Optional[List[Dict]]:
        """Evaluate all windows/markets, return best candidate or None."""
        now = time.time()
        _scan_tick_start_perf = time.perf_counter()
        # Phase H-2: per-tick monotonic counter + scan-loop-start pin for
        # the bot microstate snapshot. These attributes live on MainLoop
        # (this method runs in OpportunityScanner; `self._ml` is the
        # MainLoop ref). Round-1 wiring review caught this — writing to
        # `self._scan_iter` here would AttributeError on every tick.
        if self._ml is not None:
            try:
                self._ml._scan_iter += 1
                self._ml._scan_loop_start = _scan_tick_start_perf
            except Exception:
                # Defensive: a missing attr or non-int would not crash scan
                # (insert_evaluated_opportunity already tolerates None).
                pass
        ob_fetches_this_tick = 0
        candidates: List[Dict] = []

        # WS vs REST drift probe (self-throttles to 60s cadence). See
        # _drift_probe_tick docstring and kb/failures/kalshi-ws-schema-drift.md.
        try:
            self._drift_probe_tick()
        except Exception:
            logging.debug("drift probe failed", exc_info=True)

        # 15M silence watchdog (2026-04-24 17:30 UTC outage defense).
        # If no 15M evaluation has been inserted in 10+ min, alert on
        # Telegram. Self-throttled to one alert per 10 min via dedup_key.
        # See kb/failures/ws-15m-silence-2026-04-24.md.
        try:
            self._check_15m_silence_alert(active_windows)
        except Exception:
            logging.debug("15M silence alert check failed", exc_info=True)

        # Slow-tick instrumentation — log when gap between consecutive
        # scan() entries exceeds 2s. Diagnoses event-loop / main-thread
        # stalls (PM Fix 5 deferred — clock_drift_detected pattern).
        _prev_tick_start_perf = getattr(self, "_last_tick_start_perf", None)
        if _prev_tick_start_perf is not None:
            _gap = _scan_tick_start_perf - _prev_tick_start_perf
            if _gap > 2.0:
                logging.warning(
                    "SLOW_SCAN_TICK: %.2fs since previous tick start "
                    "(suggests main-thread stall — see PM Fix 5)", _gap)
        self._last_tick_start_perf = _scan_tick_start_perf

        # Scan-productive watchdog (fix #3 — 2026-04-24 22:12 UTC defense).
        # Checks whether the PREVIOUS tick wrote any 15m DB rows OR
        # iterated a 15M window body (heartbeat). Fires Telegram after
        # 5 consecutive silent-bail ticks (~2.5 min). See
        # kb/failures/ws-cache-drift-silent-scan-2026-04-24.md.
        _prev_tick_ts = getattr(self, "_last_tick_start_iso", None)
        _now_iso = datetime.datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        self._last_tick_start_iso = _now_iso
        if _prev_tick_ts is not None:
            try:
                self._check_scan_productive_15m(active_windows, _prev_tick_ts)
            except Exception:
                logging.debug(
                    "scan-productive watchdog check failed", exc_info=True)

        # Reset hourly per-window tracking each tick, seeded from existing positions
        self._hourly_window_counts = {}
        self._hourly_window_risk = {}
        self._config_b_window_counts = {}  # Config B (BTC 70-89c wl2) separate counter
        self._dc_window_risk = {}  # Decided contract per-window risk tracker
        self._dc_window_cap_skips = 0  # Session counter for window cap skips
        self._lp_window_counts = {}  # Low-price shadow: per-window signal count
        # Weather NO-side kill switch: auto-disable if cumulative PnL below threshold
        # Check once per tick, uses module-level _weather_no_killed flag
        if WEATHER_NO_SIDE_LIVE:
            try:
                _wx_no_pnl = self._state.conn.execute(
                    "SELECT COALESCE(SUM(pnl_cents - COALESCE(fee_cents, 0)), 0) FROM settled_trades "
                    "WHERE product_type='weather' AND side='no'"
                ).fetchone()[0]
                if _wx_no_pnl < WEATHER_NO_KILL_THRESHOLD:
                    import bot._impl as _self_module
                    _self_module.WEATHER_NO_SIDE_LIVE = False
                    logging.error(
                        "WEATHER_NO_KILL: cumulative PnL=%dc < %dc — auto-disabling",
                        _wx_no_pnl, WEATHER_NO_KILL_THRESHOLD)
                    if _telegram_state._TELEGRAM:
                        _telegram_state._TELEGRAM.send(
                            f"\U0001f6a8 *WEATHER NO-SIDE AUTO-KILLED*\n"
                            f"Cumulative PnL: ${_wx_no_pnl/100:.2f} "
                            f"(threshold: ${WEATHER_NO_KILL_THRESHOLD/100:.2f})")
            except Exception:
                pass  # Non-critical
        # Hourly NO kill switch
        if HOURLY_NO_SIDE_LIVE:
            try:
                _hno_pnl = self._state.conn.execute(
                    "SELECT COALESCE(SUM(pnl_cents - COALESCE(fee_cents, 0)), 0) FROM settled_trades "
                    "WHERE product_type='hourly' AND side='no'"
                ).fetchone()[0]
                if _hno_pnl < HOURLY_NO_KILL_THRESHOLD:
                    import bot._impl as _self_module
                    _self_module.HOURLY_NO_SIDE_LIVE = False
                    logging.error(
                        "HOURLY_NO_KILL: cumulative PnL=%dc < %dc — auto-disabling",
                        _hno_pnl, HOURLY_NO_KILL_THRESHOLD)
                    if _telegram_state._TELEGRAM:
                        _telegram_state._TELEGRAM.send(
                            f"\U0001f6a8 *HOURLY NO-SIDE AUTO-KILLED*\n"
                            f"Cumulative PnL: ${_hno_pnl/100:.2f} "
                            f"(threshold: ${HOURLY_NO_KILL_THRESHOLD/100:.2f})")
            except Exception:
                pass  # Non-critical
        # Bracket NO kill switch (separate from general weather NO)
        if BRACKET_NO_ENABLED:
            try:
                _bn_pnl = self._state.conn.execute(
                    "SELECT COALESCE(SUM(pnl_cents - COALESCE(fee_cents, 0)), 0) FROM settled_trades "
                    "WHERE strategy='bracket_no'"
                ).fetchone()[0]
                if _bn_pnl < BRACKET_NO_KILL_THRESHOLD:
                    import bot._impl as _self_module
                    _self_module.BRACKET_NO_ENABLED = False
                    logging.error(
                        "BRACKET_NO_KILL: cumulative PnL=%dc < %dc — auto-disabling",
                        _bn_pnl, BRACKET_NO_KILL_THRESHOLD)
                    if _telegram_state._TELEGRAM:
                        _telegram_state._TELEGRAM.send(
                            f"\U0001f6a8 *BRACKET NO AUTO-KILLED*\n"
                            f"Cumulative PnL: ${_bn_pnl/100:.2f} "
                            f"(threshold: ${BRACKET_NO_KILL_THRESHOLD/100:.2f})")
            except Exception:
                pass  # Non-critical
        self._lp_hour_signals = {}  # Low-price shadow: per-hour signal count

        # Loss burst cooldown: build per-asset lockout set (see kb/failures/loss-clustering.md)
        # Any 15M loss in the last LOSS_COOLDOWN_SECONDS locks out that asset's 15M entries.
        # CRITICAL: use julianday() comparison, NOT string/`datetime('now',...)`. settled_at is
        # stored in ISO-T format ('2026-04-11T00:45:30.365339Z') and datetime('now',...) returns
        # space-format ('2026-04-11 09:28:14'). Lex-comparing them matches by ASCII where
        # 'T' (84) > ' ' (32), so a naive `settled_at > datetime('now','-2 hours')` returns TRUE
        # for ANY same-UTC-date loss, silently blocking the asset until UTC midnight.
        self._cooldown_assets = set()
        if LOSS_COOLDOWN_ENABLED:
            try:
                _cd_rows = self._state.conn.execute(
                    "SELECT DISTINCT asset FROM settled_trades "
                    "WHERE product_type='15m' AND pnl_cents < 0 "
                    "AND julianday(settled_at) > julianday('now') - ?/86400.0",
                    (LOSS_COOLDOWN_SECONDS,)
                ).fetchall()
                self._cooldown_assets = {r[0] for r in _cd_rows if r[0]}
                if self._cooldown_assets and self._cooldown_assets != getattr(self, "_cooldown_assets_last_logged", None):
                    logging.info("LOSS_COOLDOWN_ACTIVE: %s (window=%ds)",
                                 sorted(self._cooldown_assets), LOSS_COOLDOWN_SECONDS)
                    self._cooldown_assets_last_logged = set(self._cooldown_assets)
            except Exception:
                pass  # Non-critical

        try:
            for pos in self._state.get_open_positions():
                evt = pos.get("event_ticker", "")
                if evt:
                    self._hourly_window_counts[evt] = self._hourly_window_counts.get(evt, 0) + 1
                    # Seed DC window risk from existing decided positions
                    strat = pos.get("strategy", "")
                    if strat in ("decided_t1", "decided_t1b", "decided_t2"):
                        _dc_cost = pos.get("count", 0) * pos.get("avg_price_cents", 0)
                        self._dc_window_risk[evt] = self._dc_window_risk.get(evt, 0.0) + _dc_cost
        except Exception:
            pass  # Non-critical: worst case is slight over-allocation

        # Clean up ask history and dedup set for tickers no longer in active windows
        active_tickers = set()
        for w in active_windows:
            for m in w.get("markets", []):
                active_tickers.add(m.get("ticker", ""))
        expired = [t for t in self._ticker_ask_history if t not in active_tickers]
        for t in expired:
            del self._ticker_ask_history[t]
        expired_ob = [t for t in self._ob_cache if t not in active_tickers]
        for t in expired_ob:
            del self._ob_cache[t]
        # Unsubscribe expired tickers from WS orderbook_delta
        if self._kalshi_feed:
            ws_expired = set(expired) | set(expired_ob)
            for t in ws_expired:
                try:
                    self._kalshi_feed.unsubscribe_ticker(t)
                except Exception:
                    pass
        self._eval_opp_seen = {
            key for key in self._eval_opp_seen if key[0] in active_tickers
        }
        # Clean expired DC skip cooldowns
        _now_cd = time.time()
        self._dc_skip_cooldown = {
            t: exp for t, exp in self._dc_skip_cooldown.items()
            if exp > _now_cd and t in active_tickers
        }
        # Clean expired API error counters (prevent memory growth)
        if self._ml and hasattr(self._ml, "executor"):
            self._ml.executor._ticker_api_errors = {
                t: c for t, c in self._ml.executor._ticker_api_errors.items()
                if t in active_tickers
            }
        if self._kalshi_oft is not None:
            try:
                self._kalshi_oft.cleanup_stale(active_tickers)
            except Exception:
                pass
        if self._ml and getattr(self._ml, "fifteenm_shadow", None):
            try:
                self._ml.fifteenm_shadow.cleanup_expired(active_tickers)
            except Exception:
                pass
        if self._ml and getattr(self._ml, "hourly_alt_shadow", None):
            try:
                self._ml.hourly_alt_shadow.cleanup_expired(active_tickers)
            except Exception:
                pass

        # Build ticker set for product types that skip WS orderbook subscription (too many strikes)
        _hourly_tickers = set()
        for w in active_windows:
            if w.get("product_type") in ("hourly", "spx_hourly", "weather"):
                for m in w.get("markets", []):
                    _hourly_tickers.add(m.get("ticker", ""))

        # Pre-subscribe all active tickers to WS and feed OFT from WS orderbooks
        if self._kalshi_feed and self._kalshi_feed.is_connected:
            for t in active_tickers:
                if t in _hourly_tickers:
                    continue  # skip WS subscription for hourly (too many strikes per event)
                try:
                    self._kalshi_feed.subscribe_ticker(t)
                except Exception:
                    pass
            # Feed OFT with any available WS orderbook data (zero API cost)
            if self._kalshi_oft is not None:
                for t in active_tickers:
                    try:
                        ws_ob = self._kalshi_feed.get_orderbook(t)
                        if ws_ob and now - ws_ob.get("ts", 0) < 30:
                            best_ask = self._best_yes_ask_cents(ws_ob)
                            if best_ask is not None:
                                self._kalshi_oft.record_snapshot(t, ws_ob, best_ask)
                    except Exception:
                        pass

        # Dynamic scan_stats: include all assets from active windows (SPX, weather, etc.)
        _all_scan_assets = set(ASSETS)
        for w in active_windows:
            _all_scan_assets.add(w["asset"])
        scan_stats: Dict[str, Dict[str, int]] = {
            a: {"evaluated": 0, "low_prob": 0, "no_orderbook": 0, "no_best_ask": 0,
                "price_out_of_range": 0, "insufficient_edge": 0, "zero_sizing": 0,
                "strategy_wait": 0, "candidates": 0}
            for a in _all_scan_assets
        }

        _price_shadow_queue = []
        _no_side_queue = []  # NO-side shadow: markets queued for NO evaluation
        _overnight_lp_queue = []  # Overnight low-price shadow: 50-85c YES during overnight hours
        _low_price_shadow_queue = []  # Low-price shadow: 20-79c 15M signals (Phase C)

        # 1. Filter windows by time range (config-driven thresholds)
        time_ok_windows = []
        for w in active_windows:
            stc = w["seconds_to_close"]
            _tcfg = get_market_config(w.get("product_type"))
            if _tcfg.min_seconds_before_close <= stc <= _tcfg.max_seconds_before_close:
                time_ok_windows.append(w)
        # SPX diagnostic: log when SPX windows are filtered by STC
        _spx_in = [w for w in active_windows if w.get("product_type") == "spx_hourly"]
        _spx_ok = [w for w in time_ok_windows if w.get("product_type") == "spx_hourly"]
        if _spx_in and not _spx_ok:
            logging.warning("SPX_DIAG_STC: %d SPX windows ALL filtered — stc=[%s]",
                            len(_spx_in), ", ".join(f"{w['seconds_to_close']:.0f}" for w in _spx_in))
        elif _spx_ok:
            logging.info("SPX_DIAG_STC: %d/%d SPX windows passed time filter (stc=[%s])",
                         len(_spx_ok), len(_spx_in),
                         ", ".join(f"{w['seconds_to_close']:.0f}" for w in _spx_ok))
        # F/U 6 diagnostic: log when ALL 15M windows are filtered out
        # at the time-range gate but other product types survive. This
        # is the silent path that leaves the 15M heartbeat blind while
        # scan() body still iterates hourly/weather. Throttled to once
        # per 30s per code-path — see kb/failures (when written).
        _15m_in = [w for w in active_windows if w.get("product_type") == "15m"]
        _15m_time_ok = [w for w in time_ok_windows if w.get("product_type") == "15m"]
        if _15m_in and not _15m_time_ok:
            _now = time.time()
            _last = getattr(self, "_last_15m_time_filter_log_ts", 0.0)
            if _now - _last >= 30.0:
                self._last_15m_time_filter_log_ts = _now
                logging.warning(
                    "F_U6_15M_TIME_FILTER_DROPPED_ALL: %d 15M windows ALL "
                    "filtered by time range — details=[%s] (scan iterates "
                    "non-15M; heartbeat blind for this tick)",
                    len(_15m_in),
                    ", ".join(
                        f"{w.get('asset', '?')}={w.get('seconds_to_close', '?'):.1f}s"
                        for w in _15m_in))
        if not time_ok_windows:
            return None

        # 2. Get occupied timeslots (positions + resting orders)
        occupied = self._get_occupied_timeslots()

        # 3. Filter out windows whose timeslot already has this SAME asset
        eligible_windows = []
        for w in time_ok_windows:
            if w.get("product_type") in ("hourly", "spx_hourly", "weather"):
                eligible_windows.append(w)
                continue  # hourly/spx/weather windows bypass timeslot logic
            ts = self._window_timeslot(w["event_ticker"])
            if ts in occupied and w["asset"] in occupied[ts]:
                continue  # this asset already has a position/order in this timeslot
            eligible_windows.append(w)

        # F/U 6 diagnostic: log when ALL 15M survived the time filter
        # but were dropped by the timeslot-occupancy filter. Throttled
        # to once per 30s.
        _15m_eligible = [w for w in eligible_windows if w.get("product_type") == "15m"]
        if _15m_time_ok and not _15m_eligible:
            _now = time.time()
            _last = getattr(self, "_last_15m_eligible_filter_log_ts", 0.0)
            if _now - _last >= 30.0:
                self._last_15m_eligible_filter_log_ts = _now
                logging.warning(
                    "F_U6_15M_ELIGIBLE_FILTER_DROPPED_ALL: %d 15M windows "
                    "passed time but ALL filtered by timeslot occupancy — "
                    "details=[%s] occupied=%s (scan iterates non-15M; "
                    "heartbeat blind)",
                    len(_15m_time_ok),
                    ", ".join(
                        f"{w.get('asset', '?')}={w.get('seconds_to_close', '?'):.1f}s"
                        for w in _15m_time_ok),
                    {k: sorted(list(v)) for k, v in occupied.items()})

        if not eligible_windows:
            return None

        # 4. Evaluate each market in each surviving window.
        # Per-section timing — `SCAN_LOOP_SLOW` fires when the for-loop
        # body alone exceeds 1.5s. Distinguishes "main loop is slow"
        # from "post-loop processing is slow" (price_shadow, candidate
        # selection, etc.) under the SCAN_BODY_SLOW umbrella.
        # Apr 25 01:09 incident: SCAN_BODY_SLOW 5.64s — need to
        # localize within scan() body.
        _scan_loop_start = time.perf_counter()
        for window in eligible_windows:
            # Per-window timer (Phase 1 of scan-loop optimization).
            # SCAN_WINDOW_SLOW fires when one window's iteration body
            # exceeds 500ms. Localizes which window is the cost: if
            # several windows hit ~300-500ms each, orderbook REST is
            # the bottleneck (Phase 2: parallel prefetch). If one
            # window hits multi-second, a specific op in it is slow
            # (different fix needed). See Apr 25 01:17 incident.
            _window_start = time.perf_counter()
            asset = window["asset"]
            _pt = window.get("product_type")

            # Bit 9.2 ride-along (ticket 86b9vppn3): initialize best_ask
            # at iteration start so the low_probability_15m insert_rejection
            # branch (search anchor: `"low_probability_15m"`) doesn't
            # UnboundLocalError when the cal_prob < min_prob_needed
            # early-return fires before the orderbook fetch (search anchor:
            # `best_ask = self._best_yes_ask_cents(ob_data)`) sets best_ask.
            # Predates Bit 8.1 per git blame — the latent bug shipped April
            # 2026 in the phase-1/prevention-3 commit that added the
            # low_probability_15m insert_rejection branch but didn't
            # initialize best_ask above the early-return.
            best_ask = None

            # Productivity heartbeat — set as soon as scan() reaches a 15M
            # window iteration body. Decoupled from DB writes because dedup
            # at insert sites can silence rows for an entire window's
            # lifetime even though scan is iterating normally.
            if _pt in (None, "15m"):
                self._scan_15m_iter_heartbeat_ts = time.time()

            # Loss burst cooldown: skip 15M entries for assets with a recent loss.
            # Bursts are driven by correlated macro moves; pausing 2h after any
            # loss prevents the 2nd-7th trades of the burst from entering.
            if (LOSS_COOLDOWN_ENABLED
                    and _pt in (None, "15m")
                    and asset in self._cooldown_assets):
                try:
                    if asset in scan_stats:
                        scan_stats[asset]["loss_cooldown"] = (
                            scan_stats[asset].get("loss_cooldown", 0) + 1)
                except Exception:
                    pass
                # Trace row so this silent-bail isn't a diagnostic black
                # hole. ws-cache-drift-silent-scan-2026-04-24 PM Prevention #3.
                try:
                    self._state.insert_evaluated_opportunity(
                        ticker=window["event_ticker"],
                        event_ticker=window["event_ticker"],
                        asset=asset,
                        filter_stage="silent_loss_cooldown",
                        rejection_reason="asset in cooldown_assets",
                        seconds_to_close=window.get("seconds_to_close"),
                        product_type=_pt or "15m")
                except Exception:
                    logging.debug(
                        "silent_loss_cooldown trace insert failed",
                        exc_info=True)
                continue

            # Route price/vol to appropriate engine based on product type
            if _pt == "spx_hourly" and self._ml and getattr(self._ml, "spx_engine", None):
                spot = self._ml.spx_engine.get_spot_price(asset)
                if spot is None or spot <= 0:
                    logging.warning("SPX_DIAG_SPOT: spot=%s for %s — skipping window", spot, asset)
                    continue
                seconds_remaining = window["seconds_to_close"]
                vol_est = self._ml.spx_engine.get_vol_estimate(asset, seconds_remaining)
            elif _pt == "weather" and self._ml and getattr(self._ml, "weather_engine", None):
                spot = self._ml.weather_engine.get_spot_price(asset)
                if spot is None or spot <= 0:
                    continue
                seconds_remaining = window["seconds_to_close"]
                vol_est = self._ml.weather_engine.get_vol_estimate(asset, seconds_remaining)
            else:
                _vol_start = time.perf_counter()
                spot = self._feed.get_price(asset)
                if spot is None or spot <= 0:
                    # Trace row — Coinbase price feed gap or restart warmup.
                    # ws-cache-drift-silent-scan-2026-04-24 PM Prevention #3.
                    try:
                        self._state.insert_evaluated_opportunity(
                            ticker=window["event_ticker"],
                            event_ticker=window["event_ticker"],
                            asset=asset,
                            filter_stage="silent_spot_none",
                            rejection_reason=f"spot={spot} from feed",
                            spot_price=spot if spot is not None else None,
                            seconds_to_close=window.get("seconds_to_close"),
                            product_type=_pt or "15m")
                    except Exception:
                        logging.debug(
                            "silent_spot_none trace insert failed",
                            exc_info=True)
                    continue
                seconds_remaining = window["seconds_to_close"]
                vol_est = self._vol.update(asset, seconds_to_close=seconds_remaining)
                # Per-section timing — SCAN_VOL_SLOW fires when the
                # spot fetch + vol.update call exceeds 300ms. Apr 25
                # 01:35: 10.43s BTC window stall with no slow OB
                # fetch — vol compute is a top suspect.
                _vol_dt = time.perf_counter() - _vol_start
                if _vol_dt > 0.3:
                    logging.warning(
                        "SCAN_VOL_SLOW: asset=%s took %.2fs", asset, _vol_dt)

            if vol_est is None or vol_est["blended_rv"] <= 0:
                if _pt == "spx_hourly":
                    logging.warning("SPX_DIAG_VOL: vol_est=%s blended_rv=%s — skipping window",
                                    "None" if vol_est is None else "ok",
                                    vol_est.get("blended_rv") if vol_est else "N/A")
                # Post-warmup escalation — first ~30s post-restart is
                # benign vol-engine warmup (RK + EGARCH need a few price
                # ticks). After 60s, vol_est=None is a real bug and must
                # log loudly so the next outage class self-announces.
                # See Apr 25 00:43 silent_vol_none investigation — all
                # 6 silent_vol_none events fired at 18.3s post-restart
                # in a single tick, then never again.
                _proc_start = getattr(self, "_scan_15m_process_start_ts", None)
                if (_proc_start is not None
                        and (time.time() - _proc_start) > 60.0
                        and _pt in (None, "15m", "hourly")):
                    logging.warning(
                        "VOL_NONE_POST_WARMUP: %s vol_est=None at "
                        "uptime=%.0fs (past warmup) — vol engine may "
                        "be stalled, see kb/failures/"
                        "ws-cache-drift-silent-scan-2026-04-24.md",
                        asset, time.time() - _proc_start)
                # Trace row for 15M/hourly — vol engine warmup or divergence.
                # ws-cache-drift-silent-scan-2026-04-24 PM Prevention #3.
                if _pt in (None, "15m", "hourly"):
                    try:
                        _br = vol_est.get("blended_rv") if vol_est else None
                        # Apr 25 2026: enrich rejection_reason so we can
                        # tell warmup (uptime<30s) from feed-blip
                        # (buf_len short post-Coinbase-reconnect) from
                        # real bug (uptime>>warmup AND buf_len adequate).
                        # Pre-enrichment we just saw "vol_est=None" 132×/day
                        # with no way to triage. See ws-cache-drift-silent-
                        # scan-2026-04-24.md PM Prevention #3.
                        try:
                            _buf_len = (
                                len(self._feed.get_buffer(asset))
                                if self._feed else 0)
                        except Exception:
                            _buf_len = -1
                        _uptime_s = (
                            (time.time() - _proc_start)
                            if _proc_start is not None else -1)
                        self._state.insert_evaluated_opportunity(
                            ticker=window["event_ticker"],
                            event_ticker=window["event_ticker"],
                            asset=asset,
                            filter_stage="silent_vol_none",
                            rejection_reason=(
                                f"vol_est=None buf_len={_buf_len} "
                                f"uptime={_uptime_s:.0f}s"
                                if vol_est is None
                                else f"blended_rv={_br} buf_len={_buf_len}"),
                            spot_price=spot,
                            seconds_to_close=seconds_remaining,
                            product_type=_pt or "15m")
                    except Exception:
                        logging.debug(
                            "silent_vol_none trace insert failed",
                            exc_info=True)
                continue

            blended_rv = vol_est["blended_rv"]

            # Extract shadow diagnostics for per-evaluation logging
            # _shadow_diag: fields that match insert_evaluated_opportunity/insert_rejection params
            _ebs_var = vol_est.get("egarch_blend_var")
            # Fallback: if engine didn't compute egarch_blend_var but has constituents, compute here
            if _ebs_var is None:
                _fb_sigma = vol_est.get("egarch_sigma")
                _fb_bw = vol_est.get("egarch_blend_weight")
                _fb_rk = vol_est.get("rk_rv")
                _fb_sf = vol_est.get("seasonal_factor", 1.0)
                if _fb_sigma and _fb_bw and _fb_bw > 0 and _fb_rk and _fb_rk > 0 and _fb_sf:
                    _fb_erv = _fb_sigma * _fb_sf
                    _ebs_var = _fb_bw * (_fb_erv ** 2) + (1 - _fb_bw) * (_fb_rk ** 2)
            # Settlement divergence measurement: 60s trailing avg + multi-exchange
            # These are logged for analysis — do NOT affect probability or sizing.
            _spot_60s_avg = None
            _spot_multi_exchange = None
            if _pt not in ("spx_hourly", "weather"):
                _spot_60s_avg = self._feed.get_price_trailing_avg(asset, 60)
                try:
                    _kraken_price = self._ml.cross_feed.get_prices(asset).get("kraken") \
                        if hasattr(self._ml, "cross_feed") and self._ml.cross_feed else None
                    if _kraken_price is not None and spot is not None:
                        _spot_multi_exchange = round((spot + _kraken_price) / 2, 6)
                        # Phase 1: cache per-asset gap in bps (Kraken − Coinbase).
                        # Read by insert_evaluated_opportunity via _scan_cx_gap_cache.
                        # See kb/concepts/feature-engineering-phase1.md.
                        if spot > 0:
                            self._state._scan_cx_gap_cache[asset] = round(
                                (_kraken_price - spot) / spot * 10000, 4)
                except Exception:
                    pass
            _shadow_diag = {
                "egarch_sigma": vol_est.get("egarch_sigma"),
                "egarch_blend_sigma": math.sqrt(_ebs_var) if _ebs_var and _ebs_var > 0 else None,
                "egarch_blend_weight": vol_est.get("egarch_blend_weight"),
                "mz_r_squared": vol_est.get("mz_r_squared"),
                "shadow_tv_blend_rv": vol_est.get("shadow_tv_blend_rv"),
                "mz_shadow_sigmoid_w": vol_est.get("mz_shadow_sigmoid_w"),
                "mz_baseline_qlike": vol_est.get("mz_baseline_qlike"),
                "mz_qlike": vol_est.get("mz_qlike"),
            }
            # _shadow_extra_base: additional fields for log_opportunity (not in DB insert params)
            # Copied per-market to avoid OFT field bleed between tickers
            _shadow_extra_base = {
                "shadow_tv_weights": vol_est.get("shadow_tv_weights"),
                "mz_sigmoid_improvement": vol_est.get("mz_sigmoid_improvement"),
                "mz_sigmoid_blend_rv": vol_est.get("mz_sigmoid_blend_rv"),
                "egarch_n_updates": vol_est.get("egarch_n_updates"),
                "egarch_ratio_clamped": vol_est.get("egarch_ratio_clamped"),
                # Settlement divergence: logged for analysis, not used for trading
                "spot_60s_avg": round(_spot_60s_avg, 6) if _spot_60s_avg is not None else None,
                "spot_coinbase_kraken_avg": _spot_multi_exchange,
            }

            for mkt in window["markets"]:
                ticker = mkt.get("ticker", "")
                # Phase D (shadow coverage expansion 2026-05-02): _shadow_diag
                # is window-scoped (built at line ~11247) and reused per
                # market. The pre-Phase-D `_calmlp_annotate_async` only
                # mutated cal_mlp_* keys on the success path AND only in
                # one branch — so iterations N+1...K inherited a stale
                # cal_mlp_request_id from iteration N until N+1's annotate
                # ran. Inserts firing BEFORE the annotate (price_out_of_range,
                # floor_raise_shadow, queue snapshots, etc.) carried the
                # prior ticker's uuid. Reset here defensively.
                # See kb/decisions/shadow-coverage-expansion-may01.md.
                for _cmk in [
                    "cal_mlp_request_id", "cal_mlp_skipped_reason",
                    "cal_mlp_p_mean", "cal_mlp_p_std",
                    "cal_mlp_final_lo", "cal_mlp_final_hi",
                    "cal_mlp_train_id",
                ]:
                    _shadow_diag.pop(_cmk, None)
                threshold = self._parse_threshold(mkt)
                if threshold is None:
                    # R1 [P0-1] / R2 [A1]: previously silent. Schema
                    # drift on floor_strike/yes_sub_title could fire
                    # on EVERY market simultaneously → 15M scan
                    # silent with no trace. Dedup per (ticker, reason)
                    # to avoid the PM-001 commit-in-loop / lock
                    # contention pattern. INSERT OR IGNORE alone
                    # bounds row count but still acquires DB lock
                    # every tick.
                    _dk_thr = (ticker, "threshold_unparsable")
                    if _dk_thr not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dk_thr)
                        try:
                            self._state.insert_rejection(
                                ticker, window.get("event_ticker", ""),
                                asset, "threshold_unparsable",
                                None, spot, None, blended_rv, None,
                                seconds_remaining, None,
                                product_type=window.get("product_type"))
                        except Exception:
                            logging.warning(
                                "threshold_unparsable insert_rejection failed",
                                exc_info=True)
                    continue

                # Threshold sanity gate — defensive against upstream data corruption.
                # On 2026-04-13 15:00-15:35 UTC, Kalshi API returned floor_strike values
                # scaled by 10^-1 (XRP) or 10^-4 (BTC/ETH) for 6 crypto 15M markets,
                # causing 2 live trades (XRP -$49.98, BTC -$3.26) to fill on garbage
                # probabilities (model saturated to 0.97). Max |thr/spot - 1| observed
                # in 1,795 historical wins is 1.59% — 0.05 is 3x safety margin.
                # See kb/failures/apr13-threshold-corruption.md.
                #
                # 15M ONLY — hourly/spx_hourly are multi-strike ladders where strikes
                # legitimately span ±5% around spot. Early NBBO price filter (above)
                # already rejects out-of-range hourly strikes. Applying this gate to
                # hourly blocked 319 legit candidates in 2h after Apr 15 deploy.
                if _pt == "15m" and spot > 0 and threshold > 0:
                    _thr_ratio = abs(threshold - spot) / spot
                    if _thr_ratio > 0.05:
                        logging.warning(
                            "THRESHOLD_IMPLAUSIBLE: %s threshold=%.6f spot=%.4f ratio=%.2f%% "
                            "raw_floor_strike=%r yes_sub_title=%r",
                            ticker, threshold, spot, _thr_ratio * 100,
                            mkt.get("floor_strike"), mkt.get("yes_sub_title"))
                        _dedup_key_ti = (ticker, "threshold_implausible")
                        if _dedup_key_ti not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key_ti)
                            try:
                                self._state.insert_evaluated_opportunity(
                                    ticker=ticker,
                                    event_ticker=window.get("event_ticker", ""),
                                    asset=asset, product_type=_pt,
                                    filter_stage="threshold_implausible",
                                    rejection_reason=f"|thr-spot|/spot={_thr_ratio:.4f} > 0.05",
                                    spot_price=spot, threshold=threshold,
                                )
                            except Exception:
                                logging.debug("threshold_implausible log failed", exc_info=True)
                        continue

                # Phase 2: update per-window spot-path state for 15M (feeds Tier 1 features)
                if _pt in (None, "15m"):
                    self._update_window_state(ticker, spot, threshold)

                # Early NBBO price filter for multi-strike events (SPX: 60-400 markets).
                # Skip probability computation for strikes clearly outside entry range.
                if _pt in ("spx_hourly", "hourly", "weather"):
                    _nbbo_raw = mkt.get("yes_ask_dollars") or mkt.get("yes_ask")
                    if _nbbo_raw is not None:
                        _nbbo = dollars_str_to_cents(_nbbo_raw) if isinstance(_nbbo_raw, str) else int(_nbbo_raw)
                        _pcfg_early = get_market_config(_pt)
                        if _nbbo > 0 and not (_pcfg_early.min_entry_price <= _nbbo <= _pcfg_early.max_entry_price):
                            # R1 [P0-1] / R2 [A1]: previously silent.
                            # SPX has 60-400 strikes per event — most
                            # fall outside entry range and silently
                            # bail. Dedup per (ticker, reason) — see
                            # threshold_unparsable site for rationale.
                            _dk_oor = (ticker, "price_out_of_range_early")
                            if _dk_oor not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_dk_oor)
                                try:
                                    self._state.insert_rejection(
                                        ticker,
                                        window.get("event_ticker", ""),
                                        asset, "price_out_of_range_early",
                                        None, spot, threshold,
                                        blended_rv, _nbbo,
                                        seconds_remaining, None,
                                        product_type=_pt)
                                except Exception:
                                    logging.warning(
                                        "price_out_of_range_early insert_rejection failed",
                                        exc_info=True)
                            continue

                # Per-market copy of shadow extras (OFT fields added per-ticker below)
                _shadow_extra = dict(_shadow_extra_base)
                # OFT fields for DB insert — populated after ofa_signals computed
                _oft_db = {}

                scan_stats[asset]["evaluated"] += 1
                self._session_total_scanned += 1

                # Pre-filter: compute probability without market price
                if _pt == "weather" and self._ml and getattr(self._ml, "weather_engine", None):
                    # Weather uses ensemble-based Gaussian model, NOT lognormal ProbabilityEngine
                    _wx_city = asset.replace("_TEMP", "")
                    _wx_info = self._parse_weather_market_info(mkt)
                    _wx_mtype = _wx_info[0] if _wx_info else None
                    _wx_bounds = (_wx_info[1], _wx_info[2]) if (_wx_info and _wx_info[0] == "bracket") else None
                    _shadow_extra["wx_market_type"] = _wx_mtype
                    _wx_prob = self._ml.weather_engine.get_probability(
                        _wx_city, threshold,
                        market_type=_wx_mtype, bracket_bounds=_wx_bounds)
                    if _wx_prob is None:
                        # R1 [P0-1] / R2 [A1]: previously silent.
                        # Dedup per (ticker, reason).
                        _dk_wxp = (ticker, "weather_prob_none")
                        if _dk_wxp not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dk_wxp)
                            try:
                                self._state.insert_rejection(
                                    ticker,
                                    window.get("event_ticker", ""),
                                    asset, "weather_prob_none",
                                    None, spot, threshold,
                                    blended_rv, None,
                                    seconds_remaining, None,
                                    product_type=_pt)
                            except Exception:
                                logging.warning(
                                    "weather_prob_none insert_rejection failed",
                                    exc_info=True)
                        continue
                    # R3: Ensemble quality gate — skip if no ensemble data available
                    if not _wx_prob.get("n_members"):
                        logging.debug("WEATHER_SKIP: %s no ensemble data (n_members=None/0)", ticker)
                        _shadow_extra["wx_ensemble_mean"] = None
                        _shadow_extra["wx_ensemble_std"] = None
                        _shadow_extra["wx_n_members"] = 0
                        _fs = "data_unavailable"
                        _dedup_key_ens = (ticker, _fs)
                        if _dedup_key_ens not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key_ens)
                            _nbbo_val = int(_nbbo) if _nbbo_raw is not None else None
                            self._state.insert_evaluated_opportunity(
                                ticker=ticker, event_ticker=window.get("event_ticker", ""),
                                asset=asset, product_type=_pt, filter_stage=_fs,
                                market_price=_nbbo_val,
                                wx_ensemble_mean=None, wx_ensemble_std=None,
                                wx_n_members=0,
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                            )
                        continue
                    prob_result = {
                        "calibrated_prob": _wx_prob.get("calibrated_prob"),
                        "raw_prob": _wx_prob.get("raw_prob"),
                        "calibration_method": "weather_ensemble",
                        "tradeable": True,  # weather ensemble always tradeable (no z-score gate)
                        "z_score": 0.0,
                    }
                    # Store weather ensemble diagnostics for DB
                    _shadow_extra["wx_ensemble_mean"] = _wx_prob.get("ensemble_mean")
                    _shadow_extra["wx_ensemble_std"] = _wx_prob.get("ensemble_std")
                    _shadow_extra["wx_bias_correction"] = _wx_prob.get("bias_correction")
                    _shadow_extra["wx_n_members"] = _wx_prob.get("n_members")
                    _shadow_extra["wx_hrrr_temp"] = _wx_prob.get("hrrr_temp")
                    _shadow_extra["wx_corrected_mean"] = _wx_prob.get("corrected_mean")
                    logging.info(
                        "WEATHER_PROB: %s thresh=%.1fF ens_mean=%.1fF ens_std=%.2fF prob=%.4f type=%s",
                        ticker, threshold,
                        _wx_prob.get("ensemble_mean") or 0.0,
                        _wx_prob.get("ensemble_std") or 0.0,
                        _wx_prob.get("calibrated_prob") or 0.0,
                        _wx_mtype or "unknown")
                else:
                    prob_result = ProbabilityEngine.compute(
                        spot, threshold, seconds_remaining, blended_rv,
                        asset=asset, product_type=window.get("product_type")
                    )
                cal_prob = prob_result.get("calibrated_prob")
                raw_prob_pre = prob_result.get("raw_prob")
                calibration_method_pre = prob_result.get("calibration_method")
                if not prob_result.get("tradeable"):
                    reason = prob_result.get("reason", "tradeable_false")
                    # Apr 25 2026 (Phase 1 / Prevention #3): write a DB
                    # row for EVERY tradeable=False reason, not just
                    # z_score/refusing. Pre-fix, reasons like 'invalid
                    # inputs' and 'sigma_move is zero' silently continued
                    # — the proximate cause of recurring 15M scan-silence
                    # outages. With every continue writing a row, the
                    # scan-productive watchdog becomes 100% reliable
                    # AND the rejection_reason field tells us exactly
                    # what's going wrong upstream.
                    # R1 [P1-3]: use cheap NBBO from `mkt` rather than
                    # an orderbook fetch on the fail-fast path. The
                    # downstream z_score/refusing branch keeps a richer
                    # log_rejection JSONL with the OB-derived ask.
                    _nbbo_for_row = (
                        mkt.get("yes_ask_dollars") or mkt.get("yes_ask"))
                    try:
                        if isinstance(_nbbo_for_row, str):
                            rej_ask = dollars_str_to_cents(_nbbo_for_row)
                        elif _nbbo_for_row is not None:
                            rej_ask = int(_nbbo_for_row)
                        else:
                            rej_ask = None
                    except Exception:
                        rej_ask = None
                    # R2 [A1]: dedup per (ticker, reason_class) to
                    # avoid commit-in-loop. Use a stable reason CLASS
                    # (the prefix before any dynamic detail) so
                    # different float values for the same root cause
                    # share a single dedup slot.
                    _reason_class = reason.split(" — ")[0].split(" (")[0][:64]
                    _dk_tf1 = (ticker, "tf1:" + _reason_class)
                    if _dk_tf1 not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dk_tf1)
                        try:
                            self._state.insert_rejection(
                                ticker, window["event_ticker"], asset, reason,
                                prob_result.get("z_score"), spot, threshold,
                                blended_rv, rej_ask, seconds_remaining, cal_prob,
                                raw_prob=raw_prob_pre,
                                product_type=window.get("product_type"),
                                **_oft_db, **_shadow_diag)
                        except Exception:
                            logging.warning(
                                "tradeable_false insert_rejection failed",
                                exc_info=True)
                    if "z_score" in reason or "refusing" in reason:
                        rej_data = {
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": reason,
                            "z_score": prob_result.get("z_score"),
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": rej_ask,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": cal_prob,
                            "raw_prob": raw_prob_pre,
                            **_shadow_diag,
                            **_shadow_extra,
                        }
                        self._logger.log_rejection(rej_data)
                        logging.info(
                            f"Rejected opportunity: {ticker} — {reason}")
                    continue

                # Skip if calibrated prob too low to ever produce an edge.
                # Observation-mode products bypass entirely — data collection is the goal.
                _pcfg = get_market_config(window.get("product_type"))
                _min_price = _pcfg.min_entry_price
                min_prob_needed = (_min_price + MIN_EDGE_PCT) / 100.0
                if cal_prob < min_prob_needed and not _pcfg.observation_only:
                    scan_stats[asset]["low_prob"] += 1
                    if window.get("product_type") in ("hourly", "spx_hourly", "weather"):
                        # Volume control: count but don't log (many strikes are low_prob)
                        if _pt == "spx_hourly":
                            _spx_lp_key = "_spx_diag_lowprob_" + window["event_ticker"]
                            _spx_lp_cnt = getattr(self, _spx_lp_key, 0) + 1
                            setattr(self, _spx_lp_key, _spx_lp_cnt)
                            if _spx_lp_cnt == 1:
                                logging.warning(
                                    "SPX_DIAG_LOWPROB: %s cal=%.4f < needed=%.4f (price=%s)",
                                    ticker, cal_prob, min_prob_needed,
                                    mkt.get("yes_ask_dollars") or mkt.get("yes_ask") or "?")
                        continue
                    # Apr 25 2026 (Phase 1 / Prevention #3): write a DB
                    # row for the 15M low_probability path. Pre-fix
                    # this only logged JSONL via log_opportunity, so
                    # the scan-productive watchdog (which counts DB
                    # rows) was blind to a 15M scan stuck in
                    # low_probability — a candidate root cause for the
                    # Apr 25 outage if cal_prob collapsed across all 4
                    # 15M assets simultaneously.
                    _lowprob_reason = (
                        f"cal_prob {cal_prob:.4f} < "
                        f"min_needed {min_prob_needed:.4f}")
                    # R2 [A1]: dedup per ticker — cal_prob value
                    # changes tick-to-tick but the ROOT CAUSE
                    # ("low_probability_15m") is constant.
                    _dk_lp = (ticker, "low_probability_15m")
                    if _dk_lp not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dk_lp)
                        try:
                            self._state.insert_rejection(
                                ticker, window["event_ticker"], asset,
                                "low_probability_15m",
                                None, spot, threshold,
                                blended_rv, best_ask, seconds_remaining,
                                cal_prob, raw_prob=raw_prob_pre,
                                product_type=window.get("product_type"),
                                **_oft_db, **_shadow_diag)
                        except Exception:
                            logging.warning(
                                "low_probability_15m insert_rejection failed",
                                exc_info=True)
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "low_probability",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": _lowprob_reason,
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(cal_prob, 6),
                            "raw_prob": round(raw_prob_pre, 6) if raw_prob_pre is not None else None,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                    except Exception:
                        pass
                    continue

                # SPX diagnostic: market passed low_prob check
                if _pt == "spx_hourly":
                    logging.info("SPX_DIAG_PASS: %s cal=%.4f spot=%.1f thresh=%.1f rv=%.6f stc=%.0f",
                                 ticker, cal_prob, spot, threshold, blended_rv, seconds_remaining)

                # Fetch orderbook (cached, rate-limited).
                # Per-fetch timing — SCAN_OB_FETCH_SLOW fires when one
                # cache-or-REST orderbook fetch exceeds 500ms. Apr 25
                # 01:28: single XRP window took 5.73s; suspect this
                # call is the dominant cost on certain tickers (REST
                # retry, Kalshi rate limit, network).
                _ob_fetch_start = time.perf_counter()
                ob_data, was_fresh = self._get_orderbook_cached(ticker)
                _ob_fetch_dt = time.perf_counter() - _ob_fetch_start
                if _ob_fetch_dt > 0.5:
                    logging.warning(
                        "SCAN_OB_FETCH_SLOW: ticker=%s fresh=%s took %.2fs",
                        ticker, was_fresh, _ob_fetch_dt)
                if was_fresh:
                    ob_fetches_this_tick += 1
                if ob_data is None:
                    # Try NBBO fallback before giving up (prefer *_dollars field)
                    mkt_yes_ask_raw = mkt.get("yes_ask_dollars") or mkt.get("yes_ask")
                    if mkt_yes_ask_raw:
                        if isinstance(mkt_yes_ask_raw, str):
                            mkt_yes_ask = dollars_str_to_cents(mkt_yes_ask_raw)
                        else:
                            mkt_yes_ask = int(mkt_yes_ask_raw)
                    else:
                        mkt_yes_ask = None
                    if mkt_yes_ask and mkt_yes_ask > 0:
                        ob_data = {}  # empty dict so downstream code works
                        logging.info(
                            "Orderbook unavailable for %s, will use market NBBO yes_ask=%d¢",
                            ticker, mkt_yes_ask,
                        )
                    else:
                        scan_stats[asset]["no_orderbook"] += 1
                        # Flag ticker for WS-bypass so next tick uses REST
                        # directly — gives corrupt WS cache a chance to
                        # re-snapshot. Fix #1a from PM.
                        self.flag_ticker_drifted(ticker)
                        # DB rejection row so silent-bail paths leave a trace
                        # (ws-cache-drift-silent-scan-2026-04-24 PM)
                        try:
                            self._state.insert_rejection(
                                ticker, window["event_ticker"], asset,
                                "no_orderbook",
                                prob_result.get("z_score"), spot, threshold,
                                blended_rv, None, seconds_remaining, cal_prob,
                                raw_prob=raw_prob_pre,
                                product_type=window.get("product_type"),
                                **_oft_db, **_shadow_diag)
                        except Exception:
                            logging.debug(
                                "insert_rejection no_orderbook failed",
                                exc_info=True)
                        try:
                            self._logger.log_opportunity({
                                "filter_stage": "no_orderbook",
                                "ticker": ticker,
                                "event_ticker": window["event_ticker"],
                                "asset": asset,
                                "rejection_reason": "orderbook data unavailable and no market NBBO",
                                "spot_price": spot,
                                "threshold": threshold,
                                "volatility": blended_rv,
                                "seconds_to_close": round(seconds_remaining, 1),
                                "calibrated_prob": round(cal_prob, 6),
                                "mkt_yes_ask": mkt_yes_ask,
                                "raw_prob": round(raw_prob_pre, 6) if raw_prob_pre is not None else None,
                                **_shadow_diag,
                                **_shadow_extra,
                            })
                        except Exception:
                            pass
                        continue

                best_ask = self._best_yes_ask_cents(ob_data)
                best_ask_source = "orderbook"
                if best_ask is None:
                    # Fallback: use market's NBBO yes_ask (prefer *_dollars field)
                    mkt_yes_ask_raw = mkt.get("yes_ask_dollars") or mkt.get("yes_ask")
                    if mkt_yes_ask_raw:
                        if isinstance(mkt_yes_ask_raw, str):
                            mkt_yes_ask = dollars_str_to_cents(mkt_yes_ask_raw)
                        else:
                            mkt_yes_ask = int(mkt_yes_ask_raw)
                    else:
                        mkt_yes_ask = None
                    if mkt_yes_ask and mkt_yes_ask > 0:
                        best_ask = mkt_yes_ask
                        best_ask_source = "market_nbbo"
                        logging.info(
                            "Using market NBBO yes_ask=%d¢ for %s (orderbook NO bids empty)",
                            best_ask, ticker,
                        )
                # Read NO ask for pricing analysis (logged to evaluated_opportunities via _shadow_diag)
                _mkt_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                _mkt_no_ask_cents = (dollars_str_to_cents(_mkt_no_ask_raw) if isinstance(_mkt_no_ask_raw, str)
                                     else int(_mkt_no_ask_raw)) if _mkt_no_ask_raw is not None else None
                _shadow_diag["no_ask_cents"] = _mkt_no_ask_cents
                if best_ask is not None:
                    if ticker not in self._ticker_ask_history:
                        self._ticker_ask_history[ticker] = deque(maxlen=300)
                    self._ticker_ask_history[ticker].append((time.time(), best_ask))
                if best_ask is None:
                    scan_stats[asset]["no_best_ask"] += 1
                    # Flag ticker for WS-bypass so next tick uses REST
                    # directly — gives corrupt WS cache a chance to
                    # re-snapshot. Fix #1a from PM.
                    self.flag_ticker_drifted(ticker)
                    # DB rejection row so silent-bail paths leave a trace
                    # (ws-cache-drift-silent-scan-2026-04-24 PM)
                    try:
                        self._state.insert_rejection(
                            ticker, window["event_ticker"], asset,
                            "no_best_ask",
                            prob_result.get("z_score"), spot, threshold,
                            blended_rv, None, seconds_remaining, cal_prob,
                            raw_prob=raw_prob_pre,
                            product_type=window.get("product_type"),
                            **_oft_db, **_shadow_diag)
                    except Exception:
                        logging.debug(
                            "insert_rejection no_best_ask failed",
                            exc_info=True)
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "no_best_ask",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": "no best ask in orderbook or market NBBO",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(cal_prob, 6),
                            "mkt_yes_ask": mkt.get("yes_ask"),
                            "raw_prob": round(raw_prob_pre, 6) if raw_prob_pre is not None else None,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                    except Exception:
                        pass
                    continue

                # Phase D (shadow coverage expansion 2026-05-02): annotate
                # cal_mlp_request_id on _shadow_diag EARLY — before any
                # downstream insert_evaluated_opportunity / queue snapshot
                # splats _shadow_diag. The pre-Phase-D annotate at the
                # post-`prob_with_market` candidate path only stamped uuids
                # on the candidate row; shadow stages (low_price_shadow,
                # floor_raise_shadow, overnight_lp_shadow, weekend_discount,
                # no_side, etc.) had NULL cal_mlp_request_id and the
                # post-hoc daemon never predicted on them. Stamping here
                # fans the uuid out to every downstream **_shadow_diag
                # splat in this iteration. side="yes" is the iteration's
                # primary side; NO-side rows inherit the same uuid via
                # queue snapshot — the post-hoc daemon dispatches features
                # by the row's stored `side` column, so the uuid is just a
                # "predict me" marker (master plan caveat: v1 may not be
                # calibrated for NO-side; capture anyway).
                # entry_price_cents is documented as ignored by the
                # post-hoc processor (re-derives features from DB row);
                # passed for API stability with the v1.5 signature.
                # See kb/decisions/shadow-coverage-expansion-may01.md.
                #
                # Phase D adversarial review (round 1, MEDIUM-1): gate is
                # `_pt == "15m"` only — strict-equal to the daemon's
                # `WHERE product_type = '15m'` filter at
                # scripts/cal_mlp/post_hoc_processor.py:172. Pre-Phase-D
                # the gate was `_pt in (None, "15m")` to allow legacy
                # null-product-type 15M rows; in current production
                # 15M markets always have product_type="15m" set by
                # discover_active_windows(). Tighter gate prevents
                # stamping cal_mlp_request_id on rows the daemon will
                # never read (orphaned annotations).
                if _pt == "15m":
                    _calmlp_annotate_async(
                        _shadow_diag, raw_prob=raw_prob_pre, ticker=ticker,
                        side="yes", entry_price_cents=best_ask, row_features={},
                        predictor=_calmlp_predictors.get(asset), db_path=DB_PATH,
                    )

                # Diagnostic: log when orderbook and market NBBO disagree
                try:
                    mkt_yes_ask_raw = mkt.get("yes_ask")
                    if mkt_yes_ask_raw and best_ask_source == "orderbook":
                        mkt_nbbo = int(mkt_yes_ask_raw)
                        if mkt_nbbo != best_ask:
                            logging.debug(
                                "NBBO mismatch %s: orderbook=%d¢ market_nbbo=%d¢ (diff=%d¢)",
                                ticker, best_ask, mkt_nbbo, abs(best_ask - mkt_nbbo),
                            )
                except Exception:
                    pass

                # Compute orderbook depth early (used in logging + strategy)
                ask_depth = OrderExecutor._best_ask_depth(ob_data)
                total_depth = OrderExecutor._total_ob_depth(ob_data)
                # Extract YES bid for buy-low-sell-higher and exit-price analysis.
                # Stored in StateManager._scan_bid_cache so all insert_evaluated_opportunity
                # calls within this scan tick automatically pick it up — no need to thread
                # the value through 50+ call sites.
                yes_bid_cents = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                if yes_bid_cents is not None:
                    self._state._scan_bid_cache[ticker] = yes_bid_cents
                # Per-level orderbook ladder snapshot (top 10 each side).
                # Cached as (monotonic_ts, json) so insert_evaluated_opportunity
                # can enforce a freshness gate when auto-filling — stale
                # entries write NULL instead of a misleading old ladder.
                # None if ob_data missing this tick.
                _ob_levels = OrderExecutor._extract_book_levels(ob_data)
                if _ob_levels is not None:
                    self._state._scan_ob_cache[ticker] = (
                        time.monotonic(), _ob_levels)
                # Eviction runs UNCONDITIONALLY (outside the ob_data guard) —
                # otherwise a quiet-market scenario where every ticker stops
                # returning ob_data leaves the cache permanently stale.
                # Same failure class as failure_15m_silence_apr24_second.
                self._state._evict_stale_ob_cache()

                # ── MM fill simulation: check if shadow buy orders would fill ──
                # For hourly tickers with active MM shadow orders, check if the
                # current ask has dropped to/below the shadow buy price.
                if (_pt == "hourly"
                        and self._ml and getattr(self._ml, "hourly_alt_shadow", None)):
                    try:
                        _mm_bid = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                        self._ml.hourly_alt_shadow.check_mm_fills(
                            ticker, best_ask, _mm_bid or 0)
                    except Exception:
                        pass  # Fill check is advisory, don't break scan

                # Record orderbook snapshot for flow tracking
                try:
                    if self._kalshi_oft is not None and ob_data:
                        self._kalshi_oft.record_snapshot(ticker, ob_data, best_ask)
                except Exception:
                    pass

                # Phase 1 feature capture: microstructure + Kalshi flow signals.
                # Populates self._state._scan_ms_cache[ticker] so all
                # insert_evaluated_opportunity calls within this tick auto-fill.
                # See kb/concepts/feature-engineering-phase1.md.
                try:
                    _bid_depth = (OrderExecutor._best_yes_bid_depth(ob_data)
                                  if ob_data else None)
                    _spread = ((best_ask - yes_bid_cents)
                               if (best_ask is not None and yes_bid_cents is not None)
                               else None)
                    _ms: Dict[str, Any] = {
                        "yes_spread_cents": _spread,
                        "bid_depth": _bid_depth,
                    }
                    # Phase F-2 (shadow coverage expansion 2026-05-02):
                    # maker counterfactual snapshot. Reuses _ob_levels JSON
                    # already extracted above, so no extra orderbook fetch.
                    # See kb/decisions/shadow-coverage-expansion-may01.md.
                    _maker = self._compute_maker_counterfactual(
                        best_yes_bid=yes_bid_cents,
                        best_yes_ask=best_ask,
                        ladder_json=_ob_levels,
                    )
                    _ms["maker_price_cents"] = _maker["maker_price_cents"]
                    _ms["maker_depth_at_post"] = _maker["maker_depth_at_post"]
                    if self._kalshi_oft is not None:
                        _flow = self._kalshi_oft.get_signals(ticker)
                        if _flow:
                            _ms["kalshi_flow_imbalance_level"] = _flow.get("imbalance_level")
                            _ms["kalshi_flow_depth_velocity"] = _flow.get("depth_velocity")
                            _drain = _flow.get("depth_drain")
                            _ms["kalshi_flow_depth_drain"] = (
                                1 if _drain else 0 if _drain is False else None)
                    self._state._scan_ms_cache[ticker] = _ms
                except Exception:
                    pass  # Feature capture is advisory — never break scan

                # Log price snapshot for all markets with orderbook data
                try:
                    self._logger.log_scan({
                        "type": "price_snapshot",
                        "ticker": ticker,
                        "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask,
                        "best_ask_source": best_ask_source,
                        "best_ask_depth": ask_depth,
                        "total_ob_depth": total_depth,
                        "convergence_velocity": self._scanner_convergence_velocity(ticker),
                        "calibrated_prob": round(cal_prob, 6),
                    })
                except Exception:
                    pass

                # ── 15M Shadow Engine (pre-filter: all signals) ──
                # Evaluate shadow approaches for ALL 15M signals, not just those
                # passing the price filter.  _seen dedup in shadow engine prevents
                # double-eval if the signal also passes filters and hits the
                # post-filter call below.  Uses cal_prob (pre-temperature/blend)
                # as live_prob approximation — shadow approaches do their own cal.
                if (window.get("product_type") in (None, "15m")
                        and self._ml and getattr(self._ml, "fifteenm_shadow", None)
                        and best_ask is not None and cal_prob is not None):
                    try:
                        _15m_pre_bid = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                        _15m_pre_edge = cal_prob - best_ask / 100.0
                        _15m_pre_fee = calculate_fee(
                            1, best_ask, is_taker=True,
                            fee_mult_taker=get_market_config("15m").fee_multiplier_taker,
                            fee_mult_maker=get_market_config("15m").fee_multiplier_maker)
                        _15m_pre_fee_edge = _15m_pre_edge - _15m_pre_fee / 100.0
                        _15m_pre_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                        _15m_pre_no_ask = (dollars_str_to_cents(_15m_pre_no_ask_raw) if isinstance(_15m_pre_no_ask_raw, str)
                                           else int(_15m_pre_no_ask_raw)) if _15m_pre_no_ask_raw is not None else None
                        self._ml.fifteenm_shadow.evaluate_strike(
                            asset=asset, ticker=ticker,
                            event_ticker=window["event_ticker"],
                            spot_price=spot, threshold=threshold,
                            seconds_to_close=seconds_remaining,
                            market_price=best_ask,
                            best_bid=_15m_pre_bid, best_ask=best_ask,
                            blended_rv=blended_rv,
                            egarch_sigma=vol_est.get("egarch_sigma"),
                            z_score=prob_result.get("z_score", 0.0),
                            live_prob=cal_prob,
                            live_edge=_15m_pre_edge,
                            live_fee_edge=_15m_pre_fee_edge,
                            egarch_blend_weight=_shadow_diag.get("egarch_blend_weight"),
                            fee_adjusted_edge=_15m_pre_fee_edge,
                            no_ask=_15m_pre_no_ask)
                    except Exception:
                        logging.warning("fifteenm_shadow pre-filter evaluate failed", exc_info=True)

                # Filter: ask must be in entry price range
                _pricecfg = get_market_config(window.get("product_type"))
                _entry_floor = _pricecfg.min_entry_price
                _entry_ceil = _pricecfg.max_entry_price
                if not (_entry_floor <= best_ask <= _entry_ceil):
                    scan_stats[asset]["price_out_of_range"] += 1
                    # Compute raw edge for instrumentation (pre-temperature, pre-blend)
                    _por_edge = cal_prob - best_ask / 100.0
                    _por_fee = calculate_fee(
                        1, best_ask, is_taker=True,
                        fee_mult_taker=_pricecfg.fee_multiplier_taker,
                        fee_mult_maker=_pricecfg.fee_multiplier_maker)
                    _por_fee_edge = _por_edge - _por_fee / 100.0
                    _por_z = prob_result.get("z_score")
                    self._recent_opportunities.append({
                        "ticker": ticker, "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask, "edge_bps": round(_por_edge * 10000) if _por_edge else None,
                        "chosen_strategy": None,
                        "rejection_reason": "price_out_of_range",
                        "ts": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    })
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "price_out_of_range",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": f"best_ask {best_ask}¢ outside [{_entry_floor}, {MAX_ENTRY_PRICE}]",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": best_ask,
                            "best_ask_source": best_ask_source,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(cal_prob, 6),
                            "best_ask_depth": ask_depth,
                            "total_ob_depth": total_depth,
                            "convergence_velocity": self._scanner_convergence_velocity(ticker),
                            "raw_prob": round(raw_prob_pre, 6) if raw_prob_pre is not None else None,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                        _dedup_key = (ticker, "price_out_of_range")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "price_out_of_range",
                                rejection_reason=f"best_ask {best_ask}¢ outside range",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=cal_prob,
                                edge=_por_edge,
                                fee_adjusted_edge=_por_fee_edge,
                                z_score=_por_z,
                                vol_regime=vol_est["regime"],
                                breakeven_wr=best_ask / 100.0,
                                ask_depth=ask_depth,
                                best_ask_source=best_ask_source,
                                raw_prob=raw_prob_pre,
                                calibration_method=calibration_method_pre,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                hourly_pre_temp_prob=None, hourly_applied_temp_t=None,
                                hourly_shadow_temp_2_0=None, hourly_shadow_temp_1_0=None,
                                hourly_shadow_temp_2_5=None, hourly_shadow_blend_50=None,
                                hourly_shadow_temp_1_75=None, hourly_shadow_temp_3_0=None,
                                hourly_shadow_blend_20=None, hourly_shadow_blend_30=None,
                                hourly_shadow_blend_60=None, hourly_post_temp_prob=None,
                                **_oft_db, **_shadow_diag)
                    except Exception:
                        logging.warning("insert_evaluated_opportunity failed (price_out_of_range)", exc_info=True)
                    if PRICE_SHADOW_ENABLED and PRICE_SHADOW_FLOOR <= best_ask < _entry_floor:
                        _price_shadow_queue.append({
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "best_ask": best_ask,
                            "spot": spot,
                            "threshold": threshold,
                            "blended_rv": blended_rv,
                            "seconds_remaining": seconds_remaining,
                            "vol_regime": vol_est["regime"],
                            "ask_depth": ask_depth,
                            "best_ask_source": best_ask_source,
                            "product_type": window.get("product_type"),
                            "_shadow_diag": _shadow_diag.copy(),
                            "_oft_db": _oft_db.copy(),
                        })
                    # Overnight LP shadow: queue 50-85c 15M contracts during overnight hours
                    _olp_utc_hour = datetime.datetime.now(timezone.utc).hour
                    if (OVERNIGHT_LP_SHADOW
                            and _pt in (None, "15m")
                            and OVERNIGHT_LP_HOURS_START <= _olp_utc_hour < OVERNIGHT_LP_HOURS_END
                            and OVERNIGHT_LP_MIN_ENTRY_PRICE <= best_ask <= OVERNIGHT_LP_MAX_ENTRY_PRICE
                            and OVERNIGHT_LP_MIN_STC <= seconds_remaining <= OVERNIGHT_LP_MAX_STC):
                        _overnight_lp_queue.append({
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "best_ask": best_ask,
                            "best_bid": mkt.get("yes_bid") or (100 - (mkt.get("no_ask") or 100)),
                            "spot": spot,
                            "threshold": threshold,
                            "blended_rv": blended_rv,
                            "seconds_remaining": seconds_remaining,
                            "vol_regime": vol_est["regime"],
                            "ask_depth": ask_depth,
                            "best_ask_source": best_ask_source,
                            "product_type": window.get("product_type"),
                            "_shadow_diag": _shadow_diag.copy(),
                            "_oft_db": _oft_db.copy(),
                        })
                    # Low-price shadow: queue 20-79c 15M signals for dual-sizing analysis
                    if (LOW_PRICE_SHADOW_ENABLED
                            and _pt in (None, "15m")
                            and LOW_PRICE_SHADOW_MIN_PRICE <= best_ask <= LOW_PRICE_SHADOW_MAX_PRICE
                            and seconds_remaining <= LOW_PRICE_SHADOW_MAX_STC):
                        _low_price_shadow_queue.append({
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "best_ask": best_ask,
                            "spot": spot,
                            "threshold": threshold,
                            "blended_rv": blended_rv,
                            "seconds_remaining": seconds_remaining,
                            "vol_regime": vol_est["regime"],
                            "ask_depth": ask_depth,
                            "best_ask_source": best_ask_source,
                            "product_type": window.get("product_type"),
                            "_shadow_diag": _shadow_diag.copy(),
                            "_oft_db": _oft_db.copy(),
                            "has_prob": False,
                        })
                    # NO-side shadow: read actual NO ask from market NBBO
                    _no_ask_por = None
                    _no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                    if _no_ask_raw is not None:
                        _no_ask_por = dollars_str_to_cents(_no_ask_raw) if isinstance(_no_ask_raw, str) else int(_no_ask_raw)
                    if _no_ask_por is not None and _no_ask_por <= 0:
                        _no_ask_por = None
                    if _no_ask_por is not None and NO_SIDE_MIN_ENTRY_PRICE <= _no_ask_por <= _entry_ceil:
                        _no_side_queue.append({
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "best_ask": best_ask,
                            "no_ask": _no_ask_por,
                            "spot": spot,
                            "threshold": threshold,
                            "blended_rv": blended_rv,
                            "seconds_remaining": seconds_remaining,
                            "vol_regime": vol_est["regime"],
                            "ask_depth": ask_depth,
                            "best_ask_source": best_ask_source,
                            "product_type": window.get("product_type"),
                            "_shadow_diag": _shadow_diag.copy(),
                            "_oft_db": _oft_db.copy(),
                            "_shadow_extra": _shadow_extra.copy(),
                            "final_prob": None,  # needs computation
                            "cal_prob": cal_prob,
                            "raw_prob": raw_prob_pre,
                            "calibration_method": calibration_method_pre,
                            "hourly_pre_temp_prob": None,  # POR path — before temp computation
                            "hourly_applied_temp_t": None,
                            "hourly_post_temp_prob": None,
                        })

                    # DC NO-side shadow: z≥5 means spot is FAR above strike (YES worthless, NO is the bet)
                    # Must run here because price_out_of_range blocks the main DC shadow block downstream.
                    # These tickers have best_ask=0-1c (YES side) but NO side may have real depth.
                    # Uses _por_z (computed at line 6711) — z_score is not defined until later in the loop.
                    if (DECIDED_CONTRACT_SHADOW
                            and _pt in (None, "15m")
                            and _por_z is not None
                            and _por_z >= 5.0
                            and best_ask <= 20
                            and seconds_remaining < DECIDED_CONTRACT_MAX_STC):
                        _no_ask_dc_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                        _no_ask_dc = None
                        if _no_ask_dc_raw is not None:
                            _no_ask_dc = (dollars_str_to_cents(_no_ask_dc_raw)
                                          if isinstance(_no_ask_dc_raw, str)
                                          else int(_no_ask_dc_raw))
                        if _no_ask_dc is not None and _no_ask_dc > 0 and _no_ask_dc <= 93:
                            _dcs_dedup_no = (ticker, "dc_shadow_no_side")
                            if _dcs_dedup_no not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_dcs_dedup_no)
                                _no_fee_dc = calculate_fee(1, _no_ask_dc, is_taker=True,
                                                           fee_mult_taker=get_market_config("15m").fee_multiplier_taker,
                                                           fee_mult_maker=get_market_config("15m").fee_multiplier_maker)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset, "dc_shadow_no_side",
                                        rejection_reason="shadow: z={:.1f} no_ask={}c yes_price={}c (NO-side decided, POR path)".format(
                                            _por_z, _no_ask_dc, best_ask),
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=_no_ask_dc,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=1.0 - (best_ask / 100.0),
                                        edge=round((1.0 - best_ask / 100.0) - _no_ask_dc / 100.0, 6),
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=_por_z, vol_regime=vol_est["regime"],
                                        raw_prob=raw_prob_pre,
                                        fee_adjusted_edge=round(
                                            (1.0 - best_ask / 100.0) - _no_ask_dc / 100.0 - _no_fee_dc / 100.0, 6),
                                        product_type=window.get("product_type"),
                                        side="no",
                                        **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (dc_shadow_no_side POR)", exc_info=True)

                    continue

                # ── Per-asset price floor (15M only) ─────────────────────────
                # BTC 89c+: 86-88c below taker BE. ETH 75c+ (30-contract cap sub-80c).
                # SOL 80c+. XRP 92c+: PnL-negative at every floor below 90c.
                _asset_floor = MIN_ENTRY_PRICE  # default
                if _pt in (None, "15m"):
                    if asset == "BTC":
                        _asset_floor = BTC_MIN_ENTRY_PRICE
                    elif asset == "ETH":
                        _asset_floor = ETH_MIN_ENTRY_PRICE
                    elif asset == "SOL":
                        _asset_floor = SOL_MIN_ENTRY_PRICE
                    elif asset == "XRP":
                        _asset_floor = XRP_MIN_ENTRY_PRICE
                if _pt in (None, "15m") and best_ask < _asset_floor:
                    # ── LPNE intercept: BTC 80-87c near-expiry ──────────────
                    # Data: BTC 80-87c at STC<=120s = 97.6% WR (42 obs), p=0.031.
                    # Intercept BEFORE floor rejection. BTC ONLY — see LPNE_ASSETS.
                    if (LPNE_ENABLED
                            and not OBSERVATION_MODE
                            and asset in LPNE_ASSETS
                            and LPNE_MIN_PRICE <= best_ask <= LPNE_MAX_PRICE
                            and LPNE_MIN_STC <= seconds_remaining <= LPNE_MAX_STC
                            and cal_prob >= best_ask / 100.0):  # model must believe at least break-even
                        _lpne_dc_overlap = any(
                            c["ticker"] == ticker and c.get("strategy", "").startswith("decided_")
                            for c in candidates)
                        if not _lpne_dc_overlap:
                            _lpne_has_position = any(
                                p["ticker"] == ticker
                                for p in self._state.get_open_positions())
                            if not _lpne_has_position:
                                _lpne_count = sum(1 for c in candidates if c.get("strategy") == "low_price_near_expiry")
                                if _lpne_count < LPNE_MAX_CONCURRENT:
                                    logging.info(
                                        "LPNE_CANDIDATE: %s %s %dx@%dc prob=%.3f stc=%.0fs",
                                        asset, ticker, LPNE_FIXED_CONTRACTS, best_ask,
                                        cal_prob, seconds_remaining)
                                    candidates.append({
                                        "ticker": ticker,
                                        "event_ticker": window["event_ticker"],
                                        "asset": asset,
                                        "product_type": window.get("product_type"),
                                        "spot": spot,
                                        "threshold": threshold,
                                        "seconds_to_close": round(seconds_remaining, 1),
                                        "blended_rv": blended_rv,
                                        "calibrated_prob": round(cal_prob, 6),
                                        "z_score": prob_result.get("z_score"),
                                        "best_yes_ask": best_ask,
                                        "best_ask_source": best_ask_source,
                                        "edge": round(cal_prob - best_ask / 100.0, 6),
                                        "fee_adjusted_edge": round((cal_prob - best_ask / 100.0) - (calculate_fee(1, best_ask, is_taker=True, fee_mult_taker=_pricecfg.fee_multiplier_taker, fee_mult_maker=_pricecfg.fee_multiplier_maker) / 100.0), 6),
                                        "position_size": LPNE_FIXED_CONTRACTS,
                                        "kelly_f": 0.0,
                                        "drawdown_scaler": 1.0,
                                        "vol_regime": vol_est["regime"],
                                        "balance_at_scan": self._get_balance_cached(),
                                        "strategy": "low_price_near_expiry",
                                        "strategy_scores": {"certainty": 1.0, "certainty_detail": "lpne",
                                                            "orderbook": 0.5, "orderbook_detail": "n/a",
                                                            "urgency": 1.0, "urgency_detail": "lpne",
                                                            "composite": 1.0, "reason": "low_price_near_expiry"},
                                        "ob_snapshot": {
                                            "best_ask": best_ask,
                                            "ask_depth": ask_depth,
                                            "total_depth": total_depth,
                                            "best_bid": OrderExecutor._best_yes_bid(ob_data) if ob_data else None,
                                            "bid_depth": OrderExecutor._best_yes_bid_depth(ob_data) if ob_data else 0,
                                            "spread": (best_ask - OrderExecutor._best_yes_bid(ob_data))
                                                      if ob_data and OrderExecutor._best_yes_bid(ob_data) is not None else None,
                                        },
                                        "calibrated_prob_raw": round(cal_prob, 6),
                                        "ofa_adjustment": 0.0,
                                        "ofa_confidence": "none",
                                        "raw_prob": raw_prob_pre,
                                    })
                                    _lpne_dedup = (ticker, "low_price_near_expiry")
                                    if _lpne_dedup not in self._eval_opp_seen:
                                        self._eval_opp_seen.add(_lpne_dedup)
                                        try:
                                            self._state.insert_evaluated_opportunity(
                                                ticker, window["event_ticker"], asset,
                                                "low_price_near_expiry",
                                                spot_price=spot, threshold=threshold,
                                                volatility=blended_rv, market_price=best_ask,
                                                seconds_to_close=seconds_remaining,
                                                calibrated_prob=final_prob,
                                                edge=cal_prob - best_ask / 100.0,
                                                z_score=prob_result.get("z_score"),
                                                vol_regime=vol_est["regime"],
                                                raw_prob=raw_prob_pre,
                                                calibration_method=calibration_method_pre,
                                                fee_adjusted_edge=round((cal_prob - best_ask / 100.0) - (calculate_fee(1, best_ask, is_taker=True, fee_mult_taker=_pricecfg.fee_multiplier_taker, fee_mult_maker=_pricecfg.fee_multiplier_maker) / 100.0), 6),
                                                breakeven_wr=best_ask / 100.0,
                                                ask_depth=ask_depth,
                                                best_ask_source=best_ask_source,
                                                position_size=LPNE_FIXED_CONTRACTS,
                                                kelly_f=0.0,
                                                drawdown_scaler=1.0,
                                                strategy="low_price_near_expiry",
                                                product_type=window.get("product_type"),
                                                **_oft_db, **_shadow_diag)
                                        except Exception:
                                            logging.warning("insert_evaluated_opportunity failed (lpne)", exc_info=True)
                                    continue  # Skip floor rejection — this is now an LPNE candidate

                    _frs_edge = cal_prob - best_ask / 100.0
                    _frs_fee = calculate_fee(
                        1, best_ask, is_taker=True,
                        fee_mult_taker=_pricecfg.fee_multiplier_taker,
                        fee_mult_maker=_pricecfg.fee_multiplier_maker)
                    _frs_fee_edge = _frs_edge - _frs_fee / 100.0
                    # Tag aggressive-floor shadow variants for forward validation
                    _frs_stage = "floor_raise_shadow"
                    if asset == "ETH" and best_ask >= 70:
                        _frs_stage = "eth_low_floor_shadow"
                    _dedup_key = (ticker, _frs_stage)
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset,
                            _frs_stage,
                            rejection_reason=f"{asset} floor {_asset_floor}c (global {MIN_ENTRY_PRICE}c), ask={best_ask}c",
                            spot_price=spot, threshold=threshold,
                            volatility=blended_rv, market_price=best_ask,
                            seconds_to_close=seconds_remaining,
                            calibrated_prob=cal_prob,
                            edge=_frs_edge,
                            fee_adjusted_edge=_frs_fee_edge,
                            z_score=prob_result.get("z_score"),
                            vol_regime=vol_est["regime"],
                            breakeven_wr=best_ask / 100.0,
                            ask_depth=ask_depth,
                            best_ask_source=best_ask_source,
                            raw_prob=raw_prob_pre,
                            calibration_method=calibration_method_pre,
                            product_type=window.get("product_type"),
                            **_oft_db, **_shadow_diag)
                    continue

                # Re-run probability with market price for sanity check
                if _pt == "weather":
                    # Weather: ensemble model doesn't use market price; skip z-score sanity check
                    prob_with_market = prob_result.copy()
                    prob_with_market["tradeable"] = True
                    prob_with_market["z_score"] = 0.0
                else:
                    prob_with_market = ProbabilityEngine.compute(
                        spot, threshold, seconds_remaining, blended_rv,
                        market_price_cents=best_ask,
                        asset=asset, product_type=window.get("product_type")
                    )
                if not prob_with_market.get("tradeable"):
                    reason = prob_with_market.get(
                        "reason", "tradeable_false")
                    # Apr 25 2026 (Phase 1 / Prevention #3): always write
                    # a DB row regardless of reason. R2 [A1]: dedup per
                    # (ticker, reason_class) to avoid commit-in-loop.
                    _reason_class2 = reason.split(" — ")[0].split(" (")[0][:64]
                    _dk_tf2 = (ticker, "tf2:" + _reason_class2)
                    if _dk_tf2 not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dk_tf2)
                        try:
                            # Phase D (shadow coverage expansion 2026-05-02):
                            # this is the ONLY insert_rejection that fires
                            # AFTER the new early annotate site. Strip
                            # cal_mlp_* keys so the splat doesn't TypeError
                            # against insert_rejection's signature (which
                            # never accepted cal_mlp_* per the
                            # _SHADOW_DIAG_KEYS startup assertion at
                            # line ~10254).
                            self._state.insert_rejection(
                                ticker, window["event_ticker"], asset, reason,
                                prob_with_market.get("z_score"), spot, threshold,
                                blended_rv, best_ask, seconds_remaining,
                                prob_with_market.get("calibrated_prob"),
                                raw_prob=prob_with_market.get("raw_prob"),
                                product_type=window.get("product_type"),
                                **_oft_db,
                                **{k: v for k, v in _shadow_diag.items()
                                   if not k.startswith("cal_mlp_")})
                        except Exception:
                            logging.warning(
                                "tradeable_false (with_market) insert_rejection failed",
                                exc_info=True)
                    if "z_score" in reason or "refusing" in reason:
                        rej_data = {
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": reason,
                            "z_score": prob_with_market.get("z_score"),
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": best_ask,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": prob_with_market.get("calibrated_prob"),
                            "raw_prob": prob_with_market.get("raw_prob"),
                            **_shadow_diag,
                            **_shadow_extra,
                        }
                        self._logger.log_rejection(rej_data)
                        logging.info(
                            f"Rejected opportunity: {ticker} — {reason}")
                    continue

                final_prob = prob_with_market["calibrated_prob"]
                z_score = prob_with_market["z_score"]
                raw_prob = prob_with_market.get("raw_prob")
                calibration_method = prob_with_market.get("calibration_method")

                # cal_mlp_request_id annotation moved EARLIER in the
                # iteration (Phase D shadow coverage expansion 2026-05-02)
                # so shadow-stage inserts/queue snapshots that fire BEFORE
                # this point get cal_mlp_request_id stamped via _shadow_diag
                # splat. See the new annotate site after no_best_ask, plus
                # kb/decisions/shadow-coverage-expansion-may01.md.

                # ── Temperature scaling (Layer 1) ──────────────
                _hourly_pre_temp_prob = None
                _tempcfg = get_market_config(window.get("product_type"))
                _temp_t = _tempcfg.temperature_t if _tempcfg.temperature_enabled else None
                _configured_temp_t = _temp_t  # record configured T for instrumentation (before CalEngine override)
                # T=1.0 is identity — skip scaling
                if _temp_t is not None and _temp_t == 1.0:
                    _temp_t = None
                # Skip temperature if registered engine is active (already calibrated)
                _reg_engine_t = _cal_state._resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine_t is not None and _reg_engine_t.is_learned_method_active():
                    _temp_t = None
                    _hourly_pre_temp_prob = final_prob  # still record for shadow instrumentation
                if _temp_t is not None:
                    _hourly_pre_temp_prob = final_prob
                    _p = max(0.001, min(0.999, final_prob))
                    _z = math.log(_p / (1.0 - _p))
                    _z_scaled = _z / _temp_t
                    final_prob = 1.0 / (1.0 + math.exp(-_z_scaled))

                # ── Shadow temperature + blend instrumentation (hourly + SPX) ──
                _hourly_shadow_temp_2_0 = None
                _hourly_shadow_temp_1_0 = None
                _hourly_shadow_temp_2_5 = None
                _hourly_shadow_temp_1_75 = None
                _hourly_shadow_temp_3_0 = None
                _hourly_shadow_blend_50 = None
                _hourly_shadow_blend_20 = None
                _hourly_shadow_blend_30 = None
                _hourly_shadow_blend_60 = None
                _hourly_post_temp_prob = None

                _shadow_base = _hourly_pre_temp_prob
                if _shadow_base is None and _pt == "spx_hourly":
                    _shadow_base = final_prob
                    _hourly_pre_temp_prob = final_prob
                    _temp_t = 1.0

                if _shadow_base is not None:
                    _sp = max(0.001, min(0.999, _shadow_base))
                    _sz = math.log(_sp / (1.0 - _sp))

                    # Temperature shadows (pre-blend)
                    _hourly_shadow_temp_2_0 = 1.0 / (1.0 + math.exp(-_sz / 2.0))
                    _hourly_shadow_temp_2_5 = 1.0 / (1.0 + math.exp(-_sz / 2.5))
                    _hourly_shadow_temp_1_75 = 1.0 / (1.0 + math.exp(-_sz / 1.75))
                    _hourly_shadow_temp_3_0 = 1.0 / (1.0 + math.exp(-_sz / 3.0))
                    if _pt == "spx_hourly":
                        _hourly_shadow_temp_1_0 = 1.0 / (1.0 + math.exp(-_sz / 1.5))
                    else:
                        _hourly_shadow_temp_1_0 = _shadow_base

                    # Post-temp prob: tempered value before OFA and blend
                    # For offline analysis: (1-W) * post_temp + W * (mkt/100) = any blend
                    _hourly_post_temp_prob = final_prob

                    # Blend shadows: full final = liveT + shadowW
                    # Uses final_prob (tempered, pre-OFA) — isolates T×W effect
                    if best_ask < ENDGAME_BLEND_PRICE:
                        _mkt_p = best_ask / 100.0
                        _hourly_shadow_blend_20 = 0.80 * final_prob + 0.20 * _mkt_p
                        _hourly_shadow_blend_30 = 0.70 * final_prob + 0.30 * _mkt_p
                        _hourly_shadow_blend_50 = 0.50 * final_prob + 0.50 * _mkt_p
                        _hourly_shadow_blend_60 = 0.40 * final_prob + 0.60 * _mkt_p

                # Order flow adjustment
                ofa_signals = None
                ofa_adjustment = 0.0
                if self._order_flow is not None:
                    try:
                        ofa_signals = self._order_flow.get_signals(asset, ticker=ticker)
                        ofa_adjustment = ofa_signals["prob_adjustment"]
                    except Exception:
                        logging.debug("OrderFlowEngine.get_signals failed", exc_info=True)
                calibrated_prob_raw = final_prob
                # Always compute dynamic cap for counterfactual logging
                _dyn_cap = ProbabilityEngine._dynamic_cap(seconds_remaining, product_type=window.get("product_type"))
                _reg_engine_c = _cal_state._resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine_c is not None and _reg_engine_c.is_learned_method_active():
                    _active_cal = _reg_engine_c
                elif get_market_config(_pt).cal_eligible:
                    _active_cal = _cal_state._CALIBRATION_ENGINE
                else:
                    _active_cal = None
                if _active_cal is not None and _active_cal.is_learned_method_active():
                    # Learned method: no dynamic cap, use safety ceiling only
                    final_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, final_prob + ofa_adjustment))
                else:
                    final_prob = max(0.01, min(_dyn_cap, final_prob + ofa_adjustment))
                # Counterfactual: calibrated prob with dynamic cap applied
                # (after temp promotion, CF5 "old_cal_system" has the full Beta Cal + blend counterfactual)
                _old_system_prob = max(0.01, min(_dyn_cap, calibrated_prob_raw + ofa_adjustment))
                if best_ask < ENDGAME_BLEND_PRICE:
                    _mkt = best_ask / 100.0
                    _old_system_prob = (1.0 - MARKET_BLEND_W) * _old_system_prob + MARKET_BLEND_W * _mkt

                # ── Market-price blending ──────────────────────────────────
                # For mid-range prices, blend model with market to temper overconfidence.
                # Skip blending for endgame (≥96c) where dynamic cap provides the edge.
                _mcfg = get_market_config(window.get("product_type"))
                _effective_blend_w = _mcfg.market_blend_w
                if best_ask < ENDGAME_BLEND_PRICE:
                    market_implied_prob = best_ask / 100.0
                    final_prob = (1.0 - _effective_blend_w) * final_prob + _effective_blend_w * market_implied_prob

                edge = final_prob - best_ask / 100.0

                # Fee-adjusted edge: subtract taker fee per contract.
                # 15M/SPX/weather: conservative 1-contract fee (more contracts = lower per-unit).
                # Hourly: use actual 10-contract batch fee ÷ 10, because fixed sizing means
                # the 1-contract ceiling inflation (ceil(1.75)=2c vs 18c/10=1.8c) kills
                # every sub-60c signal that the observation data showed is profitable.
                if _pt == "hourly":
                    _hourly_batch_fee = calculate_fee(HOURLY_FIXED_CONTRACTS, best_ask, is_taker=True,
                                                      fee_mult_taker=_mcfg.fee_multiplier_taker,
                                                      fee_mult_maker=_mcfg.fee_multiplier_maker)
                    est_fee_1c = _hourly_batch_fee / HOURLY_FIXED_CONTRACTS
                else:
                    est_fee_1c = calculate_fee(1, best_ask, is_taker=True,
                                               fee_mult_taker=_mcfg.fee_multiplier_taker,
                                               fee_mult_maker=_mcfg.fee_multiplier_maker)
                fee_adjusted_edge = edge - est_fee_1c / 100.0

                # ── Weather NO-side shadow edge ──
                if _pt == "weather":
                    _no_prob = 1.0 - final_prob
                    # NO ask from market NBBO
                    _wx_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                    _wx_no_ask = (dollars_str_to_cents(_wx_no_ask_raw) if isinstance(_wx_no_ask_raw, str)
                                  else int(_wx_no_ask_raw)) if _wx_no_ask_raw is not None else None
                    if _wx_no_ask is not None and _wx_no_ask <= 0:
                        _wx_no_ask = None
                    if _wx_no_ask is not None:
                        _no_fee = calculate_fee(1, _wx_no_ask, is_taker=True,
                                                fee_mult_taker=_mcfg.fee_multiplier_taker,
                                                fee_mult_maker=_mcfg.fee_multiplier_maker)
                        _shadow_extra["wx_no_side_edge"] = round(
                            _no_prob - _wx_no_ask / 100.0 - _no_fee / 100.0, 6)

                # ── NO-side shadow queue (all markets that reach edge computation) ──
                # NO ask from market NBBO
                _no_ask_eq_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                _no_ask_eq = (dollars_str_to_cents(_no_ask_eq_raw) if isinstance(_no_ask_eq_raw, str)
                              else int(_no_ask_eq_raw)) if _no_ask_eq_raw is not None else None
                if _no_ask_eq is not None and _no_ask_eq <= 0:
                    _no_ask_eq = None
                if _no_ask_eq is not None:
                    _no_side_queue.append({
                        "ticker": ticker,
                        "event_ticker": window["event_ticker"],
                        "asset": asset,
                        "best_ask": best_ask,
                        "no_ask": _no_ask_eq,
                        "spot": spot,
                        "threshold": threshold,
                        "blended_rv": blended_rv,
                        "seconds_remaining": seconds_remaining,
                        "vol_regime": vol_est["regime"],
                        "ask_depth": ask_depth,
                        "best_ask_source": best_ask_source,
                        "product_type": window.get("product_type"),
                        # Phase D (shadow coverage expansion 2026-05-02):
                        # PROPAGATE cal_mlp_request_id to NO-side shadow rows
                        # so the post-hoc daemon predicts on them too. The
                        # pre-Phase-D R-p7-deploy-r2#MED1 strip was the
                        # opposite design (shadow rows kept raw); master
                        # plan now requires shadow coverage. NO-side gets
                        # the iteration's YES uuid; daemon dispatches per
                        # row's stored `side` column.
                        "_shadow_diag": _shadow_diag.copy(),
                        "_oft_db": _oft_db.copy(),
                        "_shadow_extra": _shadow_extra.copy(),
                        "final_prob": final_prob,  # already computed (temp+blend+cap)
                        "cal_prob": None,  # not needed — final_prob available
                        "raw_prob": raw_prob,
                        "calibration_method": calibration_method,
                        "hourly_pre_temp_prob": _hourly_pre_temp_prob,
                        "hourly_applied_temp_t": _configured_temp_t,
                        "hourly_post_temp_prob": _hourly_post_temp_prob,
                    })

                # ── Augment _shadow_diag with Kalshi OFT fields ──
                if ofa_signals:
                    # Kalshi-specific signals are nested under signals.kalshi_orderbook
                    _koft = ofa_signals.get("signals", {}).get("kalshi_orderbook", {})
                    _shadow_extra["oft_imbalance_ratio"] = _koft.get("imbalance_ratio")
                    _shadow_extra["oft_imbalance_level"] = _koft.get("imbalance_level")
                    _shadow_extra["oft_prob_adjustment"] = _koft.get("prob_adjustment")
                    _shadow_extra["oft_confidence"] = _koft.get("confidence")
                    _shadow_extra["oft_n_snapshots"] = _koft.get("n_snapshots")
                    _shadow_extra["oft_depth_velocity"] = _koft.get("depth_velocity")
                    # OFT fields for DB persistence
                    _oft_db = {
                        "oft_prob_adjustment": _koft.get("prob_adjustment"),
                        "oft_imbalance_ratio": _koft.get("imbalance_ratio"),
                        "oft_n_snapshots": _koft.get("n_snapshots"),
                    }
                    _shadow_extra["oft_ask_velocity"] = _koft.get("ask_velocity")

                # ── Counterfactual analysis: what would each shadow feature produce? ──
                _cf = {}

                # CF1: EGARCH blend ↔ RV-only counterfactual (bidirectional)
                if EGARCH_BLEND_SHADOW_MODE:
                    # Shadow: EGARCH blend not live, show what it would do
                    _cf_ebs = _shadow_diag.get("egarch_blend_sigma")
                    if _cf_ebs and _cf_ebs > 0:
                        _cf_prob = ProbabilityEngine.counterfactual_prob(
                            spot, threshold, seconds_remaining, _cf_ebs, asset, product_type=_pt)
                        if _cf_prob is not None:
                            _cf_edge = _cf_prob - best_ask / 100.0
                            _cf_fee_edge = _cf_edge - est_fee_1c / 100.0
                            _cf["egarch_blend"] = {
                                "prob": _cf_prob, "edge": round(_cf_edge, 6),
                                "fee_edge": round(_cf_fee_edge, 6),
                                "would_trade": _cf_fee_edge >= MIN_EDGE_PCT / 100.0,
                            }
                else:
                    # Promoted: EGARCH blend IS live, show what RV-only would do
                    _cf_rvo = vol_est.get("rv_only_blended")
                    if _cf_rvo and _cf_rvo > 0:
                        _cf_prob = ProbabilityEngine.counterfactual_prob(
                            spot, threshold, seconds_remaining, _cf_rvo, asset, product_type=_pt)
                        if _cf_prob is not None:
                            _cf_edge = _cf_prob - best_ask / 100.0
                            _cf_fee_edge = _cf_edge - est_fee_1c / 100.0
                            _cf["rv_only"] = {
                                "prob": _cf_prob, "edge": round(_cf_edge, 6),
                                "fee_edge": round(_cf_fee_edge, 6),
                                "would_trade": _cf_fee_edge >= MIN_EDGE_PCT / 100.0,
                            }

                # CF2: TV-RK ↔ fixed-RK counterfactual (bidirectional)
                _cf_tv = _shadow_diag.get("shadow_tv_blend_rv")
                if _cf_tv and _cf_tv > 0:
                    _cf_prob = ProbabilityEngine.counterfactual_prob(
                        spot, threshold, seconds_remaining, _cf_tv, asset, product_type=_pt)
                    if _cf_prob is not None:
                        _cf_edge = _cf_prob - best_ask / 100.0
                        _cf_fee_edge = _cf_edge - est_fee_1c / 100.0
                        _cf_key = "tv_rk" if RK_TV_SHADOW_MODE else "fixed_rk"
                        _cf[_cf_key] = {
                            "prob": _cf_prob, "edge": round(_cf_edge, 6),
                            "fee_edge": round(_cf_fee_edge, 6),
                            "would_trade": _cf_fee_edge >= MIN_EDGE_PCT / 100.0,
                        }

                # CF3: Sigmoid QLIKE blend as primary
                _cf_sig = _shadow_extra.get("mz_sigmoid_blend_rv")
                if _cf_sig and _cf_sig > 0:
                    _cf_prob = ProbabilityEngine.counterfactual_prob(
                        spot, threshold, seconds_remaining, _cf_sig, asset, product_type=_pt)
                    if _cf_prob is not None:
                        _cf_edge = _cf_prob - best_ask / 100.0
                        _cf_fee_edge = _cf_edge - est_fee_1c / 100.0
                        _cf["sigmoid_qlike"] = {
                            "prob": _cf_prob, "edge": round(_cf_edge, 6),
                            "fee_edge": round(_cf_fee_edge, 6),
                            "would_trade": _cf_fee_edge >= MIN_EDGE_PCT / 100.0,
                        }

                # CF4: Kalshi OFT adjusted prob
                if ofa_signals and KALSHI_OFT_SHADOW_MODE:
                    _koft_cf = ofa_signals.get("signals", {}).get("kalshi_orderbook", {})
                    _koft_adj = _koft_cf.get("prob_adjustment", 0)
                    if _koft_adj != 0:
                        _cf["kalshi_oft"] = {
                            "prob_adjustment": round(_koft_adj, 6),
                            "adj_prob": round(final_prob + _koft_adj, 6),
                            "imbalance_ratio": _koft_cf.get("imbalance_ratio"),
                            "imbalance_level": _koft_cf.get("imbalance_level"),
                            "depth_velocity": _koft_cf.get("depth_velocity"),
                            "ask_velocity": _koft_cf.get("ask_velocity"),
                            "confidence": _koft_cf.get("confidence"),
                            "n_snapshots": _koft_cf.get("n_snapshots"),
                        }

                # CF5: Old system counterfactual (Beta Cal + 50% market blend)
                if not SHADOW_CAL_PIPELINE and _cal_state._CALIBRATION_ENGINE is not None and _cal_state._CALIBRATION_ENGINE._beta_trained:
                    try:
                        _cf5_cal = _cal_state._CALIBRATION_ENGINE._beta_cal_predict(raw_prob)
                        _cf5_cal = min(_cf5_cal, _dyn_cap)
                        _cf5_cal = _cal_state._CALIBRATION_ENGINE._apply_uncertainty_shrinkage(_cf5_cal)
                        _cf5_cal = max(0.001, min(NUMERICAL_SAFETY_CEILING, _cf5_cal))
                        _cf5_cal = max(0.01, min(NUMERICAL_SAFETY_CEILING, _cf5_cal + ofa_adjustment))
                        if best_ask < ENDGAME_BLEND_PRICE:
                            _cf5_cal = 0.50 * _cf5_cal + 0.50 * (best_ask / 100.0)
                        _cf5_edge = _cf5_cal - best_ask / 100.0
                        _cf5_fee_edge = _cf5_edge - est_fee_1c / 100.0
                        _cf["old_cal_system"] = {
                            "prob": round(_cf5_cal, 6),
                            "edge": round(_cf5_edge, 6),
                            "fee_edge": round(_cf5_fee_edge, 6),
                            "would_trade": _cf5_fee_edge >= MIN_EDGE_PCT / 100.0,
                        }
                    except Exception:
                        pass
                elif SHADOW_CAL_PIPELINE and _cal_state._CALIBRATION_ENGINE is not None:
                    _cf_cal = _cal_state._CALIBRATION_ENGINE.shadow_calibration_pipeline(
                        raw_prob, best_ask, seconds_remaining, ofa_adjustment)
                    if _cf_cal is not None:
                        _cf["cal_pipeline"] = _cf_cal

                _cf_json = json.dumps(_cf) if _cf else None

                # Log divergences: cases where a shadow feature disagrees with production
                _live_would_trade = (fee_adjusted_edge >= MIN_EDGE_PCT / 100.0)
                for _cf_name, _cf_data in _cf.items():
                    _shadow_would = _cf_data.get("would_trade")
                    if _shadow_would is not None and _shadow_would != _live_would_trade:
                        logging.info(
                            "COUNTERFACTUAL DIVERGENCE %s %s: live=%s shadow=%s "
                            "live_edge=%.4f shadow_edge=%.4f",
                            ticker, _cf_name, _live_would_trade, _shadow_would,
                            fee_adjusted_edge, _cf_data.get("fee_edge", 0),
                        )

                # Log shadow calibration pipeline summary every 5 minutes per asset
                if SHADOW_CAL_PIPELINE and "cal_pipeline" in _cf:
                    try:
                        _cp = _cf["cal_pipeline"]
                        if now - self._shadow_cal_last_log.get(asset, 0) >= 300:
                            logging.info(
                                "shadow_cal_pipeline %s: prob=%.4f edge=%.4f fee_edge=%.4f "
                                "would_trade=%s temp=%.3f temp_brier=%.4f "
                                "prod_prob=%.4f prod_edge=%.4f blend_w=%.2f",
                                ticker, _cp["prob"], _cp["edge"], _cp["fee_edge"],
                                _cp["would_trade"], _cp.get("temperature") or 0,
                                _cp.get("temperature_brier") or 0,
                                final_prob, fee_adjusted_edge, MARKET_BLEND_W)
                            self._shadow_cal_last_log[asset] = now
                    except Exception:
                        pass

                # ── DC Shadow Variants (edge-independent) ─────────────────
                # These evaluate BEFORE the edge gate so they see all signals,
                # not just edge-rejected ones. Targets where z-score implies
                # near-certain outcome but price/tier rules exclude from live DC.
                if (DECIDED_CONTRACT_SHADOW
                        and _pt in (None, "15m")
                        and z_score is not None
                        and seconds_remaining < DECIDED_CONTRACT_MAX_STC):

                    def _dc_shadow_insert_pre(stage, rej_detail, **kwargs):
                        _dcs_dedup = (ticker, stage)
                        if _dcs_dedup not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dcs_dedup)
                            try:
                                self._state.insert_evaluated_opportunity(
                                    ticker, window["event_ticker"], asset, stage,
                                    rejection_reason=rej_detail,
                                    spot_price=spot, threshold=threshold,
                                    volatility=blended_rv,
                                    market_price=kwargs.get("market_price", best_ask),
                                    seconds_to_close=seconds_remaining,
                                    calibrated_prob=kwargs.get("calibrated_prob", final_prob),
                                    edge=kwargs.get("edge", edge),
                                    ofa_adjustment=ofa_adjustment,
                                    z_score=z_score,
                                    vol_regime=vol_est["regime"],
                                    raw_prob=raw_prob,
                                    fee_adjusted_edge=kwargs.get("fee_adjusted_edge", fee_adjusted_edge),
                                    product_type=window.get("product_type"),
                                    side=kwargs.get("side"),
                                    **_shadow_diag)
                            except Exception:
                                logging.warning("insert_evaluated_opportunity failed (%s)", stage, exc_info=True)

                    # T1B at 93-94c (live T1B fires at 95c+, this captures 93-94c)
                    if (DECIDED_CONTRACT_Z_T1 < z_score <= DECIDED_CONTRACT_Z_T1B
                            and 93 <= best_ask < DECIDED_CONTRACT_T1B_MIN_PRICE):
                        _dc_shadow_insert_pre("dc_shadow_t1b_93c",
                                              "shadow: z={:.1f} price={}c (T1B 93-94c expansion)".format(z_score, best_ask))

                    # T2 price floor 90c for BTC/ETH/SOL (live T2 requires 93c+)
                    if (z_score <= DECIDED_CONTRACT_Z_T2
                            and 90 <= best_ask < DECIDED_CONTRACT_MIN_PRICE
                            and asset in ("BTC", "ETH", "SOL")):
                        _dc_shadow_insert_pre("dc_shadow_t2_90c",
                                              "shadow: z={:.1f} price={}c asset={} (T2 90c floor)".format(z_score, best_ask, asset))

                    # T2 price floor 90c for XRP only
                    if (z_score <= DECIDED_CONTRACT_Z_T2
                            and 90 <= best_ask < DECIDED_CONTRACT_MIN_PRICE
                            and asset == "XRP"):
                        _dc_shadow_insert_pre("dc_shadow_t2_90c_xrp",
                                              "shadow: z={:.1f} price={}c (T2 90c XRP)".format(z_score, best_ask))

                    # NO-side decided (z≥5 → YES nearly worthless, NO is the bet)
                    if z_score >= 5.0 and best_ask <= 20:
                        _no_ask_dc_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                        _no_ask_dc = None
                        if _no_ask_dc_raw is not None:
                            _no_ask_dc = (dollars_str_to_cents(_no_ask_dc_raw)
                                          if isinstance(_no_ask_dc_raw, str)
                                          else int(_no_ask_dc_raw))
                        if _no_ask_dc is not None and _no_ask_dc > 0 and _no_ask_dc <= 93:
                            _no_fee = calculate_fee(1, _no_ask_dc, is_taker=True,
                                                    fee_mult_taker=get_market_config("15m").fee_multiplier_taker,
                                                    fee_mult_maker=get_market_config("15m").fee_multiplier_maker)
                            _dc_shadow_insert_pre("dc_shadow_no_side",
                                                  "shadow: z={:.1f} no_ask={}c yes_price={}c fee={}c (NO-side decided)".format(
                                                      z_score, _no_ask_dc, best_ask, _no_fee),
                                                  market_price=_no_ask_dc,
                                                  calibrated_prob=1.0 - (best_ask / 100.0),
                                                  edge=round((1.0 - best_ask / 100.0) - _no_ask_dc / 100.0, 6),
                                                  fee_adjusted_edge=round(
                                                      (1.0 - best_ask / 100.0) - _no_ask_dc / 100.0 - _no_fee / 100.0, 6),
                                                  side="no")

                # Filter: fee-adjusted edge must meet price-dependent minimum
                if _pt == "weather":
                    _min_edge = WEATHER_MIN_EDGE_PCT
                elif _pt == "hourly":
                    _min_edge = HOURLY_MIN_EDGE_PCT
                else:
                    _min_edge = get_min_edge(best_ask)
                    if asset == "SOL":
                        _min_edge = max(_min_edge, SOL_MIN_EDGE)
                # SOL high-edge shadow: log but don't block (5%+ band is 80% WR, PnL-negative)
                _sol_high_edge_shadow = (asset == "SOL" and fee_adjusted_edge > SOL_HIGH_EDGE_SHADOW
                                         and _pt in (None, "15m"))
                if fee_adjusted_edge < _min_edge:
                    # ── Terminal Momentum intercept ──────────────────────────
                    # Before rejecting as insufficient_edge, check if this contract
                    # qualifies for the terminal momentum strategy: extreme price,
                    # high model confidence, final minutes before expiry.
                    _tm_intercepted = False
                    # R-p7-deploy-r10 Round-2 H1: init at outer scope. The
                    # nested-block init (deep inside TM eligibility chain)
                    # caused UnboundLocalError on the dominant path: any
                    # insufficient-edge candidate that DIDN'T match TM
                    # eligibility (price not in {96,98,99}, etc.) reached
                    # the read site without the local being assigned.
                    _tm96_gate_blocked_trade = False
                    if (TERMINAL_MOMENTUM_ENABLED
                            and not OBSERVATION_MODE
                            and _pt in (None, "15m")
                            and best_ask in TM_PRICE_SET
                            and final_prob >= TM_MIN_PROB
                            and TM_MIN_STC <= seconds_remaining <= TM_MAX_STC
                            # T1 (2026-05-10): shadow assets must not route live through TM
                            # The XRP_15M_SHADOW gate downstream fires AFTER candidate.append,
                            # so per-strategy asset gating is required here. See
                            # tests/test_doge_hype_onboarding_t1.py::TestAtomicActivationSafety.
                            and not (HYPE_15M_SHADOW and asset == "HYPE")
                            and not (DOGE_15M_SHADOW and asset == "DOGE")):
                        # Check DC overlap: skip if ticker already claimed by DC
                        _tm_dc_overlap = any(c["ticker"] == ticker and c.get("strategy", "").startswith("decided_")
                                             for c in candidates)
                        if not _tm_dc_overlap:
                            # Check position overlap: skip if we already hold TM at THIS price
                            # Stacking at different prices is allowed — rising prices = confirmation signal.
                            # Data: 40/40 stackable tickers settled YES, 0/4 losses had stacking opportunities.
                            _tm_target_group = f"terminal_momentum_{best_ask}"
                            if STACKING_ENABLED:
                                from bot.models import strategy_to_group
                                _tm_has_position = any(
                                    p["ticker"] == ticker
                                    and p.get("strategy_group",
                                        strategy_to_group(p.get("strategy", "")))
                                        == _tm_target_group
                                    for p in self._state.get_open_positions())
                            else:
                                _tm_has_position = any(
                                    p["ticker"] == ticker
                                    for p in self._state.get_open_positions())
                            if not _tm_has_position:
                                # Check concurrent TM position cap
                                _tm_count = sum(1 for c in candidates if c.get("strategy", "").startswith("terminal_momentum"))
                                if _tm_count < TM_MAX_CONCURRENT:
                                    # NBBO gate: two layers.
                                    # 1) Block 96-97c on NBBO entirely (data: 181 trades -$326; orderbook 21/21 +$60)
                                    # 2) At 98-99c, require minimum buffer on NBBO (thin buf = stale pricing)
                                    # Orderbook-sourced TM trades pass freely at all prices.
                                    _tm_buf_pct = (spot - threshold) / threshold * 100 if threshold and threshold > 0 else 0
                                    _tm_nbbo_blocked = False
                                    if best_ask_source == "market_nbbo":
                                        if best_ask in TM_NBBO_BLOCKED_PRICES:
                                            _tm_nbbo_blocked = True
                                        elif _tm_buf_pct < TM_NBBO_MIN_BUFFER_PCT:
                                            _tm_nbbo_blocked = True
                                    if _tm_nbbo_blocked:
                                        _tm_nbbo_reason = (f"price {best_ask}c in BLOCKED_PRICES"
                                                           if best_ask in TM_NBBO_BLOCKED_PRICES
                                                           else f"buf={_tm_buf_pct:.3f}% < {TM_NBBO_MIN_BUFFER_PCT}%")
                                        logging.info(
                                            "TM_NBBO_GATE: %s %s @%dc buf=%.3f%% (%s, skipping)",
                                            asset, ticker, best_ask, _tm_buf_pct, _tm_nbbo_reason)
                                        # Log as shadow for counterfactual tracking
                                        _tm_dedup_shadow = (ticker, "tm_nbbo_buffer_shadow")
                                        if _tm_dedup_shadow not in self._eval_opp_seen:
                                            self._eval_opp_seen.add(_tm_dedup_shadow)
                                            try:
                                                self._state.insert_evaluated_opportunity(
                                                    ticker, window["event_ticker"], asset,
                                                    "tm_nbbo_buffer_shadow",
                                                    rejection_reason=f"TM NBBO {_tm_nbbo_reason} (ask={best_ask}c stc={seconds_remaining:.0f}s)",
                                                    spot_price=spot, threshold=threshold,
                                                    volatility=blended_rv, market_price=best_ask,
                                                    seconds_to_close=seconds_remaining,
                                                    calibrated_prob=final_prob, edge=edge,
                                                    ofa_adjustment=ofa_adjustment,
                                                    strategy=f"terminal_momentum_{best_ask}",
                                                    z_score=z_score, raw_prob=raw_prob,
                                                    fee_adjusted_edge=fee_adjusted_edge,
                                                    best_ask_source=best_ask_source,
                                                    product_type="15m", **_shadow_diag)
                                            except Exception:
                                                logging.warning("insert_evaluated_opportunity failed (tm_nbbo_buffer)", exc_info=True)
                                    else:
                                        _tm_intercepted = True
                                    # R-p7-deploy-r10: cal_mlp lower-bound gate for TM-96.
                                    # Evaluated whenever _tm_intercepted (i.e., we'd otherwise
                                    # trade) AND best_ask == 96. ALWAYS shadow-logs when
                                    # would_block=True (independent of env-flag, so we have
                                    # data to validate the gate's PnL impact before flipping
                                    # live). Only the trade-block (_tm_intercepted=False) is
                                    # env-gated by TM96_CALMLP_GATE_ENABLED.
                                    # R-p7-deploy-r10 Round-1 #1: pass calibrated_prob (final_prob,
                                    # post-CalEngine) so gate's prob_breakeven_gap matches the
                                    # training-time formula. Round-1 #2: shadow row writes
                                    # on would_block regardless of env-flag. Round-2 H1:
                                    # _tm96_gate_blocked_trade init moved to outer scope.
                                    if _tm_intercepted and best_ask == 96:
                                        from integration import should_block_tm96
                                        _tm96_would_block, _tm96_diag = should_block_tm96(
                                            predictor=_calmlp_predictors.get(asset),
                                            raw_prob=raw_prob, calibrated_prob=final_prob,
                                            ticker=ticker, market_price=best_ask,
                                            seconds_to_close=seconds_remaining,
                                            spot=spot, threshold=threshold,
                                            blended_rv=blended_rv,
                                            vol_regime=(vol_est or {}).get("regime", "normal"),
                                        )
                                        logging.info(
                                            "TM96_CALMLP_GATE: %s %s @%dc raw=%.4f "
                                            "p_mean=%s lo=%s would_block=%s enabled=%s",
                                            asset, ticker, best_ask, raw_prob,
                                            _tm96_diag.get('cal_mlp_p_mean'),
                                            _tm96_diag.get('cal_mlp_final_lo'),
                                            _tm96_would_block, TM96_CALMLP_GATE_ENABLED)
                                        # ALWAYS write the shadow row on would_block — even
                                        # when env-flag is off — so the validation query
                                        # `WHERE filter_stage='tm96_calmlp_gate_blocked'`
                                        # has data to join with settlement outcomes.
                                        if _tm96_would_block:
                                            _tm96_dedup_shadow = (ticker, "tm96_calmlp_gate_blocked")
                                            if _tm96_dedup_shadow not in self._eval_opp_seen:
                                                self._eval_opp_seen.add(_tm96_dedup_shadow)
                                                try:
                                                    _tm96_diag_clean = {k: v for k, v in _tm96_diag.items()
                                                                          if k.startswith('cal_mlp_')}
                                                    self._state.insert_evaluated_opportunity(
                                                        ticker, window["event_ticker"], asset,
                                                        "tm96_calmlp_gate_blocked",
                                                        rejection_reason=(
                                                            f"cal_mlp_final_lo={_tm96_diag.get('cal_mlp_final_lo')} "
                                                            f"< market_price/100={best_ask/100} "
                                                            f"(env_enabled={TM96_CALMLP_GATE_ENABLED}, "
                                                            f"shadow_only={not TM96_CALMLP_GATE_ENABLED}, "
                                                            f"trade_actually_blocked={TM96_CALMLP_GATE_ENABLED})"),
                                                        spot_price=spot, threshold=threshold,
                                                        volatility=blended_rv, market_price=best_ask,
                                                        seconds_to_close=seconds_remaining,
                                                        calibrated_prob=final_prob, edge=edge,
                                                        ofa_adjustment=ofa_adjustment,
                                                        strategy=f"terminal_momentum_{best_ask}",
                                                        z_score=z_score, raw_prob=raw_prob,
                                                        fee_adjusted_edge=fee_adjusted_edge,
                                                        best_ask_source=best_ask_source,
                                                        product_type="15m",
                                                        **{k: v for k, v in _shadow_diag.items()
                                                           if not k.startswith('cal_mlp_')},
                                                        **_tm96_diag_clean)
                                                except Exception:
                                                    logging.warning("insert_evaluated_opportunity failed (tm96_calmlp_gate)", exc_info=True)
                                            # Round-1 #3: distinguish "trade was actually
                                            # blocked" from "would-have-been-blocked" so the
                                            # outer `if _tm_intercepted` falls-through-to-
                                            # insufficient_edge logic doesn't double-count.
                                            if TM96_CALMLP_GATE_ENABLED:
                                                _tm_intercepted = False
                                                _tm96_gate_blocked_trade = True
                                    if _tm_intercepted:
                                        _tm_balance = self._get_balance_cached() or 100000
                                        # Adversary A6: when sweep is live, size against worst-case fill
                                        # price (MAX_ENTRY_PRICE) so per-asset risk cap respects the
                                        # actual capital-at-risk after a sweep up to 99c.
                                        _tm_risk_price = (MAX_ENTRY_PRICE
                                                          if TM_SWEEP_LIVE_ENABLED else None)
                                        _tm_size = tm_compute_contracts(
                                            best_ask, seconds_remaining, _tm_balance, asset,
                                            buf_pct=_tm_buf_pct, risk_cap_price=_tm_risk_price)
                                        logging.info(
                                            "TM_CANDIDATE: %s %s %dx@%dc prob=%.3f stc=%.0fs edge=%.4f margin=%dc stc_zone=%s buf=%.3f%% src=%s",
                                            asset, ticker, _tm_size, best_ask,
                                            final_prob, seconds_remaining, fee_adjusted_edge,
                                            100 - best_ask,
                                            "safe" if seconds_remaining < TM_STC_SAFE_THRESHOLD else
                                            ("danger" if seconds_remaining < TM_STC_DANGER_HI else "normal"),
                                            _tm_buf_pct, best_ask_source)
                                        candidates.append({
                                            "ticker": ticker,
                                            "event_ticker": window["event_ticker"],
                                            "asset": asset,
                                            "product_type": window.get("product_type"),
                                            "spot": spot,
                                            "threshold": threshold,
                                            "seconds_to_close": round(seconds_remaining, 1),
                                            "blended_rv": blended_rv,
                                            "calibrated_prob": round(final_prob, 6),
                                            "z_score": z_score,
                                            "best_yes_ask": best_ask,
                                            "best_ask_source": best_ask_source,
                                            "edge": round(edge, 6),
                                            "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                            "position_size": _tm_size,
                                            "kelly_f": 0.0,
                                            "drawdown_scaler": 1.0,
                                            "vol_regime": vol_est["regime"],
                                            "balance_at_scan": self._get_balance_cached(),
                                            "spot_buffer_pct": round(_tm_buf_pct, 4),
                                            "strategy": f"terminal_momentum_{best_ask}",
                                            "strategy_scores": {"certainty": 1.0, "certainty_detail": "terminal_momentum",
                                                                "orderbook": 0.5, "orderbook_detail": "n/a",
                                                                "urgency": 1.0, "urgency_detail": "terminal_momentum",
                                                                "composite": 1.0, "reason": "terminal_momentum"},
                                            "ob_snapshot": {
                                                "best_ask": best_ask,
                                                "ask_depth": ask_depth,
                                                "total_depth": total_depth,
                                                "best_bid": OrderExecutor._best_yes_bid(ob_data) if ob_data else None,
                                                "bid_depth": OrderExecutor._best_yes_bid_depth(ob_data) if ob_data else 0,
                                                "spread": (best_ask - OrderExecutor._best_yes_bid(ob_data))
                                                          if ob_data and OrderExecutor._best_yes_bid(ob_data) is not None else None,
                                            },
                                            "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                                            "ofa_adjustment": round(ofa_adjustment, 6),
                                            "ofa_confidence": ofa_signals["confidence"] if ofa_signals else "none",
                                            "raw_prob": raw_prob,
                                            "calibration_method": calibration_method,
                                            "old_system_prob": round(_old_system_prob, 6),
                                            **_shadow_diag,
                                            **_shadow_extra,
                                        })
                                        # Log to evaluated_opportunities
                                        _tm_dedup = (ticker, f"terminal_momentum_{best_ask}")
                                        if _tm_dedup not in self._eval_opp_seen:
                                            self._eval_opp_seen.add(_tm_dedup)
                                            try:
                                                self._state.insert_evaluated_opportunity(
                                                    ticker, window["event_ticker"], asset,
                                                    "terminal_momentum",
                                                    rejection_reason=None,
                                                    spot_price=spot, threshold=threshold,
                                                    volatility=blended_rv, market_price=best_ask,
                                                    seconds_to_close=seconds_remaining,
                                                    calibrated_prob=final_prob, edge=edge,
                                                    ofa_adjustment=ofa_adjustment,
                                                    strategy=f"terminal_momentum_{best_ask}",
                                                    z_score=z_score,
                                                    vol_regime=vol_est["regime"],
                                                    calibrated_prob_raw=calibrated_prob_raw,
                                                    kelly_f=0.0,
                                                    position_size=_tm_size,
                                                    breakeven_wr=best_ask / 100.0,
                                                    ask_depth=ask_depth,
                                                    best_ask_source=best_ask_source,
                                                    raw_prob=raw_prob,
                                                    calibration_method=calibration_method,
                                                    fee_adjusted_edge=fee_adjusted_edge,
                                                    product_type=window.get("product_type"),
                                                    **_shadow_diag)
                                            except Exception:
                                                logging.warning("insert_evaluated_opportunity failed (terminal_momentum)", exc_info=True)

                    if _tm_intercepted:
                        continue  # Skip insufficient_edge rejection — this is now a TM candidate

                    # R-p7-deploy-r10 Round-1 #3: when TM-96 gate blocks the
                    # trade, _tm_intercepted=False but the candidate has
                    # already been logged as `tm96_calmlp_gate_blocked`.
                    # Falling through to `insufficient_edge` would double-
                    # count the funnel (one row + one JSONL entry both
                    # claiming the same window). Skip the insufficient_edge
                    # path for gate-blocked candidates.
                    if _tm96_gate_blocked_trade:
                        continue

                    scan_stats[asset]["insufficient_edge"] += 1
                    self._recent_opportunities.append({
                        "ticker": ticker, "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask,
                        "edge_bps": round(fee_adjusted_edge * 10000),
                        "gross_edge_bps": round(edge * 10000),
                        "chosen_strategy": None,
                        "rejection_reason": "insufficient_edge",
                        "ts": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    })
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "insufficient_edge",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": f"net_edge {fee_adjusted_edge:.4f} < min {_min_edge:.4f} @{best_ask}c (gross {edge:.4f}, fee {est_fee_1c}c)",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": best_ask,
                            "best_ask_source": best_ask_source,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(final_prob, 6),
                            "edge": round(edge, 6),
                            "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                            "ofa_adjustment": round(ofa_adjustment, 6),
                            "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                            "old_system_prob": round(_old_system_prob, 6),
                            "kalshi_oft": (ofa_signals or {}).get("signals", {}).get("kalshi_orderbook", {}),
                            "counterfactual": _cf,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                        _dedup_key = (ticker, "insufficient_edge")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            # Compute sizing for instrumentation (pure math, no side effects)
                            _ie_kelly_f = None
                            _ie_position = None
                            _ie_drawdown = None
                            _ie_balance = self._get_balance_cached()
                            if _ie_balance and _ie_balance > 0:
                                _ie_sizing = self._sizer.compute(final_prob, best_ask, _ie_balance)
                                _ie_kelly_f = _ie_sizing["kelly_f"]
                                _ie_position = _ie_sizing["contracts"]
                                _ie_drawdown = _ie_sizing["drawdown_scaler"]
                                # Apply product-type Kelly fraction + risk cap
                                _ie_scfg = get_market_config(window.get("product_type"))
                                if _ie_scfg.kelly_fraction < 1.0:
                                    _ie_position = max(1, int(_ie_position * _ie_scfg.kelly_fraction))
                                _ie_type_max = int((_ie_balance * _ie_scfg.max_risk_per_trade) / best_ask)
                                if _ie_position > _ie_type_max:
                                    _ie_position = max(1, _ie_type_max)
                            # Compute strategy for instrumentation (pure computation, no side effects)
                            _ie_strategy = None
                            try:
                                _ie_scfg2 = get_market_config(window.get("product_type"))
                                _ie_strat_data = {
                                    "z_score": z_score,
                                    "calibrated_prob": final_prob,
                                    "spot": spot, "threshold": threshold,
                                    "seconds_to_close": seconds_remaining,
                                    "blended_rv": blended_rv,
                                    "vol_regime": vol_est["regime"],
                                    "best_yes_ask": best_ask,
                                    "best_ask_depth": ask_depth,
                                    "total_ob_depth": total_depth,
                                    "convergence_velocity": self._scanner_convergence_velocity(ticker),
                                    "edge": edge,
                                    "min_entry_price": _ie_scfg2.min_entry_price,
                                    "max_entry_price": _ie_scfg2.max_entry_price,
                                }
                                _ie_strategy, _ = evaluate_execution_strategy(_ie_strat_data)
                            except Exception:
                                logging.debug("IE strategy computation failed", exc_info=True)
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "insufficient_edge",
                                rejection_reason=f"net_edge {fee_adjusted_edge:.4f} < min",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge,
                                ofa_adjustment=ofa_adjustment,
                                strategy=_ie_strategy,
                                z_score=z_score,
                                vol_regime=vol_est["regime"],
                                calibrated_prob_raw=calibrated_prob_raw,
                                kelly_f=_ie_kelly_f,
                                position_size=_ie_position,
                                drawdown_scaler=_ie_drawdown,
                                breakeven_wr=best_ask / 100.0,
                                expected_value=round(_ev, 2),
                                ask_depth=ask_depth,
                                best_ask_source=best_ask_source,
                                ofa_confidence=ofa_signals["confidence"] if ofa_signals else "none",
                                raw_prob=raw_prob,
                                calibration_method=calibration_method,
                                old_system_prob=_old_system_prob,
                                fee_adjusted_edge=fee_adjusted_edge,
                                counterfactual=_cf_json,
                                shadow_cal_prob=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("prob") if _cf else None,
                                shadow_cal_fee_edge=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("fee_edge") if _cf else None,
                                shadow_cal_temperature=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("temperature") if _cf else None,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                                **_oft_db, **_shadow_diag)
                    except Exception:
                        logging.warning("insert_evaluated_opportunity failed (hourly_observation)", exc_info=True)
                    # V2 variant: shadow cal pipeline (temperature + no blend)
                    if _pt == "hourly" and _cf:
                        self._insert_hourly_v2_variant(
                            ticker, window, asset, raw_prob, best_ask,
                            seconds_remaining, spot, threshold, blended_rv,
                            ofa_adjustment, z_score, vol_est,
                            calibrated_prob_raw, est_fee_1c,
                            ask_depth, best_ask_source, _cf, _shadow_diag)

                    # ── Low-price shadow: queue IE signals at 20-79c ──
                    if (LOW_PRICE_SHADOW_ENABLED
                            and _pt in (None, "15m")
                            and LOW_PRICE_SHADOW_MIN_PRICE <= best_ask <= LOW_PRICE_SHADOW_MAX_PRICE
                            and seconds_remaining <= LOW_PRICE_SHADOW_MAX_STC):
                        _low_price_shadow_queue.append({
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "best_ask": best_ask,
                            "spot": spot,
                            "threshold": threshold,
                            "blended_rv": blended_rv,
                            "seconds_remaining": seconds_remaining,
                            "vol_regime": vol_est["regime"],
                            "ask_depth": ask_depth,
                            "best_ask_source": best_ask_source,
                            "product_type": window.get("product_type"),
                            # Phase D (shadow coverage expansion 2026-05-02):
                            # propagate cal_mlp_request_id so the post-hoc
                            # daemon predicts on low_price_shadow rows too.
                            # See kb/decisions/shadow-coverage-expansion-may01.md.
                            "_shadow_diag": _shadow_diag.copy(),
                            "_oft_db": _oft_db.copy(),
                            "has_prob": True,
                            "final_prob": final_prob,
                            "raw_prob": raw_prob,
                            "calibration_method": calibration_method,
                            "edge": edge,
                            "fee_adjusted_edge": fee_adjusted_edge,
                            "z_score": z_score,
                            "est_fee_1c": est_fee_1c,
                            "calibrated_prob_raw": calibrated_prob_raw,
                        })

                    # ── Weekend Edge Discount (Live + Shadow) ────────────────
                    # On Sat/Sun, re-evaluate 15M insufficient_edge rejections
                    # at relaxed thresholds (0.6x MIN_EDGE_BY_PRICE).
                    # Live: 89c+, STC ≤ 600s, no DC overlap → candidate
                    # Shadow: sub-89c, high STC, or DC overlap → log only
                    if (_pt in (None, "15m")
                            and datetime.datetime.now(timezone.utc).weekday() >= 5
                            and best_ask >= MIN_ENTRY_PRICE):
                        _wknd_discounted_min = _min_edge * WEEKEND_EDGE_DISCOUNT
                        _wknd_discounted_min = min(_wknd_discounted_min, WEEKEND_EDGE_FLOOR)
                        if fee_adjusted_edge >= _wknd_discounted_min:
                            # Compute sizing (shared by live and shadow paths)
                            _wknd_balance = self._get_balance_cached()
                            _wknd_kelly_f = None
                            _wknd_position = None
                            _wknd_ev = None
                            _wknd_drawdown = None
                            if _wknd_balance and _wknd_balance > 0:
                                _wknd_sizing = self._sizer.compute(final_prob, best_ask, _wknd_balance)
                                _wknd_kelly_f = _wknd_sizing["kelly_f"]
                                _wknd_position = _wknd_sizing["contracts"]
                                _wknd_drawdown = _wknd_sizing["drawdown_scaler"]
                                # Apply product-type Kelly fraction + risk cap
                                _wknd_scfg = get_market_config(window.get("product_type"))
                                if _wknd_scfg.kelly_fraction < 1.0:
                                    _wknd_position = max(1, int(_wknd_position * _wknd_scfg.kelly_fraction))
                                _wknd_type_max = int((_wknd_balance * _wknd_scfg.max_risk_per_trade) / best_ask)
                                if _wknd_position > _wknd_type_max:
                                    _wknd_position = max(1, _wknd_type_max)
                                # STC sizing scaler (consistent with main pipeline)
                                if (STC_SIZING_SCALER_ENABLED
                                        and seconds_remaining > STC_SIZING_SCALER_KNEE
                                        and _wknd_position > 0):
                                    _wknd_position = max(1, int(_wknd_position * (STC_SIZING_SCALER_KNEE / seconds_remaining)))
                                _wknd_ev = round((final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c, 2)
                                # Fixed sizing fallback when Kelly produces 0 (edge near zero)
                                if _wknd_position == 0:
                                    _wknd_fixed_raw = max(1, int((_wknd_balance * WEEKEND_FIXED_RISK) / best_ask))
                                    # Apply drawdown scaler to fixed sizing
                                    if _wknd_drawdown is not None and _wknd_drawdown < 1.0:
                                        _wknd_fixed_raw = max(1, int(_wknd_fixed_raw * _wknd_drawdown))
                                    _wknd_position = _wknd_fixed_raw
                                    logging.info(
                                        "WEEKEND_FIXED_SIZE: %s edge=%.4f kelly=0 fixed=%dct risk=%.0f%% balance=%d scaler=%.2f",
                                        ticker, fee_adjusted_edge, _wknd_position,
                                        WEEKEND_FIXED_RISK * 100, _wknd_balance,
                                        _wknd_drawdown if _wknd_drawdown else 1.0)

                            # Check live eligibility gates
                            _wknd_dc_overlap = (z_score is not None
                                                and z_score <= DECIDED_CONTRACT_Z_T2
                                                and best_ask >= DECIDED_CONTRACT_MIN_PRICE
                                                and seconds_remaining < DECIDED_CONTRACT_MAX_STC)
                            _wknd_live_eligible = (
                                WEEKEND_DISCOUNT_LIVE
                                and not OBSERVATION_MODE
                                # T1 (2026-05-10): shadow assets must not route live (T4 gates live promotion)
                                and not (HYPE_15M_SHADOW and asset == "HYPE")
                                and not (DOGE_15M_SHADOW and asset == "DOGE")
                                and best_ask >= WEEKEND_DISCOUNT_MIN_PRICE
                                and seconds_remaining <= WEEKEND_DISCOUNT_MAX_STC
                                and not _wknd_dc_overlap
                                and _wknd_balance and _wknd_balance > 0
                                and _wknd_position and _wknd_position > 0)

                            # Determine filter stage for DB insert
                            _wknd_stage = "weekend_discount" if _wknd_live_eligible else "weekend_discount_shadow"

                            # JSONL log (always — both live and shadow)
                            try:
                                self._logger.log_opportunity({
                                    "filter_stage": _wknd_stage,
                                    "ticker": ticker,
                                    "event_ticker": window["event_ticker"],
                                    "asset": asset,
                                    "side": "yes",
                                    "market_price": best_ask,
                                    "model_prob": round(final_prob, 6),
                                    "edge": round(edge, 6),
                                    "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                    "kelly_f": round(_wknd_kelly_f, 6) if _wknd_kelly_f else None,
                                    "position_size": _wknd_position,
                                    "expected_value": _wknd_ev,
                                    "seconds_to_close": round(seconds_remaining, 1),
                                    "spot_price": spot,
                                    "threshold": threshold,
                                    "volatility": blended_rv,
                                    "vol_regime": vol_est["regime"],
                                    "discount_factor": WEEKEND_EDGE_DISCOUNT,
                                    "original_min_edge": round(_min_edge, 6),
                                    "discounted_min_edge": round(_wknd_discounted_min, 6),
                                    "edge_vs_discounted": round(fee_adjusted_edge - _wknd_discounted_min, 6),
                                    "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                                    "ofa_adjustment": round(ofa_adjustment, 6),
                                    "z_score": z_score,
                                })
                            except Exception:
                                logging.debug("weekend_discount log failed", exc_info=True)

                            # DB insert (always — for settlement tracking)
                            _wknd_dedup = (ticker, _wknd_stage)
                            if _wknd_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_wknd_dedup)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        _wknd_stage,
                                        rejection_reason=f"{'live' if _wknd_live_eligible else 'shadow'}: edge {fee_adjusted_edge:.4f} >= discounted_min {_wknd_discounted_min:.4f} (orig {_min_edge:.4f} x {WEEKEND_EDGE_DISCOUNT})",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score,
                                        vol_regime=vol_est["regime"],
                                        calibrated_prob_raw=calibrated_prob_raw,
                                        kelly_f=_wknd_kelly_f,
                                        position_size=_wknd_position,
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=_wknd_ev,
                                        ask_depth=ask_depth,
                                        best_ask_source=best_ask_source,
                                        raw_prob=raw_prob,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        product_type=window.get("product_type"),
                                        **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (%s)", _wknd_stage, exc_info=True)

                            # Live path: append to candidates for execution
                            if _wknd_live_eligible:
                                logging.info(
                                    "WKND_DISCOUNT_CANDIDATE: %s %dx@%dc edge=%.4f disc_min=%.4f stc=%.0fs",
                                    ticker, _wknd_position, best_ask,
                                    fee_adjusted_edge, _wknd_discounted_min, seconds_remaining)
                                candidates.append({
                                    "ticker": ticker,
                                    "event_ticker": window["event_ticker"],
                                    "asset": asset,
                                    "product_type": window.get("product_type"),
                                    "spot": spot,
                                    "threshold": threshold,
                                    "seconds_to_close": round(seconds_remaining, 1),
                                    "blended_rv": blended_rv,
                                    "calibrated_prob": round(final_prob, 6),
                                    "z_score": z_score,
                                    "best_yes_ask": best_ask,
                                    "best_ask_source": best_ask_source,
                                    "edge": round(edge, 6),
                                    "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                    "position_size": _wknd_position,
                                    "kelly_f": _wknd_kelly_f,
                                    "drawdown_scaler": _wknd_drawdown or 1.0,
                                    "vol_regime": vol_est["regime"],
                                    "balance_at_scan": _wknd_balance,
                                    "strategy": "weekend_discount",
                                    "ob_snapshot": {
                                        "best_ask": best_ask,
                                        "ask_depth": ask_depth,
                                        "total_depth": total_depth,
                                        "best_bid": OrderExecutor._best_yes_bid(ob_data) if ob_data else None,
                                        "bid_depth": OrderExecutor._best_yes_bid_depth(ob_data) if ob_data else 0,
                                        "spread": (best_ask - OrderExecutor._best_yes_bid(ob_data))
                                                  if ob_data and OrderExecutor._best_yes_bid(ob_data) is not None else None,
                                    },
                                    "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                                    "ofa_adjustment": round(ofa_adjustment, 6),
                                    "ofa_confidence": ofa_signals["confidence"] if ofa_signals else "none",
                                    "raw_prob": raw_prob,
                                    "calibration_method": calibration_method,
                                    "old_system_prob": round(_old_system_prob, 6),
                                    **_shadow_diag,
                                    **_shadow_extra,
                                })

                    # ── Overnight Edge Discount (Live + Shadow) ────────────────
                    # On weekday quiet hours (04-11 UTC), re-evaluate 15M
                    # insufficient_edge at relaxed thresholds (0.6x).
                    # Live: 89c+, STC ≤ 600s, no DC overlap → candidate
                    # Shadow: sub-89c, high STC, or DC overlap → log only
                    # Skip if weekend discount already applied (don't double-count).
                    _now_utc = datetime.datetime.now(timezone.utc)
                    _is_weekend = _now_utc.weekday() >= 5
                    if (_pt in (None, "15m")
                            and not _is_weekend
                            and OVERNIGHT_QUIET_START <= _now_utc.hour <= OVERNIGHT_QUIET_END
                            and best_ask >= MIN_ENTRY_PRICE):
                        _ovn_discounted_min = _min_edge * OVERNIGHT_EDGE_DISCOUNT
                        if fee_adjusted_edge >= _ovn_discounted_min:
                            _ovn_balance = self._get_balance_cached()
                            _ovn_kelly_f = None
                            _ovn_position = None
                            _ovn_ev = None
                            _ovn_drawdown = None
                            if _ovn_balance and _ovn_balance > 0:
                                _ovn_sizing = self._sizer.compute(final_prob, best_ask, _ovn_balance)
                                _ovn_kelly_f = _ovn_sizing["kelly_f"]
                                _ovn_position = _ovn_sizing["contracts"]
                                _ovn_drawdown = _ovn_sizing["drawdown_scaler"]
                                _ovn_scfg = get_market_config(window.get("product_type"))
                                if _ovn_scfg.kelly_fraction < 1.0:
                                    _ovn_position = max(1, int(_ovn_position * _ovn_scfg.kelly_fraction))
                                _ovn_type_max = int((_ovn_balance * _ovn_scfg.max_risk_per_trade) / best_ask)
                                if _ovn_position > _ovn_type_max:
                                    _ovn_position = max(1, _ovn_type_max)
                                # STC sizing scaler (consistent with main pipeline)
                                if (STC_SIZING_SCALER_ENABLED
                                        and seconds_remaining > STC_SIZING_SCALER_KNEE
                                        and _ovn_position > 0):
                                    _ovn_position = max(1, int(_ovn_position * (STC_SIZING_SCALER_KNEE / seconds_remaining)))
                                _ovn_ev = round((final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c, 2)

                            # Check live eligibility gates
                            _ovn_dc_overlap = (z_score is not None
                                               and z_score <= DECIDED_CONTRACT_Z_T2_Z2
                                               and best_ask >= DECIDED_CONTRACT_MIN_PRICE
                                               and seconds_remaining < DECIDED_CONTRACT_MAX_STC)
                            _ovn_live_eligible = (
                                OVERNIGHT_DISCOUNT_LIVE
                                and not OBSERVATION_MODE
                                # T1 (2026-05-10): shadow assets must not route live (T4 gates live promotion)
                                and not (HYPE_15M_SHADOW and asset == "HYPE")
                                and not (DOGE_15M_SHADOW and asset == "DOGE")
                                and best_ask >= OVERNIGHT_DISCOUNT_MIN_PRICE
                                and seconds_remaining <= OVERNIGHT_DISCOUNT_MAX_STC
                                and not _ovn_dc_overlap
                                and _ovn_balance and _ovn_balance > 0
                                and _ovn_position and _ovn_position > 0)

                            # Determine filter stage for DB insert
                            _ovn_stage = "overnight_discount" if _ovn_live_eligible else "overnight_discount_shadow"

                            try:
                                self._logger.log_opportunity({
                                    "filter_stage": _ovn_stage,
                                    "ticker": ticker,
                                    "event_ticker": window["event_ticker"],
                                    "asset": asset,
                                    "side": "yes",
                                    "market_price": best_ask,
                                    "model_prob": round(final_prob, 6),
                                    "edge": round(edge, 6),
                                    "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                    "kelly_f": round(_ovn_kelly_f, 6) if _ovn_kelly_f else None,
                                    "position_size": _ovn_position,
                                    "expected_value": _ovn_ev,
                                    "seconds_to_close": round(seconds_remaining, 1),
                                    "spot_price": spot,
                                    "threshold": threshold,
                                    "volatility": blended_rv,
                                    "vol_regime": vol_est["regime"],
                                    "discount_factor": OVERNIGHT_EDGE_DISCOUNT,
                                    "original_min_edge": round(_min_edge, 6),
                                    "discounted_min_edge": round(_ovn_discounted_min, 6),
                                    "edge_vs_discounted": round(fee_adjusted_edge - _ovn_discounted_min, 6),
                                    "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                                    "ofa_adjustment": round(ofa_adjustment, 6),
                                    "z_score": z_score,
                                })
                            except Exception:
                                logging.debug("overnight_discount log failed", exc_info=True)

                            # DB insert (always — for settlement tracking)
                            _ovn_dedup = (ticker, _ovn_stage)
                            if _ovn_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_ovn_dedup)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        _ovn_stage,
                                        rejection_reason=f"{'live' if _ovn_live_eligible else 'shadow'}: edge {fee_adjusted_edge:.4f} >= discounted_min {_ovn_discounted_min:.4f} (orig {_min_edge:.4f} x {OVERNIGHT_EDGE_DISCOUNT})",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score,
                                        vol_regime=vol_est["regime"],
                                        calibrated_prob_raw=calibrated_prob_raw,
                                        kelly_f=_ovn_kelly_f,
                                        position_size=_ovn_position,
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=_ovn_ev,
                                        ask_depth=ask_depth,
                                        best_ask_source=best_ask_source,
                                        raw_prob=raw_prob,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        product_type=window.get("product_type"),
                                        **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (%s)", _ovn_stage, exc_info=True)

                            # Live path: append to candidates for execution
                            if _ovn_live_eligible:
                                logging.info(
                                    "OVN_DISCOUNT_CANDIDATE: %s %dx@%dc edge=%.4f disc_min=%.4f stc=%.0fs",
                                    ticker, _ovn_position, best_ask,
                                    fee_adjusted_edge, _ovn_discounted_min, seconds_remaining)
                                candidates.append({
                                    "ticker": ticker,
                                    "event_ticker": window["event_ticker"],
                                    "asset": asset,
                                    "product_type": window.get("product_type"),
                                    "spot": spot,
                                    "threshold": threshold,
                                    "seconds_to_close": round(seconds_remaining, 1),
                                    "blended_rv": blended_rv,
                                    "calibrated_prob": round(final_prob, 6),
                                    "z_score": z_score,
                                    "best_yes_ask": best_ask,
                                    "best_ask_source": best_ask_source,
                                    "edge": round(edge, 6),
                                    "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                    "position_size": _ovn_position,
                                    "kelly_f": _ovn_kelly_f,
                                    "drawdown_scaler": _ovn_drawdown or 1.0,
                                    "vol_regime": vol_est["regime"],
                                    "balance_at_scan": _ovn_balance,
                                    "strategy": "overnight_discount",
                                    "ob_snapshot": {
                                        "best_ask": best_ask,
                                        "ask_depth": ask_depth,
                                        "total_depth": total_depth,
                                        "best_bid": OrderExecutor._best_yes_bid(ob_data) if ob_data else None,
                                        "bid_depth": OrderExecutor._best_yes_bid_depth(ob_data) if ob_data else 0,
                                        "spread": (best_ask - OrderExecutor._best_yes_bid(ob_data))
                                                  if ob_data and OrderExecutor._best_yes_bid(ob_data) is not None else None,
                                    },
                                    "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                                    "est_fee_1c": est_fee_1c,
                                })

                    # ── Decided Contract (overlay strategy + shadow) ─────────
                    # When z-score is very negative (spot far above strike) near expiry,
                    # the contract is essentially decided. EGARCH can't compute edge
                    # because calibration squashes prob below market price.
                    # T1 (z ≤ -5, 93c+), T1B (z ≤ -4, 95c+), T2 (z ≤ -3, 93-96c).
                    # Shadow always runs. Live overlay fires when DECIDED_T1/T1B/T2_ENABLED.
                    if (DECIDED_CONTRACT_SHADOW
                            and _pt in (None, "15m")
                            and z_score is not None
                            and best_ask >= DECIDED_CONTRACT_MIN_PRICE
                            and best_ask <= MAX_ENTRY_PRICE
                            and seconds_remaining < DECIDED_CONTRACT_MAX_STC):
                        _dc_tier = None
                        if z_score <= DECIDED_CONTRACT_Z_T1:
                            _dc_tier = "decided_contract_t1"
                        elif (z_score <= DECIDED_CONTRACT_Z_T1B
                              and best_ask >= DECIDED_CONTRACT_T1B_MIN_PRICE):
                            _dc_tier = "decided_contract_t1b"
                        elif (z_score <= DECIDED_CONTRACT_Z_T2
                              and best_ask <= DECIDED_CONTRACT_T2_MAX_PRICE):
                            _dc_tier = "decided_contract_t2"
                        elif (z_score <= DECIDED_CONTRACT_Z_T2_Z25
                              and best_ask <= DECIDED_CONTRACT_T2_MAX_PRICE):
                            _dc_tier = "decided_contract_t2_z25"
                        elif (z_score <= DECIDED_CONTRACT_Z_T2_Z2
                              and best_ask <= DECIDED_CONTRACT_T2_MAX_PRICE):
                            _dc_tier = "decided_contract_t2_z2"

                        if _dc_tier:
                            # Fixed sizing per tier (not Kelly — model edge is negative)
                            _dc_balance = self._get_balance_cached()
                            _dc_position = None
                            _dc_kelly_f = None
                            _dc_ev = None
                            if _dc_balance and _dc_balance > 0:
                                _dc_risk = (DECIDED_CONTRACT_T2_Z25_RISK if _dc_tier == "decided_contract_t2_z25"
                                            else DECIDED_CONTRACT_T2_Z2_RISK if _dc_tier == "decided_contract_t2_z2"
                                            else DECIDED_CONTRACT_RISK)
                                # SOL price-tiered risk: reduce sizing at high prices to contain loss asymmetry
                                if asset == "SOL":
                                    for _sol_floor, _sol_risk in SOL_DC_RISK_TIERS:
                                        if best_ask >= _sol_floor:
                                            _dc_risk = _sol_risk
                                            break
                                _dc_position = max(1, int((_dc_balance * _dc_risk) / best_ask))
                                # Per-asset risk caps (DC uses DECIDED_CONTRACT_RISK=20%
                                # which can exceed per-asset caps like SOL 15%, BTC 15%)
                                if asset == "SOL":
                                    _dc_asset_max = int((_dc_balance * SOL_MAX_RISK_PER_TRADE) / best_ask)
                                    if _dc_position > _dc_asset_max >= 1:
                                        _dc_position = _dc_asset_max
                                elif asset == "BTC":
                                    _dc_asset_max = int((_dc_balance * BTC_MAX_RISK_PER_TRADE) / best_ask)
                                    if _dc_position > _dc_asset_max >= 1:
                                        _dc_position = _dc_asset_max
                                elif asset == "XRP":
                                    _dc_asset_max = int((_dc_balance * XRP_MAX_RISK_PER_TRADE) / best_ask)
                                    if _dc_position > _dc_asset_max >= 1:
                                        _dc_position = _dc_asset_max
                                # EV with assumed win prob — calibrated from 14-day settlement data:
                                # T1: 92/92 (100%) at 95-98c → 0.99 (unchanged)
                                # T1B: 47/47 (100%) at 95-98c → 0.98 (was 0.97, unlocks 97c)
                                # T2: 106/106 (100%) at 95-98c → 0.97 (was 0.96, unlocks 96c)
                                # T2-Z25: 69/71 (97.2%) has 2 losses → 0.96 (unchanged, conservative)
                                # T2-Z2: 144/144 (100%) at 95-98c → 0.97 (was 0.95, unlocks 96c)
                                _dc_assumed_p = (0.99 if _dc_tier == "decided_contract_t1"
                                                 else 0.98 if _dc_tier == "decided_contract_t1b"
                                                 else 0.97 if _dc_tier == "decided_contract_t2"
                                                 else 0.96 if _dc_tier == "decided_contract_t2_z25"
                                                 else 0.97)
                                _dc_ev = round((_dc_assumed_p * (100 - best_ask))
                                               - ((1 - _dc_assumed_p) * best_ask) - est_fee_1c, 2)
                                _dc_kelly_f = round((_dc_assumed_p - best_ask / 100.0), 6)

                            # Always log shadow signal (continues accumulating shadow stats)
                            _dc_dedup = (ticker, _dc_tier)
                            if _dc_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_dc_dedup)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        _dc_tier,
                                        rejection_reason=f"shadow: z={z_score:.1f} stc={seconds_remaining:.0f}s price={best_ask}c",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score,
                                        vol_regime=vol_est["regime"],
                                        calibrated_prob_raw=calibrated_prob_raw,
                                        kelly_f=_dc_kelly_f,
                                        position_size=_dc_position,
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=_dc_ev,
                                        ask_depth=ask_depth,
                                        best_ask_source=best_ask_source,
                                        raw_prob=raw_prob,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        product_type=window.get("product_type"),
                                        **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (%s)", _dc_tier, exc_info=True)

                            # ── Phase 1 re-promotion shadow (T2-Z2 BTC+ETH @ 10%) ──
                            # Logs the exact cohort of the rejected Apr 22 promotion
                            # proposal so clean Kelly-actual cf_pnl can be computed at
                            # the PROPOSED 10% sizing (not the current 20%).
                            # See kb/decisions/t2-z2-shadowed.md Apr 22 section.
                            if (_dc_tier == "decided_contract_t2_z2"
                                    and asset in ("BTC", "ETH")
                                    and _dc_balance and _dc_balance > 0):
                                _p1_dedup = (ticker, "dc_t2_z2_phase1_shadow")
                                if _p1_dedup not in self._eval_opp_seen:
                                    self._eval_opp_seen.add(_p1_dedup)
                                    _p1_position = max(1, int((_dc_balance * DC_T2_Z2_PHASE1_RISK) / best_ask))
                                    try:
                                        self._state.insert_evaluated_opportunity(
                                            ticker, window["event_ticker"], asset,
                                            "dc_t2_z2_phase1_shadow",
                                            rejection_reason=f"phase1 sim: BTC+ETH {int(DC_T2_Z2_PHASE1_RISK*100)}% sizing pos={_p1_position} @ {best_ask}c",
                                            spot_price=spot, threshold=threshold,
                                            volatility=blended_rv, market_price=best_ask,
                                            seconds_to_close=seconds_remaining,
                                            calibrated_prob=final_prob, edge=edge,
                                            ofa_adjustment=ofa_adjustment,
                                            z_score=z_score,
                                            vol_regime=vol_est["regime"],
                                            calibrated_prob_raw=calibrated_prob_raw,
                                            kelly_f=_dc_kelly_f,
                                            position_size=_p1_position,
                                            breakeven_wr=best_ask / 100.0,
                                            expected_value=_dc_ev,
                                            ask_depth=ask_depth,
                                            best_ask_source=best_ask_source,
                                            raw_prob=raw_prob,
                                            calibration_method=calibration_method,
                                            fee_adjusted_edge=fee_adjusted_edge,
                                            product_type=window.get("product_type"),
                                            **_shadow_diag)
                                    except Exception:
                                        logging.warning("insert_evaluated_opportunity failed (dc_t2_z2_phase1_shadow)", exc_info=True)

                            # ── Live overlay: queue as candidate if tier enabled ──
                            _dc_live_enabled = (
                                (_dc_tier == "decided_contract_t1" and DECIDED_T1_ENABLED)
                                or (_dc_tier == "decided_contract_t1b" and DECIDED_T1B_ENABLED)
                                or (_dc_tier == "decided_contract_t2" and DECIDED_T2_ENABLED)
                                or (_dc_tier == "decided_contract_t2_z25" and DECIDED_T2_Z25_ENABLED)
                                or (_dc_tier == "decided_contract_t2_z2" and DECIDED_T2_Z2_ENABLED))
                            # T1 (2026-05-10): shadow assets must not route live via DC
                            if (HYPE_15M_SHADOW and asset == "HYPE") or (DOGE_15M_SHADOW and asset == "DOGE"):
                                _dc_live_enabled = False
                            if (_dc_live_enabled
                                    and not OBSERVATION_MODE
                                    and _dc_balance and _dc_balance > 0
                                    and _dc_position and _dc_position > 0):
                                # Per-window risk cap: 25% bankroll across all DC signals
                                _dc_wkey = window["event_ticker"]
                                _dc_existing_risk = self._dc_window_risk.get(_dc_wkey, 0.0)
                                _dc_this_cost = _dc_position * best_ask
                                _dc_max_cost = _dc_balance * DECIDED_CONTRACT_MAX_WINDOW_RISK
                                if _dc_existing_risk + _dc_this_cost > _dc_max_cost:
                                    # Reduce position to fit within cap
                                    _dc_remaining = _dc_max_cost - _dc_existing_risk
                                    if _dc_remaining >= best_ask:
                                        _dc_position = max(1, int(_dc_remaining / best_ask))
                                        _dc_this_cost = _dc_position * best_ask
                                    else:
                                        # Window cap exceeded — log skip and don't trade
                                        _dc_skip_dedup = (ticker, "decided_window_cap_skip")
                                        if _dc_skip_dedup not in self._eval_opp_seen:
                                            self._eval_opp_seen.add(_dc_skip_dedup)
                                            try:
                                                self._state.insert_evaluated_opportunity(
                                                    ticker, window["event_ticker"], asset,
                                                    "decided_window_cap_skip",
                                                    rejection_reason=f"window cap: existing={_dc_existing_risk:.0f}c max={_dc_max_cost:.0f}c tier={_dc_tier}",
                                                    spot_price=spot, threshold=threshold,
                                                    volatility=blended_rv, market_price=best_ask,
                                                    seconds_to_close=seconds_remaining,
                                                    calibrated_prob=final_prob, edge=edge,
                                                    z_score=z_score,
                                                    vol_regime=vol_est["regime"],
                                                    raw_prob=raw_prob,
                                                    fee_adjusted_edge=fee_adjusted_edge,
                                                    product_type=window.get("product_type"),
                                                    **_shadow_diag)
                                            except Exception:
                                                logging.warning("insert_evaluated_opportunity failed (decided_window_cap_skip)", exc_info=True)
                                        self._dc_window_cap_skips += 1
                                        logging.info("DC_WINDOW_CAP: %s %s skipped (existing=%.0fc max=%.0fc)",
                                                     _dc_tier, ticker, _dc_existing_risk, _dc_max_cost)
                                        _dc_live_enabled = False  # skip candidate below

                                # Cap by existing exposure on same ticker
                                if _dc_live_enabled:
                                    _dc_existing_exposure = 0
                                    if STACKING_ENABLED:
                                        from bot.models import strategy_to_group
                                        for pos in self._state.get_open_positions():
                                            if pos["ticker"] == ticker and \
                                               pos.get("strategy_group",
                                                   strategy_to_group(pos.get("strategy"))) == "decided":
                                                _dc_existing_exposure += pos["count"]
                                                break
                                    else:
                                        for pos in self._state.get_open_positions():
                                            if pos["ticker"] == ticker:
                                                _dc_existing_exposure += pos["count"]
                                                break
                                        for resting in self._state.get_resting_orders(ticker=ticker):
                                            _dc_existing_exposure += resting["count"]
                                    if _dc_existing_exposure > 0:
                                        _dc_position = max(0, _dc_position - _dc_existing_exposure)

                                if _dc_live_enabled and _dc_position > 0:
                                    # Cooldown: skip if this ticker was recently skipped due to "no asks"
                                    if ticker in self._dc_skip_cooldown and time.time() < self._dc_skip_cooldown[ticker]:
                                        _dc_live_enabled = False
                                if _dc_live_enabled and _dc_position > 0:
                                    _dc_strat = {"decided_contract_t1": "decided_t1",
                                                 "decided_contract_t1b": "decided_t1b",
                                                 "decided_contract_t2": "decided_t2",
                                                 "decided_contract_t2_z25": "decided_t2_z25",
                                                 "decided_contract_t2_z2": "decided_t2_z2"}[_dc_tier]
                                    self._dc_window_risk[_dc_wkey] = _dc_existing_risk + _dc_position * best_ask
                                    logging.info("DC_CANDIDATE: %s %s %dx@%dc z=%.1f stc=%.0fs",
                                                 _dc_strat, ticker, _dc_position, best_ask, z_score, seconds_remaining)
                                    candidates.append({
                                        "ticker": ticker,
                                        "event_ticker": window["event_ticker"],
                                        "asset": asset,
                                        "product_type": window.get("product_type"),
                                        "spot": spot,
                                        "threshold": threshold,
                                        "seconds_to_close": round(seconds_remaining, 1),
                                        "blended_rv": blended_rv,
                                        "calibrated_prob": round(_dc_assumed_p, 6),
                                        "z_score": z_score,
                                        "best_yes_ask": best_ask,
                                        "best_ask_source": best_ask_source,
                                        "edge": round(_dc_assumed_p - best_ask / 100.0, 6),
                                        "position_size": _dc_position,
                                        "kelly_f": _dc_kelly_f,
                                        "drawdown_scaler": 1.0,
                                        "vol_regime": vol_est["regime"],
                                        "balance_at_scan": _dc_balance,
                                        "strategy": _dc_strat,
                                        "strategy_scores": {"certainty": 1.0, "certainty_detail": "decided",
                                                            "orderbook": 0.5, "orderbook_detail": "n/a",
                                                            "urgency": 1.0, "urgency_detail": "decided",
                                                            "composite": 1.0, "reason": "decided_contract"},
                                        "ob_snapshot": {
                                            "best_ask": best_ask,
                                            "ask_depth": ask_depth,
                                            "total_depth": total_depth,
                                            "best_bid": OrderExecutor._best_yes_bid(ob_data) if ob_data else None,
                                            "bid_depth": OrderExecutor._best_yes_bid_depth(ob_data) if ob_data else 0,
                                            "spread": (best_ask - OrderExecutor._best_yes_bid(ob_data))
                                                      if ob_data and OrderExecutor._best_yes_bid(ob_data) is not None else None,
                                        },
                                        "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                                        "ofa_adjustment": round(ofa_adjustment, 6),
                                        "ofa_confidence": ofa_signals["confidence"] if ofa_signals else "none",
                                        "raw_prob": raw_prob,
                                        "calibration_method": calibration_method,
                                        "old_system_prob": round(_old_system_prob, 6),
                                        "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                        **_shadow_diag,
                                        **_shadow_extra,
                                    })

                    # ── DC Shadow z-score expansion (edge-dependent) ─────────
                    # These two variants target z-scores near -2 to -2.5 where
                    # edge is genuinely insufficient. They correctly live here.
                    if (DECIDED_CONTRACT_SHADOW
                            and _pt in (None, "15m")
                            and z_score is not None
                            and seconds_remaining < DECIDED_CONTRACT_MAX_STC):
                        def _dc_shadow_insert(stage, rej_detail):
                            _dcs_dedup = (ticker, stage)
                            if _dcs_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_dcs_dedup)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset, stage,
                                        rejection_reason=rej_detail,
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score,
                                        vol_regime=vol_est["regime"],
                                        raw_prob=raw_prob,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        product_type=window.get("product_type"),
                                        **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (%s)", stage, exc_info=True)

                        # T2 z≤-2.5 at 93-96c (loosening from z≤-3)
                        if (DECIDED_CONTRACT_Z_T2 < z_score <= -2.5
                                and 93 <= best_ask <= DECIDED_CONTRACT_T2_MAX_PRICE):
                            _dc_shadow_insert("dc_shadow_t2_z25",
                                              f"shadow: z={z_score:.1f} price={best_ask}c (T2 z≤-2.5 expansion)")

                        # T2 z≤-1.5 at 93-96c (shadow for next expansion beyond live -1.75)
                        if (DECIDED_CONTRACT_Z_T2_Z2 < z_score <= -1.5
                                and 93 <= best_ask <= DECIDED_CONTRACT_T2_MAX_PRICE):
                            _dc_shadow_insert("dc_shadow_t2_z2",
                                              f"shadow: z={z_score:.1f} price={best_ask}c (T2 z≤-1.5 expansion)")

                    # ── HOURLY DECIDED CONTRACTS ──────────────────────────────
                    # Same DC thesis on hourly BTC tickers. Separate from sub-60c.
                    # Conservative: z≤-4, 93-96c, BTC only, sigma gate, 1 per window.
                    if (_pt == "hourly"
                            and HOURLY_DC_ENABLED
                            and asset in HOURLY_DC_ASSETS
                            and z_score is not None
                            and z_score <= HOURLY_DC_Z_THRESHOLD
                            and best_ask >= HOURLY_DC_MIN_PRICE
                            and best_ask <= HOURLY_DC_MAX_PRICE):
                        # Sigma gate: block cold-RK false signals
                        _hdc_sigma = vol_est.get("egarch_sigma") if vol_est else None
                        if _hdc_sigma is not None and _hdc_sigma >= HOURLY_DC_MIN_SIGMA:
                            _hdc_dedup = (ticker, "hourly_dc")
                            if _hdc_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_hdc_dedup)
                                _hdc_ev = round((HOURLY_DC_ASSUMED_PROB * (100 - best_ask))
                                                - ((1 - HOURLY_DC_ASSUMED_PROB) * best_ask) - est_fee_1c, 2)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset, "hourly_dc",
                                        spot_price=spot, threshold=threshold, volatility=blended_rv,
                                        market_price=best_ask, seconds_to_close=seconds_remaining,
                                        calibrated_prob=HOURLY_DC_ASSUMED_PROB, edge=HOURLY_DC_ASSUMED_PROB - best_ask / 100.0,
                                        z_score=z_score, vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                        fee_adjusted_edge=HOURLY_DC_ASSUMED_PROB - best_ask / 100.0 - est_fee_1c / 100.0,
                                        breakeven_wr=best_ask / 100.0, expected_value=_hdc_ev,
                                        ask_depth=ask_depth, best_ask_source=best_ask_source,
                                        position_size=HOURLY_DC_CONTRACTS,
                                        product_type="hourly",
                                        **_oft_db, **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (hourly_dc)", exc_info=True)

                                # Shadow only — 87% WR at 93-96c is below breakeven.
                                # Keep logging for comparison data but never execute.
                                # (Execution path removed Mar 25 — was live but never fired)

                    # ── HOURLY DC SHADOW: 97c+ z≤-3 STC≤600s ──────────────────
                    # New variant targeting the profitable zone. Data: 37/37 (100% WR)
                    # at z≤-3, STC≤600s, price≥97c, sig≥250 over 23 days.
                    # All 12 losses in the broader z≤-3 97c+ pool were at STC>600s.
                    # Runs alongside existing hourly DC shadow — separate evaluation.
                    if (_pt == "hourly"
                            and HOURLY_DC_ENABLED
                            and z_score is not None
                            and z_score <= -3.0
                            and best_ask >= 97
                            and best_ask <= 99
                            and seconds_remaining <= 600):
                        _hdc2_sigma = vol_est.get("egarch_sigma") if vol_est else None
                        if _hdc2_sigma is not None and _hdc2_sigma >= HOURLY_DC_MIN_SIGMA:
                            _hdc2_dedup = (ticker, "hourly_dc_97c_stc600")
                            if _hdc2_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_hdc2_dedup)
                                _hdc2_ev = round((0.99 * (100 - best_ask))
                                                 - (0.01 * best_ask) - est_fee_1c, 2)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        "hourly_dc_97c_stc600",
                                        rejection_reason=f"shadow: z={z_score:.1f} price={best_ask}c stc={seconds_remaining:.0f}s sig={_hdc2_sigma:.6f}",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=0.99,
                                        edge=round(0.99 - best_ask / 100.0, 6),
                                        z_score=z_score, vol_regime=vol_est["regime"],
                                        raw_prob=raw_prob,
                                        fee_adjusted_edge=round(0.99 - best_ask / 100.0 - est_fee_1c / 100.0, 6),
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=_hdc2_ev,
                                        ask_depth=ask_depth,
                                        best_ask_source=best_ask_source,
                                        position_size=25,
                                        product_type="hourly",
                                        egarch_sigma=_hdc2_sigma,
                                        **_oft_db)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (hourly_dc_97c_stc600)", exc_info=True)

                    # ── HOURLY DC SHADOW TIER 2: 93-96c z≤-3 STC≤300s ────────
                    # Data: 8/8 (100% WR) at z≤-3, STC≤300s, 93-96c, sig≥250.
                    # All 10 losses in the broader 93-96c pool are at STC>300s.
                    # Does NOT overlap with Tier 1 (97c+): only fires on 93-96c.
                    if (_pt == "hourly"
                            and HOURLY_DC_ENABLED
                            and z_score is not None
                            and z_score <= -3.0
                            and best_ask >= 93
                            and best_ask <= 96
                            and seconds_remaining <= 300):
                        _hdc3_sigma = vol_est.get("egarch_sigma") if vol_est else None
                        if _hdc3_sigma is not None and _hdc3_sigma >= HOURLY_DC_MIN_SIGMA:
                            _hdc3_dedup = (ticker, "hourly_dc_93c_stc300")
                            if _hdc3_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_hdc3_dedup)
                                _hdc3_assumed = 0.97
                                _hdc3_ev = round((_hdc3_assumed * (100 - best_ask))
                                                 - ((1 - _hdc3_assumed) * best_ask) - est_fee_1c, 2)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        "hourly_dc_93c_stc300",
                                        rejection_reason=f"shadow: z={z_score:.1f} price={best_ask}c stc={seconds_remaining:.0f}s sig={_hdc3_sigma:.6f}",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=_hdc3_assumed,
                                        edge=round(_hdc3_assumed - best_ask / 100.0, 6),
                                        z_score=z_score, vol_regime=vol_est["regime"],
                                        raw_prob=raw_prob,
                                        fee_adjusted_edge=round(_hdc3_assumed - best_ask / 100.0 - est_fee_1c / 100.0, 6),
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=_hdc3_ev,
                                        ask_depth=ask_depth,
                                        best_ask_source=best_ask_source,
                                        position_size=25,
                                        product_type="hourly",
                                        egarch_sigma=_hdc3_sigma,
                                        **_oft_db)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (hourly_dc_93c_stc300)", exc_info=True)

                    # ── SPX DECIDED CONTRACTS SHADOW ──────────────────────────
                    # Shadow-only: log signals, never trade. Mon-Wed, z≤-3, 93-96c.
                    # 1-week validation before promotion. No sigma gate (SPX EGARCH
                    # produces sigma=0 on 97% of signals — z-score still valid via
                    # raw spot-vs-strike distance).
                    if (_pt == "spx_hourly"
                            and SPX_DC_SHADOW_ENABLED
                            and z_score is not None
                            and z_score <= SPX_DC_Z_THRESHOLD
                            and best_ask >= SPX_DC_MIN_PRICE
                            and best_ask <= SPX_DC_MAX_PRICE):
                        # Day-of-week filter: Mon-Wed only (all 6 losses were Thu-Fri)
                        import datetime as _dt
                        _spx_dc_day = _dt.datetime.now(_dt.timezone.utc).weekday()
                        if _spx_dc_day in SPX_DC_VALID_DAYS:
                            _spx_dc_dedup = (ticker, "spx_dc_shadow")
                            if _spx_dc_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_spx_dc_dedup)
                                _spx_dc_sigma = vol_est.get("egarch_sigma") if vol_est else None
                                _spx_dc_spot = spot
                                _spx_dc_thresh = threshold
                                _spx_dc_dist = abs(_spx_dc_spot - _spx_dc_thresh) if _spx_dc_spot and _spx_dc_thresh else None
                                _spx_dc_dist_pct = (_spx_dc_dist / _spx_dc_spot * 100) if _spx_dc_dist and _spx_dc_spot else None
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset, "spx_dc_shadow",
                                        spot_price=spot, threshold=threshold, volatility=blended_rv,
                                        market_price=best_ask, seconds_to_close=seconds_remaining,
                                        calibrated_prob=0.97, edge=0.97 - best_ask / 100.0,
                                        z_score=z_score, vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                        fee_adjusted_edge=0.97 - best_ask / 100.0 - est_fee_1c / 100.0,
                                        breakeven_wr=best_ask / 100.0,
                                        ask_depth=ask_depth, best_ask_source=best_ask_source,
                                        product_type="spx_hourly",
                                        **_oft_db, **_shadow_diag)
                                    logging.info(
                                        "SPX_DC_SHADOW: %s %dc z=%.1f sig=%s spot=%.1f thresh=%.1f dist=$%.0f (%.2f%%) stc=%.0f day=%d",
                                        ticker, best_ask, z_score,
                                        f"{_spx_dc_sigma:.6f}" if _spx_dc_sigma else "0",
                                        _spx_dc_spot or 0, _spx_dc_thresh or 0,
                                        _spx_dc_dist or 0, _spx_dc_dist_pct or 0,
                                        seconds_remaining, _spx_dc_day)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (spx_dc_shadow)", exc_info=True)

                    # ── Relaxed Edge Shadow (Fix #1) ──────────────────────────
                    # Edge thresholds at 88-93c may be too conservative.
                    # Data: insufficient_edge rejections at 88c=97.2% WR, 89c=93.3%,
                    # 91c=94.4% — all well above breakeven. Shadow with halved thresholds.
                    if (RELAXED_EDGE_SHADOW
                            and _pt in (None, "15m")
                            and best_ask >= RELAXED_EDGE_MIN_PRICE
                            and best_ask < RELAXED_EDGE_MAX_PRICE):
                        _rel_min_edge = _min_edge * RELAXED_EDGE_DISCOUNT
                        if fee_adjusted_edge >= _rel_min_edge:
                            _rel_balance = self._get_balance_cached()
                            _rel_position = None
                            _rel_kelly_f = None
                            _rel_ev = None
                            if _rel_balance and _rel_balance > 0:
                                _rel_sizing = self._sizer.compute(final_prob, best_ask, _rel_balance)
                                _rel_kelly_f = _rel_sizing["kelly_f"]
                                _rel_position = _rel_sizing["contracts"]
                                _rel_scfg = get_market_config(window.get("product_type"))
                                if _rel_scfg.kelly_fraction < 1.0:
                                    _rel_position = max(1, int(_rel_position * _rel_scfg.kelly_fraction))
                                _rel_type_max = int((_rel_balance * _rel_scfg.max_risk_per_trade) / best_ask)
                                if _rel_position > _rel_type_max:
                                    _rel_position = max(1, _rel_type_max)
                                _rel_ev = round((final_prob * (100 - best_ask))
                                                - ((1 - final_prob) * best_ask) - est_fee_1c, 2)

                            _rel_dedup = (ticker, "relaxed_edge_shadow")
                            if _rel_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_rel_dedup)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        "relaxed_edge_shadow",
                                        rejection_reason=f"shadow: edge {fee_adjusted_edge:.4f} >= relaxed {_rel_min_edge:.4f} (orig {_min_edge:.4f} x {RELAXED_EDGE_DISCOUNT})",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score,
                                        vol_regime=vol_est["regime"],
                                        calibrated_prob_raw=calibrated_prob_raw,
                                        kelly_f=_rel_kelly_f,
                                        position_size=_rel_position,
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=_rel_ev,
                                        ask_depth=ask_depth,
                                        best_ask_source=best_ask_source,
                                        raw_prob=raw_prob,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        product_type=window.get("product_type"),
                                        **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (relaxed_edge_shadow)", exc_info=True)

                    continue

                # Compute position size via Kelly criterion
                balance = self._get_balance_cached()
                if balance is None or balance <= 0:
                    continue
                # Capital allocator: per-strategy budget (defaults to full balance)
                _strategy_key = window.get("product_type") or "crypto_15m"
                if _strategy_key == "15m":
                    _strategy_key = "crypto_15m"
                elif _strategy_key == "hourly":
                    _strategy_key = "crypto_hourly"
                _sizing_balance = balance
                # Product-specific bankroll fractions: size off a virtual sub-bankroll
                # so secondary products can never reduce 15M's available capital
                if _pt == "spx_hourly":
                    _sizing_balance = int(balance * SPX_HOURLY_BANKROLL_FRACTION)
                    if _sizing_balance <= 0:
                        _sizing_balance = 1  # safety: never zero
                elif _pt == "hourly":
                    _sizing_balance = int(balance * HOURLY_BANKROLL_FRACTION)
                    if _sizing_balance <= 0:
                        _sizing_balance = 1
                elif self._ml and getattr(self._ml, "capital_allocator", None):
                    try:
                        _sizing_balance = self._ml.capital_allocator.get_budget_cents(
                            _strategy_key, balance, locked_by_strategy=None)
                        if _sizing_balance <= 0:
                            _sizing_balance = balance  # fallback: never zero out live trading
                    except Exception:
                        _sizing_balance = balance
                _sizing_start = time.perf_counter()
                sizing = self._sizer.compute(final_prob, best_ask, _sizing_balance)
                _sizing_dt = time.perf_counter() - _sizing_start
                if _sizing_dt > 0.2:
                    logging.warning(
                        "SCAN_SIZING_SLOW: asset=%s ticker=%s took %.2fs",
                        asset, ticker, _sizing_dt)

                # Product-type-specific sizing: fractional Kelly + conservative per-trade risk cap
                _scfg = get_market_config(window.get("product_type"))
                if _pt == "hourly":
                    # Hourly: fixed 10-contract sizing — bypass Kelly entirely
                    sizing["contracts"] = HOURLY_FIXED_CONTRACTS
                elif _scfg.kelly_fraction < 1.0:
                    _full_kelly_contracts = sizing["contracts"]
                    sizing["contracts"] = max(1, int(sizing["contracts"] * _scfg.kelly_fraction))
                    _type_max = int((_sizing_balance * _scfg.max_risk_per_trade) / best_ask)
                    if sizing["contracts"] > _type_max:
                        sizing["contracts"] = max(1, _type_max)

                # Asset-specific risk caps (15M only — hourly has fixed sizing)
                # DC candidates don't flow through this code path (separate sizing at line 8231).
                if asset == "XRP" and _pt in (None, "15m"):
                    _xrp_max = int((_sizing_balance * XRP_MAX_RISK_PER_TRADE) / best_ask)
                    if sizing["contracts"] > _xrp_max >= 1:
                        logging.info("ASSET_CAP: XRP raw=%d capped=%d balance=$%.2f",
                                     sizing["contracts"], _xrp_max, _sizing_balance / 100)
                        sizing["contracts"] = _xrp_max
                elif asset == "BTC" and _pt in (None, "15m"):
                    _btc_max = int((_sizing_balance * BTC_MAX_RISK_PER_TRADE) / best_ask)
                    if sizing["contracts"] > _btc_max >= 1:
                        logging.info("ASSET_CAP: BTC raw=%d capped=%d balance=$%.2f",
                                     sizing["contracts"], _btc_max, _sizing_balance / 100)
                        sizing["contracts"] = _btc_max
                elif asset == "SOL" and _pt in (None, "15m"):
                    _sol_max = int((_sizing_balance * SOL_MAX_RISK_PER_TRADE) / best_ask)
                    if sizing["contracts"] > _sol_max >= 1:
                        logging.info("ASSET_CAP: SOL raw=%d capped=%d balance=$%.2f",
                                     sizing["contracts"], _sol_max, _sizing_balance / 100)
                        sizing["contracts"] = _sol_max
                elif asset == "ETH" and _pt in (None, "15m"):
                    _eth_max = int((_sizing_balance * ETH_MAX_RISK_PER_TRADE) / best_ask)
                    if sizing["contracts"] > _eth_max >= 1:
                        logging.info("ASSET_CAP: ETH raw=%d capped=%d balance=$%.2f",
                                     sizing["contracts"], _eth_max, _sizing_balance / 100)
                        sizing["contracts"] = _eth_max

                # ETH sub-80c position cap: clamp to [20, 50] contracts
                # Half-Kelly at 75c/87% WR = 322-645 contracts — uncapped is reckless.
                # Cap at 50 (half-Kelly ceiling), floor at 20 (minimum meaningful size).
                # Will right-size from orderbook depth data after 1 week.
                if (asset == "ETH" and _pt in (None, "15m") and best_ask < 80):
                    _eth_sub80_capped = max(20, min(ETH_SUB80_POSITION_CAP, sizing["contracts"]))
                    if _eth_sub80_capped != sizing["contracts"]:
                        logging.info("ETH sub-80c clamp: %d -> %d contracts (ask=%dc, cap=%d)",
                                     sizing["contracts"], _eth_sub80_capped, best_ask, ETH_SUB80_POSITION_CAP)
                        sizing["contracts"] = _eth_sub80_capped

                # Low-STC sizing cap: halve position when STC < 100s
                # Data: 0-100s STC is -$84/14d (12W/2L, catastrophic losses wipe gains)
                if (_pt in (None, "15m") and seconds_remaining < LOW_STC_SIZING_CAP_THRESHOLD
                        and LOW_STC_SIZING_CAP < 1.0 and sizing["contracts"] > 0):
                    _pre_stc_cap = sizing["contracts"]
                    sizing["contracts"] = max(1, int(sizing["contracts"] * LOW_STC_SIZING_CAP))
                    if sizing["contracts"] < _pre_stc_cap:
                        logging.info("Low-STC cap: %d -> %d contracts (STC=%.0fs, cap=%.1fx)",
                                     _pre_stc_cap, sizing["contracts"], seconds_remaining, LOW_STC_SIZING_CAP)

                # High-STC sizing scaler: reduce position for far-from-expiry trades
                # Data: 3-5m 94.6% WR (+$667), 5-7m 90.8% (+$35), 7m+ net negative
                # Scaler: 300/STC — monotonic, one parameter, no trades cut
                if (STC_SIZING_SCALER_ENABLED
                        and _pt in (None, "15m")
                        and seconds_remaining > STC_SIZING_SCALER_KNEE
                        and sizing["contracts"] > 0):
                    _stc_scaler = STC_SIZING_SCALER_KNEE / seconds_remaining
                    _pre_stc_scale = sizing["contracts"]
                    sizing["contracts"] = max(1, int(sizing["contracts"] * _stc_scaler))
                    if sizing["contracts"] < _pre_stc_scale:
                        logging.info("STC_SCALER: %d -> %d contracts (STC=%.0fs, scaler=%.2fx)",
                                     _pre_stc_scale, sizing["contracts"], seconds_remaining, _stc_scaler)

                # Buffer-aware sizing: scale contracts by spot buffer at entry
                # (infrastructure — disabled until PPO data matures, ~10+ losses)
                _spot_buffer_pct = None
                if threshold and threshold > 0:
                    _spot_buffer_pct = (spot - threshold) / threshold * 100
                if (BUFFER_SIZING_ENABLED
                        and _pt in (None, "15m")
                        and _spot_buffer_pct is not None
                        and sizing["contracts"] > 0):
                    _buf_mult = buffer_sizing_multiplier(_spot_buffer_pct)
                    _pre_buf = sizing["contracts"]
                    sizing["contracts"] = max(1, int(sizing["contracts"] * _buf_mult))
                    if sizing["contracts"] != _pre_buf:
                        logging.info("BUFFER_SIZING: %d -> %d contracts (buf=%.3f%%, mult=%.2fx)",
                                     _pre_buf, sizing["contracts"], _spot_buffer_pct, _buf_mult)

                # Cap by existing exposure (positions + resting orders) to prevent
                # accumulation across scan ticks on the same ticker
                existing_exposure = 0
                for pos in self._state.get_open_positions():
                    if pos["ticker"] == ticker:
                        existing_exposure += pos["count"]
                        break
                for resting in self._state.get_resting_orders(ticker=ticker):
                    existing_exposure += resting["count"]
                if existing_exposure > 0:
                    sizing["contracts"] = max(0, sizing["contracts"] - existing_exposure)

                if sizing["contracts"] <= 0:
                    scan_stats[asset]["zero_sizing"] += 1
                    self._recent_opportunities.append({
                        "ticker": ticker, "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask,
                        "edge_bps": round(edge * 10000),
                        "chosen_strategy": None,
                        "rejection_reason": "zero_sizing",
                        "ts": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    })
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "zero_sizing",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": "position sizing yielded 0 contracts",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": best_ask,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(final_prob, 6),
                            "edge": round(edge, 6),
                            "ofa_adjustment": round(ofa_adjustment, 6),
                            "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                            "old_system_prob": round(_old_system_prob, 6),
                            "counterfactual": _cf,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                        _dedup_key = (ticker, "zero_sizing")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            _dbw_start = time.perf_counter()
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "zero_sizing",
                                rejection_reason="0 contracts from sizing",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge,
                                ofa_adjustment=ofa_adjustment,
                                z_score=z_score,
                                vol_regime=vol_est["regime"],
                                calibrated_prob_raw=calibrated_prob_raw,
                                kelly_f=sizing["kelly_f"],
                                position_size=0,
                                breakeven_wr=best_ask / 100.0,
                                expected_value=round(_ev, 2),
                                drawdown_scaler=sizing["drawdown_scaler"],
                                ask_depth=ask_depth,
                                best_ask_source=best_ask_source,
                                ofa_confidence=ofa_signals["confidence"] if ofa_signals else "none",
                                raw_prob=raw_prob,
                                calibration_method=calibration_method,
                                old_system_prob=_old_system_prob,
                                fee_adjusted_edge=fee_adjusted_edge,
                                counterfactual=_cf_json,
                                shadow_cal_prob=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("prob") if _cf else None,
                                shadow_cal_fee_edge=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("fee_edge") if _cf else None,
                                shadow_cal_temperature=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("temperature") if _cf else None,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                                **_oft_db, **_shadow_diag)
                            _dbw_dt = time.perf_counter() - _dbw_start
                            if _dbw_dt > 0.2:
                                logging.warning(
                                    "SCAN_DBWRITE_SLOW: zero_sizing "
                                    "insert ticker=%s took %.2fs",
                                    ticker, _dbw_dt)
                    except Exception:
                        logging.warning("insert_evaluated_opportunity failed (spx/weather observation)", exc_info=True)
                    continue

                # Evaluate execution strategy for this market
                strategy_data = {
                    "z_score": z_score,
                    "calibrated_prob": final_prob,
                    "spot": spot,
                    "threshold": threshold,
                    "seconds_to_close": seconds_remaining,
                    "blended_rv": blended_rv,
                    "vol_regime": vol_est["regime"],
                    "best_yes_ask": best_ask,
                    "best_ask_depth": ask_depth,
                    "total_ob_depth": total_depth,
                    "convergence_velocity": self._scanner_convergence_velocity(ticker),
                    "edge": edge,
                    "min_entry_price": _entry_floor,
                    "max_entry_price": _entry_ceil,
                }
                strategy, strategy_scores = evaluate_execution_strategy(
                    strategy_data
                )
                if strategy in self._session_strategy_counts:
                    self._session_strategy_counts[strategy] += 1

                # Log strategy evaluation for every market evaluated
                self._logger.log_scan({
                    "type": "strategy_eval",
                    "ticker": ticker,
                    "asset": asset,
                    "seconds_to_close": round(seconds_remaining, 1),
                    "spot": spot,
                    "threshold": threshold,
                    "best_yes_ask": best_ask,
                    "edge": round(edge, 6),
                    "outcome_certainty_score": strategy_scores["certainty"],
                    "outcome_certainty_detail": strategy_scores["certainty_detail"],
                    "orderbook_state_score": strategy_scores["orderbook"],
                    "orderbook_state_detail": strategy_scores["orderbook_detail"],
                    "urgency_score": strategy_scores["urgency"],
                    "urgency_detail": strategy_scores["urgency_detail"],
                    "composite_score": strategy_scores["composite"],
                    "chosen_strategy": strategy,
                    "reason": strategy_scores["reason"],
                    "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                    "ofa_adjustment": round(ofa_adjustment, 6),
                    "ofa_confidence": ofa_signals["confidence"] if ofa_signals else "none",
                    "ofa_adjustments_applied": ofa_signals["adjustments_applied"] if ofa_signals else [],
                })

                if strategy == STRATEGY_WAIT:
                    scan_stats[asset]["strategy_wait"] += 1
                    self._recent_opportunities.append({
                        "ticker": ticker, "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask,
                        "edge_bps": round(edge * 10000),
                        "chosen_strategy": "WAIT",
                        "rejection_reason": "strategy_wait",
                        "ts": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    })
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "strategy_wait",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": "strategy engine returned WAIT",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": best_ask,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(final_prob, 6),
                            "edge": round(edge, 6),
                            "ofa_adjustment": round(ofa_adjustment, 6),
                            "composite_score": strategy_scores.get("composite"),
                            "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                            "old_system_prob": round(_old_system_prob, 6),
                            "counterfactual": _cf,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                        _dedup_key = (ticker, "strategy_wait")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "strategy_wait",
                                rejection_reason="WAIT strategy",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge,
                                ofa_adjustment=ofa_adjustment,
                                strategy=strategy,
                                z_score=z_score,
                                vol_regime=vol_est["regime"],
                                calibrated_prob_raw=calibrated_prob_raw,
                                kelly_f=sizing["kelly_f"],
                                position_size=sizing["contracts"],
                                breakeven_wr=best_ask / 100.0,
                                expected_value=round(_ev, 2),
                                drawdown_scaler=sizing["drawdown_scaler"],
                                ask_depth=ask_depth,
                                best_ask_source=best_ask_source,
                                ofa_confidence=ofa_signals["confidence"] if ofa_signals else "none",
                                raw_prob=raw_prob,
                                calibration_method=calibration_method,
                                old_system_prob=_old_system_prob,
                                fee_adjusted_edge=fee_adjusted_edge,
                                counterfactual=_cf_json,
                                shadow_cal_prob=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("prob") if _cf else None,
                                shadow_cal_fee_edge=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("fee_edge") if _cf else None,
                                shadow_cal_temperature=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("temperature") if _cf else None,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                                **_oft_db, **_shadow_diag)
                    except Exception:
                        logging.warning("insert_evaluated_opportunity failed (strategy_wait)", exc_info=True)
                    # For observation-only product types, let signal flow through
                    # to observation gate — strategy timing isn't relevant for
                    # data collection.  strategy_wait is still logged above for
                    # counterfactual analysis.
                    _sw_cfg = get_market_config(window.get("product_type"))
                    if not _sw_cfg.observation_only:
                        continue
                    # else: fall through to config-driven filters → observation gate

                # ── Config-driven per-window filters (any market type can opt in) ──
                _fltcfg = get_market_config(window.get("product_type"))
                _flt_pt = _fltcfg.product_type

                # Layer 3a: Asset exclusion (config-driven)
                if _fltcfg.excluded_assets and asset in _fltcfg.excluded_assets:
                    _dedup_key = (ticker, f"{_flt_pt}_asset_excluded")
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, f"{_flt_pt}_asset_excluded",
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            product_type=window.get("product_type"),
                            hourly_pre_temp_prob=_hourly_pre_temp_prob,
                            hourly_applied_temp_t=_configured_temp_t,
                            hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                            hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                            hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                            hourly_shadow_blend_50=_hourly_shadow_blend_50,
                            hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                            hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                            hourly_shadow_blend_20=_hourly_shadow_blend_20,
                            hourly_shadow_blend_30=_hourly_shadow_blend_30,
                            hourly_shadow_blend_60=_hourly_shadow_blend_60,
                            hourly_post_temp_prob=_hourly_post_temp_prob,
                            **_oft_db, **_shadow_diag)
                    continue

                # Layer 2: STC timing restriction (config-driven)
                if (_fltcfg.min_stc_entry is not None
                        and (seconds_remaining < _fltcfg.min_stc_entry
                             or seconds_remaining > _fltcfg.max_stc_entry)):
                    _dedup_key = (ticker, f"{_flt_pt}_timing_restricted")
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, f"{_flt_pt}_timing_restricted",
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            product_type=window.get("product_type"),
                            wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                            wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                            wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                            wx_n_members=_shadow_extra.get("wx_n_members"),
                            wx_market_type=_shadow_extra.get("wx_market_type"),
                            wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                            wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                            wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                            hourly_pre_temp_prob=_hourly_pre_temp_prob,
                            hourly_applied_temp_t=_configured_temp_t,
                            hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                            hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                            hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                            hourly_shadow_blend_50=_hourly_shadow_blend_50,
                            hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                            hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                            hourly_shadow_blend_20=_hourly_shadow_blend_20,
                            hourly_shadow_blend_30=_hourly_shadow_blend_30,
                            hourly_shadow_blend_60=_hourly_shadow_blend_60,
                            hourly_post_temp_prob=_hourly_post_temp_prob,
                            **_oft_db, **_shadow_diag)
                    continue

                # Layer 3b: Per-window position limit (config-driven)
                if _fltcfg.max_positions_per_window is not None:
                    _wkey = window["event_ticker"]
                    if self._hourly_window_counts.get(_wkey, 0) >= _fltcfg.max_positions_per_window:
                        _dedup_key = (ticker, f"{_flt_pt}_window_limit")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset, f"{_flt_pt}_window_limit",
                                spot_price=spot, threshold=threshold, volatility=blended_rv,
                                market_price=best_ask, seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge, z_score=z_score,
                                vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                                breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                                ask_depth=ask_depth, best_ask_source=best_ask_source,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                                **_oft_db, **_shadow_diag)
                        continue

                # Layer 3c: Per-window aggregate risk cap (config-driven)
                if _fltcfg.max_window_risk is not None:
                    _wkey = window["event_ticker"]
                    _wrisk = self._hourly_window_risk.get(_wkey, 0.0)
                    _this_risk = (sizing["contracts"] * best_ask) / (balance if balance > 0 else 1)
                    if _wrisk + _this_risk > _fltcfg.max_window_risk:
                        _dedup_key = (ticker, f"{_flt_pt}_window_risk_cap")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset, f"{_flt_pt}_window_risk_cap",
                                spot_price=spot, threshold=threshold, volatility=blended_rv,
                                market_price=best_ask, seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge, z_score=z_score,
                                vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                                breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                                ask_depth=ask_depth, best_ask_source=best_ask_source,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                                **_oft_db, **_shadow_diag)
                        continue

                # ── WEATHER FOCUS FILTER ──
                # Shadow trade signals only for lower_tail in NE/Midwest cities.
                # Other weather markets still logged as raw data in earlier filter stages
                # (price_out_of_range, insufficient_edge, timing_restricted) but don't
                # reach the observation gate for shadow trade simulation.
                if _pt == "weather":
                    from bot.engines.weather_engine import WEATHER_SHADOW_FOCUS_MARKET_TYPES, WEATHER_SHADOW_FOCUS_CITIES  # Sprint 10.1c sibling-reorg (2026-05-11)
                    _wx_mtype_here = _shadow_extra.get("wx_market_type")
                    _wx_city_here = asset.replace("_TEMP", "") if asset else ""
                    _wx_excluded_reason = None
                    if _wx_mtype_here and _wx_mtype_here not in WEATHER_SHADOW_FOCUS_MARKET_TYPES:
                        _wx_excluded_reason = f"weather_excluded_mtype_{_wx_mtype_here}"
                    elif _wx_city_here not in WEATHER_SHADOW_FOCUS_CITIES:
                        _wx_excluded_reason = "weather_excluded_city"
                    if _wx_excluded_reason:
                        _dedup_key = (ticker, _wx_excluded_reason)
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset, _wx_excluded_reason,
                                spot_price=spot, threshold=threshold, volatility=blended_rv,
                                market_price=best_ask, seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge, z_score=z_score,
                                vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                                breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                                ask_depth=ask_depth, best_ask_source=best_ask_source,
                                product_type=_pt,
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                kelly_f=sizing.get("kelly_f") if sizing else None,
                                position_size=sizing.get("contracts") if sizing else None,
                                drawdown_scaler=sizing.get("drawdown_scaler") if sizing else None,
                                **_oft_db, **_shadow_diag)
                        continue

                # ── HOURLY EDGE CAP ──
                # Reject hourly signals with edge > 5% — the 10%+ zone has 24.2% WR (edge inversion).
                # Log rejection and continue. Does NOT affect 15M (gated on _pt == "hourly").
                if _pt == "hourly" and fee_adjusted_edge > HOURLY_MAX_EDGE:
                    _hecap_dedup = (ticker, "hourly_edge_cap")
                    if _hecap_dedup not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_hecap_dedup)
                        try:
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset, "hourly_edge_cap",
                                spot_price=spot, threshold=threshold, volatility=blended_rv,
                                market_price=best_ask, seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge, z_score=z_score,
                                vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                                breakeven_wr=best_ask / 100.0,
                                product_type="hourly", **_oft_db, **_shadow_diag)
                        except Exception:
                            logging.warning("insert_evaluated_opportunity failed (hourly_edge_cap)", exc_info=True)
                    continue

                # ── GENERIC OBSERVATION GATE ──
                # Config-driven: any market type with observation_only=True is blocked here
                if _fltcfg.observation_only and _fltcfg.observation_filter_label:
                    _obs_label = _fltcfg.observation_filter_label
                    _obs_pt = window.get("product_type")
                    # Hourly-specific: journal logging with extra detail
                    if _obs_pt == "hourly":
                        try:
                            self._logger.log_opportunity({
                                "filter_stage": _obs_label,
                                "product_type": _obs_pt,
                                "ticker": ticker,
                                "event_ticker": window["event_ticker"],
                                "asset": asset,
                                "spot_price": spot, "threshold": threshold,
                                "volatility": blended_rv, "market_price": best_ask,
                                "seconds_to_close": round(seconds_remaining, 1),
                                "calibrated_prob": round(final_prob, 6),
                                "edge": round(edge, 6),
                                "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                "position_size": sizing["contracts"],
                                "kelly_f": sizing["kelly_f"],
                                "drawdown_scaler": sizing["drawdown_scaler"],
                                "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                                "ofa_adjustment": round(ofa_adjustment, 6),
                                "strategy": strategy,
                                "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                                "hourly_pre_temp_prob": round(_hourly_pre_temp_prob, 6) if _hourly_pre_temp_prob is not None else None,
                                "hourly_temp_t": _fltcfg.temperature_t if _fltcfg.temperature_enabled else None,
                                "old_system_prob": round(_old_system_prob, 6),
                                "counterfactual": _cf,
                                **_shadow_diag,
                            })
                        except Exception:
                            pass
                    _dedup_key = (ticker, _obs_label)
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        # Build type-specific extra kwargs for DB insert
                        _obs_extra = {}
                        if _obs_pt == "hourly":
                            _obs_extra.update(
                                counterfactual=_cf_json,
                                shadow_cal_prob=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("prob") if _cf else None,
                                shadow_cal_fee_edge=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("fee_edge") if _cf else None,
                                shadow_cal_temperature=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("temperature") if _cf else None,
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                            )
                        elif _obs_pt == "spx_hourly":
                            # SPX-specific diagnostics: VIX, seasonal, data quality
                            _spx_diag = {
                                "vix_implied_rv": vol_est.get("vix_implied_rv"),
                                "seasonal_factor": vol_est.get("seasonal_factor"),
                                "n_returns": vol_est.get("n_returns"),
                                "rk_rv": vol_est.get("rk_rv"),
                            }
                            _obs_extra.update(
                                counterfactual=json.dumps(_spx_diag),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                            )
                        elif _obs_pt == "weather":
                            _obs_extra.update(
                                wx_ensemble_mean=vol_est.get("ensemble_mean"),
                                wx_ensemble_std=vol_est.get("ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=vol_est.get("n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                            )
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, _obs_label,
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            position_size=sizing["contracts"],
                            kelly_f=sizing["kelly_f"],
                            drawdown_scaler=sizing["drawdown_scaler"],
                            calibrated_prob_raw=calibrated_prob_raw,
                            ofa_adjustment=ofa_adjustment,
                            strategy=strategy,
                            old_system_prob=_old_system_prob,
                            product_type=_obs_pt, **_obs_extra, **_oft_db, **_shadow_diag)
                    _obs_log_prefix = {"hourly": "HOURLY_OBS", "spx_hourly": "SPX_OBS", "weather": "WEATHER_OBS"}.get(_obs_pt, "OBS")
                    logging.info("%s: %s ask=%d edge=%.2f%% prob=%.1f%% stc=%.0fs",
                                 _obs_log_prefix, ticker, best_ask, fee_adjusted_edge * 100, final_prob * 100, seconds_remaining)
                    # ── SPX NO-side observation ──
                    # Log NO-side data for offline analysis when YES is in 65-85c range
                    if _obs_pt == "spx_hourly" and 65 <= best_ask <= 85 and seconds_remaining >= 600:
                        _no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                        _spx_no_price = None
                        if _no_ask_raw is not None:
                            _spx_no_price = (dollars_str_to_cents(_no_ask_raw) if isinstance(_no_ask_raw, str)
                                             else int(_no_ask_raw))
                            if _spx_no_price <= 0:
                                _spx_no_price = None
                        if _spx_no_price and 0 < _spx_no_price < 100:
                            _spx_no_prob = 1.0 - final_prob
                            _spx_no_edge = _spx_no_prob - _spx_no_price / 100.0
                            _spx_no_dedup = (ticker, "spx_no_side_observation")
                            if _spx_no_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_spx_no_dedup)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        "spx_no_side_observation",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv,
                                        market_price=_spx_no_price,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=round(_spx_no_prob, 6),
                                        edge=round(_spx_no_edge, 6),
                                        z_score=z_score, raw_prob=round(1.0 - raw_prob, 6) if raw_prob else None,
                                        product_type="spx_hourly",
                                        side="no",
                                        **_shadow_diag)
                                except Exception:
                                    logging.debug("spx_no_side_observation insert failed", exc_info=True)
                    # ── Weather Shadow Variants (capped30, short_stc) ──
                    if _obs_pt == "weather":
                        for _wscfg in WEATHER_SHADOW_CONFIGS:
                            _wsname = _wscfg["name"]
                            if "max_price" in _wscfg and best_ask > _wscfg["max_price"]:
                                continue
                            if "max_stc" in _wscfg and seconds_remaining > _wscfg["max_stc"]:
                                continue
                            _ws_dedup = (ticker, _wsname)
                            if _ws_dedup in self._eval_opp_seen:
                                continue
                            self._eval_opp_seen.add(_ws_dedup)
                            _ws_ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            try:
                                self._state.insert_evaluated_opportunity(
                                    ticker, window["event_ticker"], asset,
                                    _wsname,
                                    spot_price=spot, threshold=threshold,
                                    volatility=blended_rv, market_price=best_ask,
                                    seconds_to_close=seconds_remaining,
                                    calibrated_prob=final_prob, edge=edge,
                                    ofa_adjustment=ofa_adjustment,
                                    z_score=z_score, vol_regime=vol_est["regime"],
                                    raw_prob=raw_prob,
                                    calibrated_prob_raw=calibrated_prob_raw,
                                    calibration_method=calibration_method,
                                    fee_adjusted_edge=fee_adjusted_edge,
                                    breakeven_wr=best_ask / 100.0,
                                    expected_value=round(_ws_ev, 2),
                                    ask_depth=ask_depth, best_ask_source=best_ask_source,
                                    position_size=sizing["contracts"],
                                    kelly_f=sizing["kelly_f"],
                                    drawdown_scaler=sizing["drawdown_scaler"],
                                    strategy=strategy, old_system_prob=_old_system_prob,
                                    product_type="weather",
                                    wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                    wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                    wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                    wx_n_members=_shadow_extra.get("wx_n_members"),
                                    wx_market_type=_shadow_extra.get("wx_market_type"),
                                    wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                    wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                    wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                    **_oft_db, **_shadow_diag)
                            except Exception:
                                logging.warning("insert_evaluated_opportunity failed (%s)", _wsname, exc_info=True)
                        # ── Bracket NO intercept ──────────────────────────
                        # Buy NO on bracket contracts when YES is 88-96c. Computes NO cost
                        # from YES price (bypasses corrupted _no_ask_eq). 91.7% NO rate on
                        # 157 single-strike contracts, breakeven 4-12%.
                        if (BRACKET_NO_ENABLED
                                and _wx_mtype == "bracket"
                                and BRACKET_NO_YES_MIN <= best_ask <= BRACKET_NO_YES_MAX
                                and seconds_remaining >= BRACKET_NO_MIN_STC):
                            _bn_no_cost = 100 - best_ask  # 4-12c (bypasses corrupted _no_ask_eq)
                            _bn_edge = BRACKET_NO_ASSUMED_PROB - _bn_no_cost / 100.0
                            # Count existing bracket NO positions + candidates this scan
                            _bn_existing = sum(1 for p in self._state.get_open_positions()
                                               if p.get("side") == "no" and (p.get("ticker") or "").startswith("KXHIGH"))
                            _bn_in_scan = sum(1 for c in candidates if c.get("strategy") == "bracket_no")
                            if _bn_existing + _bn_in_scan < BRACKET_NO_MAX_CONCURRENT:
                                # Per-ticker dedup: skip if already holding this specific bracket strike
                                _bn_has_pos = any(p.get("ticker") == ticker for p in self._state.get_open_positions())
                                if not _bn_has_pos:
                                    _bn_dedup = (ticker, "bracket_no")
                                    _bn_fee = calculate_fee(BRACKET_NO_FIXED_CONTRACTS, _bn_no_cost,
                                                            is_taker=True,
                                                            fee_mult_taker=_mcfg.fee_multiplier_taker,
                                                            fee_mult_maker=_mcfg.fee_multiplier_maker)
                                    _bn_fee_edge = _bn_edge - _bn_fee / (BRACKET_NO_FIXED_CONTRACTS * 100.0)
                                    logging.info(
                                        "BRACKET_NO_CANDIDATE: %s yes=%dc no_cost=%dc edge=%.2f%% "
                                        "fee_edge=%.2f%% stc=%.0fs mtype=%s",
                                        ticker, best_ask, _bn_no_cost, _bn_edge * 100,
                                        _bn_fee_edge * 100, seconds_remaining, _wx_mtype)
                                    candidates.append({
                                        "ticker": ticker,
                                        "event_ticker": window["event_ticker"],
                                        "asset": asset,
                                        "product_type": "weather",
                                        "side": "no",
                                        "spot": spot,
                                        "threshold": threshold,
                                        "seconds_to_close": round(seconds_remaining, 1),
                                        "blended_rv": blended_rv,
                                        "calibrated_prob": BRACKET_NO_ASSUMED_PROB,
                                        "z_score": z_score,
                                        "best_yes_ask": _bn_no_cost,  # NO cost for execution (mirrors existing pattern)
                                        "best_ask_source": best_ask_source,
                                        "edge": round(_bn_edge, 6),
                                        "fee_adjusted_edge": round(_bn_fee_edge, 6),
                                        "position_size": BRACKET_NO_FIXED_CONTRACTS,
                                        "kelly_f": 0.0,
                                        "drawdown_scaler": 1.0,
                                        "vol_regime": vol_est["regime"],
                                        "balance_at_scan": balance,
                                        "strategy": "bracket_no",
                                        "strategy_scores": {},
                                        "ob_snapshot": {},
                                        "calibrated_prob_raw": round(1.0 - calibrated_prob_raw, 6) if calibrated_prob_raw is not None else None,
                                        "ofa_adjustment": round(ofa_adjustment, 6),
                                        "ofa_confidence": "none",
                                        "raw_prob": 1.0 - raw_prob if raw_prob is not None else None,
                                        "calibration_method": "assumed_prob",
                                        "old_system_prob": round(1.0 - _old_system_prob, 6),
                                        "kalshi_oft_signals": {},
                                        "counterfactual_json": None,
                                        "_bracket_yes_price": best_ask,  # Store original YES price for logging
                                    })
                                    # DB insert (deduped per ticker)
                                    if _bn_dedup not in self._eval_opp_seen:
                                        self._eval_opp_seen.add(_bn_dedup)
                                        try:
                                            self._state.insert_evaluated_opportunity(
                                                ticker, window["event_ticker"], asset,
                                                "bracket_no",
                                                spot_price=spot, threshold=threshold,
                                                volatility=blended_rv, market_price=best_ask,
                                                seconds_to_close=seconds_remaining,
                                                calibrated_prob=BRACKET_NO_ASSUMED_PROB,
                                                edge=round(_bn_edge, 6),
                                                ofa_adjustment=ofa_adjustment,
                                                z_score=z_score, vol_regime=vol_est["regime"],
                                                raw_prob=1.0 - raw_prob if raw_prob is not None else None,
                                                calibrated_prob_raw=1.0 - calibrated_prob_raw if calibrated_prob_raw is not None else None,
                                                calibration_method="assumed_prob",
                                                fee_adjusted_edge=round(_bn_fee_edge, 6),
                                                breakeven_wr=_bn_no_cost / 100.0,
                                                ask_depth=ask_depth, best_ask_source=best_ask_source,
                                                position_size=BRACKET_NO_FIXED_CONTRACTS,
                                                kelly_f=0.0, drawdown_scaler=1.0,
                                                strategy="bracket_no", old_system_prob=_old_system_prob,
                                                product_type="weather", side="no",
                                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                                **_oft_db, **_shadow_diag)
                                        except Exception:
                                            logging.warning("insert_evaluated_opportunity failed (bracket_no)", exc_info=True)

                        # ── Weather NO-side (shadow + live candidate) ──
                        # Model is structurally wrong on NO (predicts 3-16%, actual 79.7% WR).
                        # The shadow log uses MODEL edge (almost always negative).
                        # The live candidate uses an ASSUMED 0.70 prob to bypass the broken model.
                        # CRITICAL: the live candidate must NOT be nested inside the shadow
                        # edge-gate (_wn_no_fee_edge > 0) — that branch essentially never fires
                        # because the model is broken, which would suppress the live candidate
                        # entirely. This was the bug that kept weather at 0 trades Apr 4-11.
                        # See kb/failures/weather-no-candidate-never-fires.md
                        if final_prob >= WEATHER_NO_SHADOW_MIN_YES_PROB and _no_ask_eq is not None:
                            # Compute NO-side economics once (cheap, used by both paths).
                            _wn_no_prob = 1.0 - final_prob
                            _wn_no_fee = calculate_fee(1, _no_ask_eq, is_taker=True,
                                                       fee_mult_taker=_mcfg.fee_multiplier_taker,
                                                       fee_mult_maker=_mcfg.fee_multiplier_maker)
                            _wn_no_edge = _wn_no_prob - _no_ask_eq / 100.0
                            _wn_no_fee_edge = _wn_no_edge - _wn_no_fee / 100.0

                            # Shadow path: model-edge gated (rarely fires; model is wrong on NO).
                            _wn_dedup = (ticker, "weather_no_shadow")
                            if _wn_dedup not in self._eval_opp_seen and _wn_no_fee_edge > 0:
                                self._eval_opp_seen.add(_wn_dedup)
                                _wn_ev = (_wn_no_prob * (100 - _no_ask_eq)) - ((1 - _wn_no_prob) * _no_ask_eq) - _wn_no_fee
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        "weather_no_shadow",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=_no_ask_eq,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=_wn_no_prob, edge=_wn_no_edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score, vol_regime=vol_est["regime"],
                                        raw_prob=1.0 - raw_prob if raw_prob is not None else None,
                                        calibrated_prob_raw=1.0 - calibrated_prob_raw if calibrated_prob_raw is not None else None,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=_wn_no_fee_edge,
                                        breakeven_wr=_no_ask_eq / 100.0,
                                        expected_value=round(_wn_ev, 2),
                                        ask_depth=ask_depth, best_ask_source=best_ask_source,
                                        position_size=1,  # Fixed 1-contract sizing
                                        kelly_f=0.0,
                                        drawdown_scaler=1.0,
                                        strategy=strategy, old_system_prob=_old_system_prob,
                                        product_type="weather", side="no",
                                        wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                        wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                        wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                        wx_n_members=_shadow_extra.get("wx_n_members"),
                                        wx_market_type=_shadow_extra.get("wx_market_type"),
                                        wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                        wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                        wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                        **_oft_db, **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (weather_no_shadow)", exc_info=True)
                            # Note: the weather NO LIVE CANDIDATE was previously here, nested
                            # inside the YES-side observation gate. That location was dead code
                            # because weather YES evals almost always fail the insufficient_edge
                            # check before reaching this branch, and the 1050+ NO-side evals
                            # flow through _process_no_side_shadow() instead. The live candidate
                            # has been moved there. See kb/failures/weather-no-candidate-never-fires.md
                    # V2 variant: shadow cal pipeline (temperature + no blend)
                    if _obs_pt == "hourly" and _cf:
                        self._insert_hourly_v2_variant(
                            ticker, window, asset, raw_prob, best_ask,
                            seconds_remaining, spot, threshold, blended_rv,
                            ofa_adjustment, z_score, vol_est,
                            calibrated_prob_raw, est_fee_1c,
                            ask_depth, best_ask_source, _cf, _shadow_diag)
                    # ── Config A shadow variant (no_XRP + edge ≤ 0.7%) ──
                    if (_obs_pt == "hourly"
                            and asset not in HOURLY_CONFIG_A_EXCLUDED
                            and fee_adjusted_edge <= HOURLY_CONFIG_A_MAX_EDGE):
                        _ca_dedup = (ticker, "hourly_config_a")
                        if _ca_dedup not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_ca_dedup)
                            _ca_ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            try:
                                self._state.insert_evaluated_opportunity(
                                    ticker, window["event_ticker"], asset,
                                    "hourly_config_a",
                                    spot_price=spot, threshold=threshold,
                                    volatility=blended_rv, market_price=best_ask,
                                    seconds_to_close=seconds_remaining,
                                    calibrated_prob=final_prob, edge=edge,
                                    ofa_adjustment=ofa_adjustment,
                                    z_score=z_score, vol_regime=vol_est["regime"],
                                    raw_prob=raw_prob,
                                    calibrated_prob_raw=calibrated_prob_raw,
                                    calibration_method=calibration_method,
                                    fee_adjusted_edge=fee_adjusted_edge,
                                    breakeven_wr=best_ask / 100.0,
                                    expected_value=round(_ca_ev, 2),
                                    ask_depth=ask_depth, best_ask_source=best_ask_source,
                                    position_size=sizing["contracts"],
                                    kelly_f=sizing["kelly_f"],
                                    drawdown_scaler=sizing["drawdown_scaler"],
                                    strategy=strategy, old_system_prob=_old_system_prob,
                                    product_type="hourly",
                                    **_oft_db, **_shadow_diag)
                            except Exception:
                                logging.warning("insert_evaluated_opportunity failed (hourly_config_a)", exc_info=True)
                    # ── Config B: BTC 70-89c wl2 (promotion candidate) ──
                    # Tracks the high-alpha low-price tier for BTC hourly.
                    # Uses its own per-window counter (_cb_window_counts) with price-sorted
                    # top-2 selection (same as backtest methodology).
                    if (_obs_pt == "hourly"
                            and asset == HOURLY_CONFIG_B_ASSET
                            and HOURLY_CONFIG_B_MIN_PRICE <= best_ask <= HOURLY_CONFIG_B_MAX_PRICE
                            and fee_adjusted_edge > 0):
                        _cb_wkey = window["event_ticker"]
                        _cb_count = self._config_b_window_counts.get(_cb_wkey, 0)
                        if _cb_count < HOURLY_CONFIG_B_MAX_PER_WINDOW:
                            _cb_dedup = (ticker, "hourly_config_b")
                            if _cb_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_cb_dedup)
                                self._config_b_window_counts[_cb_wkey] = _cb_count + 1
                                _cb_ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        "hourly_config_b",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score, vol_regime=vol_est["regime"],
                                        raw_prob=raw_prob,
                                        calibrated_prob_raw=calibrated_prob_raw,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=round(_cb_ev, 2),
                                        ask_depth=ask_depth, best_ask_source=best_ask_source,
                                        position_size=sizing["contracts"],
                                        kelly_f=sizing["kelly_f"],
                                        drawdown_scaler=sizing["drawdown_scaler"],
                                        strategy=strategy, old_system_prob=_old_system_prob,
                                        product_type="hourly",
                                        **_oft_db, **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (hourly_config_b)", exc_info=True)
                    # ── Configs C–G: data-driven shadow variants ──
                    if _obs_pt == "hourly":
                        for _scfg in HOURLY_SHADOW_CONFIGS:
                            _sname = _scfg["name"]
                            if "included_assets" in _scfg and asset not in _scfg["included_assets"]:
                                continue
                            if "excluded_assets" in _scfg and asset in _scfg["excluded_assets"]:
                                continue
                            if "min_stc" in _scfg and seconds_remaining < _scfg["min_stc"]:
                                continue
                            if "max_stc" in _scfg and seconds_remaining > _scfg["max_stc"]:
                                continue
                            if "max_edge" in _scfg and fee_adjusted_edge > _scfg["max_edge"]:
                                continue
                            _s_dedup = (ticker, _sname)
                            if _s_dedup in self._eval_opp_seen:
                                continue
                            self._eval_opp_seen.add(_s_dedup)
                            # Recompute prob for configs with custom temperature/blend
                            _s_final = final_prob
                            _s_edge = edge
                            _s_fee_edge = fee_adjusted_edge
                            if "temperature" in _scfg or "blend_w" in _scfg:
                                _s_base = _hourly_pre_temp_prob
                                if _s_base is not None:
                                    _s_t = _scfg.get("temperature", _configured_temp_t or 1.0)
                                    _sp = max(0.001, min(0.999, _s_base))
                                    _slz = math.log(_sp / (1.0 - _sp))
                                    _s_final = 1.0 / (1.0 + math.exp(-_slz / _s_t))
                                    _s_final = max(0.01, min(NUMERICAL_SAFETY_CEILING, _s_final + ofa_adjustment))
                                    _s_bw = _scfg.get("blend_w", _effective_blend_w)
                                    if best_ask < ENDGAME_BLEND_PRICE and _s_bw > 0:
                                        _s_final = (1.0 - _s_bw) * _s_final + _s_bw * (best_ask / 100.0)
                                    _s_edge = _s_final - best_ask / 100.0
                                    _s_fee_edge = _s_edge - est_fee_1c / 100.0
                            _s_ev = (_s_final * (100 - best_ask)) - ((1 - _s_final) * best_ask) - est_fee_1c
                            try:
                                self._state.insert_evaluated_opportunity(
                                    ticker, window["event_ticker"], asset,
                                    _sname,
                                    spot_price=spot, threshold=threshold,
                                    volatility=blended_rv, market_price=best_ask,
                                    seconds_to_close=seconds_remaining,
                                    calibrated_prob=_s_final, edge=_s_edge,
                                    ofa_adjustment=ofa_adjustment,
                                    z_score=z_score, vol_regime=vol_est["regime"],
                                    raw_prob=raw_prob,
                                    calibrated_prob_raw=calibrated_prob_raw,
                                    calibration_method=calibration_method,
                                    fee_adjusted_edge=_s_fee_edge,
                                    breakeven_wr=best_ask / 100.0,
                                    expected_value=round(_s_ev, 2),
                                    ask_depth=ask_depth, best_ask_source=best_ask_source,
                                    position_size=sizing["contracts"],
                                    kelly_f=sizing["kelly_f"],
                                    drawdown_scaler=sizing["drawdown_scaler"],
                                    strategy=strategy, old_system_prob=_old_system_prob,
                                    product_type="hourly",
                                    **_oft_db, **_shadow_diag)
                            except Exception:
                                logging.warning("insert_evaluated_opportunity failed (%s)", _sname, exc_info=True)
                    # Increment per-window counters even in observation mode so Layer 3b/3c
                    # limits work for counterfactual analysis (without this, counter stays 0
                    # and the limit is dead code — bug found by audit: 11 SPX positions in one window)
                    if _fltcfg.max_positions_per_window is not None:
                        _wkey = window["event_ticker"]
                        self._hourly_window_counts[_wkey] = self._hourly_window_counts.get(_wkey, 0) + 1
                        self._hourly_window_risk[_wkey] = self._hourly_window_risk.get(_wkey, 0.0) + \
                            (sizing["contracts"] * best_ask) / (balance if balance > 0 else 1)

                    # ── Hourly Alt Shadow Strategies (BTC/ETH/SOL/XRP) ──
                    # Evaluate Market-Making and HAR-RV shadow strategies in parallel
                    # with the existing EGARCH pipeline. Shadow-only, cannot place orders.
                    if (_obs_pt == "hourly"
                            and self._ml and getattr(self._ml, "hourly_alt_shadow", None)):
                        try:
                            _alt_bid = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                            _alt_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                            _alt_no_ask = (dollars_str_to_cents(_alt_no_ask_raw) if isinstance(_alt_no_ask_raw, str)
                                           else int(_alt_no_ask_raw)) if _alt_no_ask_raw is not None else None
                            self._ml.hourly_alt_shadow.evaluate_strike(
                                asset=asset, ticker=ticker,
                                event_ticker=window["event_ticker"],
                                spot_price=spot, threshold=threshold,
                                seconds_to_close=seconds_remaining,
                                best_bid=_alt_bid, best_ask=best_ask,
                                market_price=best_ask, ob_data=ob_data,
                                egarch_prob=final_prob,
                                egarch_edge=fee_adjusted_edge,
                                no_ask=_alt_no_ask)
                        except Exception:
                            logging.debug("hourly_alt_shadow evaluate failed", exc_info=True)

                    # ── SPX HAR-RV Shadow Strategy ──
                    # Evaluate HAR-RV shadow strategy in parallel with EGARCH
                    if (_obs_pt == "spx_hourly"
                            and self._ml and getattr(self._ml, "spx_harrv_shadow", None)):
                        try:
                            _harv_bid = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                            _harv_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                            _harv_no_ask = (dollars_str_to_cents(_harv_no_ask_raw) if isinstance(_harv_no_ask_raw, str)
                                            else int(_harv_no_ask_raw)) if _harv_no_ask_raw is not None else None
                            self._ml.spx_harrv_shadow.evaluate_strike(
                                ticker=ticker,
                                event_ticker=window["event_ticker"],
                                spot_price=spot, threshold=threshold,
                                seconds_to_close=seconds_remaining,
                                best_bid=_harv_bid, best_ask=best_ask,
                                market_price=best_ask,
                                egarch_prob=final_prob,
                                egarch_edge=fee_adjusted_edge,
                                no_ask=_harv_no_ask)
                        except Exception:
                            logging.debug("spx_harrv_shadow evaluate failed", exc_info=True)

                    continue  # DO NOT add to candidates — observation gate

                # ── 15M Shadow Engine (post-filter) ──
                # _seen dedup in shadow engine prevents double-eval with pre-filter call.
                if (window.get("product_type") in (None, "15m")
                        and self._ml and getattr(self._ml, "fifteenm_shadow", None)):
                    try:
                        _15m_bid = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                        _15m_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                        _15m_no_ask = (dollars_str_to_cents(_15m_no_ask_raw) if isinstance(_15m_no_ask_raw, str)
                                       else int(_15m_no_ask_raw)) if _15m_no_ask_raw is not None else None
                        self._ml.fifteenm_shadow.evaluate_strike(
                            asset=asset, ticker=ticker,
                            event_ticker=window["event_ticker"],
                            spot_price=spot, threshold=threshold,
                            seconds_to_close=seconds_remaining,
                            market_price=best_ask,
                            best_bid=_15m_bid, best_ask=best_ask,
                            blended_rv=blended_rv,
                            egarch_sigma=vol_est.get("egarch_sigma"),
                            z_score=z_score, live_prob=final_prob,
                            live_edge=edge, live_fee_edge=fee_adjusted_edge,
                            egarch_blend_weight=_shadow_diag.get("egarch_blend_weight"),
                            fee_adjusted_edge=fee_adjusted_edge,
                            no_ask=_15m_no_ask)
                    except Exception:
                        logging.warning("fifteenm_shadow evaluate failed", exc_info=True)

                # ── STC SHADOW GATE (15M only, >600s) ──
                # Outer boundary: everything above 600s is shadow-only.
                _stc_limit = STC_SHADOW_THRESHOLD
                if window.get("product_type") in (None, "15m") and seconds_remaining > _stc_limit:
                    _stc_stage = "stc_shadow_no_xrp" if asset != "XRP" else "stc_shadow_xrp"
                    _dedup_key = (ticker, _stc_stage)
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, _stc_stage,
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            position_size=sizing["contracts"],
                            kelly_f=sizing["kelly_f"],
                            drawdown_scaler=sizing["drawdown_scaler"],
                            calibrated_prob_raw=calibrated_prob_raw,
                            ofa_adjustment=ofa_adjustment,
                            strategy=strategy,
                            old_system_prob=_old_system_prob,
                            product_type=window.get("product_type"),
                            **_oft_db, **_shadow_diag)
                    continue

                # ── STC EXTENDED ZONE FLOOR (300-600s, 15M only) ──
                # Model is 9pp overconfident at 300-600s at low prices, but
                # high-price trades are profitable. Per-asset higher floors
                # extract the safe segment: ETH 90c+, BTC 93c+, XRP 92c+, SOL 95c+.
                # STC sizing scaler (300/STC) already reduces position sizes here.
                if (window.get("product_type") in (None, "15m")
                        and seconds_remaining > STC_EXTENDED_LIVE_FLOOR):
                    _stc_ext_floor = {"BTC": STC_EXTENDED_BTC_MIN_PRICE, "ETH": STC_EXTENDED_ETH_MIN_PRICE,
                                      "SOL": STC_EXTENDED_SOL_MIN_PRICE, "XRP": STC_EXTENDED_XRP_MIN_PRICE}.get(asset, MAX_ENTRY_PRICE)
                    if best_ask < _stc_ext_floor:
                        # Buffer rescue: fat buffer overrides the floor check.
                        # Data (Apr 1-8): buf>=0.25% at 300-600s = 21/21 100% WR,
                        # Wilson LB 88.6% > 87% breakeven. Uses scanner's computed
                        # size (Kelly × STC scaler × asset cap already applied).
                        _ext_buf = (spot - threshold) / threshold * 100 if threshold and threshold > 0 else 0
                        if _ext_buf >= STC_EXTENDED_BUFFER_RESCUE:
                            # SOL-only sizing cap: data (Apr 8-19, n=46) shows SOL rescue losses
                            # average 56ct vs wins 37ct — Kelly sizes UP on thin-buffer high-prob
                            # setups that fail. Cap to 25ct limits tail loss to ~$22 vs $45-72.
                            # BTC rescue (13/13 clean) unaffected.
                            if asset == "SOL" and sizing["contracts"] > SOL_RESCUE_CONTRACT_CAP:
                                _pre_cap = sizing["contracts"]
                                sizing["contracts"] = SOL_RESCUE_CONTRACT_CAP
                                logging.info(
                                    "SOL_RESCUE_CAP: %s capped %d→%d contracts (rescue buf=%.3f%%)",
                                    ticker, _pre_cap, SOL_RESCUE_CONTRACT_CAP, _ext_buf)
                            logging.info(
                                "STC_EXTENDED_BUFFER_RESCUE: %s %s @%dc buf=%.3f%% >= %.2f%% "
                                "(floor=%dc, STC=%.0fs) — allowing trade",
                                asset, ticker, best_ask, _ext_buf,
                                STC_EXTENDED_BUFFER_RESCUE, _stc_ext_floor, seconds_remaining)
                            pass  # fall through to normal candidate path
                        else:
                            _stc_ext_stage = "stc_extended_floor_shadow"
                            _dedup_key = (ticker, _stc_ext_stage)
                            if _dedup_key not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_dedup_key)
                                _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                                self._state.insert_evaluated_opportunity(
                                    ticker, window["event_ticker"], asset, _stc_ext_stage,
                                    rejection_reason=f"STC extended floor: ask={best_ask}c < {asset} floor {_stc_ext_floor}c (STC={seconds_remaining:.0f}s buf={_ext_buf:.3f}%)",
                                    spot_price=spot, threshold=threshold, volatility=blended_rv,
                                    market_price=best_ask, seconds_to_close=seconds_remaining,
                                    calibrated_prob=final_prob, edge=edge, z_score=z_score,
                                    vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                    calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                                    breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                                    ask_depth=ask_depth, best_ask_source=best_ask_source,
                                    position_size=sizing["contracts"],
                                    kelly_f=sizing["kelly_f"],
                                    drawdown_scaler=sizing["drawdown_scaler"],
                                    calibrated_prob_raw=calibrated_prob_raw,
                                    ofa_adjustment=ofa_adjustment,
                                    strategy=strategy,
                                    old_system_prob=_old_system_prob,
                                    product_type=window.get("product_type"),
                                    **_oft_db, **_shadow_diag)
                            continue

                # ── XRP SHADOW GATE (15M only) ──
                # XRP 15M: -$32.97 all-time. Log for counterfactual, don't trade.
                if XRP_15M_SHADOW and asset == "XRP" and window.get("product_type") in (None, "15m"):
                    _dedup_key = (ticker, "xrp_shadow")
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        _xrp_88_tag = "xrp_88_eligible" if best_ask >= XRP_SHADOW_MIN_PRICE else "xrp_88_ineligible"
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, "xrp_shadow",
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            position_size=sizing["contracts"],
                            kelly_f=sizing["kelly_f"],
                            drawdown_scaler=sizing["drawdown_scaler"],
                            calibrated_prob_raw=calibrated_prob_raw,
                            ofa_adjustment=ofa_adjustment,
                            strategy=strategy,
                            old_system_prob=_old_system_prob,
                            product_type=window.get("product_type"),
                            counterfactual=_xrp_88_tag,
                            **_oft_db, **_shadow_diag)
                    continue

                # ── HYPE SHADOW GATE (15M only, T1 onboarding 2026-05-10) ──
                # HYPE: shadow data collection — no live trades until T4 promotion
                # (per-asset MIN_ENTRY_PRICE/MAX_RISK_PER_TRADE elif chains + NBBO).
                if HYPE_15M_SHADOW and asset == "HYPE" and window.get("product_type") in (None, "15m"):
                    _dedup_key = (ticker, "hype_shadow")
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, "hype_shadow",
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            position_size=sizing["contracts"],
                            kelly_f=sizing["kelly_f"],
                            drawdown_scaler=sizing["drawdown_scaler"],
                            calibrated_prob_raw=calibrated_prob_raw,
                            ofa_adjustment=ofa_adjustment,
                            strategy=strategy,
                            old_system_prob=_old_system_prob,
                            product_type=window.get("product_type"),
                            **_oft_db, **_shadow_diag)
                    continue

                # ── DOGE SHADOW GATE (15M only, T1 onboarding 2026-05-10) ──
                # DOGE: shadow data collection — no live trades until T4 promotion.
                if DOGE_15M_SHADOW and asset == "DOGE" and window.get("product_type") in (None, "15m"):
                    _dedup_key = (ticker, "doge_shadow")
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, "doge_shadow",
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            position_size=sizing["contracts"],
                            kelly_f=sizing["kelly_f"],
                            drawdown_scaler=sizing["drawdown_scaler"],
                            calibrated_prob_raw=calibrated_prob_raw,
                            ofa_adjustment=ofa_adjustment,
                            strategy=strategy,
                            old_system_prob=_old_system_prob,
                            product_type=window.get("product_type"),
                            **_oft_db, **_shadow_diag)
                    continue

                # ── SOL SUB-86c TIME GATE (15M only) ──
                # SOL ≤85c far-from-expiry: 78.3% WR, -$289 (STC≥300s).
                # Near-expiry (<300s): 100% WR, +$228. Block the far, keep the near.
                if (SOL_LOW_ENTRY_STC_GATE
                        and asset == "SOL"
                        and window.get("product_type") in (None, "15m")
                        and best_ask <= 85
                        and seconds_remaining >= 300):
                    _sol_low_dedup = (ticker, "sol_low_entry_high_stc")
                    if _sol_low_dedup not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_sol_low_dedup)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        try:
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset, "sol_low_entry_high_stc",
                                rejection_reason=f"SOL sub-86c gate: ask={best_ask}c stc={seconds_remaining:.0f}s",
                                spot_price=spot, threshold=threshold, volatility=blended_rv,
                                market_price=best_ask, seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge, z_score=z_score,
                                vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                                breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                                ask_depth=ask_depth, best_ask_source=best_ask_source,
                                position_size=sizing["contracts"],
                                kelly_f=sizing["kelly_f"],
                                drawdown_scaler=sizing["drawdown_scaler"],
                                calibrated_prob_raw=calibrated_prob_raw,
                                ofa_adjustment=ofa_adjustment,
                                strategy=strategy,
                                old_system_prob=_old_system_prob,
                                product_type=window.get("product_type"),
                                **_oft_db, **_shadow_diag)
                        except Exception:
                            logging.warning("insert_evaluated_opportunity failed (sol_low_entry_high_stc)", exc_info=True)
                    continue

                # Track per-window counts for Layer 3b/3c limits (config-driven)
                if _fltcfg.max_positions_per_window is not None:
                    _wkey = window["event_ticker"]
                    self._hourly_window_counts[_wkey] = self._hourly_window_counts.get(_wkey, 0) + 1
                    self._hourly_window_risk[_wkey] = self._hourly_window_risk.get(_wkey, 0.0) + \
                        (sizing["contracts"] * best_ask) / (balance if balance > 0 else 1)

                scan_stats[asset]["candidates"] += 1
                self._session_total_candidates += 1
                self._session_asset_perf.setdefault(
                    asset, {"opportunities_found": 0, "times_selected": 0, "times_rejected": 0}
                )["opportunities_found"] += 1
                self._last_opportunity_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                self._recent_opportunities.append({
                    "ticker": ticker,
                    "asset": asset,
                    "seconds_to_close": round(seconds_remaining, 1),
                    "best_ask": best_ask,
                    "edge_bps": round(edge * 10000),
                    "chosen_strategy": strategy,
                    "rejection_reason": None,
                    "ts": self._last_opportunity_ts,
                })
                _cand_filter_stage = "hourly_live" if _pt == "hourly" else "candidate"
                try:
                    self._logger.log_opportunity({
                        "filter_stage": _cand_filter_stage,
                        "ticker": ticker,
                        "event_ticker": window["event_ticker"],
                        "asset": asset,
                        "spot_price": spot,
                        "threshold": threshold,
                        "volatility": blended_rv,
                        "market_price": best_ask,
                        "best_ask_source": best_ask_source,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "calibrated_prob": round(final_prob, 6),
                        "edge": round(edge, 6),
                        "position_size": sizing["contracts"],
                        "strategy": strategy,
                        "ofa_adjustment": round(ofa_adjustment, 6),
                        "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                        "old_system_prob": round(_old_system_prob, 6),
                        "kalshi_oft": (ofa_signals or {}).get("signals", {}).get("kalshi_orderbook", {}),
                        "counterfactual": _cf,
                        **_shadow_diag,
                        **_shadow_extra,
                    })
                except Exception:
                    logging.warning("insert_evaluated_opportunity failed (%s)", _cand_filter_stage, exc_info=True)

                # SOL high-edge shadow: log additional entry for 5%+ edge analysis
                if _sol_high_edge_shadow:
                    try:
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset,
                            "sol_high_edge_shadow",
                            rejection_reason=f"SOL edge {fee_adjusted_edge:.4f} > {SOL_HIGH_EDGE_SHADOW} (shadow only, trade NOT blocked)",
                            spot_price=spot, threshold=threshold,
                            volatility=blended_rv, market_price=best_ask,
                            seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge,
                            z_score=z_score, raw_prob=raw_prob,
                            fee_adjusted_edge=fee_adjusted_edge,
                            product_type=window.get("product_type"),
                            **_shadow_diag)
                    except Exception:
                        logging.debug("sol_high_edge_shadow insert failed", exc_info=True)

                candidates.append({
                    "ticker": ticker,
                    "event_ticker": window["event_ticker"],
                    "asset": asset,
                    "product_type": window.get("product_type"),
                    "spot": spot,
                    "threshold": threshold,
                    "seconds_to_close": round(seconds_remaining, 1),
                    "blended_rv": blended_rv,
                    "calibrated_prob": round(final_prob, 6),
                    "z_score": z_score,
                    "best_yes_ask": best_ask,
                    "best_ask_source": best_ask_source,
                    "edge": round(edge, 6),
                    "position_size": sizing["contracts"],
                    "kelly_f": sizing["kelly_f"],
                    "drawdown_scaler": sizing["drawdown_scaler"],
                    "vol_regime": vol_est["regime"],
                    "balance_at_scan": balance,
                    "spot_buffer_pct": round(_spot_buffer_pct, 4) if _spot_buffer_pct is not None else None,
                    "strategy": strategy,
                    "strategy_scores": strategy_scores,
                    "ob_snapshot": {
                        "best_ask": best_ask,
                        "ask_depth": ask_depth,
                        "total_depth": total_depth,
                        "best_bid": OrderExecutor._best_yes_bid(ob_data) if ob_data else None,
                        "bid_depth": OrderExecutor._best_yes_bid_depth(ob_data) if ob_data else 0,
                        "spread": (best_ask - OrderExecutor._best_yes_bid(ob_data))
                                  if ob_data and OrderExecutor._best_yes_bid(ob_data) is not None else None,
                    },
                    "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                    "ofa_adjustment": round(ofa_adjustment, 6),
                    "ofa_confidence": ofa_signals["confidence"] if ofa_signals else "none",
                    "raw_prob": raw_prob,
                    "calibration_method": calibration_method,
                    "old_system_prob": round(_old_system_prob, 6),
                    "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                    "kalshi_oft_signals": (ofa_signals or {}).get("signals", {}).get("kalshi_orderbook", {}),
                    "counterfactual_json": _cf_json,
                    "shadow_cal_prob": (_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("prob") if _cf else None,
                    "shadow_cal_fee_edge": (_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("fee_edge") if _cf else None,
                    "shadow_cal_temperature": (_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("temperature") if _cf else None,
                    "hourly_pre_temp_prob": _hourly_pre_temp_prob,
                    "hourly_applied_temp_t": _configured_temp_t,
                    "hourly_shadow_temp_2_0": _hourly_shadow_temp_2_0,
                    "hourly_shadow_temp_1_0": _hourly_shadow_temp_1_0,
                    "hourly_shadow_temp_2_5": _hourly_shadow_temp_2_5,
                    "hourly_shadow_blend_50": _hourly_shadow_blend_50,
                    "hourly_shadow_temp_1_75": _hourly_shadow_temp_1_75,
                    "hourly_shadow_temp_3_0": _hourly_shadow_temp_3_0,
                    "hourly_shadow_blend_20": _hourly_shadow_blend_20,
                    "hourly_shadow_blend_30": _hourly_shadow_blend_30,
                    "hourly_shadow_blend_60": _hourly_shadow_blend_60,
                    "hourly_post_temp_prob": _hourly_post_temp_prob,
                    **_shadow_diag,
                    **_shadow_extra,
                })

                # ── Time-of-day shadow: tag dead-zone and golden-hour candidates ──
                # Dead zones: 14, 16, 18, 22 UTC (data: 82-89% WR, negative PnL)
                # Golden hours: 3, 5, 6, 11 UTC (data: 96-100% WR, +$300/trade)
                # Shadow only — does NOT change live trading. Logs what 1.5x and 2.0x
                # thresholds would have done, for forward validation.
                if _pt in (None, "15m"):
                    _tod_hour = datetime.datetime.now(timezone.utc).hour
                    _tod_dead = _tod_hour in (14, 16, 18, 22)
                    _tod_golden = _tod_hour in (3, 5, 6, 11)
                    if _tod_dead or _tod_golden:
                        if _tod_golden:
                            _tod_stage = "golden_hour_shadow"
                        elif fee_adjusted_edge < _min_edge * 2.0:
                            if fee_adjusted_edge < _min_edge * 1.5:
                                _tod_stage = "dead_hour_shadow_1.5x"
                            else:
                                _tod_stage = "dead_hour_shadow_2.0x"
                        else:
                            _tod_stage = "dead_hour_passed"
                        _tod_dedup = (ticker, _tod_stage)
                        if _tod_dedup not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_tod_dedup)
                            try:
                                self._state.insert_evaluated_opportunity(
                                    ticker, window["event_ticker"], asset,
                                    _tod_stage,
                                    rejection_reason=f"hour={_tod_hour} edge={fee_adjusted_edge:.4f} thresh={_min_edge:.4f}",
                                    spot_price=spot, threshold=threshold,
                                    volatility=blended_rv, market_price=best_ask,
                                    seconds_to_close=seconds_remaining,
                                    calibrated_prob=final_prob, edge=edge,
                                    ofa_adjustment=ofa_adjustment,
                                    z_score=z_score,
                                    vol_regime=vol_est["regime"],
                                    calibrated_prob_raw=calibrated_prob_raw,
                                    kelly_f=sizing.get("kelly_f"),
                                    position_size=sizing.get("contracts"),
                                    breakeven_wr=best_ask / 100.0,
                                    ask_depth=ask_depth,
                                    best_ask_source=best_ask_source,
                                    raw_prob=raw_prob,
                                    calibration_method=calibration_method,
                                    fee_adjusted_edge=fee_adjusted_edge,
                                    product_type=window.get("product_type"),
                                    **_shadow_diag)
                            except Exception:
                                logging.warning("insert_evaluated_opportunity failed (%s)", _tod_stage, exc_info=True)

                # ── Forward observation tags: collect data under current config ──
                # Tag 1: SOL sub-88c during US morning (UTC 12-17)
                # Data: 60% WR pre-BLR, only 5 trades post-BLR — need forward data
                if (_pt in (None, "15m") and asset == "SOL" and best_ask < 88
                        and 12 <= _tod_hour <= 17):
                    _obs_dedup = (ticker, "sol_usmorn_sub88")
                    if _obs_dedup not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_obs_dedup)
                        try:
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "sol_usmorn_sub88",
                                rejection_reason=f"SOL {best_ask}c hour={_tod_hour} (observation only)",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge,
                                z_score=z_score, raw_prob=raw_prob,
                                fee_adjusted_edge=fee_adjusted_edge,
                                position_size=sizing.get("contracts"),
                                product_type=window.get("product_type"),
                                **_shadow_diag)
                        except Exception:
                            logging.debug("sol_usmorn_sub88 insert failed", exc_info=True)

                # Tag 2: Sub-2-min STC during US afternoon (UTC 18-23), non-DC only
                # Data: 19W/7L, avg win $2.91 vs avg loss $46.87 — need forward data
                if (_pt in (None, "15m") and seconds_remaining < 120
                        and 18 <= _tod_hour <= 23
                        and strategy not in ("decided_t1", "decided_t1b", "decided_t2",
                                             "decided_t2_z25", "decided_t2_z2")):
                    _obs_dedup = (ticker, "usaft_short_stc")
                    if _obs_dedup not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_obs_dedup)
                        try:
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "usaft_short_stc",
                                rejection_reason=f"STC={seconds_remaining:.0f}s hour={_tod_hour} (observation only)",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge,
                                z_score=z_score, raw_prob=raw_prob,
                                fee_adjusted_edge=fee_adjusted_edge,
                                position_size=sizing.get("contracts"),
                                product_type=window.get("product_type"),
                                **_shadow_diag)
                        except Exception:
                            logging.debug("usaft_short_stc insert failed", exc_info=True)

                # Respect per-tick orderbook fetch cap
                if ob_fetches_this_tick >= MAX_OB_FETCHES_PER_TICK:
                    break
            # Per-window timing — captures iterations that reach the
            # natural end (slow iterations doing orderbook fetch +
            # filter checks + maybe candidate stage). Fast `continue`
            # exits are skipped, but those are sub-ms anyway.
            _window_dt = time.perf_counter() - _window_start
            if _window_dt > 0.5:
                logging.warning(
                    "SCAN_WINDOW_SLOW: asset=%s ticker=%s took %.2fs",
                    asset, window.get("event_ticker", "?"), _window_dt)
            if ob_fetches_this_tick >= MAX_OB_FETCHES_PER_TICK:
                break

        _scan_loop_dt = time.perf_counter() - _scan_loop_start
        if _scan_loop_dt > 1.5:
            logging.warning(
                "SCAN_LOOP_SLOW: per-window for-loop took %.2fs "
                "(eligible_windows=%d)",
                _scan_loop_dt, len(eligible_windows))

        # Post-loop section timing — `SCAN_POSTLOOP_SLOW` fires when
        # the work AFTER the per-window loop (shadow processors +
        # candidate selection) exceeds 1.5s. Logged via nested helper
        # so each of the 3 post-loop returns can call it.
        _scan_postloop_start = time.perf_counter()

        def _log_postloop_dt():
            _dt = time.perf_counter() - _scan_postloop_start
            if _dt > 1.5:
                logging.warning(
                    "SCAN_POSTLOOP_SLOW: post-loop processing took "
                    "%.2fs", _dt)

        if PRICE_SHADOW_ENABLED and _price_shadow_queue:
            self._process_price_shadow(_price_shadow_queue)

        # NO-side shadow evaluation for all queued markets
        if _no_side_queue:
            self._process_no_side_shadow(_no_side_queue, candidates)

        # Overnight LP shadow evaluation for 50-85c contracts during overnight hours
        if _overnight_lp_queue:
            self._process_overnight_lp_shadow(_overnight_lp_queue)

        # Low-price shadow: dual-sizing sim for 20-79c expansion analysis
        if LOW_PRICE_SHADOW_ENABLED and _low_price_shadow_queue:
            self._process_low_price_shadow(_low_price_shadow_queue)

        if not candidates:
            self._last_scan_stats = scan_stats
            _log_postloop_dt()
            return None

        # ── Separate overlay candidates (bypass single-asset filter) ──
        _dc_candidates = [c for c in candidates if c.get("strategy", "").startswith("decided_")]
        _tm_candidates = [c for c in candidates if c.get("strategy", "").startswith("terminal_momentum")]
        _bn_candidates = [c for c in candidates if c.get("strategy") == "bracket_no"]
        _lpne_candidates = [c for c in candidates if c.get("strategy") == "low_price_near_expiry"]
        _dc_tickers = {c["ticker"] for c in _dc_candidates}
        _tm_tickers = {c["ticker"] for c in _tm_candidates}
        # Remove weekend/overnight discount candidates that overlap with DC (DC takes priority)
        _main_candidates = [c for c in candidates
                            if not c.get("strategy", "").startswith("decided_")
                            and not c.get("strategy", "").startswith("terminal_momentum")
                            and c.get("strategy") not in ("bracket_no", "low_price_near_expiry")
                            and not (c.get("strategy") in ("weekend_discount", "overnight_discount") and c["ticker"] in (_dc_tickers | _tm_tickers))]
        candidates = _main_candidates  # single-asset filter only applies to main pipeline

        # ── Single-asset-per-timeslot: pick highest edge per 15-min window ──
        if ONE_ASSET_PER_WINDOW:
            # Group candidates by timeslot (shared across assets)
            by_timeslot: Dict[str, List[Dict]] = {}
            for c in candidates:
                ts = self._window_timeslot(c["event_ticker"])
                by_timeslot.setdefault(ts, []).append(c)

            # Keep only the single best-edge candidate per timeslot
            filtered: List[Dict] = []
            for ts, slot_candidates in by_timeslot.items():
                slot_candidates.sort(key=lambda c: c["edge"], reverse=True)
                winner = slot_candidates[0]
                filtered.append(winner)
                self._session_asset_perf[winner["asset"]]["times_selected"] += 1

                # Log which assets were rejected in favor of the winner
                if len(slot_candidates) > 1:
                    for c in slot_candidates[1:]:
                        self._session_asset_perf[c["asset"]]["times_rejected"] += 1
                    rejected = [
                        {"asset": c["asset"], "ticker": c["ticker"],
                         "edge": round(c["edge"], 6), "calibrated_prob": c["calibrated_prob"]}
                        for c in slot_candidates[1:]
                    ]
                    self._logger.log_scan({
                        "type": "single_asset_selection",
                        "timeslot": ts,
                        "chosen_asset": winner["asset"],
                        "chosen_ticker": winner["ticker"],
                        "chosen_edge": round(winner["edge"], 6),
                        "rejected_assets": rejected,
                        "reason": "single best asset per window (correlation-adjusted)",
                    })
                    for c in slot_candidates[1:]:
                        try:
                            self._logger.log_opportunity({
                                "filter_stage": "single_asset_selection",
                                "ticker": c["ticker"],
                                "event_ticker": c["event_ticker"],
                                "asset": c["asset"],
                                "rejection_reason": f"lost to {winner['asset']} (edge {winner['edge']:.4f} vs {c['edge']:.4f})",
                                "spot_price": c["spot"],
                                "threshold": c["threshold"],
                                "volatility": c["blended_rv"],
                                "market_price": c["best_yes_ask"],
                                "seconds_to_close": c["seconds_to_close"],
                                "calibrated_prob": c["calibrated_prob"],
                                "edge": c["edge"],
                                "ofa_adjustment": c.get("ofa_adjustment"),
                                "raw_prob": round(c["raw_prob"], 6) if c.get("raw_prob") is not None else None,
                                "old_system_prob": c.get("old_system_prob"),
                                "egarch_sigma": c.get("egarch_sigma"),
                                "egarch_blend_sigma": c.get("egarch_blend_sigma"),
                                "egarch_blend_weight": c.get("egarch_blend_weight"),
                                "mz_r_squared": c.get("mz_r_squared"),
                                "shadow_tv_blend_rv": c.get("shadow_tv_blend_rv"),
                                "mz_sigmoid_blend_rv": c.get("mz_sigmoid_blend_rv"),
                                "mz_sigmoid_improvement": c.get("mz_sigmoid_improvement"),
                                "shadow_tv_weights": c.get("shadow_tv_weights"),
                                "egarch_n_updates": c.get("egarch_n_updates"),
                                "egarch_ratio_clamped": c.get("egarch_ratio_clamped"),
                                "counterfactual": c.get("counterfactual_json"),
                            })
                            _dedup_key = (c["ticker"], "single_asset_selection")
                            if _dedup_key not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_dedup_key)
                                _ba = c["best_yes_ask"]
                                _cp = c["calibrated_prob"]
                                _fee1 = calculate_taker_fee(1, _ba)
                                _ev = (_cp * (100 - _ba)) - ((1 - _cp) * _ba) - _fee1
                                self._state.insert_evaluated_opportunity(
                                    c["ticker"], c["event_ticker"], c["asset"],
                                    "single_asset_selection",
                                    rejection_reason=f"lost to {winner['asset']}",
                                    spot_price=c["spot"], threshold=c["threshold"],
                                    volatility=c["blended_rv"], market_price=_ba,
                                    seconds_to_close=c["seconds_to_close"],
                                    calibrated_prob=_cp, edge=c["edge"],
                                    ofa_adjustment=c.get("ofa_adjustment"),
                                    strategy=c.get("strategy"),
                                    z_score=c.get("z_score"),
                                    vol_regime=c.get("vol_regime"),
                                    calibrated_prob_raw=c.get("calibrated_prob_raw"),
                                    kelly_f=c.get("kelly_f"),
                                    position_size=c.get("position_size"),
                                    breakeven_wr=_ba / 100.0,
                                    expected_value=round(_ev, 2),
                                    drawdown_scaler=c.get("drawdown_scaler"),
                                    ask_depth=c.get("ob_snapshot", {}).get("ask_depth"),
                                    best_ask_source=c.get("best_ask_source"),
                                    ofa_confidence=c.get("ofa_confidence"),
                                    raw_prob=c.get("raw_prob"),
                                    calibration_method=c.get("calibration_method"),
                                    old_system_prob=c.get("old_system_prob"),
                                    fee_adjusted_edge=c.get("fee_adjusted_edge"),
                                    egarch_sigma=c.get("egarch_sigma"),
                                    egarch_blend_sigma=c.get("egarch_blend_sigma"),
                                    egarch_blend_weight=c.get("egarch_blend_weight"),
                                    mz_r_squared=c.get("mz_r_squared"),
                                    shadow_tv_blend_rv=c.get("shadow_tv_blend_rv"),
                                    mz_shadow_sigmoid_w=c.get("mz_shadow_sigmoid_w"),
                                    mz_baseline_qlike=c.get("mz_baseline_qlike"),
                                    mz_qlike=c.get("mz_qlike"),
                                    counterfactual=c.get("counterfactual_json"),
                                    shadow_cal_prob=c.get("shadow_cal_prob"),
                                    shadow_cal_fee_edge=c.get("shadow_cal_fee_edge"),
                                    shadow_cal_temperature=c.get("shadow_cal_temperature"),
                                    oft_prob_adjustment=c.get("oft_prob_adjustment"),
                                    oft_imbalance_ratio=c.get("oft_imbalance_ratio"),
                                    oft_n_snapshots=c.get("oft_n_snapshots"),
                                    hourly_pre_temp_prob=c.get("hourly_pre_temp_prob"),
                                    hourly_applied_temp_t=c.get("hourly_applied_temp_t"),
                                    hourly_shadow_temp_2_0=c.get("hourly_shadow_temp_2_0"),
                                    hourly_shadow_temp_1_0=c.get("hourly_shadow_temp_1_0"),
                                    hourly_shadow_temp_2_5=c.get("hourly_shadow_temp_2_5"),
                                    hourly_shadow_blend_50=c.get("hourly_shadow_blend_50"),
                                    hourly_shadow_temp_1_75=c.get("hourly_shadow_temp_1_75"),
                                    hourly_shadow_temp_3_0=c.get("hourly_shadow_temp_3_0"),
                                    hourly_shadow_blend_20=c.get("hourly_shadow_blend_20"),
                                    hourly_shadow_blend_30=c.get("hourly_shadow_blend_30"),
                                    hourly_shadow_blend_60=c.get("hourly_shadow_blend_60"),
                                    hourly_post_temp_prob=c.get("hourly_post_temp_prob"))
                        except Exception:
                            logging.warning("single_asset_selection insert failed for %s", c.get("ticker"), exc_info=True)
        else:
            filtered = candidates

        # ── Per-asset selection: best strike per asset for hourly ──
        hourly_cands = [c for c in filtered if c.get("product_type") == "hourly"]
        fifteenm_cands = [c for c in filtered if c.get("product_type") != "hourly"]

        selected: List[Dict] = []

        # Hourly: best edge per asset (up to 4 simultaneous)
        hourly_by_asset: Dict[str, List[Dict]] = {}
        for c in hourly_cands:
            hourly_by_asset.setdefault(c["asset"], []).append(c)
        for asset_key, asset_cands in hourly_by_asset.items():
            selected.append(max(asset_cands, key=lambda c: c["edge"]))

        # 15M: single global best (existing behavior)
        if fifteenm_cands:
            selected.append(max(fifteenm_cands, key=lambda c: c["edge"]))

        # Decided contract overlay: add all DC candidates (already window-capped in scan)
        # Priority by payoff: lower price = higher payoff, so sort ascending by price
        _dc_candidates.sort(key=lambda c: c["best_yes_ask"])
        selected.extend(_dc_candidates)

        # Terminal momentum overlay: add all TM candidates (bypass single-asset filter)
        selected.extend(_tm_candidates)

        # Bracket NO overlay: add all bracket NO candidates
        selected.extend(_bn_candidates)

        # LPNE overlay: add all low-price near-expiry candidates
        selected.extend(_lpne_candidates)

        # ── 96¢ × {SOL,XRP} × 2-5min STC danger-band filter (strategy-aware) ──
        # Strips bleeder-strategy candidates from the cell while preserving
        # profitable strategies (TM-96, TAKER_NOW, decided_t1*, etc.).
        # See kb/decisions/96c-sol-xrp-2to5min-block-2026-04-26.md
        if HIGH_PRICE_STC_BLOCK_ENABLED:
            _hpsb_kept: List[Dict] = []
            _hpsb_dropped: List[Dict] = []
            for _cand in selected:
                _cand_pt = _cand.get("product_type")
                # Side convention (verified 2026-04-26 by grep):
                #   - NO-side candidates ALWAYS set "side": "no" explicitly
                #     (lines 13964, 15867, 15960 — scan_no_side path).
                #   - YES-side candidates may omit "side" or set it to "yes" explicitly
                #     (DC, TM overlays in scan() omit it; other paths set it).
                # Therefore default="yes" is correct for missing-key candidates.
                # If a future NO-side path forgets to set side="no", the gate would
                # incorrectly fire — invariant guarded by the convention test below.
                _cand_strat = _cand.get("strategy")
                _cand_side = _cand.get("side", "yes")
                if (_cand_pt in (None, "15m")
                        and should_block_high_price_stc_candidate(
                            asset=_cand.get("asset"),
                            side=_cand_side,
                            entry_price_cents=_cand.get("best_yes_ask"),
                            seconds_to_close=_cand.get("seconds_to_close"),
                            strategy=_cand_strat)):
                    _hpsb_dropped.append(_cand)
                else:
                    _hpsb_kept.append(_cand)
            if _hpsb_dropped:
                selected = _hpsb_kept
                for _drop in _hpsb_dropped:
                    _drop_strat = _drop.get("strategy")
                    _drop_asset = _drop.get("asset")
                    _drop_ticker = _drop.get("ticker")
                    _drop_stc_raw = _drop.get("seconds_to_close")
                    _drop_stc = int(_drop_stc_raw) if _drop_stc_raw is not None else None
                    _drop_price = _drop.get("best_yes_ask")
                    _drop_side = _drop.get("side") or "yes"
                    _drop_reason = (
                        f"96c {_drop_asset} {_drop_side.upper()} {_drop_strat} blocked: "
                        f"stc={_drop_stc}s in danger band (calibrator over-confident, see KB)")
                    logging.info("HPSB_DROP: %s strat=%s asset=%s stc=%s",
                                 _drop_ticker, _drop_strat, _drop_asset, _drop_stc)
                    # setdefault guards against scan_stats not being pre-populated
                    # for an unexpected asset key (defense for adversarial review A2)
                    _stats_bucket = scan_stats.setdefault(_drop_asset, {})
                    _stats_bucket[HIGH_PRICE_STC_BLOCK_FILTER_STAGE] = \
                        _stats_bucket.get(HIGH_PRICE_STC_BLOCK_FILTER_STAGE, 0) + 1
                    # Breakeven WR only meaningful for YES-side fills; for NO-side it
                    # would be (1 - price/100). Compute conditionally.
                    _bewr = (_drop_price / 100.0) if (_drop_side == "yes" and _drop_price) else None
                    _hpsb_dedup = (_drop_ticker, HIGH_PRICE_STC_BLOCK_FILTER_STAGE)
                    if _hpsb_dedup not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_hpsb_dedup)
                        try:
                            self._state.insert_evaluated_opportunity(
                                _drop_ticker, _drop.get("event_ticker"), _drop_asset,
                                HIGH_PRICE_STC_BLOCK_FILTER_STAGE,
                                rejection_reason=_drop_reason,
                                spot_price=_drop.get("spot"),
                                threshold=_drop.get("threshold"),
                                volatility=_drop.get("blended_rv"),
                                market_price=_drop_price,
                                seconds_to_close=_drop_stc_raw,
                                calibrated_prob=_drop.get("calibrated_prob"),
                                edge=_drop.get("edge"),
                                ofa_adjustment=_drop.get("ofa_adjustment"),
                                strategy=_drop_strat,
                                z_score=_drop.get("z_score"),
                                vol_regime=_drop.get("vol_regime"),
                                calibrated_prob_raw=_drop.get("calibrated_prob_raw"),
                                kelly_f=_drop.get("kelly_f"),
                                position_size=_drop.get("position_size"),
                                breakeven_wr=_bewr,
                                ask_depth=_drop.get("ob_snapshot", {}).get("ask_depth"),
                                best_ask_source=_drop.get("best_ask_source"),
                                raw_prob=_drop.get("raw_prob"),
                                calibration_method=_drop.get("calibration_method"),
                                fee_adjusted_edge=_drop.get("fee_adjusted_edge"),
                                product_type=_drop.get("product_type"),
                                cal_mlp_request_id=_drop.get("cal_mlp_request_id"),
                                cal_mlp_skipped_reason=_drop.get("cal_mlp_skipped_reason"),
                                cal_mlp_p_mean=_drop.get("cal_mlp_p_mean"),
                                cal_mlp_p_std=_drop.get("cal_mlp_p_std"),
                                cal_mlp_final_lo=_drop.get("cal_mlp_final_lo"),
                                cal_mlp_final_hi=_drop.get("cal_mlp_final_hi"),
                                cal_mlp_train_id=_drop.get("cal_mlp_train_id"))
                        except Exception:
                            logging.warning(
                                "insert_evaluated_opportunity failed (%s)",
                                HIGH_PRICE_STC_BLOCK_FILTER_STAGE, exc_info=True)
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": HIGH_PRICE_STC_BLOCK_FILTER_STAGE,
                            "ticker": _drop_ticker,
                            "asset": _drop_asset,
                            "strategy": _drop_strat,
                            "side": _drop_side,
                            "rejection_reason": _drop_reason,
                            "market_price": _drop_price,
                            "seconds_to_close": _drop_stc,
                            "calibrated_prob": _drop.get("calibrated_prob"),
                            "edge": _drop.get("edge"),
                            "fee_adjusted_edge": _drop.get("fee_adjusted_edge"),
                            "position_size": _drop.get("position_size"),
                        })
                    except Exception:
                        logging.debug("high_price_stc_band log failed", exc_info=True)

        # ── R-bleed-1: TM98 high-price + SOL TAKER low-price bleed cells ──
        # Strategy-aware blocks targeting two cells identified in 7d
        # post-WS-fix data as catastrophic-tail dominators. Each cell has
        # its own env flag so operators can roll back independently.
        # Blocked candidates STILL get a shadow row written for v2/v3
        # training data continuity.
        for _bleed_predicate, _bleed_stage, _bleed_log_tag in (
            (should_block_tm98_highprice_bleed_candidate,
             TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE, "TM98_BLEED_DROP"),
            (should_block_sol_taker_lowprice_bleed_candidate,
             SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE, "SOL_TAKER_BLEED_DROP"),
            (should_block_sol_bleed_v2_candidate,
             SOL_BLEED_V2_BLOCK_FILTER_STAGE, "SOL_BLEED_V2_DROP"),
        ):
            _bleed_kept: List[Dict] = []
            _bleed_dropped: List[Dict] = []
            for _cand in selected:
                _cand_pt = _cand.get("product_type")
                _cand_strat = _cand.get("strategy")
                _cand_side = _cand.get("side", "yes")
                if (_cand_pt in (None, "15m")
                        and _bleed_predicate(
                            asset=_cand.get("asset"),
                            side=_cand_side,
                            entry_price_cents=_cand.get("best_yes_ask"),
                            seconds_to_close=_cand.get("seconds_to_close"),
                            strategy=_cand_strat)):
                    _bleed_dropped.append(_cand)
                else:
                    _bleed_kept.append(_cand)
            if _bleed_dropped:
                selected = _bleed_kept
                for _drop in _bleed_dropped:
                    _drop_strat = _drop.get("strategy")
                    _drop_asset = _drop.get("asset")
                    _drop_ticker = _drop.get("ticker")
                    _drop_stc_raw = _drop.get("seconds_to_close")
                    _drop_stc = int(_drop_stc_raw) if _drop_stc_raw is not None else None
                    _drop_price = _drop.get("best_yes_ask")
                    _drop_side = _drop.get("side") or "yes"
                    _drop_reason = (
                        f"{_bleed_stage}: {_drop_asset} {_drop_side.upper()} "
                        f"{_drop_strat} @{_drop_price}c stc={_drop_stc}s "
                        f"(7d data: catastrophic-tail cell, see "
                        f"kb/decisions/bleed-cell-blocks-2026-04-30.md)")
                    logging.info(
                        "%s: %s strat=%s asset=%s stc=%s price=%s",
                        _bleed_log_tag, _drop_ticker, _drop_strat,
                        _drop_asset, _drop_stc, _drop_price)
                    _stats_bucket = scan_stats.setdefault(_drop_asset, {})
                    _stats_bucket[_bleed_stage] = _stats_bucket.get(_bleed_stage, 0) + 1
                    _bewr = (_drop_price / 100.0) if (_drop_side == "yes" and _drop_price) else None
                    _bleed_dedup = (_drop_ticker, _bleed_stage)
                    if _bleed_dedup not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_bleed_dedup)
                        try:
                            self._state.insert_evaluated_opportunity(
                                _drop_ticker, _drop.get("event_ticker"), _drop_asset,
                                _bleed_stage,
                                rejection_reason=_drop_reason,
                                spot_price=_drop.get("spot"),
                                threshold=_drop.get("threshold"),
                                volatility=_drop.get("blended_rv"),
                                market_price=_drop_price,
                                seconds_to_close=_drop_stc_raw,
                                calibrated_prob=_drop.get("calibrated_prob"),
                                edge=_drop.get("edge"),
                                ofa_adjustment=_drop.get("ofa_adjustment"),
                                strategy=_drop_strat,
                                z_score=_drop.get("z_score"),
                                vol_regime=_drop.get("vol_regime"),
                                calibrated_prob_raw=_drop.get("calibrated_prob_raw"),
                                kelly_f=_drop.get("kelly_f"),
                                position_size=_drop.get("position_size"),
                                breakeven_wr=_bewr,
                                ask_depth=_drop.get("ob_snapshot", {}).get("ask_depth"),
                                best_ask_source=_drop.get("best_ask_source"),
                                raw_prob=_drop.get("raw_prob"),
                                calibration_method=_drop.get("calibration_method"),
                                fee_adjusted_edge=_drop.get("fee_adjusted_edge"),
                                product_type=_drop.get("product_type"),
                                cal_mlp_request_id=_drop.get("cal_mlp_request_id"),
                                cal_mlp_skipped_reason=_drop.get("cal_mlp_skipped_reason"),
                                cal_mlp_p_mean=_drop.get("cal_mlp_p_mean"),
                                cal_mlp_p_std=_drop.get("cal_mlp_p_std"),
                                cal_mlp_final_lo=_drop.get("cal_mlp_final_lo"),
                                cal_mlp_final_hi=_drop.get("cal_mlp_final_hi"),
                                cal_mlp_train_id=_drop.get("cal_mlp_train_id"))
                        except Exception:
                            logging.warning(
                                "insert_evaluated_opportunity failed (%s)",
                                _bleed_stage, exc_info=True)

        if not selected:
            self._last_scan_stats = scan_stats
            _log_postloop_dt()
            return None

        # Log top pick for scan journal
        best = max(selected, key=lambda c: c["edge"])
        self._logger.log_scan({
            "type": "opportunity",
            "candidates_evaluated": len(candidates),
            "candidates_after_single_asset": len(filtered),
            "selected_count": len(selected),
            "chosen_strategy": best.get("strategy"),
            **{k: v for k, v in best.items() if k not in ("strategy_scores", "ob_snapshot")},
        })
        self._last_scan_stats = scan_stats
        _log_postloop_dt()
        return selected

    # ── Price shadow processor ───────────────────────────────────────────
    def _process_price_shadow(self, queue: list) -> None:
        """Shadow-evaluate 70-85c POR rejections to collect edge data.

        Runs AFTER both scan loops complete. Entire body in try/except —
        a crash here cannot affect candidate selection or trading.
        """
        try:
            for item in queue:
                ticker = item["ticker"]
                best_ask = item["best_ask"]
                spot = item["spot"]
                threshold = item["threshold"]
                blended_rv = item["blended_rv"]
                stc = item["seconds_remaining"]
                asset = item["asset"]
                _pt = item["product_type"]

                # Re-run probability with market price (z-score sanity check)
                prob_with_market = ProbabilityEngine.compute(
                    spot, threshold, stc, blended_rv,
                    market_price_cents=best_ask,
                    asset=asset, product_type=_pt,
                )
                if not prob_with_market.get("tradeable"):
                    continue

                final_prob = prob_with_market["calibrated_prob"]
                raw_prob = prob_with_market.get("raw_prob")
                calibration_method = prob_with_market.get("calibration_method")

                # Temperature scaling (Layer 1)
                _hourly_pre_temp_prob = None
                _tempcfg = get_market_config(_pt)
                _temp_t = _tempcfg.temperature_t if _tempcfg.temperature_enabled else None
                _configured_temp_t = _temp_t  # record configured T for instrumentation (before CalEngine override)
                if _temp_t is not None and _temp_t == 1.0:
                    _temp_t = None
                _reg_engine_t = _cal_state._resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine_t is not None and _reg_engine_t.is_learned_method_active():
                    _temp_t = None
                    _hourly_pre_temp_prob = final_prob
                if _temp_t is not None:
                    _hourly_pre_temp_prob = final_prob
                    _p = max(0.001, min(0.999, final_prob))
                    _z = math.log(_p / (1.0 - _p))
                    final_prob = 1.0 / (1.0 + math.exp(-_z / _temp_t))

                # Dynamic cap / learned ceiling
                _dyn_cap = ProbabilityEngine._dynamic_cap(stc, product_type=_pt)
                _reg_engine_c = _cal_state._resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine_c is not None and _reg_engine_c.is_learned_method_active():
                    final_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, final_prob))
                else:
                    final_prob = max(0.01, min(_dyn_cap, final_prob))

                # ── Shadow instrumentation ──
                _hourly_shadow_temp_2_0 = None
                _hourly_shadow_temp_1_0 = None
                _hourly_shadow_temp_2_5 = None
                _hourly_shadow_temp_1_75 = None
                _hourly_shadow_temp_3_0 = None
                _hourly_shadow_blend_50 = None
                _hourly_shadow_blend_20 = None
                _hourly_shadow_blend_30 = None
                _hourly_shadow_blend_60 = None
                _hourly_post_temp_prob = None

                _shadow_base_ps = _hourly_pre_temp_prob
                if _shadow_base_ps is None and _pt == "spx_hourly":
                    _shadow_base_ps = prob_with_market["calibrated_prob"]
                    _hourly_pre_temp_prob = _shadow_base_ps
                    _temp_t = 1.0

                if _shadow_base_ps is not None:
                    _sp = max(0.001, min(0.999, _shadow_base_ps))
                    _sz = math.log(_sp / (1.0 - _sp))
                    _hourly_shadow_temp_2_0 = 1.0 / (1.0 + math.exp(-_sz / 2.0))
                    _hourly_shadow_temp_2_5 = 1.0 / (1.0 + math.exp(-_sz / 2.5))
                    _hourly_shadow_temp_1_75 = 1.0 / (1.0 + math.exp(-_sz / 1.75))
                    _hourly_shadow_temp_3_0 = 1.0 / (1.0 + math.exp(-_sz / 3.0))
                    if _pt == "spx_hourly":
                        _hourly_shadow_temp_1_0 = 1.0 / (1.0 + math.exp(-_sz / 1.5))
                    else:
                        _hourly_shadow_temp_1_0 = _shadow_base_ps
                    _hourly_post_temp_prob = final_prob
                    # 70-85c always < ENDGAME_BLEND_PRICE
                    _mkt_p = best_ask / 100.0
                    _hourly_shadow_blend_20 = 0.80 * final_prob + 0.20 * _mkt_p
                    _hourly_shadow_blend_30 = 0.70 * final_prob + 0.30 * _mkt_p
                    _hourly_shadow_blend_50 = 0.50 * final_prob + 0.50 * _mkt_p
                    _hourly_shadow_blend_60 = 0.40 * final_prob + 0.60 * _mkt_p

                # Market blend (70-85c always < ENDGAME_BLEND_PRICE)
                _mcfg = get_market_config(_pt)
                _effective_blend_w = _mcfg.market_blend_w
                market_implied_prob = best_ask / 100.0
                final_prob = (1.0 - _effective_blend_w) * final_prob + _effective_blend_w * market_implied_prob

                edge = final_prob - best_ask / 100.0

                # Fee-adjusted edge
                est_fee_1c = calculate_fee(1, best_ask, is_taker=True,
                                           fee_mult_taker=_mcfg.fee_multiplier_taker,
                                           fee_mult_maker=_mcfg.fee_multiplier_maker)
                fee_adjusted_edge = edge - est_fee_1c / 100.0

                # Sizing + strategy for instrumentation (all in try/except — cannot break insert)
                _ps_kelly_f = None
                _ps_position = None
                _ps_drawdown = None
                _ps_strategy = None
                _ps_ev = None
                try:
                    _ps_ev = round(
                        (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c, 2)
                    _ps_balance = self._get_balance_cached()
                    if _ps_balance and _ps_balance > 0:
                        _ps_sizing = self._sizer.compute(final_prob, best_ask, _ps_balance)
                        _ps_kelly_f = _ps_sizing["kelly_f"]
                        _ps_position = _ps_sizing["contracts"]
                        _ps_drawdown = _ps_sizing["drawdown_scaler"]
                        # Apply product-type Kelly fraction + risk cap
                        _ps_scfg = get_market_config(_pt)
                        if _ps_scfg.kelly_fraction < 1.0:
                            _ps_position = max(1, int(_ps_position * _ps_scfg.kelly_fraction))
                        _ps_type_max = int((_ps_balance * _ps_scfg.max_risk_per_trade) / best_ask)
                        if _ps_position > _ps_type_max:
                            _ps_position = max(1, _ps_type_max)
                        _ps_strat_data = {
                            "z_score": prob_with_market.get("z_score"),
                            "calibrated_prob": final_prob,
                            "spot": spot, "threshold": threshold,
                            "seconds_to_close": stc, "blended_rv": blended_rv,
                            "vol_regime": item["vol_regime"],
                            "best_yes_ask": best_ask,
                            "best_ask_depth": item["ask_depth"],
                            "total_ob_depth": 0,
                            "convergence_velocity": 0,
                            "edge": edge,
                            "min_entry_price": _ps_scfg.min_entry_price,
                            "max_entry_price": _ps_scfg.max_entry_price,
                        }
                        _ps_strategy, _ = evaluate_execution_strategy(_ps_strat_data)
                except Exception:
                    logging.debug("price_shadow sizing/strategy failed", exc_info=True)

                # Dedup + DB insert
                _ps_stage = "price_shadow_no_xrp" if asset != "XRP" else "price_shadow_xrp"
                _dedup_key = (ticker, _ps_stage)
                if _dedup_key in self._eval_opp_seen:
                    continue
                self._eval_opp_seen.add(_dedup_key)
                self._state.insert_evaluated_opportunity(
                    ticker, item["event_ticker"], asset,
                    _ps_stage,
                    spot_price=spot, threshold=threshold,
                    volatility=blended_rv, market_price=best_ask,
                    seconds_to_close=stc,
                    calibrated_prob=final_prob,
                    edge=edge, fee_adjusted_edge=fee_adjusted_edge,
                    z_score=prob_with_market.get("z_score"),
                    vol_regime=item["vol_regime"],
                    breakeven_wr=best_ask / 100.0,
                    calibrated_prob_raw=prob_with_market["calibrated_prob"],
                    kelly_f=_ps_kelly_f,
                    position_size=_ps_position,
                    drawdown_scaler=_ps_drawdown,
                    strategy=_ps_strategy,
                    expected_value=_ps_ev,
                    raw_prob=raw_prob,
                    calibration_method=calibration_method,
                    ask_depth=item["ask_depth"],
                    best_ask_source=item["best_ask_source"],
                    product_type=_pt,
                    hourly_pre_temp_prob=_hourly_pre_temp_prob,
                    hourly_applied_temp_t=_configured_temp_t,
                    hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                    hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                    hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                    hourly_shadow_blend_50=_hourly_shadow_blend_50,
                    hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                    hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                    hourly_shadow_blend_20=_hourly_shadow_blend_20,
                    hourly_shadow_blend_30=_hourly_shadow_blend_30,
                    hourly_shadow_blend_60=_hourly_shadow_blend_60,
                    hourly_post_temp_prob=_hourly_post_temp_prob,
                    **item["_oft_db"], **item["_shadow_diag"])
        except Exception:
            logging.warning("price_shadow processing error", exc_info=True)

    def _process_overnight_lp_shadow(self, queue: list) -> None:
        """Shadow-evaluate 50-85c YES contracts during overnight hours (00-12 UTC).

        Thesis: overnight market makers are slow/absent, so cheap YES contracts
        have stale pricing. The model correctly predicts 90%+ probability on
        outcomes the market only quotes at 50-60c.

        Includes vol-spike circuit breaker (2x overnight median → skip asset)
        and dual execution simulation (taker at best_ask, maker at best_bid+1).

        Shadow-only — never places orders. Entire body in try/except so
        a crash here cannot affect candidate selection or live trading.
        """
        try:
            now_ts = time.time()
            _cutoff_ts = now_ts - OVERNIGHT_LP_VOL_HISTORY_DAYS * 86400

            for item in queue:
                ticker = item["ticker"]
                best_ask = item["best_ask"]
                best_bid = item.get("best_bid")
                spot = item["spot"]
                threshold = item["threshold"]
                blended_rv = item["blended_rv"]
                stc = item["seconds_remaining"]
                asset = item["asset"]
                _pt = item["product_type"]

                # ── Vol-spike circuit breaker ──
                # Prune entries older than OVERNIGHT_LP_VOL_HISTORY_DAYS
                while (self._overnight_rv_history[asset]
                       and self._overnight_rv_history[asset][0][0] < _cutoff_ts):
                    self._overnight_rv_history[asset].popleft()
                # Compute median from PRIOR history (before appending current value,
                # so the current observation doesn't bias the median toward itself)
                _rv_vals = [rv for _, rv in self._overnight_rv_history[asset]]
                # Record current blended_rv AFTER extracting prior history
                self._overnight_rv_history[asset].append((now_ts, blended_rv))
                # Need at least 100 prior observations (~8 min of 5s ticks) before
                # the breaker activates. On cold start / night one, the breaker is
                # disabled — we want data collection, not false blocks.
                _rv_median = None
                _vol_breaker_active = len(_rv_vals) >= 100
                if _vol_breaker_active:
                    _rv_sorted = sorted(_rv_vals)
                    _rv_median = _rv_sorted[len(_rv_sorted) // 2]
                if _vol_breaker_active and blended_rv > OVERNIGHT_LP_VOL_SPIKE_MULT * _rv_median:
                    self._overnight_lp_vol_skip_count += 1
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "overnight_lp_vol_skip",
                            "ticker": ticker,
                            "asset": asset,
                            "blended_rv": round(blended_rv, 8),
                            "overnight_median_rv": round(_rv_median, 8),
                            "spike_ratio": round(blended_rv / _rv_median, 4) if _rv_median > 0 else None,
                            "threshold_mult": OVERNIGHT_LP_VOL_SPIKE_MULT,
                            "seconds_to_close": round(stc, 1),
                            "market_price": best_ask,
                        })
                    except Exception:
                        pass
                    continue

                # Re-run probability engine with market price
                prob_result = ProbabilityEngine.compute(
                    spot, threshold, stc, blended_rv,
                    market_price_cents=best_ask,
                    asset=asset, product_type=_pt,
                )
                if not prob_result.get("tradeable"):
                    continue

                final_prob = prob_result["calibrated_prob"]
                raw_prob = prob_result.get("raw_prob")
                calibration_method = prob_result.get("calibration_method")
                z_score = prob_result.get("z_score")

                # Temperature scaling (same as main pipeline)
                _tempcfg = get_market_config(_pt)
                _temp_t = _tempcfg.temperature_t if _tempcfg.temperature_enabled else None
                if _temp_t is not None and _temp_t == 1.0:
                    _temp_t = None
                _reg_engine = _cal_state._resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine is not None and _reg_engine.is_learned_method_active():
                    _temp_t = None
                if _temp_t is not None:
                    _p = max(0.001, min(0.999, final_prob))
                    _z = math.log(_p / (1.0 - _p))
                    final_prob = 1.0 / (1.0 + math.exp(-_z / _temp_t))

                # Dynamic cap / learned ceiling
                _dyn_cap = ProbabilityEngine._dynamic_cap(stc, product_type=_pt)
                _reg_engine_c = _cal_state._resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine_c is not None and _reg_engine_c.is_learned_method_active():
                    final_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, final_prob))
                else:
                    final_prob = max(0.01, min(_dyn_cap, final_prob))

                # Market blend
                _mcfg = get_market_config(_pt)
                _effective_blend_w = _mcfg.market_blend_w
                market_implied_prob = best_ask / 100.0
                final_prob = (1.0 - _effective_blend_w) * final_prob + _effective_blend_w * market_implied_prob

                # ── Min cal_prob gate ──
                if final_prob < OVERNIGHT_LP_MIN_CAL_PROB:
                    continue

                # Edge computation
                edge = final_prob - best_ask / 100.0
                est_fee_1c = calculate_fee(1, best_ask, is_taker=True,
                                           fee_mult_taker=_mcfg.fee_multiplier_taker,
                                           fee_mult_maker=_mcfg.fee_multiplier_maker)
                fee_adjusted_edge = edge - est_fee_1c / 100.0

                # ── Min edge gate ──
                if fee_adjusted_edge < OVERNIGHT_LP_MIN_EDGE_PCT:
                    continue

                # ── Sizing (taker simulation — primary metric) ──
                _olp_kelly_f = None
                _olp_position_taker = None
                _olp_ev_taker = None
                _olp_drawdown = None
                _olp_balance = self._get_balance_cached()
                if _olp_balance and _olp_balance > 0:
                    _olp_sizing = self._sizer.compute(final_prob, best_ask, _olp_balance)
                    _olp_kelly_f = _olp_sizing["kelly_f"]
                    _olp_position_taker = _olp_sizing["contracts"]
                    _olp_drawdown = _olp_sizing["drawdown_scaler"]
                    # Apply overnight LP Kelly fraction + risk cap
                    if OVERNIGHT_LP_KELLY_FRACTION < 1.0:
                        _olp_position_taker = max(1, int(_olp_position_taker * OVERNIGHT_LP_KELLY_FRACTION))
                    _olp_type_max = int((_olp_balance * OVERNIGHT_LP_MAX_RISK_PER_TRADE) / best_ask)
                    if _olp_position_taker > _olp_type_max:
                        _olp_position_taker = max(1, _olp_type_max)
                    _olp_ev_taker = round(
                        (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c, 2)

                # ── Maker simulation ──
                # Post at best_bid + 1c (penny improvement)
                _olp_maker_price = None
                _olp_position_maker = None
                _olp_ev_maker = None
                _olp_maker_fee_adj_edge = None
                if best_bid is not None and best_bid > 0:
                    _olp_maker_price = best_bid + 1
                    if _olp_maker_price <= best_ask:  # sanity: maker must be below ask
                        _maker_edge = final_prob - _olp_maker_price / 100.0
                        _maker_fee_1c = calculate_fee(1, _olp_maker_price, is_taker=False,
                                                      fee_mult_taker=_mcfg.fee_multiplier_taker,
                                                      fee_mult_maker=_mcfg.fee_multiplier_maker)
                        _olp_maker_fee_adj_edge = _maker_edge - _maker_fee_1c / 100.0
                        if _olp_balance and _olp_balance > 0:
                            _m_sizing = self._sizer.compute(final_prob, _olp_maker_price, _olp_balance)
                            _olp_position_maker = _m_sizing["contracts"]
                            if OVERNIGHT_LP_KELLY_FRACTION < 1.0:
                                _olp_position_maker = max(1, int(_olp_position_maker * OVERNIGHT_LP_KELLY_FRACTION))
                            _m_type_max = int((_olp_balance * OVERNIGHT_LP_MAX_RISK_PER_TRADE) / _olp_maker_price)
                            if _olp_position_maker > _m_type_max:
                                _olp_position_maker = max(1, _m_type_max)
                            _olp_ev_maker = round(
                                (final_prob * (100 - _olp_maker_price)) -
                                ((1 - final_prob) * _olp_maker_price) - _maker_fee_1c, 2)
                    else:
                        _olp_maker_price = None  # bid+1 crossed the ask — no valid maker price

                # ── JSONL log ──
                try:
                    self._logger.log_opportunity({
                        "filter_stage": "overnight_lp_shadow",
                        "ticker": ticker,
                        "event_ticker": item["event_ticker"],
                        "asset": asset,
                        "side": "yes",
                        "market_price": best_ask,
                        "model_prob": round(final_prob, 6),
                        "edge": round(edge, 6),
                        "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                        "kelly_f": round(_olp_kelly_f, 6) if _olp_kelly_f else None,
                        "taker_position_size": _olp_position_taker,
                        "taker_ev": _olp_ev_taker,
                        "maker_price": _olp_maker_price,
                        "maker_position_size": _olp_position_maker,
                        "maker_ev": _olp_ev_maker,
                        "maker_fee_adj_edge": round(_olp_maker_fee_adj_edge, 6) if _olp_maker_fee_adj_edge else None,
                        "best_bid": best_bid,
                        "seconds_to_close": round(stc, 1),
                        "spot_price": spot,
                        "threshold": threshold,
                        "volatility": blended_rv,
                        "vol_regime": item["vol_regime"],
                        "overnight_median_rv": round(_rv_median, 8) if _rv_median is not None else None,
                        "vol_spike_ratio": round(blended_rv / _rv_median, 4) if _rv_median and _rv_median > 0 else None,
                        "vol_breaker_active": _vol_breaker_active,
                        "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                        "z_score": z_score,
                        "ask_depth": item["ask_depth"],
                    })
                except Exception:
                    logging.debug("overnight_lp_shadow log failed", exc_info=True)

                # ── DB insert (taker simulation as primary) ──
                _dedup_key = (ticker, "overnight_lp_shadow")
                if _dedup_key in self._eval_opp_seen:
                    continue
                self._eval_opp_seen.add(_dedup_key)
                try:
                    self._state.insert_evaluated_opportunity(
                        ticker, item["event_ticker"], asset,
                        "overnight_lp_shadow",
                        rejection_reason=(
                            f"shadow: cal_prob {final_prob:.4f} >= {OVERNIGHT_LP_MIN_CAL_PROB}, "
                            f"fee_adj_edge {fee_adjusted_edge:.4f} >= {OVERNIGHT_LP_MIN_EDGE_PCT}, "
                            f"taker@{best_ask}c maker@{_olp_maker_price}c"
                        ),
                        spot_price=spot, threshold=threshold,
                        volatility=blended_rv, market_price=best_ask,
                        seconds_to_close=stc,
                        calibrated_prob=final_prob, edge=edge,
                        fee_adjusted_edge=fee_adjusted_edge,
                        ofa_adjustment=None,
                        z_score=z_score,
                        vol_regime=item["vol_regime"],
                        calibrated_prob_raw=prob_result["calibrated_prob"],
                        kelly_f=_olp_kelly_f,
                        position_size=_olp_position_taker,
                        drawdown_scaler=_olp_drawdown,
                        breakeven_wr=best_ask / 100.0,
                        expected_value=_olp_ev_taker,
                        ask_depth=item["ask_depth"],
                        best_ask_source=item["best_ask_source"],
                        raw_prob=raw_prob,
                        calibration_method=calibration_method,
                        product_type=_pt,
                        **item["_oft_db"], **item["_shadow_diag"])
                except Exception:
                    logging.warning("insert_evaluated_opportunity failed (overnight_lp_shadow)", exc_info=True)
        except Exception:
            logging.warning("overnight_lp_shadow processing error", exc_info=True)

    def _process_low_price_shadow(self, queue: list) -> None:
        """Shadow-evaluate 20-79c 15M signals with dual sizing simulation.

        Collects data for potential MIN_ENTRY_PRICE expansion. Logs to both
        evaluated_opportunities (for settlement linking) and low_price_shadow_signals
        (for correlation tracking and capped-sizing counterfactuals).

        Two sizing simulations per signal:
        - Full Kelly: current sizer output (what would happen if we just lowered the floor)
        - Capped Kelly: LP_KELLY_FRACTION × LP_MAX_RISK_PER_TRADE (conservative alternative)

        Correlation tracking: per-window and per-hour signal counts to measure
        simultaneous low-price exposure.

        Shadow-only — never places orders. Entire body in try/except so
        a crash here cannot affect candidate selection or live trading.
        """
        try:
            for item in queue:
                ticker = item["ticker"]
                best_ask = item["best_ask"]
                spot = item["spot"]
                threshold = item["threshold"]
                blended_rv = item["blended_rv"]
                stc = item["seconds_remaining"]
                asset = item["asset"]
                _pt = item["product_type"]
                event_ticker = item["event_ticker"]

                # Dedup: skip if already processed for this ticker
                _dedup_key = (ticker, "low_price_shadow")
                if _dedup_key in self._eval_opp_seen:
                    continue
                self._eval_opp_seen.add(_dedup_key)

                if item.get("has_prob"):
                    # IE path: prob already computed
                    final_prob = item["final_prob"]
                    raw_prob = item["raw_prob"]
                    calibration_method = item["calibration_method"]
                    z_score = item["z_score"]
                    edge = item["edge"]
                    fee_adjusted_edge = item["fee_adjusted_edge"]
                    est_fee_1c = item["est_fee_1c"]
                    calibrated_prob_raw = item["calibrated_prob_raw"]
                else:
                    # POR path: need to compute probability
                    prob_result = ProbabilityEngine.compute(
                        spot, threshold, stc, blended_rv,
                        market_price_cents=best_ask,
                        asset=asset, product_type=_pt,
                    )
                    if not prob_result.get("tradeable"):
                        continue
                    final_prob = prob_result["calibrated_prob"]
                    raw_prob = prob_result.get("raw_prob")
                    calibration_method = prob_result.get("calibration_method")
                    z_score = prob_result.get("z_score")
                    calibrated_prob_raw = final_prob

                    # Temperature scaling
                    _tempcfg = get_market_config(_pt)
                    _temp_t = _tempcfg.temperature_t if _tempcfg.temperature_enabled else None
                    if _temp_t is not None and _temp_t == 1.0:
                        _temp_t = None
                    _reg_engine = _cal_state._resolve_cal_engine(_pt, asset, require_enabled=True)
                    if _reg_engine is not None and _reg_engine.is_learned_method_active():
                        _temp_t = None
                    if _temp_t is not None:
                        _p = max(0.001, min(0.999, final_prob))
                        _z = math.log(_p / (1.0 - _p))
                        final_prob = 1.0 / (1.0 + math.exp(-_z / _temp_t))

                    # Dynamic cap / learned ceiling
                    _dyn_cap = ProbabilityEngine._dynamic_cap(stc, product_type=_pt)
                    _reg_engine_c = _cal_state._resolve_cal_engine(_pt, asset, require_enabled=True)
                    if _reg_engine_c is not None and _reg_engine_c.is_learned_method_active():
                        final_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, final_prob))
                    else:
                        final_prob = max(0.01, min(_dyn_cap, final_prob))

                    # Market blend
                    _mcfg = get_market_config(_pt)
                    _effective_blend_w = _mcfg.market_blend_w
                    market_implied_prob = best_ask / 100.0
                    final_prob = (1.0 - _effective_blend_w) * final_prob + _effective_blend_w * market_implied_prob

                    # Edge computation
                    edge = final_prob - best_ask / 100.0
                    est_fee_1c = calculate_fee(1, best_ask, is_taker=True,
                                               fee_mult_taker=_mcfg.fee_multiplier_taker,
                                               fee_mult_maker=_mcfg.fee_multiplier_maker)
                    fee_adjusted_edge = edge - est_fee_1c / 100.0

                # ── Full Kelly sizing (what would happen if we just lowered the floor) ──
                _full_kelly_f = None
                _full_position = None
                _full_drawdown = None
                _lp_balance = self._get_balance_cached()
                if _lp_balance and _lp_balance > 0:
                    _full_sizing = self._sizer.compute(final_prob, best_ask, _lp_balance)
                    _full_kelly_f = _full_sizing["kelly_f"]
                    _full_position = _full_sizing["contracts"]
                    _full_drawdown = _full_sizing["drawdown_scaler"]
                    # Apply standard 15M Kelly fraction + risk cap
                    _scfg = get_market_config(_pt)
                    if _scfg.kelly_fraction < 1.0:
                        _full_position = max(1, int(_full_position * _scfg.kelly_fraction))
                    _type_max = int((_lp_balance * _scfg.max_risk_per_trade) / best_ask)
                    if _full_position > _type_max:
                        _full_position = max(1, _type_max)

                # ── Capped Kelly sizing (conservative alternative) ──
                _capped_kelly_f = None
                _capped_position = None
                if _lp_balance and _lp_balance > 0:
                    _cap_sizing = self._sizer.compute(final_prob, best_ask, _lp_balance)
                    _capped_kelly_f = _cap_sizing["kelly_f"]
                    _capped_position = _cap_sizing["contracts"]
                    # Apply LP-specific caps
                    if LP_KELLY_FRACTION < 1.0:
                        _capped_position = max(1, int(_capped_position * LP_KELLY_FRACTION))
                    _cap_type_max = int((_lp_balance * LP_MAX_RISK_PER_TRADE) / best_ask)
                    if _capped_position > _cap_type_max:
                        _capped_position = max(1, _cap_type_max)

                # ── Correlation tracking ──
                _window_count = self._lp_window_counts.get(event_ticker, 0) + 1
                self._lp_window_counts[event_ticker] = _window_count
                _utc_hour = datetime.datetime.now(timezone.utc).strftime("%H")
                _hour_count = self._lp_hour_signals.get(_utc_hour, 0) + 1
                self._lp_hour_signals[_utc_hour] = _hour_count

                # ── JSONL log ──
                try:
                    self._logger.log_opportunity({
                        "filter_stage": "low_price_shadow",
                        "ticker": ticker,
                        "event_ticker": event_ticker,
                        "asset": asset,
                        "market_price": best_ask,
                        "model_prob": round(final_prob, 6),
                        "edge": round(edge, 6),
                        "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                        "full_kelly_f": round(_full_kelly_f, 6) if _full_kelly_f else None,
                        "full_contracts": _full_position,
                        "capped_kelly_f": round(_capped_kelly_f, 6) if _capped_kelly_f else None,
                        "capped_contracts": _capped_position,
                        "window_signal_count": _window_count,
                        "hour_signal_count": _hour_count,
                        "seconds_to_close": round(stc, 1),
                        "spot_price": spot,
                        "threshold": threshold,
                        "volatility": blended_rv,
                        "vol_regime": item["vol_regime"],
                        "z_score": z_score,
                    })
                except Exception:
                    logging.debug("low_price_shadow log failed", exc_info=True)

                # ── DB insert to evaluated_opportunities (for settlement linking) ──
                try:
                    self._state.insert_evaluated_opportunity(
                        ticker, event_ticker, asset,
                        "low_price_shadow",
                        rejection_reason=(
                            "shadow: 20-79c dual-sizing sim, "
                            "full={} capped={} w_ct={} h_ct={}".format(
                                _full_position, _capped_position,
                                _window_count, _hour_count)
                        ),
                        spot_price=spot, threshold=threshold,
                        volatility=blended_rv, market_price=best_ask,
                        seconds_to_close=stc,
                        calibrated_prob=final_prob, edge=edge,
                        fee_adjusted_edge=fee_adjusted_edge,
                        ofa_adjustment=None,
                        z_score=z_score,
                        vol_regime=item["vol_regime"],
                        calibrated_prob_raw=calibrated_prob_raw,
                        kelly_f=_full_kelly_f,
                        position_size=_full_position,
                        drawdown_scaler=_full_drawdown,
                        breakeven_wr=best_ask / 100.0,
                        expected_value=round(
                            (final_prob * (100 - best_ask)) -
                            ((1 - final_prob) * best_ask) - est_fee_1c, 2),
                        ask_depth=item["ask_depth"],
                        best_ask_source=item["best_ask_source"],
                        raw_prob=raw_prob,
                        calibration_method=calibration_method,
                        product_type=_pt,
                        **item["_oft_db"], **item["_shadow_diag"])
                except Exception:
                    logging.warning("insert_evaluated_opportunity failed (low_price_shadow)", exc_info=True)

                # ── Insert to dedicated table (for correlation & dual-sizing analysis) ──
                try:
                  with tracked_write("opportunity_scanner", "low_price_shadow_signals_insert"):
                    self._state.conn.execute(
                        "INSERT INTO low_price_shadow_signals "
                        "(ticker, event_ticker, asset, window_id, market_price, "
                        "seconds_to_close, calibrated_prob, raw_prob, edge, fee_adjusted_edge, "
                        "z_score, vol_regime, volatility, spot_price, threshold, "
                        "full_kelly_risk_fraction, full_kelly_contracts, "
                        "capped_risk_fraction, capped_contracts, "
                        "window_signal_count, hour_signal_count, evaluation_time) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ticker, event_ticker, asset, event_ticker, best_ask,
                         stc, round(final_prob, 6),
                         round(raw_prob, 6) if raw_prob is not None else None,
                         round(edge, 6), round(fee_adjusted_edge, 6),
                         z_score, item["vol_regime"], blended_rv, spot, threshold,
                         _full_kelly_f, _full_position,
                         _capped_kelly_f, _capped_position,
                         _window_count, _hour_count,
                         datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
                    self._state.conn.commit()
                except Exception:
                    # RCA instrumentation (2026-05-09): match the diag fields
                    # from StateManager.insert_evaluated_opportunity so a
                    # post-deploy contention cluster across both sites lines
                    # up cleanly in the journal.
                    _diag_thread = threading.current_thread().name
                    _diag_in_tx = getattr(self._state.conn, "in_transaction", "?")
                    logging.warning(
                        f"low_price_shadow_signals insert failed "
                        f"thread={_diag_thread!r} in_tx={_diag_in_tx!s}",
                        exc_info=True,
                    )
        except Exception:
            logging.warning("low_price_shadow processing error", exc_info=True)

    def _process_no_side_shadow(self, queue: list, candidates: list = None) -> None:
        """Shadow-evaluate NO-side (buy NO contract) for all queued markets.

        Mirrors the YES-side evaluation: NO_prob = 1 - YES_prob,
        NO_ask from market NBBO. Runs through the same filter pipeline
        (price, edge, sizing) and logs to evaluated_opportunities with side='no'.

        Shadow-only by default — never places orders. EXCEPTION: weather NO-side
        live execution bypasses the shadow path when WEATHER_NO_SIDE_LIVE=True
        and NO ≤ 40c, STC ≥ 16h, assumed-prob edge > 0. In that case, a live
        candidate is appended to `candidates` (if provided).

        Entire body in try/except so a crash here cannot affect candidate
        selection or live trading.
        """
        try:
            for item in queue:
                ticker = item["ticker"]
                best_ask = item["best_ask"]
                asset = item["asset"]
                _pt = item["product_type"]
                spot = item["spot"]
                threshold = item["threshold"]
                blended_rv = item["blended_rv"]
                stc = item["seconds_remaining"]

                # ── Compute YES-side final_prob if not pre-computed ──
                # (POR entries don't have final_prob yet — need temp+cap+blend)
                if item["final_prob"] is not None:
                    yes_final_prob = item["final_prob"]
                    raw_prob = item["raw_prob"]
                    calibration_method = item["calibration_method"]
                else:
                    # Re-run probability engine (same as _process_price_shadow)
                    prob_result = ProbabilityEngine.compute(
                        spot, threshold, stc, blended_rv,
                        market_price_cents=best_ask,
                        asset=asset, product_type=_pt,
                    )
                    if not prob_result.get("tradeable"):
                        continue
                    yes_final_prob = prob_result["calibrated_prob"]
                    raw_prob = prob_result.get("raw_prob")
                    calibration_method = prob_result.get("calibration_method")

                    # Temperature scaling (Layer 1)
                    _tempcfg = get_market_config(_pt)
                    _temp_t = _tempcfg.temperature_t if _tempcfg.temperature_enabled else None
                    if _temp_t is not None and _temp_t == 1.0:
                        _temp_t = None
                    _reg_engine = _cal_state._resolve_cal_engine(_pt, asset, require_enabled=True)
                    if _reg_engine is not None and _reg_engine.is_learned_method_active():
                        _temp_t = None
                    if _temp_t is not None:
                        _p = max(0.001, min(0.999, yes_final_prob))
                        _z = math.log(_p / (1.0 - _p))
                        yes_final_prob = 1.0 / (1.0 + math.exp(-_z / _temp_t))

                    # Dynamic cap / learned ceiling
                    _dyn_cap = ProbabilityEngine._dynamic_cap(stc, product_type=_pt)
                    _reg_engine_c = _cal_state._resolve_cal_engine(_pt, asset, require_enabled=True)
                    if _reg_engine_c is not None and _reg_engine_c.is_learned_method_active():
                        yes_final_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, yes_final_prob))
                    else:
                        yes_final_prob = max(0.01, min(_dyn_cap, yes_final_prob))

                    # Market blend
                    _mcfg = get_market_config(_pt)
                    if best_ask < ENDGAME_BLEND_PRICE:
                        mip = best_ask / 100.0
                        yes_final_prob = (1.0 - _mcfg.market_blend_w) * yes_final_prob + _mcfg.market_blend_w * mip

                # ── NO-side computation ──
                no_prob = 1.0 - yes_final_prob
                no_price = item["no_ask"]  # actual NO ask from market NBBO

                # Price filter for NO side (use lower floor for 15M shadow collection)
                _ncfg = get_market_config(_pt)
                _no_price_floor = NO_SIDE_MIN_ENTRY_PRICE  # universal floor for NO-side shadow collection
                if not (_no_price_floor <= no_price <= _ncfg.max_entry_price):
                    # NO price out of range — skip (don't log; too much volume for OOR)
                    continue

                # Edge computation
                no_edge = no_prob - no_price / 100.0
                no_fee_1c = calculate_fee(1, no_price, is_taker=True,
                                          fee_mult_taker=_ncfg.fee_multiplier_taker,
                                          fee_mult_maker=_ncfg.fee_multiplier_maker)
                no_fee_adj_edge = no_edge - no_fee_1c / 100.0

                # ── Weather NO-side LIVE candidate (bypasses observation gate) ──
                # Model is structurally wrong on NO (predicts 3-16%, actual 79.7% WR),
                # so we use an ASSUMED 0.70 probability instead of no_prob.
                # Gates: WEATHER_NO_SIDE_LIVE, NO ≤ 40c, STC ≥ 16h, assumed edge > 0.
                # This is the CORRECT location for this gate — previously it lived in the
                # YES-side observation branch but weather NO evals flow through THIS path,
                # so the previous location was structurally dead.
                # See kb/failures/weather-no-candidate-never-fires.md
                if (_pt == "weather"
                        and WEATHER_NO_SIDE_LIVE
                        and candidates is not None
                        and stc >= WEATHER_NO_SIDE_MIN_STC
                        and no_price >= WEATHER_NO_MIN_PRICE
                        and no_price <= WEATHER_NO_MAX_PRICE
                        and not should_exclude_weather_no_ticker(ticker)):
                    _wnl_assumed_edge = WEATHER_NO_ASSUMED_PROB - no_price / 100.0 - no_fee_1c / 100.0
                    if _wnl_assumed_edge > 0:
                        _wnl_cand_dedup = (ticker, "weather_no_candidate")
                        if _wnl_cand_dedup not in self._eval_opp_seen:
                            # Skip if already holding this ticker
                            _wnl_has_pos = any(
                                p.get("ticker") == ticker
                                for p in self._state.get_open_positions())
                            if not _wnl_has_pos:
                                self._eval_opp_seen.add(_wnl_cand_dedup)
                                _wnl_cand_ev = round(
                                    (WEATHER_NO_ASSUMED_PROB * (100 - no_price))
                                    - ((1 - WEATHER_NO_ASSUMED_PROB) * no_price)
                                    - no_fee_1c, 2)
                                _wnl_sx = item.get("_shadow_extra", {})
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, item["event_ticker"], asset,
                                        "candidate",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=no_price,
                                        seconds_to_close=stc,
                                        calibrated_prob=WEATHER_NO_ASSUMED_PROB,
                                        edge=round(WEATHER_NO_ASSUMED_PROB - no_price / 100.0, 6),
                                        calibration_method="assumed_prob",
                                        fee_adjusted_edge=round(_wnl_assumed_edge, 6),
                                        breakeven_wr=no_price / 100.0,
                                        expected_value=_wnl_cand_ev,
                                        vol_regime=item.get("vol_regime", "normal"),
                                        ask_depth=item.get("ask_depth"),
                                        best_ask_source=item.get("best_ask_source"),
                                        position_size=WEATHER_NO_CONTRACT_COUNT, kelly_f=0.0, drawdown_scaler=1.0,
                                        strategy="weather_no_live",
                                        product_type="weather", side="no",
                                        raw_prob=1.0 - raw_prob if raw_prob is not None else None,
                                        wx_ensemble_mean=_wnl_sx.get("wx_ensemble_mean"),
                                        wx_ensemble_std=_wnl_sx.get("wx_ensemble_std"),
                                        wx_bias_correction=_wnl_sx.get("wx_bias_correction"),
                                        wx_n_members=_wnl_sx.get("wx_n_members"),
                                        wx_market_type=_wnl_sx.get("wx_market_type"),
                                        wx_hrrr_temp=_wnl_sx.get("wx_hrrr_temp"),
                                        wx_corrected_mean=_wnl_sx.get("wx_corrected_mean"),
                                        wx_no_side_edge=_wnl_sx.get("wx_no_side_edge"),
                                        **item.get("_oft_db", {}),
                                        **item.get("_shadow_diag", {}))
                                except Exception:
                                    logging.warning(
                                        "insert_evaluated_opportunity failed (weather_no_live)",
                                        exc_info=True)
                                candidates.append({
                                    "ticker": ticker,
                                    "event_ticker": item["event_ticker"],
                                    "asset": asset,
                                    "product_type": "weather",
                                    "side": "no",
                                    "spot": spot,
                                    "threshold": threshold,
                                    "seconds_to_close": round(stc, 1),
                                    "blended_rv": blended_rv,
                                    "calibrated_prob": WEATHER_NO_ASSUMED_PROB,
                                    "z_score": 0.0,
                                    "best_yes_ask": no_price,  # NO price for execution
                                    "best_ask_source": item.get("best_ask_source"),
                                    "edge": round(WEATHER_NO_ASSUMED_PROB - no_price / 100.0, 6),
                                    "fee_adjusted_edge": round(_wnl_assumed_edge, 6),
                                    "position_size": WEATHER_NO_CONTRACT_COUNT,
                                    "kelly_f": 0.0,
                                    "drawdown_scaler": 1.0,
                                    "vol_regime": item.get("vol_regime", "normal"),
                                    "balance_at_scan": item.get("balance"),
                                    "strategy": "weather_no_live",
                                    "strategy_scores": {},
                                    "ob_snapshot": {},
                                    "calibrated_prob_raw": None,
                                    "ofa_adjustment": 0.0,
                                    "ofa_confidence": "none",
                                    "raw_prob": raw_prob,
                                    "calibration_method": "assumed_prob",
                                    "old_system_prob": None,
                                    "kalshi_oft_signals": {},
                                    "counterfactual_json": None,
                                })
                                logging.info(
                                    "WEATHER_NO_CANDIDATE: %s count=%d no_price=%dc assumed_edge=%.2f%% "
                                    "stc=%.0fs (model_no_prob=%.1f%%, assumed=%.0f%%)",
                                    ticker, WEATHER_NO_CONTRACT_COUNT, no_price, _wnl_assumed_edge * 100,
                                    stc, no_prob * 100, WEATHER_NO_ASSUMED_PROB * 100)

                # ── Hourly NO-side LIVE candidate ──
                # Uses the MODEL's no_prob (not assumed — model adds 6.9pp: 53.9% flagged vs 47.0% rejected).
                # Gate: HOURLY_NO_SIDE_LIVE, asset not in HOURLY_NO_EXCLUDED_ASSETS (empty by default
                # because all 4 assets show positive NO-side model edge — see constant comment),
                # NO price 40-54c, model edge positive, not already holding.
                # Data: z=2.61, time-split stable (54.8% both halves), all assets positive.
                if (_pt == "hourly"
                        and HOURLY_NO_SIDE_LIVE
                        and asset not in HOURLY_NO_EXCLUDED_ASSETS
                        and candidates is not None
                        and no_price >= HOURLY_NO_MIN_PRICE
                        and no_price <= HOURLY_NO_MAX_PRICE
                        and no_fee_adj_edge >= HOURLY_MIN_EDGE_PCT / 100.0):
                    _hno_cand_dedup = (ticker, "hourly_no_candidate")
                    if _hno_cand_dedup not in self._eval_opp_seen:
                        _hno_has_pos = any(
                            p.get("ticker") == ticker
                            for p in self._state.get_open_positions())
                        if not _hno_has_pos:
                            self._eval_opp_seen.add(_hno_cand_dedup)
                            _hno_ev = round(
                                (no_prob * (100 - no_price))
                                - ((1 - no_prob) * no_price)
                                - no_fee_1c, 2)
                            try:
                                self._state.insert_evaluated_opportunity(
                                    ticker, item["event_ticker"], asset,
                                    "candidate",
                                    spot_price=spot, threshold=threshold,
                                    volatility=blended_rv, market_price=no_price,
                                    seconds_to_close=stc,
                                    calibrated_prob=no_prob,
                                    edge=no_edge,
                                    calibration_method=calibration_method,
                                    fee_adjusted_edge=no_fee_adj_edge,
                                    breakeven_wr=no_price / 100.0,
                                    expected_value=_hno_ev,
                                    vol_regime=item.get("vol_regime", "normal"),
                                    ask_depth=item.get("ask_depth"),
                                    best_ask_source=item.get("best_ask_source"),
                                    position_size=HOURLY_NO_FIXED_CONTRACTS,
                                    kelly_f=0.0, drawdown_scaler=1.0,
                                    strategy="hourly_no_live",
                                    product_type="hourly", side="no",
                                    raw_prob=1.0 - raw_prob if raw_prob is not None else None,
                                    hourly_pre_temp_prob=item.get("hourly_pre_temp_prob"),
                                    hourly_applied_temp_t=item.get("hourly_applied_temp_t"),
                                    hourly_post_temp_prob=item.get("hourly_post_temp_prob"),
                                    **item.get("_oft_db", {}),
                                    **item.get("_shadow_diag", {}))
                            except Exception:
                                logging.warning(
                                    "insert_evaluated_opportunity failed (hourly_no_live)",
                                    exc_info=True)
                            candidates.append({
                                "ticker": ticker,
                                "event_ticker": item["event_ticker"],
                                "asset": asset,
                                "product_type": "hourly",
                                "side": "no",
                                "spot": spot,
                                "threshold": threshold,
                                "seconds_to_close": round(stc, 1),
                                "blended_rv": blended_rv,
                                "calibrated_prob": no_prob,
                                "z_score": 0.0,
                                "best_yes_ask": no_price,
                                "best_ask_source": item.get("best_ask_source"),
                                "edge": no_edge,
                                "fee_adjusted_edge": no_fee_adj_edge,
                                "position_size": HOURLY_NO_FIXED_CONTRACTS,
                                "kelly_f": 0.0,
                                "drawdown_scaler": 1.0,
                                "vol_regime": item.get("vol_regime", "normal"),
                                "balance_at_scan": item.get("balance"),
                                "strategy": "hourly_no_live",
                                "strategy_scores": {},
                                "ob_snapshot": {},
                                "calibrated_prob_raw": None,
                                "ofa_adjustment": 0.0,
                                "ofa_confidence": "none",
                                "raw_prob": raw_prob,
                                "calibration_method": calibration_method,
                                "old_system_prob": None,
                                "kalshi_oft_signals": {},
                                "counterfactual_json": None,
                            })
                            logging.info(
                                "HOURLY_NO_CANDIDATE: %s %s no_price=%dc edge=%.2f%% "
                                "no_prob=%.1f%% stc=%.0fs",
                                ticker, asset, no_price, no_fee_adj_edge * 100,
                                no_prob * 100, stc)

                # Edge filter (same price-dependent schedule, applied to NO price)
                if _pt == "weather":
                    _no_min_edge = WEATHER_MIN_EDGE_PCT
                elif _pt == "hourly":
                    _no_min_edge = HOURLY_MIN_EDGE_PCT
                else:
                    _no_min_edge = get_min_edge(no_price)

                # Determine filter stage
                _no_kelly_f = None
                _no_position = None
                _no_drawdown = None
                _no_ev = None
                if no_fee_adj_edge < _no_min_edge:
                    _no_filter_stage = "insufficient_edge"
                    _no_rej = f"NO net_edge {no_fee_adj_edge:.4f} < min {_no_min_edge:.4f} @{no_price}c"
                else:
                    # Compute sizing for instrumentation
                    _no_ev = round(
                        (no_prob * (100 - no_price)) - ((1 - no_prob) * no_price) - no_fee_1c, 2)
                    try:
                        _no_balance = self._get_balance_cached()
                        if _no_balance and _no_balance > 0:
                            _no_sizing = self._sizer.compute(no_prob, no_price, _no_balance)
                            _no_kelly_f = _no_sizing["kelly_f"]
                            _no_position = _no_sizing["contracts"]
                            _no_drawdown = _no_sizing["drawdown_scaler"]
                            if _ncfg.kelly_fraction < 1.0:
                                _no_position = max(1, int(_no_position * _ncfg.kelly_fraction))
                            _no_type_max = int((_no_balance * _ncfg.max_risk_per_trade) / no_price)
                            if _no_position > _no_type_max:
                                _no_position = max(1, _no_type_max)
                    except Exception:
                        logging.warning("no_side sizing failed", exc_info=True)

                    if _no_position is not None and _no_position <= 0:
                        _no_filter_stage = "zero_sizing"
                        _no_rej = "NO sizing yielded 0 contracts"
                    else:
                        # Passed all filters — assign appropriate shadow filter_stage
                        # Mirror YES-side taxonomy with no_side_ prefix
                        if _ncfg.observation_only and _ncfg.observation_filter_label:
                            _no_filter_stage = _ncfg.observation_filter_label
                        elif _pt in (None, "15m"):
                            _is_no_price_shadow = no_price < _ncfg.min_entry_price
                            if _is_no_price_shadow:
                                # NO price in shadow zone (70-85c) — mirrors YES price_shadow
                                _no_filter_stage = "no_side_price_shadow_no_xrp" if asset != "XRP" else "no_side_price_shadow_xrp"
                            elif stc > STC_SHADOW_THRESHOLD:
                                _no_filter_stage = "no_side_stc_shadow_no_xrp" if asset != "XRP" else "no_side_stc_shadow_xrp"
                            elif XRP_15M_SHADOW and asset == "XRP":
                                _no_filter_stage = "no_side_xrp_shadow"
                            elif HYPE_15M_SHADOW and asset == "HYPE":
                                _no_filter_stage = "no_side_hype_shadow"
                            elif DOGE_15M_SHADOW and asset == "DOGE":
                                _no_filter_stage = "no_side_doge_shadow"
                            else:
                                _no_filter_stage = "no_side_shadow"
                        else:
                            _no_filter_stage = "no_side_shadow"
                        _no_rej = None

                # Dedup + DB insert
                _dedup_key = (ticker, _no_filter_stage, "no")
                if _dedup_key in self._eval_opp_seen:
                    continue
                self._eval_opp_seen.add(_dedup_key)

                _sx = item.get("_shadow_extra", {})
                # Debug: warn if weather NO-side is missing ensemble data
                if _pt == "weather" and not _sx.get("wx_ensemble_mean"):
                    logging.warning(
                        "NO_SIDE_DIAG: weather %s missing wx_ensemble_mean in _shadow_extra, keys=%s",
                        ticker, list(_sx.keys()))
                # Hourly temperature fields (passed through queue)
                _no_hourly_pre = item.get("hourly_pre_temp_prob")
                _no_hourly_t = item.get("hourly_applied_temp_t")
                _no_hourly_post = item.get("hourly_post_temp_prob")
                self._state.insert_evaluated_opportunity(
                    ticker, item["event_ticker"], asset,
                    _no_filter_stage,
                    rejection_reason=_no_rej,
                    spot_price=spot, threshold=threshold,
                    volatility=blended_rv, market_price=no_price,
                    seconds_to_close=stc,
                    calibrated_prob=no_prob,
                    edge=no_edge,
                    fee_adjusted_edge=no_fee_adj_edge,
                    z_score=None,  # z-score is YES-side concept
                    vol_regime=item["vol_regime"],
                    raw_prob=1.0 - raw_prob if raw_prob is not None else None,
                    calibration_method=calibration_method,
                    breakeven_wr=no_price / 100.0,
                    expected_value=_no_ev,
                    kelly_f=_no_kelly_f,
                    position_size=_no_position,
                    drawdown_scaler=_no_drawdown,
                    ask_depth=item["ask_depth"],
                    best_ask_source=item["best_ask_source"],
                    product_type=_pt,
                    side="no",
                    wx_ensemble_mean=_sx.get("wx_ensemble_mean"),
                    wx_ensemble_std=_sx.get("wx_ensemble_std"),
                    wx_bias_correction=_sx.get("wx_bias_correction"),
                    wx_n_members=_sx.get("wx_n_members"),
                    wx_market_type=_sx.get("wx_market_type"),
                    wx_hrrr_temp=_sx.get("wx_hrrr_temp"),
                    wx_corrected_mean=_sx.get("wx_corrected_mean"),
                    wx_no_side_edge=_sx.get("wx_no_side_edge"),
                    hourly_pre_temp_prob=_no_hourly_pre,
                    hourly_applied_temp_t=_no_hourly_t,
                    hourly_post_temp_prob=_no_hourly_post,
                    **item["_oft_db"], **item["_shadow_diag"])
        except Exception:
            logging.warning("no_side_shadow processing error", exc_info=True)

    # ── Threshold parsing ─────────────────────────────────────────────────

    @staticmethod
    def _parse_threshold(market: Dict) -> Optional[float]:
        """Extract strike/threshold price from market data.

        Priority:
        1. floor_strike field (most reliable for strike-level markets)
        2. yes_sub_title "Price to beat: $X,XXX.XX" (15-minute up/down markets)
        3. Ticker pattern: KXBTC-...-B95000 -> 95000.0 (strike-level markets)
        4. Subtitle regex fallback: "above $95,000.00" -> 95000.0
        """
        # 1. floor_strike field
        floor_strike = market.get("floor_strike")
        if floor_strike is not None:
            try:
                val = float(floor_strike)
                if val > 0:
                    return val
            except (ValueError, TypeError):
                pass

        # 2. yes_sub_title: "Price to beat: $68,500.00" (15M markets)
        for field in ("yes_sub_title", "no_sub_title"):
            sub = market.get(field, "") or ""
            if "Price to beat" in sub and "TBD" not in sub:
                m = re.search(r"\$([0-9,]+\.?\d*)", sub)
                if m:
                    try:
                        return float(m.group(1).replace(",", ""))
                    except ValueError:
                        pass

        # 3. Ticker pattern: KXBTC-26FEB2114-B95000 (strike-level markets)
        ticker = market.get("ticker", "")
        parts = ticker.split("-")
        if len(parts) >= 3:
            strike_part = parts[-1]
            if strike_part.startswith("B") or strike_part.startswith("T"):
                try:
                    return float(strike_part[1:])
                except ValueError:
                    pass

        # 4. Subtitle regex: "above $95,000.00" or "above $0.55"
        subtitle = market.get("subtitle", "") or ""
        if subtitle:
            m = re.search(r"\$([0-9,]+\.?\d*)", subtitle)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    pass

        return None

    @staticmethod
    def _parse_weather_market_info(market: Dict):
        """Parse weather market type and bracket bounds from market dict.

        Returns (market_type, lower_bound, upper_bound) or None.
        market_type: "bracket" | "lower_tail" | "upper_tail"

        Detection uses floor_strike/cap_strike fields first, ticker prefix as fallback.
        Bracket (B-prefix): P(lower < X < upper)
        Tail (T-prefix): lower_tail if only cap_strike, upper_tail if only floor_strike
        """
        ticker = market.get("ticker", "")
        floor_strike = market.get("floor_strike")
        cap_strike = market.get("cap_strike")

        # Convert to float if present
        floor_val = None
        cap_val = None
        try:
            if floor_strike is not None:
                floor_val = float(floor_strike)
        except (ValueError, TypeError):
            pass
        try:
            if cap_strike is not None:
                cap_val = float(cap_strike)
        except (ValueError, TypeError):
            pass

        # Determine market type from ticker prefix
        parts = ticker.split("-")
        strike_part = parts[-1] if len(parts) >= 3 else ""

        if strike_part.startswith("B"):
            # Bracket market
            if floor_val is not None and cap_val is not None:
                return ("bracket", floor_val, cap_val)
            # Fallback: B{upper}, infer bounds from available data
            try:
                upper = float(strike_part[1:])
            except ValueError:
                return None
            lower = floor_val if floor_val is not None else upper - 2.0
            upper = cap_val if cap_val is not None else upper
            return ("bracket", lower, upper)

        elif strike_part.startswith("T"):
            try:
                t_val = float(strike_part[1:])
            except ValueError:
                return None
            # Use floor_strike/cap_strike to disambiguate tail direction
            # Lower tail (P(X < threshold)): cap_strike present, no floor
            # Upper tail (P(X > threshold)): floor_strike present, no cap
            if cap_val is not None and floor_val is None:
                return ("lower_tail", None, cap_val)
            if floor_val is not None and cap_val is None:
                return ("upper_tail", floor_val, None)
            # Fallback: use subtitle text
            subtitle = (market.get("subtitle") or "").lower()
            if "below" in subtitle or "under" in subtitle:
                return ("lower_tail", None, t_val)
            if "above" in subtitle or "over" in subtitle:
                return ("upper_tail", t_val, None)
            # Last resort: treat as upper tail (legacy behavior)
            return ("upper_tail", t_val, None)

        return None

    # ── Orderbook helpers ─────────────────────────────────────────────────

    @staticmethod
    def _best_yes_ask_cents(ob_data: Dict) -> Optional[int]:
        """Compute best YES ask = 100 - highest NO bid.

        Bit 86b9vpp2z (2026-05-11): Path-B wrapper preservation — body
        relocated to bot/helpers/orderbook.py (so OrderExecutor can drop
        the `_get_opportunity_scanner()` cycle-break helper); this
        staticmethod retained as a 1-line delegate to keep the ~20 test
        sites using `OpportunityScanner._best_yes_ask_cents(...)` working.
        """
        return best_yes_ask_cents(ob_data)

    @staticmethod
    def _is_severe_drift(ws_qty: int, rest_qty: int,
                         min_abs_diff: int = 1000,
                         min_ratio: float = 2.0) -> bool:
        """True if WS total qty differs from REST total qty enough to
        indicate cache corruption rather than normal book churn.

        Calibrated against actual 2026-04-24 incident values — see
        kb/failures/ws-cache-drift-silent-scan-2026-04-24.md. Both
        conditions must hold to avoid false-positive flagging during
        heavy but legitimate trading:

        - abs(ws - rest) >= min_abs_diff (meaningful absolute miss)
        - Either: ratio >= min_ratio (2× default), OR one side is
          zero and the other has >= min_abs_diff qty (total disagreement).

        Symmetric in (ws, rest) — we care about magnitude of mismatch,
        not direction. ETH case during the incident had WS < REST;
        others had WS > REST. Both are the same corruption class.
        """
        abs_diff = abs(ws_qty - rest_qty)
        if abs_diff < min_abs_diff:
            return False
        if rest_qty == 0:
            return ws_qty >= min_abs_diff
        if ws_qty == 0:
            return rest_qty >= min_abs_diff
        hi = max(ws_qty, rest_qty)
        lo = min(ws_qty, rest_qty)
        return (hi / lo) >= min_ratio or abs_diff >= min_abs_diff * 5

    def flag_ticker_drifted(self, ticker: str, cooldown_s: float = 60.0) -> None:
        """Flag a ticker to bypass the WS orderbook cache for `cooldown_s`
        seconds. Called from scan() silent-bail paths when WS-derived data
        is unusable (no best ask, no orderbook). During the cooldown,
        `_get_orderbook_cached` routes directly to REST, giving the WS
        cache a chance to re-snapshot cleanly.

        Also evicts `_ob_cache[ticker]` so the shared TTL cache doesn't
        silently serve the WS-corrupt orderbook during the bypass — see
        what-could-go-wrong C3 in
        kb/failures/ws-cache-drift-silent-scan-2026-04-24.md.

        No-op for hourly tickers (they skip WS in the first place).
        """
        _hourly_prefixes = tuple(HOURLY_SERIES_TICKERS.values())
        if ticker.startswith(_hourly_prefixes):
            return
        # Sweep expired entries if the dict has grown — prevents unbounded
        # accumulation from settled markets. Cheap; runs at most per flag.
        if len(self._ws_drift_cooldown) > 32:
            now_sweep = time.time()
            expired = [t for t, exp in self._ws_drift_cooldown.items()
                       if exp <= now_sweep]
            for t in expired:
                del self._ws_drift_cooldown[t]
        self._ws_drift_cooldown[ticker] = time.time() + cooldown_s
        # Evict shared cache so the bypass actually takes effect.
        self._ob_cache.pop(ticker, None)
        # Phase 2 (Apr 25 2026): replace the 60s symptom-bandaid
        # with a real cache reset. force_resubscribe sends a
        # get_snapshot to Kalshi (or falls back to unsub+resub),
        # which atomically replaces the corrupt WS cache. The
        # 60s REST-bypass cooldown above stays as belt-and-suspenders
        # for the brief window between detection and snapshot
        # arrival. Defensive: getattr handles bypassed-init
        # test fixtures.
        #
        # R2 / P1-6: pass purge_cache=False. Atomic replace via
        # _handle_ob_snapshot is strictly better than wipe-then-
        # wait. The drift-detector path was creating ~7-8s of
        # `no_orderbook` rejections during the snapshot+fallback
        # window — exactly when the price is moving. Stale data is
        # better than no data; the next snapshot atomically
        # replaces it. The REST bypass above already handles the
        # "use REST not stale-WS" hot path during the cooldown.
        _ml = getattr(self, "_ml", None)
        _kf = getattr(_ml, "kalshi_feed", None) if _ml else None
        if _kf is not None and getattr(_kf, "is_connected", False):
            try:
                _kf.force_resubscribe(ticker, purge_cache=False)
            except Exception:
                logging.warning(
                    "force_resubscribe from flag_ticker_drifted "
                    "failed for %s", ticker, exc_info=True)

    def _get_orderbook_cached(self, ticker: str) -> Tuple[Optional[Dict], bool]:
        """Return (orderbook_data, was_fresh_fetch). Uses TTL cache.

        Prefers real-time WS orderbook when available (zero API cost),
        falls back to REST fetch if WS data is missing or stale.

        Respects `_ws_drift_cooldown`: flagged tickers skip WS and use
        REST directly. If REST also fails while flagged, falls back to
        whatever WS data exists — stale WS beats no data at all.
        """
        now = time.time()

        # Skip WS subscription for hourly tickers — too many strikes (75/asset),
        # they never get unsubscribed properly, and pollute the dashboard.
        _hourly_prefixes = tuple(HOURLY_SERIES_TICKERS.values())
        is_hourly = ticker.startswith(_hourly_prefixes)

        # WS-bypass for drift-flagged tickers. On expiry, clear the flag
        # and fall back to the normal WS path on the next call.
        skip_ws = False
        _flag_expiry = self._ws_drift_cooldown.get(ticker)
        if _flag_expiry is not None:
            if now < _flag_expiry:
                skip_ws = True
            else:
                del self._ws_drift_cooldown[ticker]

        # Try WS orderbook first (free, real-time) — 15M only
        if (not skip_ws and self._kalshi_feed
                and self._kalshi_feed.is_connected and not is_hourly):
            ws_ob = self._kalshi_feed.get_orderbook(ticker)
            if ws_ob and now - ws_ob.get("ts", 0) < ORDERBOOK_CACHE_TTL * 2:
                # Subscribe if not already (ensures future deltas flow)
                self._kalshi_feed.subscribe_ticker(ticker)
                self._ob_cache[ticker] = (ws_ob, now)
                return (ws_ob, False)
            # No WS data yet — subscribe so it arrives for next scan
            self._kalshi_feed.subscribe_ticker(ticker)

        if not skip_ws:
            cached = self._ob_cache.get(ticker)
            if cached:
                data, fetch_time = cached
                if now - fetch_time < ORDERBOOK_CACHE_TTL:
                    return (data, False)

        # REST fallback
        try:
            ob_data = self._client.get_orderbook(ticker, depth=5)
        except Exception as e:
            # M1: if REST fails while WS is flagged, prefer stale WS over
            # returning None. Losing WS-bypass is less harmful than losing
            # orderbook data entirely.
            if skip_ws and self._kalshi_feed and not is_hourly:
                ws_fallback = self._kalshi_feed.get_orderbook(ticker)
                if ws_fallback:
                    logging.warning(
                        "WS_DRIFT_BYPASS %s: REST failed (%s), using stale "
                        "WS data as fallback", ticker, e)
                    return (ws_fallback, False)
            raise
        # Prefer orderbook_fp (new FP format), fall back to orderbook (legacy)
        orderbook_fp = ob_data.get("orderbook_fp") if ob_data else None
        if orderbook_fp:
            orderbook = self._convert_orderbook_fp(orderbook_fp)
        else:
            orderbook = ob_data.get("orderbook", ob_data) if ob_data else None
        self._ob_cache[ticker] = (orderbook, now)
        return (orderbook, True)

    def _scanner_convergence_velocity(self, ticker: str) -> float:
        """Upward ask movement in cents over convergence window, from scan history."""
        history = self._ticker_ask_history.get(ticker)
        if not history or len(history) < 2:
            return 0.0
        now = time.time()
        cutoff = now - CONVERGENCE_WINDOW_SECONDS
        oldest_price = None
        for ts, price in history:
            if ts >= cutoff:
                oldest_price = price
                break
        if oldest_price is None:
            return 0.0
        return history[-1][1] - oldest_price

    @staticmethod
    def _convert_orderbook_fp(ob_fp: Dict) -> Dict:
        """Convert orderbook_fp format to internal cents format.

        Bit 86b9vpp2z (2026-05-11): Path-B wrapper preservation — body
        relocated to bot/helpers/orderbook.py; staticmethod retained as
        a 1-line delegate (same rationale as `_best_yes_ask_cents` above).
        """
        return convert_orderbook_fp(ob_fp)

    # Rejection reasons that indicate scan-broken / silent-bail paths.
    # Written by `insert_rejection()` in the bail-shaped branches of
    # scan() (no orderbook, no best ask, unparsable strike) per fix #2
    # of the 2026-04-24 WS-cache-drift PM (commit 9dd396b) so silent
    # scans leave a DB trace. The silence watchdog MUST NOT count
    # these as "scan alive" — counting them would hide the exact
    # failure class the watchdog exists to detect. Any new bail-shaped
    # rejection reason added in scan() MUST be appended here in the
    # same commit. See `tests/test_15m_silence_alert.py`
    # (TestSilent15MAlertSilentBailDetection) for the regression
    # contract.
    _BAIL_REJECTION_REASONS = (
        "no_orderbook", "no_best_ask", "threshold_unparsable",
    )
    # Empty constant would yield SQL `NOT IN ()` syntax error → silent
    # watchdog disable. Catch at module load.
    #
    # Module-load assertion policy (round-8 C3 unifying comment):
    # - HARD-ASSERT (this line): permanent silent-disable contracts.
    #   An empty `_BAIL_REJECTION_REASONS` would silently disable the
    #   watchdog — unrecoverable until the bot restarts AND a fix is
    #   deployed. Crashing at import (→ systemd backoff loop, operator
    #   notices missing PnL within minutes) is strictly better than
    #   running with the watchdog silently broken indefinitely.
    # - TEST-ONLY (e.g., `_WS_SHAPE_BAIL_REASONS` / `_THRESHOLD_SHAPE_BAIL_REASONS`
    #   partition): transient routing-degradation contracts. A
    #   forgotten bucketing mis-classifies the alert message but the
    #   bot continues to trade and the watchdog continues to fire;
    #   crashing at import would be DISPROPORTIONATELY costly because
    #   the Telegram notifier never initializes, depriving the
    #   operator of any failure signal at all.
    # The asymmetry is deliberate. New invariants should be classified
    # with this distinction in mind.
    assert _BAIL_REJECTION_REASONS, (
        "_BAIL_REJECTION_REASONS must not be empty — empty produces "
        "invalid SQL `NOT IN ()` and silently disables the watchdog.")

    # Shape buckets — partition `_BAIL_REJECTION_REASONS` into the two
    # diagnostic shapes the BAIL FLOOD alert routes between. The
    # partition + disjointness invariants are enforced via tests in
    # `tests/test_15m_silence_alert.py::TestBailFloodMessageDifferentiation`
    # (search for `test_shape_buckets_partition_bail_reasons`), NOT
    # via class-body assertions — adversarial round-4 critique C4:
    # module-load assertions on a recoverable contract (a forgotten
    # bucketing only degrades routing accuracy, not correctness)
    # would crash the bot at import → systemd backoff loop, with
    # NO Telegram alert because the Telegram client never initializes.
    # Test-time enforcement preserves the contract without the
    # production-startup risk. The original `assert _BAIL_REJECTION_REASONS`
    # at module load remains because that one guards an unambiguously
    # broken state (`NOT IN ()` SQL syntax error → silent watchdog
    # disable, even worse than crash-loop).
    _WS_SHAPE_BAIL_REASONS = frozenset({"no_orderbook", "no_best_ask"})
    _THRESHOLD_SHAPE_BAIL_REASONS = frozenset({"threshold_unparsable"})

    # Threshold-dominance ratio: classify as threshold-shape when
    # threshold-bucket count >= 3 × WS-bucket count (i.e., ≥75% of
    # bail rows are threshold-shape). Round-3 critique C4: original
    # ratio of 4 (80%) was asymmetric at small n — at n_thr=3,
    # n_ws=1 → 75% threshold but routed to BAIL FLOOD (3 < 4×1).
    # A 3-ticker incident (one asset in catalog-gap) plus a single
    # WS blip would have been the exact misroute the change is
    # meant to prevent. Loosened to 3 so the boundary covers
    # n_thr=3, n_ws=1 (3 >= 3×1). Still rejects n_thr=3, n_ws=2
    # (3 < 6) and any case where WS contributes >25% — those have
    # legitimately mixed signal and conservatively route to BAIL
    # FLOOD with the breakdown line for operator inspection.
    _THRESHOLD_SHAPE_DOMINANCE_RATIO = 3
    # Bail signal triggers when primary is stale/None AND bail rows
    # ≥ `_BAIL_MIN_ROWS_WHEN_STALE` exist in the recent window.
    # Threshold of 3 is the smallest value that:
    #   (a) Catches dedup-bounded `threshold_unparsable` floods
    #       (Kalshi rename hits 4 active tickers → 4 rows). Cap is
    #       4, threshold 3 has 1-row margin.
    #   (b) Rejects single-blip false positives (one ticker fails
    #       one tick during a quiet market right at the 10-min
    #       staleness boundary — would mis-classify as cache-drift
    #       and send the operator to the wrong KB article).
    # `no_orderbook` / `no_best_ask` floods are not deduped, so they
    # easily reach hundreds — threshold has no impact there.
    _BAIL_MIN_ROWS_WHEN_STALE = 3
    # Bail-flood query is non-indexed (LIKE on PK + filters on
    # rejection_time/reason). Throttle to avoid full table scan every
    # scan tick (~2s cadence). 30s resolution is plenty for a 600s
    # staleness check.
    _BAIL_QUERY_THROTTLE_SECONDS = 30.0
    # Hoisted from local scope to a class constant so the bail-window
    # query and the staleness check share ONE source of truth — they
    # MUST stay equal or bail-flood detection becomes inconsistent
    # with the staleness condition that gates it. Same for uptime
    # guard. R6 [A6] DRY drift fix.
    _SILENCE_AGE_THRESHOLD_SECONDS = 600     # 10 min
    _SILENCE_ALERT_MIN_UPTIME_SECONDS = 900  # bot must be up >15 min

    @classmethod
    def _bail_placeholders(cls):
        """Return the SQL placeholder tuple `(?,?,?)` matching
        `_BAIL_REJECTION_REASONS`. DRY helper used by both queries
        (R6 [A8])."""
        return ",".join("?" * len(cls._BAIL_REJECTION_REASONS))

    def _query_last_15m_alive_ts(self):
        # type: () -> Tuple[bool, Optional[str]]
        """Return `(ok, ts)` tuple. `ok=True` and `ts=str|None` on
        success. `ok=False` and `ts=None` on query failure. Tuple
        contract avoids the `False`-sentinel footgun where a future
        `if not ts:` check would conflate failure and no-data
        (R6 [A2]).

        On success, ts is the most recent ISO timestamp of a
        'scan alive' 15M signal: any KX*15M row in
        evaluated_opportunities OR any KX*15M row in
        rejected_opportunities whose reason is NOT in
        `_BAIL_REJECTION_REASONS`.

        Tracks `_silence_watchdog_warned_primary` for
        once-per-failure-burst rate-limited logging.
        """
        bail_reasons = self._BAIL_REJECTION_REASONS
        placeholders = self._bail_placeholders()
        try:
            row = self._state.conn.execute(
                "SELECT MAX(ts) FROM ("
                "  SELECT MAX(evaluation_time) AS ts "
                "  FROM evaluated_opportunities "
                "  WHERE ticker LIKE 'KX%15M%' "
                "  UNION ALL "
                "  SELECT MAX(rejection_time) AS ts "
                "  FROM rejected_opportunities "
                "  WHERE ticker LIKE 'KX%15M%' "
                f"    AND rejection_reason NOT IN ({placeholders})"
                ")",
                bail_reasons,
            ).fetchone()
        except Exception:
            if not getattr(
                    self, "_silence_watchdog_warned_primary", False):
                logging.warning(
                    "silent_15m primary query failed", exc_info=True)
                self._silence_watchdog_warned_primary = True
            return (False, None)
        if getattr(self, "_silence_watchdog_warned_primary", False):
            self._silence_watchdog_warned_primary = False
        return (True, row[0] if row else None)

    def _query_recent_bail_count(self):
        """Return the count of KX*15M bail-rejection rows in the last
        `_SILENCE_AGE_THRESHOLD_SECONDS`. Throttled to once per
        `_BAIL_QUERY_THROTTLE_SECONDS` because it's a non-indexed
        scan.

        On query failure: returns 0 AND advances the throttle clock.
        Returning 0 prevents spurious bail alerts based on a stale
        cached count after the underlying schema breaks (R6 [A1]).
        Advancing the throttle prevents a query storm during permanent
        failure (R6 [A4]). The warning log surfaces the broken state
        for the operator independently.
        """
        now = time.time()
        last = getattr(self, "_silence_bail_query_last_ts", 0.0)
        cached = getattr(self, "_silence_bail_query_last_count", 0)
        if now - last < self._BAIL_QUERY_THROTTLE_SECONDS:
            return cached
        cutoff_dt = (datetime.datetime.now(timezone.utc)
                     - datetime.timedelta(
                         seconds=self._SILENCE_AGE_THRESHOLD_SECONDS))
        cutoff_iso = (
            cutoff_dt.isoformat(timespec="microseconds")
            .replace("+00:00", "Z"))
        bail_reasons = self._BAIL_REJECTION_REASONS
        placeholders = self._bail_placeholders()
        try:
            bail_row = self._state.conn.execute(
                "SELECT COUNT(*) FROM rejected_opportunities "
                "WHERE ticker LIKE 'KX%15M%' "
                "  AND rejection_time >= ? "
                f"  AND rejection_reason IN ({placeholders})",
                (cutoff_iso, *bail_reasons),
            ).fetchone()
        except Exception:
            if not getattr(
                    self,
                    "_silence_watchdog_warned_bail_flood",
                    False):
                logging.warning(
                    "silent_15m bail-flood query failed", exc_info=True)
                self._silence_watchdog_warned_bail_flood = True
            # Both reset cached AND advance throttle: prevents spurious
            # alerts (cached stale → 0) AND query storm (re-query in 30s).
            self._silence_bail_query_last_count = 0
            self._silence_bail_query_last_ts = now
            return 0
        if getattr(
                self, "_silence_watchdog_warned_bail_flood", False):
            self._silence_watchdog_warned_bail_flood = False
        count = bail_row[0] if bail_row else 0
        self._silence_bail_query_last_count = count
        self._silence_bail_query_last_ts = now
        return count

    @classmethod
    def _format_bail_breakdown(cls, breakdown: Dict[str, int]) -> str:
        """Render breakdown dict as a stable, alphabetically-sorted
        string for log + alert display. Iterates over
        `_BAIL_REJECTION_REASONS` so any future bail reason added to
        the constant (and shape-bucketed per the module-load
        assertion) is automatically surfaced — there is no
        separately-maintained literal list of reasons.

        Sort order is alphabetical (not tuple order) — alert messages
        going to operator memory benefit from stable layout
        independent of internal constant ordering. Adversarial round-2
        critique C5: tuple-order coupled display layout to opaque
        internal order, which a maintainer reordering for any reason
        would silently change.

        Round-8 C1: each `reason=count` pair is wrapped in backticks
        so word-internal underscores in reason names (e.g., `no_best_ask`,
        `threshold_unparsable`) are inside an inline-code span and
        cannot be misinterpreted as italic markers by Telegram's
        legacy Markdown parser. Without this, an odd-parity underscore
        count in the rendered message could fail at parse_mode=Markdown
        and the alert would never reach the operator.
        """
        return ", ".join(
            f"`{r}={breakdown.get(r, 0)}`"
            for r in sorted(cls._BAIL_REJECTION_REASONS))

    def _query_recent_bail_breakdown(self):
        """Return Dict[str, int] mapping bail rejection_reason → count
        over the same window as `_query_recent_bail_count`. Used by the
        BAIL FLOOD alert path to differentiate WS-cache-drift signature
        (no_orderbook/no_best_ask, tens-to-hundreds of rows) from the
        Kalshi-side threshold-publish issue (threshold_unparsable,
        ≤4 dedup-bounded rows). May 4 2026 incident: alert message
        hardcoded "WS-cache-drift recurrence" so an operator hit by
        the threshold-only shape was sent to the wrong KB doc.
        See kb/failures/threshold-tbd-stuck-may04.md.

        Throttled by `_BAIL_QUERY_THROTTLE_SECONDS` independent of
        `_query_recent_bail_count` — separate cache state by design
        so a count-query call doesn't inadvertently refresh the
        breakdown cache (or vice versa).

        Returns empty dict on query failure (caller MUST handle missing
        keys defensively via `.get(reason, 0)`). Failure path also
        advances the throttle clock to avoid query storm during
        permanent failure, matching `_query_recent_bail_count`.
        """
        now = time.time()
        last = getattr(self, "_silence_bail_breakdown_last_ts", 0.0)
        cached = getattr(self, "_silence_bail_breakdown_last_dict", {})
        if now - last < self._BAIL_QUERY_THROTTLE_SECONDS:
            return cached
        cutoff_dt = (datetime.datetime.now(timezone.utc)
                     - datetime.timedelta(
                         seconds=self._SILENCE_AGE_THRESHOLD_SECONDS))
        cutoff_iso = (
            cutoff_dt.isoformat(timespec="microseconds")
            .replace("+00:00", "Z"))
        bail_reasons = self._BAIL_REJECTION_REASONS
        placeholders = self._bail_placeholders()
        try:
            rows = self._state.conn.execute(
                "SELECT rejection_reason, COUNT(*) "
                "FROM rejected_opportunities "
                "WHERE ticker LIKE 'KX%15M%' "
                "  AND rejection_time >= ? "
                f"  AND rejection_reason IN ({placeholders}) "
                "GROUP BY rejection_reason",
                (cutoff_iso, *bail_reasons),
            ).fetchall()
        except Exception:
            # Mirror `_query_recent_bail_count` failure handling:
            # rate-limited warning so a permanent failure surfaces in
            # journalctl exactly once (not every tick), AND the alert
            # path can detect the failure via empty-dict return and
            # render an explicit "(breakdown unavailable)" message
            # instead of zero-everywhere (adversarial round-2 C1).
            #
            # Round-3 C1/C5: preserve last-good cache on transient
            # failure. Clobbering to {} on a one-tick locked-DB error
            # caused the alert to flip routing to BAIL FLOOD (with
            # "(breakdown unavailable)") for one throttle window, then
            # back to THRESHOLD when the next tick succeeded — two
            # contradictory dedup_keys for the same incident, sending
            # the operator to opposite KB docs. Now the cache survives
            # transient failures; only a never-successful path returns
            # the empty default.
            if not getattr(
                    self,
                    "_silence_watchdog_warned_breakdown",
                    False):
                logging.warning(
                    "silent_15m bail-breakdown query failed",
                    exc_info=True)
                self._silence_watchdog_warned_breakdown = True
            # Initialize cache to empty if this is the first call ever
            # (no last-good); otherwise preserve the existing dict.
            if not hasattr(self, "_silence_bail_breakdown_last_dict"):
                self._silence_bail_breakdown_last_dict = {}
            # Advance throttle clock so query storm is bounded.
            self._silence_bail_breakdown_last_ts = now
            return self._silence_bail_breakdown_last_dict
        # Reset warn-flag on success so the next failure also logs once
        # (adversarial round-2 C9 — match count query's reset pattern).
        if getattr(
                self, "_silence_watchdog_warned_breakdown", False):
            self._silence_watchdog_warned_breakdown = False
        breakdown = {row[0]: row[1] for row in rows}
        self._silence_bail_breakdown_last_dict = breakdown
        self._silence_bail_breakdown_last_ts = now
        return breakdown

    def _query_recent_threshold_unparsable_tickers(self, limit: int = 4):
        """Return up to `limit` distinct ticker strings that fired
        `threshold_unparsable` rejections in the silence window, most
        recent first. Used to embed actual affected tickers in the
        THRESHOLD UNPARSABLE alert so the operator can paste-and-go
        with the curl probe without a separate DB query — round-3 C3:
        the May 4 incident was only diagnosed by ad-hoc curl, and a
        bare `<ticker>` template recreated that friction.

        Throttled like the breakdown query. Returns empty list on
        failure (caller renders gracefully). The query is non-indexed
        (LIKE on PK + filters), but only runs in the THRESHOLD branch
        of the alert path (not on BAIL FLOOD), so it does not add
        per-tick load during WS-cache-drift incidents.
        """
        now = time.time()
        last = getattr(
            self,
            "_silence_threshold_tickers_last_ts",
            0.0)
        cached = getattr(
            self,
            "_silence_threshold_tickers_last_list",
            [])
        if now - last < self._BAIL_QUERY_THROTTLE_SECONDS:
            return cached
        cutoff_dt = (datetime.datetime.now(timezone.utc)
                     - datetime.timedelta(
                         seconds=self._SILENCE_AGE_THRESHOLD_SECONDS))
        cutoff_iso = (
            cutoff_dt.isoformat(timespec="microseconds")
            .replace("+00:00", "Z"))
        # Round-5 C1: filter from `_THRESHOLD_SHAPE_BAIL_REASONS`
        # rather than the literal `'threshold_unparsable'`. If a future
        # maintainer adds a 2nd member to the bucket (e.g.,
        # `tradeable_false_15m`), the bucket-derived header label and
        # n_thr count both surface it — but the SQL filter would have
        # silently kept showing tickers from the original reason only,
        # masking a fraction of the real affected tickers. Now SQL
        # parallels the breakdown query's `IN ({placeholders})` shape.
        thr_reasons = tuple(sorted(self._THRESHOLD_SHAPE_BAIL_REASONS))
        thr_placeholders = ",".join("?" * len(thr_reasons))
        try:
            rows = self._state.conn.execute(
                "SELECT ticker, MAX(rejection_time) AS most_recent "
                "FROM rejected_opportunities "
                "WHERE ticker LIKE 'KX%15M%' "
                f"  AND rejection_reason IN ({thr_placeholders}) "
                "  AND rejection_time >= ? "
                "GROUP BY ticker "
                "ORDER BY most_recent DESC "
                "LIMIT ?",
                (*thr_reasons, cutoff_iso, limit),
            ).fetchall()
        except Exception:
            # Round-4 C1: failure path now warn-logs once (rate-limited
            # via own flag). The earlier rationale ("breakdown query's
            # warning covers it") was wrong: this query has a different
            # shape (GROUP BY + ORDER BY + LIMIT) and can fail
            # independently — e.g., a corrupted index on `ticker` or a
            # SQLite version regression on the GROUP+ORDER combination.
            # Without its own warn-flag, the operator gets a THRESHOLD
            # alert with empty `Affected tickers` and no journal
            # evidence of the underlying failure.
            if not getattr(
                    self,
                    "_silence_watchdog_warned_tickers",
                    False):
                logging.warning(
                    "silent_15m threshold-tickers query failed",
                    exc_info=True)
                self._silence_watchdog_warned_tickers = True
            # Preserve last-good list to avoid the same dual-alert
            # pathology fixed for the breakdown cache (round-3 C1/C5).
            if not hasattr(
                    self, "_silence_threshold_tickers_last_list"):
                self._silence_threshold_tickers_last_list = []
            self._silence_threshold_tickers_last_ts = now
            return self._silence_threshold_tickers_last_list
        # Reset warn-flag on success (round-4 C1 follow-up to round-3 C9).
        if getattr(
                self, "_silence_watchdog_warned_tickers", False):
            self._silence_watchdog_warned_tickers = False
        tickers = [row[0] for row in rows]
        self._silence_threshold_tickers_last_list = tickers
        self._silence_threshold_tickers_last_ts = now
        return tickers

    def _check_15m_silence_alert(self, active_windows: List[Dict]) -> None:
        """Alert via Telegram if no productive 15M evaluation has been
        produced in the last 10 minutes. Observation-only — doesn't
        touch trading state. Self-throttled (dedup_key) so it can fire
        every scan tick without spamming. See
        kb/failures/ws-15m-silence-2026-04-24.md and
        kb/failures/ws-cache-drift-silent-scan-2026-04-24.md.

        Scope: rows in `evaluated_opportunities` (any filter_stage)
        OR `rejected_opportunities` (any reason EXCEPT
        `_BAIL_REJECTION_REASONS`), filtered by ticker LIKE 'KX%15M%'.
        Healthy rejections (low_probability_15m, edge_too_low,
        tradeable_false, price_out_of_range_early, etc.) count as
        "scan alive". Bail reasons (no_orderbook, no_best_ask,
        threshold_unparsable) do NOT — they are emitted in the exact
        silent-bail paths whose recurrence this watchdog is the
        last line of defense for.

        Known gap (NOT addressed here): a calibration-engine collapse
        that returns cal_prob≈0 for every market would cause
        low_probability_15m rejections to fire for every ticker, and
        this watchdog would stay silent. That failure mode requires a
        separate model-output sanity check — out of scope for a
        liveness watchdog.

        Startup false-positive guard: the "last eval" timestamp persists
        across bot restarts, so right after restart it will look ancient.
        We do NOT fire unless THIS bot process has been running long
        enough (SILENCE_ALERT_MIN_UPTIME_SECONDS = 900 = 15 min) that it
        could plausibly have produced an eval. Otherwise a restart during
        a quiet window would spam the Telegram with a stale-looking age.

        Kalshi catalog-gap guard: Kalshi's /events?status=open returns
        only the currently-trading 15M window per asset, and there is
        often a 10–30 min gap between when one window closes and the next
        becomes "open". `discover_active_windows()` correctly drops the
        expired window (seconds_to_close < 0), leaving the scanner with
        zero 15M windows. This is upstream sparseness, not a bot fault.
        We log it at INFO level (KALSHI_15M_CATALOG_GAP) but don't fire
        the loud Telegram alert. Apr 24 2026: 11 such gaps in 24h, all
        false positives. (kb/failures/kalshi-15m-catalog-gap-2026-04-24.md)
        """
        if not hasattr(self, "_silence_alert_process_start_ts"):
            self._silence_alert_process_start_ts = time.time()
        uptime = time.time() - self._silence_alert_process_start_ts
        if uptime < self._SILENCE_ALERT_MIN_UPTIME_SECONDS:
            return
        n_15m_windows = sum(
            1 for w in (active_windows or [])
            if w.get("product_type") == "15m"
        )
        ws_connected = (
            self._kalshi_feed.is_connected
            if self._kalshi_feed else False)

        # Step 1: primary "scan alive" check. Tuple (ok, ts).
        primary_ok, last_ts_str = self._query_last_15m_alive_ts()
        if not primary_ok:
            # Query failed; warning logged once. Bail this tick.
            return

        # Step 2: compute primary staleness. Single-expression form
        # eliminates the multi-branch hazard (R7 [A1] crashed on
        # unparseable ts because age_sec/last_ts could be undefined
        # when reaching the alert message). Now: parse failure is
        # treated equivalently to None — last_ts stays None,
        # age_sec stays None, both truth-tested explicitly downstream.
        last_ts = None
        age_sec = None
        if last_ts_str is not None:
            try:
                last_ts = datetime.datetime.fromisoformat(
                    last_ts_str.replace("Z", "+00:00"))
                age_sec = (datetime.datetime.now(timezone.utc)
                           - last_ts).total_seconds()
            except (ValueError, AttributeError):
                last_ts = None
                age_sec = None
        is_primary_stale = (
            age_sec is None
            or age_sec >= self._SILENCE_AGE_THRESHOLD_SECONDS)

        if not is_primary_stale:
            # Healthy 15M activity within threshold — silent.
            return

        # Primary is stale or absent. Decide: bail signal or generic
        # silence?
        bail_count = self._query_recent_bail_count()

        if n_15m_windows == 0:
            # Catalog gap — log INFO, no Telegram. Note the bail rows
            # (if any) are stale leftovers from before the gap, not
            # caused by the gap itself; the gap merely masks them
            # diagnostically. R6 [A5] log message clarification.
            ts_repr = (last_ts_str
                       if last_ts_str is not None else "<none>")
            logging.info(
                "KALSHI_15M_CATALOG_GAP: 0 active 15M windows from "
                "Kalshi (upstream catalog gap, not bot fault). "
                "last=%s, recent_bail_rows=%d",
                ts_repr, bail_count)
            return

        if bail_count >= self._BAIL_MIN_ROWS_WHEN_STALE:
            # Bail signal alongside primary staleness — preferred
            # diagnostic. Single transient blips that happen to
            # coincide with a quiet-market staleness boundary stay
            # under threshold and fall through to generic SILENT
            # (correct: not a cache-drift signature). The 04-24
            # WS-cache-drift shape produces hundreds of rows, far
            # above threshold; the dedup-bounded threshold_unparsable
            # case produces ≤4 rows, also above threshold of 3.
            #
            # May 4 2026: alert text was hardcoded to "WS-cache-drift
            # recurrence" but live incident was Kalshi-side TBD strikes
            # (threshold_unparsable only, zero WS rows). Operator was
            # nearly sent to wrong KB doc + would have done a useless
            # restart. Routing now branches on dominant bail-reason
            # shape via `_query_recent_bail_breakdown`. See
            # kb/failures/threshold-tbd-stuck-may04.md.
            #
            # Adversarial review summary (rounds 2-7, see inline
            # comments below + per-method docstrings for per-fix
            # rationale, plus tests/test_15m_silence_alert.py
            # ::TestBailFloodMessageDifferentiation):
            # - R2: single source of truth — breakdown drives routing
            #   AND display; "(breakdown unavailable)" annotation
            #   when query empty; alphabetical breakdown sort.
            # - R3: ratio loosened 4→3 (`_THRESHOLD_SHAPE_DOMINANCE_RATIO`)
            #   to 75% so 3-ticker incident + 1 stray WS blip routes
            #   correctly; affected-tickers embed in THRESHOLD message;
            #   bucket-derived header label; last-good cache preserved
            #   on transient breakdown failure.
            # - R4: warn-once flag for breakdown query failure (mirror
            #   count query); empty-affected probe falls back to a
            #   next-step DB query (no bare `<ticker>` placeholder);
            #   partition assertion moved from class-body to test-only
            #   to avoid systemd backoff on developer mistake.
            # - R5: in-bot tickers SQL filter parameter-bound from
            #   `_THRESHOLD_SHAPE_BAIL_REASONS` via `IN (?,?,...)`;
            #   warn-once flag for tickers query failure.
            # - R6: operator-facing fallback SQL also bucket-derived
            #   via `IN ('a','b',...)`.
            # - R7: operator-facing window cutoff derived from
            #   `_SILENCE_AGE_THRESHOLD_SECONDS` (no `-10 minutes`
            #   literal drift).
            breakdown = self._query_recent_bail_breakdown()
            if breakdown:
                n_thr = sum(breakdown.get(r, 0)
                            for r in self._THRESHOLD_SHAPE_BAIL_REASONS)
                n_ws = sum(breakdown.get(r, 0)
                           for r in self._WS_SHAPE_BAIL_REASONS)
                display_total = n_thr + n_ws
                breakdown_str = self._format_bail_breakdown(breakdown)
                # Ratio-based dominance: threshold count >=
                # `_THRESHOLD_SHAPE_DOMINANCE_RATIO` × WS count AND at
                # least the bail threshold. Constant currently 3 (≥75%
                # threshold); see its definition for rationale and
                # boundary worked examples (round-3 C4 loosened from
                # 4 to 3 to fix asymmetric routing at small n).
                # Boundary pinned by TestBailFloodMessageDifferentiation.
                is_threshold_dominant = (
                    n_thr >= self._BAIL_MIN_ROWS_WHEN_STALE
                    and n_thr >= (
                        self._THRESHOLD_SHAPE_DOMINANCE_RATIO * n_ws))
            else:
                # Breakdown query failed or returned empty. Cannot
                # route by shape — default to BAIL FLOOD with explicit
                # annotation so operator can see the gap rather than
                # be misled by zero-everywhere. Gate already admitted
                # us via cached count, which we surface as display
                # total even though it may be from an older throttle
                # window than the (failed) breakdown query.
                n_thr = 0
                n_ws = 0
                display_total = bail_count
                breakdown_str = "(breakdown unavailable)"
                is_threshold_dominant = False

            if is_threshold_dominant:
                # Round-3 C3: embed actual affected tickers (most
                # recent up to 4) so the operator can paste-and-go
                # with the curl probe without a separate DB query.
                # Round-4 C2+C7: when affected is empty (tickers
                # query failed or cache stale-empty), DO NOT emit
                # the bare `<ticker>` placeholder URL — that
                # recreates exactly the friction the embed was
                # meant to remove. Replace the Probe: line with a
                # next-step DB query so the operator has actionable
                # guidance regardless.
                affected = self._query_recent_threshold_unparsable_tickers(
                    limit=4)
                logging.error(
                    "SILENT_15M_THRESHOLD_UNPARSABLE: primary stale + "
                    "%d threshold rows (ws=%d) in last %d min; "
                    "uptime=%.1fmin (%s); affected=%s",
                    n_thr, n_ws,
                    self._SILENCE_AGE_THRESHOLD_SECONDS // 60,
                    uptime / 60, breakdown_str,
                    (", ".join(affected) if affected
                     else "<empty>"))
                # Header label is derived from the bucket so a future
                # add to `_THRESHOLD_SHAPE_BAIL_REASONS` is reflected
                # without a parallel literal edit (round-3 C2).
                # Round-8 C1: each reason wrapped in backticks so
                # word-internal underscores cannot be misinterpreted
                # as italic markers by Telegram's legacy Markdown
                # parser (would fail HTTP 400, alert never delivered).
                threshold_label = ", ".join(
                    f"`{r}`"
                    for r in sorted(
                        self._THRESHOLD_SHAPE_BAIL_REASONS))
                # Build the affected-tickers + probe sections
                # conditionally on whether tickers are populated.
                if affected:
                    affected_line = (
                        f"Affected tickers: {', '.join(affected)}\n")
                    # Round-8 C1: wrap `floor_strike` field name in
                    # backticks so its word-internal underscore is
                    # neutralized for Markdown parsing. Tickers
                    # themselves are hyphen-only so don't need wrapping.
                    probe_line = (
                        f"Probe: curl https://api.elections.kalshi.com/"
                        f"trade-api/v2/markets/{affected[0]}\n"
                        f"  → if `floor_strike=null` + sub=\"TBD\": "
                        f"wait, Kalshi will populate\n"
                        f"  → if `floor_strike` populated but parser "
                        f"fails: code fix required\n")
                else:
                    # Round-6 C2: operator-facing fallback SQL must
                    # also derive its `IN (...)` clause from the
                    # bucket. Earlier draft hardcoded
                    # `rejection_reason='threshold_unparsable'`,
                    # which would silently mask half the affected
                    # tickers if `_THRESHOLD_SHAPE_BAIL_REASONS`
                    # gained a 2nd member — exactly the drift round-5
                    # C1 fixed in the bot's own SQL.
                    #
                    # Round-7 C1: window cutoff also derives from
                    # `_SILENCE_AGE_THRESHOLD_SECONDS` rather than a
                    # literal `'-10 minutes'`. Same drift class —
                    # if the constant is tuned, the operator's
                    # copy-paste SQL would otherwise reference a
                    # different window than the bot, producing
                    # tickers from a different incident.
                    #
                    # Round-7 C2 (degenerate-case note): if the bucket
                    # is empty, `operator_thr_in` is "" and the
                    # rendered SQL becomes `IN ()` (invalid). This is
                    # unreachable in practice because `n_thr` would be
                    # 0 → `is_threshold_dominant=False` → routes to
                    # BAIL FLOOD, never entering this branch. Bucket
                    # members are also Python module-level constants,
                    # so they cannot contain SQL-special characters
                    # (apostrophe, comma) that would corrupt the
                    # interpolated literal.
                    operator_thr_in = ", ".join(
                        f"'{r}'"
                        for r in sorted(
                            self._THRESHOLD_SHAPE_BAIL_REASONS))
                    # Round-8 C2: render the SQL window in seconds
                    # (full precision) rather than `// 60` minutes
                    # which was lossy for non-multiple-of-60 values
                    # of `_SILENCE_AGE_THRESHOLD_SECONDS`. SQLite
                    # supports the `seconds` modifier; using it
                    # eliminates the bot-vs-operator window drift
                    # that integer-floor minutes introduced.
                    window_seconds = (
                        self._SILENCE_AGE_THRESHOLD_SECONDS)
                    affected_line = (
                        "Affected tickers: (cache empty — query "
                        "`rejected_opportunities` for live list)\n")
                    # Round-8 C1: wrap the SQL in a backtick code span
                    # so identifiers like `rejection_reason` and
                    # `rejected_opportunities` (containing word-internal
                    # underscores) cannot trigger Markdown parser quirks.
                    probe_line = (
                        f"Probe: `SELECT ticker FROM "
                        f"rejected_opportunities WHERE "
                        f"rejection_reason IN ({operator_thr_in}) "
                        f"AND rejection_time >= datetime('now',"
                        f"'-{window_seconds} seconds')` — then curl "
                        f"Kalshi REST for that ticker.\n")
                msg = (
                    f"\U0001f6a8 *15M SCAN SILENT (THRESHOLD "
                    f"UNPARSABLE)*\n"
                    f"Primary stale + {n_thr} {threshold_label} "
                    f"rows in last "
                    f"{self._SILENCE_AGE_THRESHOLD_SECONDS // 60} "
                    f"min.\n"
                    f"Breakdown: {breakdown_str}\n"
                    f"{affected_line}"
                    f"Bot uptime: {uptime/60:.1f} min\n"
                    f"WS connected: {ws_connected}\n"
                    f"Likely: Kalshi delayed publishing strike "
                    f"(\"Target price: TBD\") OR renamed strike "
                    f"fields (parser regression).\n"
                    f"{probe_line}"
                    f"Restart will NOT help.")
                dedup_key = "silent_15m_threshold_unparsable_alert"
            else:
                logging.error(
                    "SILENT_15M_BAIL_FLOOD: primary stale + %d "
                    "silent-bail rejection rows in last %d min; "
                    "uptime=%.1fmin (%s)",
                    display_total,
                    self._SILENCE_AGE_THRESHOLD_SECONDS // 60,
                    uptime / 60, breakdown_str)
                msg = (
                    f"\U0001f6a8 *15M SCAN SILENT (BAIL FLOOD)*\n"
                    f"Primary stale + {display_total} silent-bail "
                    f"rejection rows in last "
                    f"{self._SILENCE_AGE_THRESHOLD_SECONDS // 60} "
                    f"min.\n"
                    f"Breakdown: {breakdown_str}\n"
                    f"Bot uptime: {uptime/60:.1f} min\n"
                    f"WS connected: {ws_connected}\n"
                    f"Likely: WS-cache-drift recurrence — see "
                    f"kb/failures/ws-cache-drift-silent-scan-"
                    f"2026-04-24.md")
                dedup_key = "silent_15m_bail_flood_alert"

            if _telegram_state._TELEGRAM:
                try:
                    _telegram_state._TELEGRAM.send(msg, dedup_key=dedup_key)
                except Exception:
                    logging.debug(
                        "silent_15m bail-flood telegram send failed "
                        "(dedup_key=%s)", dedup_key, exc_info=True)
            return

        # Apr 26 incident #2: heartbeat-based aliveness check. The
        # primary query is DB-row-based, but the `_eval_opp_seen`
        # dedup at insert sites (e.g., per-(ticker, filter_stage)
        # for `low_probability_15m`) suppresses subsequent writes
        # once a tuple has been seen. After 10+ min of unchanged
        # scan outcomes (e.g., dedup-quiet market post-window-
        # rotation), the DB shows stale primary timestamps while
        # scan body is still iterating windows on every tick.
        #
        # Wall-clock dependency: heartbeat is set via `time.time()`
        # at the per-window iteration site. NTP step corrections
        # (Apr 26 incident logged 13.8s drift) shift heartbeat_age
        # by the same magnitude — small relative to the 600s
        # threshold but worth knowing when debugging.
        #
        # `_scan_15m_iter_heartbeat_ts` (set on MainLoop in bot/main_loop.py
        # every 15M window iteration; init=0.0 at MainLoop.__init__ —
        # search anchors: `_scan_15m_iter_heartbeat_ts = time.monotonic()`
        # and `self._scan_15m_iter_heartbeat_ts = 0.0`) decouples
        # "scan is alive" from "DB rows are appearing" — same fix
        # `c1c2096` applied to the productive (2.5-min) watchdog
        # per kb/failures/scan-tick-stall-cluster-2026-04-25.md.
        #
        # Bail-flood detection runs BEFORE this gate, so a real
        # silent-bail recurrence (which writes bail-rejection rows
        # bypassing the healthy-rejection dedup) still fires its
        # diagnostic alert regardless of heartbeat freshness.
        heartbeat_ts = getattr(self, "_scan_15m_iter_heartbeat_ts", 0.0)
        if heartbeat_ts > 0.0:
            heartbeat_age = time.time() - heartbeat_ts
            if heartbeat_age < self._SILENCE_AGE_THRESHOLD_SECONDS:
                # Scan body iterated a 15M window within the
                # staleness window — alive. Dedup may be hiding
                # repetitive outcomes, but that's exactly what dedup
                # is for. Silent-skip the SILENT alert.
                return
        # Generic silence path: primary stale, bail count below
        # threshold, AND heartbeat stale (or never set). Guard
        # against `last_ts is None` (no data) AND against
        # `last_ts_str is not None but unparseable` (data quality
        # blip) — in either case we lack the timestamp/age to render
        # the diagnostic message, so silently bail. (Fresh bot /
        # unparseable ts shouldn't fire a SILENT alert with bogus
        # content.)
        if last_ts is None or age_sec is None:
            return
        msg = (
            f"\U0001f6a8 *15M SCAN SILENT*\n"
            f"No 15M evaluation in {age_sec/60:.1f} min.\n"
            f"Last eval: {last_ts.isoformat(timespec='seconds')}Z\n"
            f"Bot uptime: {uptime/60:.1f} min\n"
            f"WS connected: {ws_connected}\n"
            f"Check logs for WS_SILENCE_WATCHDOG or check Kalshi status."
        )
        logging.error(
            "SILENT_15M: %.1f min since last 15M eval "
            "(last=%s uptime=%.1fmin)",
            age_sec / 60, last_ts_str, uptime / 60)
        if _telegram_state._TELEGRAM:
            try:
                _telegram_state._TELEGRAM.send(msg, dedup_key="silent_15m_alert")
            except Exception:
                logging.debug(
                    "silent_15m telegram send failed", exc_info=True)

    def _check_scan_productive_15m(
        self, active_windows: List[Dict], tick_start_ts: str) -> None:
        """Fire a Telegram alert when 15M scan runs but produces zero DB
        rows for N consecutive ticks. Complementary to
        `_check_15m_silence_alert` (which catches >10-min silences after
        the fact). This one detects the 2026-04-24 22:12 UTC failure
        shape in ~2.5 min instead of 10.

        Logic:
        - If no `product_type='15m'` in active_windows: reset counter and
          skip. Catalog gaps (upstream Kalshi sparseness) aren't a
          productivity issue.
        - If bot uptime < SCAN_UNPRODUCTIVE_MIN_UPTIME_SECONDS: skip
          entirely (and don't increment). RK warmup can legitimately
          produce nothing for ~5 min post-restart.
        - Query eval + rejected tables for 15m rows written
          since `tick_start_ts`. If any: reset counter. If zero:
          increment and alert when >= SCAN_UNPRODUCTIVE_THRESHOLD.

        See kb/failures/ws-cache-drift-silent-scan-2026-04-24.md fix #3.
        """
        SCAN_UNPRODUCTIVE_THRESHOLD = 5         # consecutive ticks
        SCAN_UNPRODUCTIVE_MIN_UPTIME_SECONDS = 420  # 7 min — RK warmup + buffer
        # Was 300 (5 min); 5 min boundary fired false positives at uptime
        # 5.1 min on slow restarts where RK warmup ran 3m 28s+ and the
        # first productive tick landed seconds after grace expired.
        # 7 min gives ~3.5 min buffer over observed warmup ceiling.

        # Snapshot the 15M subset once — `active_windows` may be
        # ref-swapped by the refresh worker mid-call, and the two
        # downstream counts must be consistent (R-review [A3]).
        _15m_windows = [
            w for w in active_windows
            if w.get("product_type") == "15m"
        ]
        n_15m = len(_15m_windows)
        if n_15m == 0:
            # No 15M windows available — reset counter so we don't carry
            # stale state into the next live window. Also clear alert
            # state so a renewed stuck-period after the gap re-fires
            # entry/reconnect; do NOT fire a recovery telegram (the
            # underlying bug is masked by the gap, not actually
            # resolved). Adversarial review [A1].
            self._scan_15m_unproductive_count = 0
            self._scan_15m_unproductive_entry_alerted = False
            self._scan_15m_unproductive_max_count = 0
            return

        # F/U 6 (Apr 26): rotation-boundary false-positive guard. At
        # rotation moments (every :00/:15/:30/:45), cached active_windows
        # briefly holds settled 15M windows with STC<0 between the
        # rotation and the next 30s `_refresh_active_windows()` call.
        # scan()'s time-range filter (line 9863-9866) `min_seconds_before_close
        # <= stc <= max_seconds_before_close` drops them all → for-loop
        # iterates non-15M only → 15M heartbeat at line 9921 (gated to
        # `_pt in (None, '15m')`) never fires → 5 silent ticks → false alert.
        # Apr 26 14:30:00 UTC log evidence (commit 159b411 instrumentation):
        #   F_U6_15M_TIME_FILTER_DROPPED_ALL: 4 15M windows ALL filtered
        #   by time range — details=[BTC=-0.0s, ETH=-0.0s, SOL=-0.0s,
        #   XRP=-0.0s]
        # Fix: extend the catalog-gap branch to also short-circuit when
        # 15M is in active_windows but ALL are time-INELIGIBLE. The
        # F_U6_15M_TIME_FILTER_DROPPED_ALL diagnostic at scan() (commit
        # 159b411) preserves observability for any non-rotation case.
        # Note: range bound MAX_SECONDS_BEFORE_CLOSE must mirror the
        # scan() time filter for 15M product. If a future per-product
        # split changes scan()'s 15M max, update here too.
        # See kb/failures/15m-scan-unproductive-rotation-2026-04-26.md.
        def _stc_in_15m_range(w):
            stc = w.get("seconds_to_close")
            if stc is None:
                # Missing STC → don't suppress the alert (R-review [A2]:
                # alert-bias is safer than silence-bias when data is
                # incomplete).
                return True
            try:
                stc_f = float(stc)
            except (TypeError, ValueError):
                # Non-numeric (R-review [A7] defensiveness) — same as None.
                return True
            return 0 <= stc_f <= MAX_SECONDS_BEFORE_CLOSE
        n_15m_time_eligible = sum(1 for w in _15m_windows if _stc_in_15m_range(w))
        if n_15m_time_eligible == 0:
            # All 15M markets currently outside the trading window —
            # benign rotation transition or catalog edge. Same code
            # path as the n_15m == 0 branch above. Adversarial
            # review [A1]: clear alert state to prevent stranding
            # entry-alerted across the gap; don't fire recovery
            # (benign mask, not real recovery).
            self._scan_15m_unproductive_count = 0
            self._scan_15m_unproductive_entry_alerted = False
            self._scan_15m_unproductive_max_count = 0
            return

        if not hasattr(self, "_scan_15m_process_start_ts"):
            self._scan_15m_process_start_ts = time.time()
        uptime = time.time() - self._scan_15m_process_start_ts
        if uptime < SCAN_UNPRODUCTIVE_MIN_UPTIME_SECONDS:
            return

        # Heartbeat-based productivity check — true when scan() actually
        # iterated a 15M window body since `tick_start_ts`. Decoupled from
        # DB row counts because the dedup at insert sites can suppress
        # writes for a window's entire 15-min lifetime once each
        # (ticker, stage) is seen, producing watchdog false positives
        # even when scan is healthy. (Apr 24 23:53 UTC false positive.)
        #
        # Phase 3 R-review A2 caveat: this OR-logic depends on Phase 1
        # (commit 27196b4 + AST audit task #46) ensuring every
        # silent-bail scan path writes a rejection row. If a future code
        # change introduces a NEW silent-continue without a rejection
        # write, heartbeat-recent could fire while rows_written=0 — the
        # exact pathology Phase 3 targets, but the productivity check
        # would falsely reset the counter. The AST audit test_scan_no_silent_continue.py
        # is the regression guard against that gap reappearing.
        try:
            tick_start_dt = datetime.datetime.strptime(
                tick_start_ts.replace("Z", "+00:00"),
                "%Y-%m-%dT%H:%M:%S.%f%z")
            tick_start_epoch = tick_start_dt.timestamp()
        except Exception:
            tick_start_epoch = 0.0
        heartbeat_recent = (
            self._scan_15m_iter_heartbeat_ts > tick_start_epoch)
        # Fallback: also check DB rows for backward-compat with the
        # original intent. Either signal indicates productive scan.
        try:
            row = self._state.conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM evaluated_opportunities "
                " WHERE product_type='15m' AND evaluation_time > ?) + "
                "(SELECT COUNT(*) FROM rejected_opportunities "
                " WHERE product_type='15m' AND rejection_time > ?)",
                (tick_start_ts, tick_start_ts)
            ).fetchone()
        except Exception:
            return
        # Phase 3 R-review A1: detect WS disconnect→reconnect
        # transition. If WS just came back from a disconnected
        # state, the counter accumulated during the dead window
        # and would immediately trip R2 (reconnect-bomb). Reset
        # counter on transition; counter + alerts continue
        # accumulating during a sustained disconnect (operator
        # still wants to know via Telegram) but R1/R2 recovery
        # actions are gated below on WS being live.
        kf_check = getattr(self, "_kalshi_feed", None) or getattr(
            self, "kalshi_feed", None)
        ws_connected = (
            kf_check is not None
            and getattr(kf_check, "is_connected", False))
        prev_ws_connected = getattr(
            self, "_scan_15m_prev_ws_connected", True)
        self._scan_15m_prev_ws_connected = ws_connected
        if not prev_ws_connected and ws_connected:
            # Just reconnected. If a stuck-period had emitted an entry
            # alert, fire recovery NOW before clearing state so the
            # operator sees closure (and the peak count, which would
            # otherwise be clobbered by the reset below). Adversarial
            # review [A3][A5].
            if getattr(
                    self, "_scan_15m_unproductive_entry_alerted", False):
                peak = getattr(
                    self, "_scan_15m_unproductive_max_count", 0)
                logging.warning(
                    "SCAN_UNPRODUCTIVE_15M_RECOVERED (ws_reconnect): "
                    "peak=%d", peak)
                if _telegram_state._TELEGRAM:
                    try:
                        _telegram_state._TELEGRAM.send(
                            f"✅ *15M SCAN RECOVERED*\n"
                            f"Stuck period ended (WS reconnect) after "
                            f"peak {peak} consecutive unproductive ticks.")
                    except Exception:
                        logging.debug(
                            "scan_unproductive_15m recovery telegram "
                            "(ws) failed", exc_info=True)
            # Start fresh. Reset alert state too so a renewed burn
            # after reconnect re-fires entry/reconnect rather than
            # being silently absorbed by stale flags.
            self._scan_15m_unproductive_count = 0
            self._scan_15m_last_recovery_ts = 0.0
            self._scan_15m_reconnect_triggered = False
            self._scan_15m_unproductive_entry_alerted = False
            self._scan_15m_unproductive_max_count = 0

        rows_written = row[0] if row else 0
        if heartbeat_recent or rows_written > 0:
            # Productive tick — reset detection counter AND Phase 3
            # auto-recovery state (throttle + one-shot reconnect flag)
            # so the next stuck period gets fresh recovery cadence.
            #
            # If we previously fired an entry alert, send a recovery
            # telegram naming the peak count so the operator can size
            # the event without grepping journal. (Apr 27 2026
            # incident: 4 escalating telegrams over 3 min were noise;
            # entry + recovery is the right pair.)
            if getattr(
                    self, "_scan_15m_unproductive_entry_alerted", False):
                peak = getattr(
                    self, "_scan_15m_unproductive_max_count",
                    self._scan_15m_unproductive_count)
                logging.warning(
                    "SCAN_UNPRODUCTIVE_15M_RECOVERED: peak=%d", peak)
                if _telegram_state._TELEGRAM:
                    # No dedup_key: state-based flags already prevent
                    # duplicates; the TelegramNotifier 60s TTL would
                    # silently drop a back-to-back recovery within the
                    # same minute. Adversarial review [A4].
                    try:
                        _telegram_state._TELEGRAM.send(
                            f"✅ *15M SCAN RECOVERED*\n"
                            f"Stuck period ended after peak "
                            f"{peak} consecutive unproductive ticks.")
                    except Exception:
                        logging.debug(
                            "scan_unproductive_15m recovery telegram "
                            "failed", exc_info=True)
            self._scan_15m_unproductive_count = 0
            self._scan_15m_last_recovery_ts = 0.0
            self._scan_15m_reconnect_triggered = False
            self._scan_15m_unproductive_entry_alerted = False
            self._scan_15m_unproductive_max_count = 0
            return

        self._scan_15m_unproductive_count = getattr(
            self, "_scan_15m_unproductive_count", 0) + 1
        # Peak tracking so recovery telegram can size the burn.
        self._scan_15m_unproductive_max_count = max(
            getattr(self, "_scan_15m_unproductive_max_count", 0),
            self._scan_15m_unproductive_count)
        if self._scan_15m_unproductive_count < SCAN_UNPRODUCTIVE_THRESHOLD:
            return

        # Always log every above-threshold tick — journal is the
        # ground truth, even though the Telegram is state-deduped.
        logging.error(
            "SCAN_UNPRODUCTIVE_15M: %d consecutive ticks with %d active "
            "15M window(s) but zero DB rows written since %s",
            self._scan_15m_unproductive_count, n_15m, tick_start_ts)
        # State-based Telegram dedup: one entry alert per stuck-period.
        # Reconnect-threshold escalation alert fires later (in the R2
        # block below). Apr 27 2026 incident shipped 4 telegrams over
        # 3 min; this caps the entry path at 1.
        if _telegram_state._TELEGRAM and not getattr(
                self, "_scan_15m_unproductive_entry_alerted", False):
            self._scan_15m_unproductive_entry_alerted = True
            msg = (
                f"\U0001f6a8 *15M SCAN UNPRODUCTIVE*\n"
                f"{self._scan_15m_unproductive_count} consecutive ticks "
                f"with {n_15m} 15M window(s) produced zero DB rows.\n"
                f"Bot uptime: {uptime/60:.1f} min\n"
                f"Scan may be silent-bailing — see "
                f"ws-cache-drift-silent-scan-2026-04-24.md"
            )
            try:
                # No dedup_key: state flag is the dedup. TelegramNotifier
                # 60s TTL would defeat back-to-back stuck-periods.
                _telegram_state._TELEGRAM.send(msg)
            except Exception:
                logging.debug(
                    "scan_unproductive_15m entry telegram failed",
                    exc_info=True)

        # Phase 3 R1 — auto-recovery: force_resubscribe every active
        # 15M ticker. purge_cache=True is the real reset (drops
        # phantom WS state); track_recovery=True so B2 watchdog can
        # surface stuck tickers. Throttled to once per
        # SCAN_UNPRODUCTIVE_RECOVERY_THROTTLE_S (60s) so we don't
        # hammer the WS during a multi-tick burn.
        SCAN_UNPRODUCTIVE_RECOVERY_THROTTLE_S = 60.0
        SCAN_UNPRODUCTIVE_RECONNECT_THRESHOLD = 10  # ~5 min
        now = time.time()
        # Use kf_check (already resolved above for transition detection).
        kf = kf_check
        if not ws_connected:
            return  # no WS to recover; counter+alert already fired
        last_rec = getattr(self, "_scan_15m_last_recovery_ts", 0.0)
        if now - last_rec >= SCAN_UNPRODUCTIVE_RECOVERY_THROTTLE_S:
            self._scan_15m_last_recovery_ts = now
            _15m_tickers = []
            for w in active_windows:
                if w.get("product_type") != "15m":
                    continue
                for mkt in w.get("markets") or []:
                    t = mkt.get("ticker", "")
                    if t:
                        _15m_tickers.append(t)
            for t in _15m_tickers:
                try:
                    kf.force_resubscribe(
                        t, purge_cache=True, track_recovery=True)
                except Exception:
                    logging.debug(
                        "force_resubscribe in scan-recovery failed: %s",
                        t, exc_info=True)
            logging.warning(
                "SCAN_UNPRODUCTIVE_15M_RECOVERY_R1: force_resubscribed "
                "%d 15M tickers (purge=True, track=True) at %d "
                "consecutive unproductive ticks",
                len(_15m_tickers),
                self._scan_15m_unproductive_count)

        # Phase 3 R2 — escalation: if STILL unproductive at 10
        # consecutive ticks (~5 min), set _force_reconnect_requested
        # so silence watchdog forces a fresh WS session. One-shot
        # per stuck-period (cleared on next productive tick).
        if (self._scan_15m_unproductive_count
                >= SCAN_UNPRODUCTIVE_RECONNECT_THRESHOLD
                and not getattr(
                    self, "_scan_15m_reconnect_triggered", False)):
            self._scan_15m_reconnect_triggered = True
            try:
                kf._force_reconnect_requested = True
            except Exception:
                logging.warning(
                    "Failed to set _force_reconnect_requested",
                    exc_info=True)
            logging.error(
                "SCAN_UNPRODUCTIVE_15M_RECOVERY_R2: requesting WS "
                "reconnect at %d consecutive unproductive ticks "
                "(R1 force_resubscribe didn't recover)",
                self._scan_15m_unproductive_count)
            # Surface the escalation to the operator. One-shot per
            # stuck-period via the same flag that gates the action,
            # so the Telegram and the action stay in lockstep.
            if _telegram_state._TELEGRAM:
                try:
                    # No dedup_key: gated by `_reconnect_triggered`
                    # flag (one-shot per stuck-period).
                    _telegram_state._TELEGRAM.send(
                        f"\U0001f6a8 *15M SCAN STILL UNPRODUCTIVE*\n"
                        f"{self._scan_15m_unproductive_count} "
                        f"consecutive ticks — forcing WS reconnect.\n"
                        f"R1 force_resubscribe didn't recover.")
                except Exception:
                    logging.debug(
                        "scan_unproductive_15m reconnect telegram "
                        "failed", exc_info=True)

    def _drift_probe_tick(self) -> None:
        """Once per minute, diff REST orderbook vs WS cache for a random
        subscribed 15M ticker. Logs WS_DRIFT_PROBE summary per side.

        Purpose: empirically measure the cache-vs-truth gap that produces
        WS delta underflow warnings. Pre-existing hypothesis (H-NEW): WS
        snapshot at subscribe time is truncated, so our cache is missing
        deep/stale levels. REST with depth=100 returns the full book at
        request time (no truncation), so REST-WS diff quantifies the miss.

        Observation-only. Does NOT mutate WS cache state. Cadence: 60s.
        Remove after hypothesis confirmed/rejected.
        """
        now = time.time()
        if now - self._drift_probe_last_run < 60:
            return
        self._drift_probe_last_run = now

        if not (self._kalshi_feed and self._kalshi_feed.is_connected):
            return

        # Pick a random subscribed 15M ticker with a non-empty WS cache.
        try:
            all_obs = self._kalshi_feed.get_all_orderbooks()
        except Exception:
            return
        candidates = [
            t for t, ob in all_obs.items()
            if "15M" in t.upper()
            and (ob.get("yes") or ob.get("no"))  # skip empty books
        ]
        if not candidates:
            return

        ticker = random.choice(candidates)
        ws_ob = all_obs[ticker]

        try:
            rest_resp = self._client.get_orderbook(ticker, depth=100)
        except Exception as e:
            logging.warning("WS_DRIFT_PROBE %s fetch_failed: %s", ticker, e)
            return
        if not rest_resp or not isinstance(rest_resp, dict):
            return

        # Kalshi REST returns one of two shapes (same pattern as
        # _get_orderbook_cached in bot/executor.py — search anchor:
        # `def _get_orderbook_cached`):
        #   - New FP format: {"orderbook_fp": {"yes_dollars": [[dollar_str,
        #     fp_qty_str], ...], "no_dollars": [...]}}
        #   - Legacy:        {"orderbook": {"yes": [[cents_int, qty_int],
        #     ...], "no": [...]}}
        rest_ob: Optional[Dict[str, List]] = None
        ob_fp = rest_resp.get("orderbook_fp")
        if ob_fp:
            rest_ob = OpportunityScanner._convert_orderbook_fp(ob_fp)
        else:
            legacy = rest_resp.get("orderbook")
            if isinstance(legacy, dict):
                rest_ob = {
                    "yes": list(legacy.get("yes") or []),
                    "no": list(legacy.get("no") or []),
                }
        if rest_ob is None:
            logging.warning(
                "WS_DRIFT_PROBE %s unknown_rest_shape: keys=%s",
                ticker, sorted(rest_resp.keys()))
            return

        # Fix #1b: if drift exceeds threshold on either side, auto-flag
        # the ticker for WS bypass. Only done once per probe call —
        # flag_ticker_drifted itself is idempotent via dict.pop.
        severe_drift_side: Optional[str] = None
        severe_ws_qty = 0
        severe_rest_qty = 0

        for side in ("yes", "no"):
            ws_levels = {int(lvl[0]): int(lvl[1])
                         for lvl in (ws_ob.get(side) or [])
                         if isinstance(lvl, (list, tuple)) and len(lvl) >= 2}
            rest_levels = {int(lvl[0]): int(lvl[1])
                           for lvl in (rest_ob.get(side) or [])
                           if isinstance(lvl, (list, tuple)) and len(lvl) >= 2}

            all_prices = set(ws_levels) | set(rest_levels)
            only_ws = sum(1 for p in all_prices
                          if p in ws_levels and p not in rest_levels)
            only_rest = sum(1 for p in all_prices
                            if p in rest_levels and p not in ws_levels)
            qty_mismatch = sum(1 for p in all_prices
                               if ws_levels.get(p, 0) != rest_levels.get(p, 0))
            total_ws = sum(ws_levels.values())
            total_rest = sum(rest_levels.values())
            max_missing_level = 0
            max_missing_qty = 0
            for p in all_prices:
                diff = rest_levels.get(p, 0) - ws_levels.get(p, 0)
                if diff > max_missing_qty:
                    max_missing_qty = diff
                    max_missing_level = p

            logging.warning(
                "WS_DRIFT_PROBE %s %s: ws_levels=%d rest_levels=%d "
                "only_ws=%d only_rest=%d qty_mismatch=%d "
                "ws_qty_total=%d rest_qty_total=%d missing_qty=%+d "
                "worst_level=%d¢ worst_missing=%d",
                ticker, side, len(ws_levels), len(rest_levels),
                only_ws, only_rest, qty_mismatch,
                total_ws, total_rest, total_rest - total_ws,
                max_missing_level, max_missing_qty)

            # Check severity — first severe side wins; second side still
            # logs its WS_DRIFT_PROBE but doesn't overwrite the trigger.
            if (severe_drift_side is None
                    and self._is_severe_drift(total_ws, total_rest)):
                severe_drift_side = side
                severe_ws_qty = total_ws
                severe_rest_qty = total_rest

        if severe_drift_side is not None:
            self.flag_ticker_drifted(ticker)
            logging.warning(
                "WS_DRIFT_AUTO_FLAG %s: %s side ws_qty=%d rest_qty=%d "
                "(|diff|=%d) — bypassing WS cache for 60s (fix #1b)",
                ticker, severe_drift_side, severe_ws_qty, severe_rest_qty,
                abs(severe_ws_qty - severe_rest_qty))

        # REST-vs-REST stability probe. Fetch REST again ~2s later and
        # compute delta against the REST we just used. Motivation:
        # WS_DRIFT_PROBE shows WS-vs-REST diverges, but that could mean
        # (a) WS drift, (b) REST drift/caching, or (c) timing race during
        # book churn. If REST_1 vs REST_2 shows big deltas, REST is not
        # stable ground truth and we can't trust it to overwrite WS.
        # See kb/failures/ws-cache-drift-investigation.md.
        #
        # The 2s wait + REST + comparison runs in a daemon thread so it
        # doesn't block the main scan() loop. Apr 25 00:21 incident:
        # synchronous time.sleep(2.0) here cascaded with other periodic
        # tasks into 6-10s main-thread stalls (SLOW_SCAN_TICK). Threaded
        # is safe — Kalshi client is thread-safe and this only logs.
        # See ws-cache-drift-silent-scan-2026-04-24 PM Fix 5.
        def _stability_reprobe_worker(_ticker, _rest_ob1, _client):
            try:
                time.sleep(2.0)
                rest_resp2 = _client.get_orderbook(_ticker, depth=100)
            except Exception as e:
                logging.debug(
                    "WS_DRIFT_PROBE %s rest2_fetch_failed: %s", _ticker, e)
                return
            if not rest_resp2 or not isinstance(rest_resp2, dict):
                return
            rest_ob2: Optional[Dict[str, List]] = None
            ob_fp2 = rest_resp2.get("orderbook_fp")
            if ob_fp2:
                rest_ob2 = OpportunityScanner._convert_orderbook_fp(ob_fp2)
            else:
                legacy2 = rest_resp2.get("orderbook")
                if isinstance(legacy2, dict):
                    rest_ob2 = {
                        "yes": list(legacy2.get("yes") or []),
                        "no": list(legacy2.get("no") or []),
                    }
            if rest_ob2 is None:
                return
            for side in ("yes", "no"):
                r1_levels = {int(lvl[0]): int(lvl[1])
                             for lvl in (_rest_ob1.get(side) or [])
                             if isinstance(lvl, (list, tuple)) and len(lvl) >= 2}
                r2_levels = {int(lvl[0]): int(lvl[1])
                             for lvl in (rest_ob2.get(side) or [])
                             if isinstance(lvl, (list, tuple)) and len(lvl) >= 2}
                all_prices = set(r1_levels) | set(r2_levels)
                only_r1 = sum(1 for p in all_prices
                              if p in r1_levels and p not in r2_levels)
                only_r2 = sum(1 for p in all_prices
                              if p in r2_levels and p not in r1_levels)
                qty_mismatch = sum(1 for p in all_prices
                                   if r1_levels.get(p, 0) != r2_levels.get(p, 0))
                total_r1 = sum(r1_levels.values())
                total_r2 = sum(r2_levels.values())
                _at_cap = max(len(r1_levels), len(r2_levels)) >= 100
                logging.warning(
                    "WS_DRIFT_PROBE_REST_STABILITY %s %s: "
                    "r1_levels=%d r2_levels=%d only_r1=%d only_r2=%d "
                    "qty_mismatch=%d r1_qty_total=%d r2_qty_total=%d "
                    "delta_qty=%+d at_depth_cap=%s",
                    _ticker, side, len(r1_levels), len(r2_levels),
                    only_r1, only_r2, qty_mismatch,
                    total_r1, total_r2, total_r2 - total_r1,
                    "yes" if _at_cap else "no")

        try:
            threading.Thread(
                target=_stability_reprobe_worker,
                args=(ticker, rest_ob, self._client),
                daemon=True,
                name=f"ws_drift_stability_{ticker[:20]}",
            ).start()
        except Exception:
            logging.debug(
                "WS_DRIFT_PROBE stability_thread_spawn_failed",
                exc_info=True)

    # ── Timeslot helpers ──────────────────────────────────────────────────

    @staticmethod
    def _window_timeslot(event_ticker: Optional[str]) -> str:
        """Extract timeslot from event ticker.

        'KXBTC15M-26FEB211545' -> '26FEB211545'
        (same across assets: KXETH15M-26FEB211545 also gives '26FEB211545')
        """
        if not event_ticker:
            # Transient race: position/order/window dict surfaced None event_ticker
            # (see kb/failures/transient-none-event-ticker-may08.md — same class
            # as the 2026-05-08 14:44 tick error at OrderExecutor's per-window
            # cap aggregator). Caller checks `if ts:` so empty string skips
            # this row safely.
            logging.warning("WINDOW_TIMESLOT_NULL: event_ticker is None/empty")
            return ""
        parts = event_ticker.split("-")
        if len(parts) >= 2:
            return parts[1]
        return event_ticker

    def _get_occupied_timeslots(self) -> Dict[str, set]:
        """Return {timeslot: set(assets)} for timeslots with open positions or resting orders."""
        occupied: Dict[str, set] = {}

        for pos in self._state.get_open_positions():
            et = pos.get("event_ticker", "")
            asset = pos.get("asset", "")
            ts = self._window_timeslot(et)
            if ts:
                occupied.setdefault(ts, set()).add(asset)

        for order in self._state.get_resting_orders():
            et = order.get("event_ticker", "")
            asset = order.get("asset", "")
            ts = self._window_timeslot(et)
            if ts:
                occupied.setdefault(ts, set()).add(asset)

        return occupied

    # ── Balance ───────────────────────────────────────────────────────────

    def _get_balance_cached(self) -> Optional[int]:
        """Get balance in cents, cached for BALANCE_CACHE_TTL seconds."""
        now = time.time()
        cached_balance, fetch_time = self._balance_cache
        if cached_balance is not None and now - fetch_time < BALANCE_CACHE_TTL:
            return cached_balance

        resp = self._client.get_balance()
        if resp is None:
            return cached_balance  # return stale if API fails
        balance = resp.get("balance") or 0
        # Absolute sanity cap — catches API glitches without blocking normal settlements
        _BALANCE_SANITY_CAP = int(os.getenv("BALANCE_SANITY_CAP_CENTS", "250000"))  # $2,500 default
        if balance > _BALANCE_SANITY_CAP:
            logging.warning("BALANCE_SANITY_CAP: api_balance=$%.2f capped=$%.2f",
                            balance / 100, _BALANCE_SANITY_CAP / 100)
            balance = _BALANCE_SANITY_CAP
        self._balance_cache = (balance, now)
        return balance
