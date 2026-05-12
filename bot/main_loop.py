"""Bit 9.3 — MainLoop extracted from bot/_impl.py to bot/main_loop.py.

Bit 9.3 (Sprint 9 closing leaf, 2026-05-10), two-step atomic per master plan
L2195-2216:
  - 9.3-i (this commit): bot/main_loop.py created; bot/_impl.py adds re-export
    `from bot.main_loop import MainLoop`; bot/__main__.py UNCHANGED (still
    `from bot._impl import MainLoop` via the proxy chain). bot/_impl.py
    SHRINKS from 3,017 to ~1,030 LOC (Δ -1,987).
  - 9.3-ii (separate later commit, soak ≥7d per master plan): swap
    bot/__main__.py to `from bot.main_loop import MainLoop`. bot/_impl.py
    NOT deleted yet (Option B — OFE + KOFT residency until Bit 9.3.5).

Largest Sprint 9 leaf by class count (15 instance methods, ~1,999 LOC verbatim
move). Path-A method-body late-binding keeps the bot._impl edge clean —
zero new `.importlinter` carve-outs, net contracts stays at 5.

## Architecture (post-Bit-9.3-iii.a, 2026-05-11)

bot/main_loop.py has ZERO top-level `from bot._impl import` or `import bot._impl`.
Top-level access into bot._impl-bound state is no longer needed:

  - `_HPSB_MISSING_BLEEDERS`, `_HPSB_VALIDATOR_UNAVAILABLE_REASON` — top-imported
    from clean-leaf `bot.boot` (Bit 9.3-iii.a relocation).
  - `OrderFlowEngine`, `KalshiOrderFlowTracker` — top-imported from clean-leaf
    `bot.order_flow` (Bit 9.3.5 collapse).
  - `detect_orphan_db_holders` — method-body late-bound inside `MainLoop.startup`
    from `bot.orphan_db_watchdog` (Bit 9.3-ii relocation; orthogonal to bot._impl).

NO `.importlinter` carve-out for bot._impl — bot/main_loop.py has no
bot._impl edge in the import graph. Net contracts stays at 6 (post-Bit-9.3-iii.a
the `state-no-impl-toplevel` contract retired alongside this relocation;
helpers-leaf gained `bot.boot`).

## Cross-class coupling preserved verbatim

  - `_telegram_state._TELEGRAM` (Bit 8.1 path-A++; ~11 read sites + 1 write
    at __init__ `_telegram_state._TELEGRAM = self.telegram`). Canonical alias
    form `import bot.notifier as _telegram_state` per L84. The post-Bit-9.3-ii
    consumer count is FIVE (bot/orphan_db_watchdog.py for the orphan-DB
    `_alert_orphan_db_holder` helper + the `detect_orphan_db_holders` lsof-
    not-found branch — relocated atomically at Bit 9.3-ii REPLACING
    bot/_impl.py in the slot; this module hosts MainLoop reads + WRITE).
  - `_cal_state._CALIBRATION_ENGINE` / `._CAL_REGISTRY` / `._resolve_cal_engine`
    (Bit 6.3 path-B; mutated at __init__ + read at the cal-registry assertion).
    Alias `from bot.engines import calibration as _cal_state`.

## Forbidden top-level imports

- **No torch / sklearn / pandas / numpy / scipy direct imports.** MainLoop is
  orchestration — engines (which carry numpy/scipy via models) are CONSTRUCTED
  here but the numerical libs themselves never enter this module's top-level.
  Locked by `tests/integration/test_main_loop_extraction.py::test_main_loop_no_forbidden_numerical_imports`.

## Bit 9.3.5 marker collapse (SHIPPED 2026-05-10)

`OrderFlowEngine` (122 LOC) + `KalshiOrderFlowTracker` (240 LOC) extracted
to `bot/order_flow.py` per Sprint 9 Bit 9.3.5. The two `# REMOVE BIT 9.3.5`
markers in MainLoop.__init__'s late-binding block (pre-9.3.5 form) have
collapsed to a top-level `from bot.order_flow import OrderFlowEngine,
KalshiOrderFlowTracker` at module scope. Sister cleanup: bot/scanner
forward-refs for these classes UNQUOTED in the same commit. bot/_impl.py
class-list is now empty (only re-exports + boot-time bindings + orphan-DB
watchdog block + module-level helpers remain; final deletion deferred to
Bit 9.3-ii after bot/__main__.py swap).

## Sister cleanup atomic in same commit

  - bot/notifier.py docstring: 5 consumers post-Bit-9.3 (bot/_impl.py STAYS
    for the orphan-DB `_alert_orphan_db_holder` helper; bot/main_loop.py
    ADDS as MainLoop host. The "4 consumers" Bit-9.2 narrative — which
    treated bot/_impl.py as the MainLoop-only consumer — is the wrong frame
    post-extraction).
  - bot/__init__.py docstring extended for Bit 9.3.
  - bot/CLAUDE.md "Deploy a change" step 3 catalog gains MainLoop paragraph.
  - bot/scanner/__init__.py + bot/scanner/CLAUDE.md: 4-consumer narrative flipped.
  - bot/_impl.py header docstring + _telegram_state alias comment block flipped
    (bot/_impl.py is no longer a consumer).
  - bot/settlement.py + bot/executor.py docstrings: 4-consumer enum flipped.
  - agent_docs/bot_layout.md: header line count, class table loses MainLoop row,
    bot/main_loop.py block added, Sprint 9 marker advanced.
  - 7+ BOT_PY-defined tests retargeted from BOT_PY → MAIN_LOOP_PY for MainLoop
    content walks (per L38, per-test).
  - tests/integration/test_state_extraction.py + test_settlement_extraction.py +
    test_executor_extraction.py walk-set extensions.
  - tests/integration/test_orphan_db_watchdog.py AST walk retargeted from BOT_PY to
    MAIN_LOOP_PY (helpers stay in bot/_impl.py per C3).
  - .importlinter helpers-leaf forbidden_modules extended with bot.main_loop.
"""
from __future__ import annotations

# NOTE — Bit 9.3-ii (2026-05-10): the bot/__main__.py swap is DONE; thread_env
# fires there as the FIRST import (line 26 of bot/__main__.py, before stdlib
# `logging` and `sys`). Production path is safe: bot/__main__.py →
# bot._thread_env → from bot.main_loop import MainLoop → models → numpy.
# Bit 9.3-iii.a (2026-05-11) demonstrated that the original griffe
# RecursionError blocker for top-level `import bot._thread_env` inside a
# bot.* module (Bit 9.3 R4 MINOR-11) can be sidestepped via the
# `__import__("bot._thread_env")` runtime-call form (search anchor `__import__("bot._thread_env")` in bot/boot.py).
# Defense-in-depth here in bot/main_loop.py remains OPTIONAL — production
# already gets the guarantee through bot/__main__.py, and the test-import
# risk paths (tests that import bot.main_loop directly without going
# through __main__.py — ~3 sites) run in pytest workers that don't hit the
# production thread-contention regime.

import datetime
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from collections import deque
from datetime import timezone
from typing import Dict, List, Optional

# Alias modules (L84 form — explicit submodule import bypasses _BotProxy.__getattr__)
import bot.notifier as _telegram_state  # Bit 8.1 path-A++ alias for _telegram_state._TELEGRAM
from bot.engines import calibration as _cal_state  # Bit 6.3 path-B alias for _cal_state._CALIBRATION_ENGINE / _CAL_REGISTRY / _resolve_cal_engine

# bot.constants — 23 explicit names per L78 free-var scan
from bot.constants import (
    ACTIVE_WINDOWS_STALENESS_BUDGET_S,
    CALIBRATION_STATE_PATH,
    CROSS_EXCHANGE_ENABLED,
    DB_PATH,
    HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
    HIGH_PRICE_STC_BLOCK_ENABLED,
    HIGH_PRICE_STC_BLOCK_FILTER_STAGE,
    HOURLY_OBSERVATION_ENABLED,
    KALSHI_OFT_ENABLED,
    MARKET_REFRESH_SECONDS,
    MAX_SECONDS_BEFORE_CLOSE,
    MIN_SECONDS_BEFORE_CLOSE,
    OBSERVATION_MODE,
    POSITION_PRICE_MONITOR_ENABLED,
    POSITION_PRICE_MONITOR_WS_STALE_SEC,
    REJECTION_JOURNAL,
    SCAN_INTERVAL_SECONDS,
    SOL_BLEED_V2_BLOCK_FILTER_STAGE,
    SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE,
    SPORTS_ENABLED,
    SPX_HOURLY_ENABLED,
    TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE,
    WEATHER_ENABLED,
    WS_PERIODIC_RESNAPSHOT_INTERVAL_S,
)
from bot.helpers.breakers import _extract_tick_error_location
from bot.helpers.strings import dollars_str_to_cents

# Leaf classes (Sprint 4 + 7)
from bot.kalshi_client import KalshiClient
from bot.logger import Logger
from bot.notifier import TelegramNotifier
from bot.state import StateManager

# Engines (Sprint 6)
from bot.engines.calibration import CalibrationEngine
from bot.engines.probability import ProbabilityEngine
from bot.engines.volatility import VolatilityEngine

# Feeds + fetchers (Sprint 4)
from bot.feeds.coinbase import CoinbaseFeed
from bot.feeds.cross_exchange import CrossExchangeFeed
from bot.feeds.kalshi import KalshiFeed
from bot.fetchers.coinglass import CoinGlassFetcher
from bot.fetchers.deribit import DeribitDVOLFetcher

# Sprint 8 + 9 big-class peers
from bot.executor import OrderExecutor
from bot.order_flow import KalshiOrderFlowTracker, OrderFlowEngine  # Bit 9.3.5 — clean-leaf; replaces __init__ late-binding markers
from bot.scanner import OpportunityScanner
from bot.settlement import SettlementTracker, discover_active_windows

# Pre-Bit-3.1 leftover-in-config + market_config + models
from bot.config import ASSETS
from market_config import MARKET_CONFIGS
from bot.models import (
    EGARCHEstimator,
    MincerZarnowitzTracker,
    PositionSizer,
    calculate_taker_fee,
)

# cal_mlp processor lifecycle (aliases match bot/_impl.py:68 + 610 conventions)
from integration import (  # noqa: E402
    _calmlp_predictors,
    start_post_hoc_processor as _calmlp_start_posthoc,
    stop_post_hoc_processor as _calmlp_drain_pool,
)


# Bit 9.3-iii.a (2026-05-11): HPSB boot-time bindings relocated to bot/boot.py
# clean leaf — top-imported here (no more method-body late-binding from bot._impl).
# `detect_orphan_db_holders` remains method-body late-bound inside MainLoop.startup
# (relocated to bot/orphan_db_watchdog.py in Bit 9.3-ii; the lazy import there is
# orthogonal to bot._impl partial-module concerns).
from bot.boot import _HPSB_MISSING_BLEEDERS, _HPSB_VALIDATOR_UNAVAILABLE_REASON


class MainLoop:
    """Continuous observation loop. Scans active windows every second."""

    def __init__(self):
        # Bit 9.3-iii.a (2026-05-11): HPSB bindings + compute_for_15m_main_path
        # closure now live in bot/boot.py clean leaf. The Bit 9.3 method-body
        # late-binding block (was `from bot._impl import (_HPSB_*, ...)`) is
        # GONE — `_HPSB_MISSING_BLEEDERS` and `_HPSB_VALIDATOR_UNAVAILABLE_REASON`
        # are top-imported at module scope.

        api_key = os.environ.get("KALSHI_API_KEY") or os.environ.get("KALSHI_API_KEY_ID", "")
        private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        if not api_key or not private_key_path:
            logging.critical(
                "KALSHI_API_KEY (or KALSHI_API_KEY_ID) and KALSHI_PRIVATE_KEY_PATH must be set"
            )
            sys.exit(1)

        # Loud one-time log of HPSB gate state — distinguishes "VPS env lost the
        # var so gate silently re-disabled" from "no candidates hit the cell" in
        # the absence of HPSB_DROP rows. Grep journal weekly for HPSB_GATE_STATE.
        logging.warning(
            "HPSB_GATE_STATE: enabled=%s bleeders=%s missing_bleeder_strings=%s validator_unavailable=%s",
            HIGH_PRICE_STC_BLOCK_ENABLED,
            sorted(HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES),
            _HPSB_MISSING_BLEEDERS or "none",
            _HPSB_VALIDATOR_UNAVAILABLE_REASON or "no")

        self.client = KalshiClient(api_key, private_key_path)
        self.state = StateManager()
        self.logger = Logger()
        self.feed = CoinbaseFeed()
        self.dvol_fetcher = DeribitDVOLFetcher()
        self.egarch_estimator = EGARCHEstimator()
        self.mz_tracker = MincerZarnowitzTracker()
        self.vol = VolatilityEngine(self.feed, dvol_fetcher=self.dvol_fetcher,
                                    egarch_estimator=self.egarch_estimator,
                                    mz_tracker=self.mz_tracker)
        self.sizer = PositionSizer()
        self.calibration = CalibrationEngine()
        _cal_state._CALIBRATION_ENGINE = self.calibration

        # Per-market CalEngines via registry
        _cal_state._CAL_REGISTRY.clear()  # defensive: ensure clean state on restart
        self._cal_engines = {}
        self._cal_engine_meta = {}  # reg_key → (product_type, subtype_code_or_None)

        for _pt, _cfg in MARKET_CONFIGS.items():
            if _cfg.cal_subtypes:
                # Per-subtype engines (weather cities, sports groups, 15M per-asset)
                # 15M engines train on candidates + cell-block shadow rows.
                # R-bleed-1 R9-H1: bleed-cell blocks intercept candidates and write
                # them under their cell tag instead of 'candidate'. Without
                # including those tags here, the CalEngines stop receiving the
                # observations from EXACTLY the cells we just gated — silently
                # narrowing training signal where the calibrator most needs it.
                _stages = (
                    ("candidate", "observation_trade",
                     HIGH_PRICE_STC_BLOCK_FILTER_STAGE,
                     TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE,
                     SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE,
                     SOL_BLEED_V2_BLOCK_FILTER_STAGE)
                    if _pt == "15m" else None
                )
                for _sub_code, _sub_path in _cfg.cal_subtypes.items():
                    _reg_key = f"{_pt}_{_sub_code}"
                    assert _sub_path != CALIBRATION_STATE_PATH, (
                        f"FATAL: {_reg_key} would share state file with 15M engine!")
                    _engine = CalibrationEngine(
                        state_path=_sub_path,
                        label=f"{_pt.capitalize()}_{_sub_code}Cal",
                        accepted_stages=_stages)
                    self._cal_engines[_reg_key] = _engine
                    _cal_state._CAL_REGISTRY[_reg_key] = _engine
                    self._cal_engine_meta[_reg_key] = (_pt, _sub_code)
                    logging.info("CalEngine registered for '%s' (state: %s, enabled=%s)",
                                 _reg_key, _sub_path, _cfg.cal_engine_enabled)

            elif _cfg.cal_engine_state_path:
                # Single engine per product_type (hourly, spx_hourly)
                # Always instantiate so settlement can collect observations via add_observation().
                # require_enabled gate in _cal_state._resolve_cal_engine() prevents disabled engines
                # from affecting predictions.
                assert _cfg.cal_engine_state_path != CALIBRATION_STATE_PATH, (
                    f"FATAL: {_pt} would share state file with 15M engine!")
                _engine = CalibrationEngine(
                    state_path=_cfg.cal_engine_state_path,
                    label=f"{_pt.capitalize()}Cal")
                self._cal_engines[_pt] = _engine
                _cal_state._CAL_REGISTRY[_pt] = _engine
                self._cal_engine_meta[_pt] = (_pt, None)
                logging.info("CalEngine registered for '%s' (state: %s, enabled=%s)",
                             _pt, _cfg.cal_engine_state_path, _cfg.cal_engine_enabled)

            else:
                logging.info("CalEngine DISABLED for '%s': no state path, passthrough", _pt)

        assert "15m" not in _cal_state._CAL_REGISTRY, "FATAL: 15M engine must never be in _cal_state._CAL_REGISTRY"

        # Backward compat for bot/snapshots/dashboard_snapshot.py
        self.hourly_calibration = self._cal_engines.get("hourly")
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.telegram = TelegramNotifier(tg_token, tg_chat)
        # Bit 8.1 path-A++ (2026-05-10): write through to the source-of-truth
        # singleton in bot/notifier.py so all consumers (this module +
        # bot/scanner + bot/executor + bot/settlement) reading via
        # `_telegram_state.<NAME>` see the mutation immediately. Drops the
        # previous module-level `global` rebind declaration since we no longer
        # rebind a module-level name in this file (Bit 9.3 inherits this state
        # from Bit 8.1 — MainLoop construction is now in bot/main_loop.py).
        _telegram_state._TELEGRAM = self.telegram
        self.cross_feed = CrossExchangeFeed(self.feed) if CROSS_EXCHANGE_ENABLED else None
        self.coinglass = CoinGlassFetcher()
        self.kalshi_oft = KalshiOrderFlowTracker() if KALSHI_OFT_ENABLED else None
        self.order_flow = OrderFlowEngine(
            cross_feed=self.cross_feed, coinglass=self.coinglass,
            kalshi_oft=self.kalshi_oft,
        )
        # Kalshi WebSocket feed for real-time fills + orderbook
        try:
            self.kalshi_feed = KalshiFeed(api_key, self.client.private_key)
        except Exception as e:
            logging.warning(f"KalshiFeed init failed: {e}")
            self.kalshi_feed = None
        # ── SPX Engine (conditional) ────────────────────────────────────
        self.spx_engine = None
        if SPX_HOURLY_ENABLED:
            try:
                from bot.engines.spx_engine import SPXEngine  # Sprint 10.1b sibling-reorg (2026-05-11)
                self.spx_engine = SPXEngine(
                    polygon_key=os.environ.get("POLYGON_API_KEY"),
                    finnhub_key=os.environ.get("FINNHUB_API_KEY"),
                )
                logging.info("SPX engine initialized")
            except Exception as e:
                logging.warning(f"SPX engine unavailable: {e}")

        # ── Weather Engine (conditional) ───────────────────────────────────
        self.weather_engine = None
        if WEATHER_ENABLED:
            try:
                from bot.engines.weather_engine import WeatherEngine  # Sprint 10.1c sibling-reorg (2026-05-11)
                self.weather_engine = WeatherEngine(db_path=DB_PATH)
                logging.info("Weather engine initialized")
            except Exception as e:
                logging.warning(f"Weather engine unavailable: {e}")

        # ── 15M Shadow Engine (recalibrated EGARCH + LightGBM) ─────────
        self.fifteenm_shadow = None
        try:
            from bot.shadows.fifteenm_shadow import FifteenMShadowEngine, FIFTEENM_SHADOW_ENABLED  # Sprint 10.2 sibling-reorg (2026-05-11)
            if FIFTEENM_SHADOW_ENABLED:
                self.fifteenm_shadow = FifteenMShadowEngine(db_path=DB_PATH)
                logging.info("15M shadow engine initialized (recalibrated EGARCH + LightGBM)")
        except Exception as e:
            logging.warning(f"15M shadow engine unavailable: {e}")

        # ── Hourly Alt Shadow Engine (ETH/SOL/XRP shadow strategies) ──────
        self.hourly_alt_shadow = None
        try:
            from bot.shadows.hourly_alt_shadow import HourlyAltShadowEngine, HOURLY_ALT_SHADOW_ENABLED  # Sprint 10.2 sibling-reorg (2026-05-11)
            if HOURLY_ALT_SHADOW_ENABLED and HOURLY_OBSERVATION_ENABLED:
                self.hourly_alt_shadow = HourlyAltShadowEngine(db_path=DB_PATH)
                logging.info("Hourly alt shadow engine initialized (MM + HAR-RV)")
        except Exception as e:
            logging.warning(f"Hourly alt shadow engine unavailable: {e}")

        # ── H-3a Continuous NBBO Snapshotter ─────────────────────────────
        # Periodic in-memory WS orderbook → market_observations_continuous.
        # Foundation for the deferred H-3 fill simulator. Reads existing WS
        # cache (no new REST traffic). Schema migration MUST run from main
        # thread (not the daemon) to avoid racing other ALTER TABLE migrations
        # at startup. Disabled if kalshi_feed unavailable (nothing to read).
        # See kb/decisions/phase-h3-deferred-needs-nbbo-infra-may02.md.
        self.market_obs_snapshotter = None
        try:
            from bot.snapshots.market_observations_snapshotter import (
                MarketObservationsSnapshotter,
                ensure_schema as _moc_ensure_schema,
                extract_active_15m_tickers as _moc_extract_15m,
            )
            _moc_ensure_schema(self.state.conn)
            # Round-1 wiring #8: defensive capability check. If the WS
            # client is ever refactored and the deep-copy method is
            # renamed, surface that immediately at init rather than
            # producing 8.6K WARNING/day from per-tick AttributeError.
            if (
                self.kalshi_feed is not None
                and hasattr(self.kalshi_feed, "get_all_orderbooks_snapshot")
            ):
                self.market_obs_snapshotter = MarketObservationsSnapshotter(
                    db_path=DB_PATH,
                    ws_client=self.kalshi_feed,
                    active_tickers_provider=lambda: _moc_extract_15m(
                        self._active_windows
                    ),
                )
                logging.info(
                    "Market observations snapshotter initialized "
                    "(H-3a — continuous NBBO capture for fill simulator)"
                )
            elif self.kalshi_feed is not None:
                logging.warning(
                    "Market observations snapshotter not started — "
                    "kalshi_feed missing get_all_orderbooks_snapshot method "
                    "(WS client API drift; review bot/_impl.py vs "
                    "bot/snapshots/market_observations_snapshotter.py contract)"
                )
        except Exception as e:
            logging.warning(
                f"Market observations snapshotter unavailable: {e}"
            )
            self.market_obs_snapshotter = None

        # ── SPX HAR-RV Shadow Engine ────────────────────────────────────────
        self.spx_harrv_shadow = None
        if SPX_HOURLY_ENABLED:
            try:
                from bot.shadows.spx_harrv_shadow import SPXHARRVShadowEngine, SPX_HARRV_SHADOW_ENABLED  # Sprint 10.2 sibling-reorg (2026-05-11)
                if SPX_HARRV_SHADOW_ENABLED:
                    self.spx_harrv_shadow = SPXHARRVShadowEngine(db_path=DB_PATH)
                    logging.info("SPX HAR-RV shadow engine initialized")
            except Exception as e:
                logging.warning(f"SPX HAR-RV shadow engine unavailable: {e}")

        # ── Sports Engine (conditional) ────────────────────────────────────
        self.sports_engine = None
        if SPORTS_ENABLED:
            try:
                from bot.engines.sports_engine import SportsEngine  # Sprint 10.1d sibling-reorg (2026-05-11)
                self.sports_engine = SportsEngine(
                    kalshi_client=self.client,
                    state_manager=self.state,
                    db_path=DB_PATH,
                )
                logging.info("Sports engine initialized")
            except Exception as e:
                logging.warning(f"Sports engine unavailable: {e}")

        # ── Capital Allocator (conditional) ────────────────────────────────
        self.capital_allocator = None
        try:
            from bot.infra.capital_allocator import CapitalAllocator  # Sprint 10.5a sibling-reorg (2026-05-11)
            _obs_strategies = set()
            for _k, _v in MARKET_CONFIGS.items():
                if _v.observation_only:
                    # Capital allocator uses "crypto_hourly" for hourly, product_type for others
                    _obs_strategies.add("crypto_hourly" if _k == "hourly" else _k)
            self.capital_allocator = CapitalAllocator(observation_strategies=_obs_strategies)
            logging.info("Capital allocator initialized")
        except Exception as e:
            logging.warning(f"Capital allocator unavailable: {e}")

        self.scanner = OpportunityScanner(
            self.client, self.state, self.feed, self.vol, self.logger,
            self.sizer, order_flow=self.order_flow,
            kalshi_oft=self.kalshi_oft,
            kalshi_feed=self.kalshi_feed,
            main_loop=self,
        )
        self.executor = OrderExecutor(
            self.client, self.state, self.logger,
            main_loop=self, kalshi_feed=self.kalshi_feed)
        self.executor._kalshi_oft = self.kalshi_oft
        self.tracker = SettlementTracker(self.client, self.state, self.logger,
                                         main_loop=self)
        self._shutdown = threading.Event()
        self._active_windows: List[Dict] = []
        self._discovery_ob_tickers: set = set()
        self._last_market_refresh: float = 0.0
        # Step #5 cache staleness watchdog: timestamp of last
        # successful `_refresh_active_windows`. Initialized to 0.0
        # so the staleness check reports STALE before the first
        # refresh — we don't trade until the cache is populated.
        self._active_windows_updated_at: float = 0.0
        # Dedup state for CACHE_STALE warnings: log once when the
        # episode has outlived the flicker threshold, then a
        # heartbeat every 60s while still stale, then a recovery
        # line when fresh again. Without dedup, a multi-hour stall
        # floods 3,600+ identical lines/hr.
        # `_episode_started_at` is set on the first stale tick;
        # `_episode_logged` flips True after the start line fires
        # (deferred until the episode outlives the flicker
        # threshold). Recovery only logs if start logged — Round
        # 3 [A4] symmetric flicker dedup.
        self._cache_stale_episode_started_at: float = 0.0
        self._cache_stale_last_heartbeat_at: float = 0.0
        self._cache_stale_episode_logged: bool = False
        # Round 4 [A3]: edge-trigger for the "0 active windows"
        # log so it doesn't fire every 30s during a sustained
        # Kalshi /events outage. CACHE_STALE handles the operator
        # alert after the budget elapses; this is just the
        # transition log.
        self._empty_refresh_in_progress: bool = False
        # Re-entry guard for market-refresh worker thread. Apr 25 00:45
        # incident: refresh_active_windows + subscribe took 1.5-2s on
        # the main thread (9+ REST calls), accounting for the residual
        # SLOW_SCAN_TICK gap after the SettlementTracker fix.
        self._market_refresh_running: bool = False
        # Re-entry guard for EGARCH MLE refit worker thread. Apr 25
        # 01:02 incident: PERIODIC_TASK_SLOW: egarch_refit took 53.30s
        # → SLOW_SCAN_TICK 54.37s. scipy L-BFGS-B optimization across
        # 4 assets is the heaviest periodic task in _tick().
        self._egarch_refit_running: bool = False
        self._last_wal_checkpoint: float = 0.0
        # Phase 2: timestamp of last periodic WS re-snapshot sweep.
        # Triggers force_resubscribe on every active 15M ticker
        # every WS_PERIODIC_RESNAPSHOT_INTERVAL_S — insurance against
        # H3' (server-side msg loss without seq increment).
        self._last_ws_periodic_resnap: float = 0.0
        self._last_error: Optional[str] = None
        self._last_error_time: float = 0.0
        self._start_time: float = time.time()
        self._peak_balance: float = 0.0
        self._balance_history: deque = deque(maxlen=8640)  # ~24h at 10s intervals
        self._recent_fill_latencies: deque = deque(maxlen=100)
        self._session_fill_count: int = 0
        self._session_maker_submissions: int = 0
        self._session_maker_fills: int = 0
        self._last_summary_date: Optional[str] = None
        self._observation_mode: bool = OBSERVATION_MODE
        self._last_db_health_check: float = 0.0
        self._db_locked_count: int = 0
        # Phase H-2: bot microstate forward capture state.
        # `_scan_iter` is the monotonic per-tick counter (incremented at
        # the top of `scan()`). `_open_positions_count_cache` is populated
        # by `_compute_bot_state_features` (60s cache) so the snapshot
        # avoids a per-insert SQL hit. `_scan_loop_start` (pinned to
        # `time.perf_counter()` at top of scan()) lets the helper derive
        # `scan_dt_ms` defensively.
        self._scan_iter: int = 0
        self._open_positions_count_cache: Optional[int] = None
        self._scan_loop_start: Optional[float] = None
        # Phase H-2: wire the bot microstate provider into StateManager.
        # AFTER all MainLoop attributes are initialized (so the provider
        # closure has everything it needs to read). The provider returns
        # the snapshot DICT (not a JSON string) — the insert site patches
        # `lock_wait_ms` in-place and serializes. This decouples the
        # heavy snapshot computation from the writer lock (round-1 wiring
        # review #3). Wrapped in try/except — if the import fails the
        # bot still runs (column stays NULL on every 15M insert).
        try:
            from bot.snapshots.bot_state_snapshot import compute_bot_state_snapshot as _moc_snap
            def _bot_state_provider():
                """Returns the snapshot dict (lock_wait_ms=None — patched
                in by insert_evaluated_opportunity after BEGIN IMMEDIATE)."""
                return _moc_snap(self, lock_wait_ms=None)
            self.state.set_bot_state_provider(_bot_state_provider)
            logging.info("H-2 bot state provider wired into StateManager")
        except Exception as e:
            logging.warning(f"H-2 bot state provider unavailable: {e}")

    # ── DB Health Watchdog ────────────────────────────────────────────────

    def _check_db_health(self):
        """Run every 5 minutes. Alert via Telegram if DB contention is unhealthy."""
        alerts = []

        # 1. Locked error count since last check
        if self._db_locked_count > 10:
            alerts.append(f"🔒 {self._db_locked_count} locked errors in last 5 min")
        self._db_locked_count = 0  # reset after check

        # 2. WAL file size — alert at 200 MB (cursor-race steady-state is
        # ~140 MB with the unfixed cursor-race; pre-2026-05-08 threshold of
        # 100 MB fired every 5 min in nuisance noise without surfacing real
        # anomalies. 200 MB still leaves comfortable headroom under the
        # operator-set 250 MB hard restart-trigger. Restoring 100 MB
        # threshold makes sense after the cursor-race fix lands.
        try:
            db_path = self.state.conn.execute(
                "PRAGMA database_list").fetchone()[2]
            wal_path = db_path + "-wal"
            if os.path.exists(wal_path):
                wal_mb = os.path.getsize(wal_path) / (1024 * 1024)
                if wal_mb > 200:
                    alerts.append(f"📁 WAL file {wal_mb:.1f} MB (>200 MB)")
        except Exception:
            pass

        # 3. Pending evaluated_opportunities (unsettled backlog)
        try:
            pending = self.state.conn.execute(
                "SELECT COUNT(*) FROM evaluated_opportunities "
                "WHERE status='open' AND filter_stage IN "
                "('candidate','observation_trade','shadow','weather_shadow',"
                "'sports_shadow','spx_observation','no_side_shadow')"
            ).fetchone()[0]
            if pending > 500:
                alerts.append(f"📊 {pending} pending evals (>500)")
        except Exception:
            pass

        if alerts:
            msg = "⚠️ *DB Health Alert*\n" + "\n".join(alerts)
            self.telegram.send(msg, dedup_key="db_health_alert")
            logging.warning("DB health alert: %s", "; ".join(alerts))

    # ── Signal Handling ───────────────────────────────────────────────────

    def _setup_signals(self):
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, _frame):
        name = signal.Signals(signum).name
        logging.info(f"Received {name}, shutting down gracefully...")
        self._shutdown.set()

    # ── Startup ───────────────────────────────────────────────────────────

    # DEPLOY_SAFE_HOURS: 04-06 UTC (midnight-2AM ET).
    # Restarts during 12-22 UTC cause ~5 min RK warmup with degraded edge.
    # Three restarts on Mar 23 during 18-20 UTC cost an estimated 5-18 fills.

    def startup(self):
        # Bit 9.3-ii (2026-05-10): direct import from the bot.orphan_db_watchdog
        # clean-leaf module. The previous `from bot._impl import detect_orphan_db_holders`
        # late-binding form (Bit 9.3-i) was needed when the function lived in
        # bot/_impl.py below the line-119 MainLoop re-export. Post-9.3-ii relocation,
        # bot.orphan_db_watchdog is a clean leaf with zero edges into bot._impl or
        # any other bot/ subpackage — top-level import here would work, but
        # method-body import is used for parity with the original load-order
        # defense and keeps the startup hot-path import cost cleanly deferred.
        from bot.orphan_db_watchdog import detect_orphan_db_holders

        logging.info("Bot starting up...")

        # Layer 3 orphan-DB watchdog: detect any non-bot PID holding
        # state.db at startup. Designed to catch the May 3 2026
        # incident pattern (H-4c backfill orphan held writer lock for
        # 2h42m, wedged the bot for 6 min via lock contention on
        # restart). Detection-only; operator triages from the alert.
        # See kb/failures/shape-d-contention-explosion-may03.md.
        try:
            detect_orphan_db_holders(DB_PATH)
        except Exception:
            logging.warning(
                "orphan-DB watchdog raised; ignoring (defense in depth, "
                "must not block startup)", exc_info=True,
            )

        # Warn if starting during peak trading hours
        _start_hour = datetime.datetime.now(timezone.utc).hour
        if 12 <= _start_hour <= 22:
            logging.warning(
                "PEAK_HOURS_START: Bot started at %02d:00 UTC — RK warmup will "
                "degrade edge for ~5 minutes. Deploy during 04-06 UTC to avoid fill loss.",
                _start_hour)

        # R-p7-deploy-r9: start the cal_mlp post-hoc processor here, AFTER
        # StateManager has run migrate_schema (so cal_mlp_request_id column
        # + partial index exist). The processor polls evaluated_opportunities
        # every 10s in its own daemon thread, finds unannotated 15M rows,
        # runs predict, and UPDATEs. Edit 4 in scan() just stamps the uuid.
        # Always start (cheap if env=0 — early-exits at the env check inside
        # _process_one_batch). Stops in _cleanup() before state.close().
        _calmlp_start_posthoc(
            db_path=DB_PATH, predictors=_calmlp_predictors,
            poll_interval_sec=10.0, batch_size=50,
        )

        # Load previously logged fill IDs
        self.logger.load_logged_fill_ids()

        # Verify API connectivity
        balance_resp = self.client.get_balance()
        if balance_resp is None:
            logging.critical("Cannot connect to Kalshi API — check credentials")
            sys.exit(1)
        balance_cents = balance_resp.get("balance") or 0
        self.sizer.starting_balance_cents = balance_cents
        # Seed HWM with CASH balance only (not portfolio value).
        # Kalshi available_balance does NOT drop when position collateral is locked,
        # so cash-only tracking is safe. Portfolio tracking (cash + positions) caused
        # HWM inflation when DC positions opened, compressing drawdown_scaler to 0.25
        # even on profitable accounts. (Learned: HWM spiked to $2,035 from simultaneous
        # DC positions, cash was $1,485, ds=0.25 for ~7 days undetected. Mar 29 2026.)
        # Position exposure is still computed for logging/dashboard visibility.
        try:
            _startup_positions = self.state.get_open_positions()
            _startup_exposure = sum(
                p.get("count", 0) * p.get("avg_price_cents", 0)
                for p in _startup_positions
            )
            logging.info(
                "HWM seed: available=%dc (position_exposure=%dc, NOT added to HWM)",
                balance_cents, _startup_exposure)
        except Exception:
            logging.warning("HWM seed: could not compute position exposure for logging")
        self.sizer.record_balance(balance_cents)
        self._peak_balance = balance_cents / 100
        logging.info(f"Connected to Kalshi. Balance: ${balance_cents / 100:.2f}")
        if _telegram_state._TELEGRAM:
            _telegram_state._TELEGRAM.send(f"\U0001f7e2 Bot started \u2014 Balance: ${balance_cents / 100:.2f}")

        # Reconcile local state with API
        self.state.reconcile_with_api(self.client)

        # Check for settlements that happened while bot was down
        self.tracker.startup()

        # Backfill raw_prob for calibration data
        self._backfill_calibration_data()

        # Load calibration training data from historical settlements
        self.calibration.load_training_data_from_db(self.state)

        # Run adaptive-vs-fixed backtest on startup
        backtest_result = self.calibration.backtest_adaptive_vs_fixed()
        if backtest_result:
            logging.info("Startup backtest result: %s", backtest_result)

        if _telegram_state._TELEGRAM and self.calibration.active_method != "fixed_beta":
            bt_msg = ""
            if backtest_result:
                bt_msg = (
                    f"\nBacktest: Brier {backtest_result['old_brier']:.4f} -> "
                    f"{backtest_result['new_brier']:.4f} "
                    f"({backtest_result['cap_truncated_count']} cap-truncated)"
                )
            _telegram_state._TELEGRAM.send(
                f"\U0001f9e0 Calibration: {self.calibration.active_method} trained "
                f"({len(self.calibration._observations)} obs){bt_msg}"
            )

        # Load per-market calibration training data (separate from 15M)
        for _reg_key, _engine in self._cal_engines.items():
            _load_pt, _sub_code = self._cal_engine_meta[_reg_key]
            _load_asset = _cal_state._derive_asset_filter(_load_pt, _sub_code) if _sub_code else None
            _engine.load_training_data_from_db(
                self.state, product_type_include=_load_pt, asset_filter=_load_asset)
            _bt = _engine.backtest_adaptive_vs_fixed()
            if _bt:
                logging.info("Startup %s cal backtest: %s", _reg_key, _bt)
            logging.info("CONFIG_VERIFY (%s_cal): method=%s active=%s obs=%d",
                         _reg_key, _engine.active_method,
                         _engine.is_learned_method_active(), len(_engine._observations))

        # Start Coinbase price feed
        self.feed.start()
        logging.info("Coinbase price feed starting...")

        # Start Deribit DVOL fetcher
        self.dvol_fetcher.start()
        logging.info("Deribit DVOL fetcher starting...")

        # Start cross-exchange feeds
        if self.cross_feed:
            self.cross_feed.start()
            logging.info("Cross-exchange feed starting...")

        # Start CoinGlass funding rate fetcher
        self.coinglass.start()

        # Start Kalshi WebSocket feed (fills + orderbook)
        if self.kalshi_feed:
            try:
                self.kalshi_feed.start()
                logging.info("Kalshi WebSocket feed starting...")
            except Exception as e:
                logging.warning(f"Kalshi WebSocket feed failed to start: {e}")

        # Start SPX engine price feed (if enabled)
        if self.spx_engine:
            try:
                self.spx_engine.start()
                logging.info("SPX engine starting...")
            except Exception as e:
                logging.warning(f"SPX engine failed to start: {e}")
                self.spx_engine = None

        # Start Weather engine ensemble fetcher (if enabled)
        if self.weather_engine:
            try:
                self.weather_engine.start()
                logging.info("Weather engine starting...")
            except Exception as e:
                logging.warning(f"Weather engine failed to start: {e}")
                self.weather_engine = None

        # Start Sports engine (if enabled)
        if self.sports_engine:
            try:
                self.sports_engine.start()
                logging.info("Sports engine starting...")
            except Exception as e:
                logging.warning(f"Sports engine failed to start: {e}")
                self.sports_engine = None

        # Dashboard snapshot builder (used by Supabase syncer)
        try:
            from bot.snapshots.dashboard_snapshot import DashboardSnapshotBuilder
            self.snapshot_builder = DashboardSnapshotBuilder(self)
        except Exception as e:
            logging.info(f"Dashboard snapshot builder not available: {e}")
            self.snapshot_builder = None

        # Start Supabase syncer (if configured)
        try:
            from bot.snapshots.supabase_sync import SupabaseSyncer
            self.supabase_syncer = SupabaseSyncer(self)
            self.supabase_syncer.start()
        except Exception as e:
            logging.info(f"Supabase sync not available: {e}")
            self.supabase_syncer = None

        # Initial market scan
        self._refresh_active_windows()

        # Start H-3a NBBO snapshotter — AFTER both kalshi_feed.start() (so
        # orderbooks are subscribing) AND _refresh_active_windows() (so
        # the active-tickers provider returns a populated list rather
        # than producing a burst of 'ws_no_data' rows on every restart).
        # Round-1 wiring review #2.
        if self.market_obs_snapshotter is not None:
            try:
                self.market_obs_snapshotter.start()
                logging.info("Market observations snapshotter starting...")
            except Exception as e:
                logging.warning(
                    f"Market observations snapshotter failed to start: {e}"
                )

        logging.info(
            f"Startup complete. Monitoring {len(ASSETS)} assets "
            f"({', '.join(ASSETS)})"
        )

    # ── Periodic Tasks ────────────────────────────────────────────────────

    def _refresh_active_windows(self):
        # Build the merged list LOCALLY, then publish atomically.
        # Round 2 [A2] fix: previously we did
        #   self._active_windows = discover_active_windows(...)
        #   self._active_windows_updated_at = time.time()
        #   self._active_windows.extend(spx_windows)   # main thread
        #   self._active_windows.extend(wx_windows)    # could race
        # The reader (main thread) could observe the freshly-swapped
        # list AFTER the timestamp update but BEFORE SPX/weather
        # were merged in — silently scanning without SPX/weather
        # candidates while the watchdog reported "fresh". And
        # `list.extend()` is GIL-atomic but NOT safe against a
        # `for ... in list` iterator running on another thread.
        # Build-then-swap eliminates both races.
        new_windows = discover_active_windows(self.client)

        # Merge SPX windows (if engine available and market open)
        if self.spx_engine and self.spx_engine.is_market_open():
            try:
                spx_windows = self.spx_engine.get_active_windows(self.client)
                new_windows.extend(spx_windows)
            except Exception as e:
                logging.warning(f"SPX window discovery failed: {e}")

        # Merge weather windows (if engine available)
        if self.weather_engine:
            try:
                wx_windows = self.weather_engine.get_active_windows(self.client)
                new_windows.extend(wx_windows)
            except Exception as e:
                logging.warning(f"Weather window discovery failed: {e}")

        # Atomic ref-swap: main-thread readers see either the
        # previous list or this fully-merged list, never a partial
        # state. Python attribute writes on a list reference are
        # GIL-atomic.
        self._active_windows = new_windows
        n = len(new_windows)
        # Round 3 [A3] empty-list guard: do NOT bump the staleness
        # timestamp if the refresh produced 0 windows. Crypto 15M
        # markets are 24/7 and hourly are continuous, so an empty
        # list is a silent failure — most commonly Kalshi /events
        # returning empty for all 8 series. Leaving the timestamp
        # un-updated lets the staleness watchdog fire CACHE_STALE
        # after the budget elapses, surfacing the failure to
        # operators. We DO publish the empty list (replacing the
        # prior list with []) so scan() doesn't silently trade on
        # ancient windows — the staleness gate then prevents scan
        # from running on the empty list.
        # Round 4 [A3]: edge-triggered logging. Log once on the
        # transition into empty, once on recovery. CACHE_STALE
        # provides the sustained-state operator alert; this just
        # marks the boundaries.
        if n == 0:
            if not self._empty_refresh_in_progress:
                self._empty_refresh_in_progress = True
                logging.warning(
                    "Market refresh returned 0 active windows — "
                    "scanner idle, staleness timestamp NOT updated. "
                    "Watchdog will fire CACHE_STALE after "
                    "budget=%.0fs. (Subsequent empty refreshes "
                    "will not re-log until recovery.)",
                    ACTIVE_WINDOWS_STALENESS_BUDGET_S)
        else:
            if self._empty_refresh_in_progress:
                self._empty_refresh_in_progress = False
                logging.warning(
                    "Market refresh recovered: %d active windows "
                    "after empty-refresh episode.", n)
            # Step #5 freshness signal: timestamp set AFTER the
            # publish. If a reader sees `_active_windows_updated_at`
            # as fresh, it is GUARANTEED to have observed the
            # merged list (because the timestamp write
            # happens-after the ref-swap in program order under
            # the GIL).
            self._active_windows_updated_at = time.time()
            logging.debug(f"Refreshed: {n} active windows")

        self._last_market_refresh = time.time()

    def _subscribe_discovery_orderbooks(self):
        """Subscribe to WS orderbook_delta for all discovered tickers.

        Dashboard visibility ONLY — does NOT affect Scanner.scan(),
        execution, or any trading logic. Called every 30s after
        _refresh_active_windows(). Skips hourly tickers (observation only).
        """
        if not self.kalshi_feed or not self.kalshi_feed.is_connected:
            return
        try:
            active_tickers: set = set()
            for window in self._active_windows:
                if window.get("product_type") == "hourly":
                    continue  # skip hourly tickers from WS subscription
                for mkt in window.get("markets", []):
                    ticker = mkt.get("ticker", "")
                    if ticker:
                        active_tickers.add(ticker)

            # Phase 2.8 [P0]: diff against the AUTHORITATIVE
            # kalshi_feed.get_subscribed_tickers() — NOT the
            # private previous-cycle `_discovery_ob_tickers` view.
            # 5 paths add to _subscribed_tickers (this method,
            # scan(), lazy _get_orderbook ×2, PPO); only 2 paths
            # remove. Pre-fix, tickers added by the other 4 paths
            # (especially after their markets closed) accumulated
            # forever — causing post-close get_snapshot to hit
            # Kalshi code=7 "Unknown subscription ID", driving
            # WS_SUBSCRIBE_STUCK + WS_FORCE_RECONNECT loops.
            # Empirical: KXXRP15M-26APR251500-00 still being
            # snapshotted at 19:00 UTC (4h post-close).
            all_subscribed = set(
                self.kalshi_feed.get_subscribed_tickers())

            # Phase 2.8 R-review A1: empty-active short-circuit
            # MUST use `all_subscribed` (the authoritative set) —
            # NOT `_discovery_ob_tickers` (the now-deprecated
            # previous-cycle view). On the first cycle after a
            # restart where Kalshi /events transiently fails,
            # `_discovery_ob_tickers` is empty too → guard
            # doesn't trigger → mass-unsubscribe of every ticker
            # added by lazy _get_orderbook / scan() / PPO during
            # startup. Empty most commonly = Kalshi /events
            # transient failure; never trigger mass cleanup
            # under that condition.
            if not active_tickers and all_subscribed:
                logging.debug(
                    "discovery_ob_subscribe: skipping cycle — "
                    "active_tickers empty (Kalshi /events likely "
                    "transient-empty), keeping %d prior subs",
                    len(all_subscribed))
                return

            # Unsubscribe expired tickers from previous cycle.
            # Phase 2.8 R-review A2: protect ALL held-position
            # tickers (any product type), not just 15M.
            #
            # Phase 2.8 R2 / P1: protection is NOT gated on
            # POSITION_PRICE_MONITOR_ENABLED. PPO is one reason to
            # keep the WS feed for held tickers, but settlement
            # detection, fill reconciliation, and other
            # position-monitoring paths also depend on having
            # orderbook data while a position is open. Don't yank
            # the feed because a single optional flag is off.
            _held_tickers = set()
            try:
                for _hp in self.state.get_open_positions():
                    if _hp.get("status") == "open":
                        _t = _hp.get("ticker", "")
                        if _t:
                            _held_tickers.add(_t)
            except Exception:
                pass
            expired = all_subscribed - active_tickers - _held_tickers
            for ticker in expired:
                try:
                    self.kalshi_feed.unsubscribe_ticker(ticker)
                except Exception:
                    pass

            # Subscribe to current active tickers (idempotent)
            for ticker in active_tickers:
                try:
                    self.kalshi_feed.subscribe_ticker(ticker)
                except Exception:
                    pass

            if expired:
                # Phase 2.8 R-review A5: log actual ticker names
                # (sorted, capped) on first ship — the whole point
                # is "we don't know which paths leaked." Forensic
                # signal for confirming which sources are leaking.
                _expired_sorted = sorted(expired)
                _sample = _expired_sorted[:10]
                _truncated = "" if len(expired) <= 10 else f" (+{len(expired)-10} more)"
                logging.info(
                    "discovery_ob_cleanup: unsubscribed %d expired "
                    "tickers: %s%s",
                    len(expired), _sample, _truncated)
            # Keep _discovery_ob_tickers tracking for backward-compat
            # diagnostic logs. The actual cleanup diff is now
            # against _subscribed_tickers (authoritative).
            self._discovery_ob_tickers = active_tickers
        except Exception as e:
            logging.warning(f"discovery_ob_subscribe failed: {e}")

    # ── Calibration Backfill ─────────────────────────────────────────────

    def _backfill_calibration_data(self):
        """One-time backfill of raw_prob for calibration training data."""
        try:
            # Step A: Backfill evaluated_opportunities raw_prob
            rows = self.state.conn.execute(
                "SELECT id, asset, spot_price, threshold, volatility, "
                "seconds_to_close, z_score "
                "FROM evaluated_opportunities "
                "WHERE raw_prob IS NULL "
                "AND spot_price IS NOT NULL AND threshold IS NOT NULL "
                "AND volatility IS NOT NULL AND seconds_to_close IS NOT NULL"
            ).fetchall()

            eval_count = 0
            for row in rows:
                z = row["z_score"]
                if z is None:
                    spot = row["spot_price"]
                    thresh = row["threshold"]
                    vol = row["volatility"]
                    ttc = row["seconds_to_close"]
                    if vol <= 0 or ttc <= 0:
                        continue
                    sigma_move = spot * vol * math.sqrt(ttc / 5.0)
                    if sigma_move <= 0:
                        continue
                    z = (thresh - spot) / sigma_move
                raw_prob = ProbabilityEngine._cdf_complement(z, row["asset"])
                self.state.conn.execute(
                    "UPDATE evaluated_opportunities SET raw_prob = ? WHERE id = ?",
                    (raw_prob, row["id"]),
                )
                eval_count += 1
            if eval_count:
                self.state.conn.commit()
                logging.info("Backfill: updated raw_prob for %d evaluated_opportunities", eval_count)

            # Step B: Backfill rejected_opportunities raw_prob + market_result
            rej_results = {}
            if os.path.exists(REJECTION_JOURNAL):
                with open(REJECTION_JOURNAL, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if entry.get("type") == "rejection_settlement":
                            rej_results[entry["ticker"]] = entry["market_result"]

            rej_rows = self.state.conn.execute(
                "SELECT ticker, asset, z_score FROM rejected_opportunities "
                "WHERE raw_prob IS NULL AND z_score IS NOT NULL"
            ).fetchall()

            rej_count = 0
            for row in rej_rows:
                z = row["z_score"]
                raw_prob = ProbabilityEngine._cdf_complement(z, row["asset"])
                market_result = rej_results.get(row["ticker"])
                self.state.conn.execute(
                    "UPDATE rejected_opportunities SET raw_prob = ?, market_result = ? "
                    "WHERE ticker = ?",
                    (raw_prob, market_result, row["ticker"]),
                )
                rej_count += 1
            if rej_count:
                self.state.conn.commit()
                logging.info("Backfill: updated raw_prob for %d rejected_opportunities", rej_count)

        except Exception as e:
            logging.warning("Backfill calibration data failed: %s", e)

    # ── Daily Summary ─────────────────────────────────────────────────────

    def _log_daily_summary(self):
        """Log aggregated daily performance metrics on date change."""
        try:
            today = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._last_summary_date is None:
                self._last_summary_date = today
                return
            if today == self._last_summary_date:
                return

            yesterday = self._last_summary_date
            self._last_summary_date = today

            # Aggregate settled trades for yesterday
            rows = self.state.conn.execute(
                "SELECT * FROM settled_trades WHERE settled_at LIKE ?",
                (yesterday + "%",)
            ).fetchall()

            total_pnl = 0
            total_fees = 0
            wins = 0
            losses = 0
            per_asset: Dict[str, Dict] = {}

            for row in rows:
                r = dict(row)
                pnl = r["pnl_cents"]
                total_pnl += pnl
                total_fees += r["fee_cents"]
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1

                a = r["asset"]
                if a not in per_asset:
                    per_asset[a] = {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0}
                per_asset[a]["trades"] += 1
                per_asset[a]["pnl_cents"] += pnl
                if pnl > 0:
                    per_asset[a]["wins"] += 1
                else:
                    per_asset[a]["losses"] += 1

            # Rejection counts for yesterday
            rej_rows = self.state.conn.execute(
                "SELECT rejection_reason, COUNT(*) as cnt FROM rejected_opportunities "
                "WHERE rejection_time LIKE ? GROUP BY rejection_reason",
                (yesterday + "%",)
            ).fetchall()
            rejection_counts = {r["rejection_reason"]: r["cnt"] for r in rej_rows}

            # Evaluated opportunity outcomes for yesterday
            eval_rows = self.state.conn.execute(
                "SELECT filter_stage, status, COUNT(*) as cnt FROM evaluated_opportunities "
                "WHERE evaluation_time LIKE ? GROUP BY filter_stage, status",
                (yesterday + "%",)
            ).fetchall()
            eval_counts = {}
            for r in eval_rows:
                stage = r["filter_stage"]
                if stage not in eval_counts:
                    eval_counts[stage] = {}
                eval_counts[stage][r["status"]] = r["cnt"]

            self.logger.log_performance({
                "summary_type": "daily",
                "date": yesterday,
                "total_trades": wins + losses,
                "wins": wins,
                "losses": losses,
                "total_pnl_cents": total_pnl,
                "total_fees_cents": total_fees,
                "per_asset": per_asset,
                "rejection_counts": rejection_counts,
                "evaluated_opportunity_counts": eval_counts,
            })
            logging.info(
                f"Daily summary ({yesterday}): {wins}W/{losses}L, "
                f"PnL={total_pnl}¢, fees={total_fees}¢"
            )
            if _telegram_state._TELEGRAM:
                _telegram_state._TELEGRAM.send(
                    f"\U0001f4c8 Daily ({yesterday}): {wins}W/{losses}L, "
                    f"PnL=${total_pnl / 100:+.2f}, fees=${total_fees / 100:.2f}"
                )

            # Sprint B Bit B.2b — 90-day retention on order_decision_snapshots.
            # Daily housekeeping hook (mirrors scripts/audit/audit_cron.prune_old).
            # Defensive: own try/except so a prune failure can't poison the
            # daily-summary path.
            try:
                self.state.prune_old_decision_snapshots(days=90)
            except Exception:
                logging.warning(
                    "prune_old_decision_snapshots failed in daily summary",
                    exc_info=True)
        except Exception as e:
            logging.debug(f"_log_daily_summary failed: {e}")

    # ── Main Tick ─────────────────────────────────────────────────────────

    def _active_windows_is_stale(self) -> bool:
        """Step #5 cache staleness watchdog. True if the Kalshi
        15M/hourly window cache (`_active_windows`) is older than
        ACTIVE_WINDOWS_STALENESS_BUDGET_S — meaning the refresh
        worker thread either hasn't run yet (initial state) or has
        silently died. The scan call site uses this to fail closed
        rather than trade on stale window data.

        Scope and known limits:
          • This is a FRESHNESS watchdog — covers "did the refresh
            worker run recently". It does NOT cover data-quality
            failures: `discover_active_windows` is not atomic
            across its 8 series-level GET /events calls, so a
            mid-loop circuit-breaker trip can publish a partial
            list while keeping the timestamp fresh. Round 3 [A2].
          • Empty-list publication IS protected by the
            non-empty check in `_refresh_active_windows` (Round
            3 [A3]) — an empty refresh result does not bump the
            timestamp, so the watchdog fires CACHE_STALE.
          • Other readers of `self._active_windows` exist outside
            the gate: `_subscribe_discovery_orderbooks` reads it
            from the worker thread (right after the publish, no
            race), and dashboard exporters may read it as a
            snapshot. The gate covers `_tick`'s scan-relevant
            iteration + `scanner.scan()` only. Round 3 [A1].
          • SPX/weather merges share this single timestamp — they
            have no independent freshness signal at this layer.
            Round 2 [A3]."""
        last = self._active_windows_updated_at
        if last <= 0.0:
            return True  # uninitialized
        return (time.time() - last) > ACTIVE_WINDOWS_STALENESS_BUDGET_S

    # Minimum sustained stale duration before the episode-start
    # warning fires. Round 3 [A4]: making start AND recovery use
    # the same threshold makes flicker dedup symmetric. A brief
    # outage (worker recovers in <10s) emits NO log lines on
    # either side; sustained outages emit a coherent
    # start→heartbeat→recovery sequence.
    _CACHE_STALE_MIN_EPISODE_S = 10.0

    def _maybe_log_cache_stale(self) -> None:
        """Emit a CACHE_STALE warning at most once per sustained
        stale episode, with a 60s heartbeat thereafter. Called
        every tick while the cache is stale; without dedup, a
        multi-hour stall logs 3,600+ identical lines/hr and drowns
        out other signals.

        Round 3 [A4]: deferred-start. The episode-start line is
        held back until the episode has lasted
        `_CACHE_STALE_MIN_EPISODE_S` seconds. This makes start
        and recovery symmetric: brief flickers below the
        threshold emit NEITHER a start nor a recovery line. The
        operator only ever sees a coherent pair (or neither).
        `_cache_stale_episode_started_at` records WHEN the stale
        episode began (set on first stale tick); `_cache_stale_episode_logged`
        flips True once the start line fires."""
        now = time.time()
        last = self._active_windows_updated_at
        budget = ACTIVE_WINDOWS_STALENESS_BUDGET_S
        # First stale tick of an episode: record the start time
        # but do NOT log yet — wait for the episode to outlive the
        # flicker threshold.
        if self._cache_stale_episode_started_at <= 0.0:
            self._cache_stale_episode_started_at = now
            self._cache_stale_last_heartbeat_at = now
            self._cache_stale_episode_logged = False
            return
        episode_age = now - self._cache_stale_episode_started_at
        # Emit the deferred episode-start line once the episode
        # outlives the flicker threshold.
        if (not self._cache_stale_episode_logged
                and episode_age >= self._CACHE_STALE_MIN_EPISODE_S):
            self._cache_stale_episode_logged = True
            self._cache_stale_last_heartbeat_at = now
            if last <= 0.0:
                logging.warning(
                    "CACHE_STALE: active_windows never refreshed "
                    "(uninitialized, budget=%.0fs, sustained=%.0fs) "
                    "— skipping scan; settlement and PPO continue. "
                    "Refresh worker may have failed on first run.",
                    budget, episode_age)
            else:
                age = now - last
                logging.warning(
                    "CACHE_STALE: active_windows age=%.1fs > "
                    "budget=%.0fs (sustained=%.0fs) — skipping scan; "
                    "settlement and PPO continue. Worker thread may "
                    "be stuck or dead.",
                    age, budget, episode_age)
            return
        # Heartbeat — only after the start line has fired.
        if (self._cache_stale_episode_logged
                and (now - self._cache_stale_last_heartbeat_at) >= 60.0):
            self._cache_stale_last_heartbeat_at = now
            age_str = (
                "%.1fs" % (now - last) if last > 0.0 else "uninitialized")
            logging.warning(
                "CACHE_STALE_HEARTBEAT: still stale after %.0fs "
                "(active_windows age=%s, budget=%.0fs)",
                episode_age, age_str, budget)

    def _maybe_log_cache_fresh_recovery(self) -> None:
        """Emit one CACHE_FRESH recovery line when the cache returns
        to fresh after a stale episode, then reset dedup state.

        Round 3 [A4]: symmetric with `_maybe_log_cache_stale`'s
        deferred-start. The recovery line ONLY fires if the
        episode-start line fired (i.e., the episode outlived
        `_CACHE_STALE_MIN_EPISODE_S`). Brief flickers leave no
        trace on either side. Sustained stalls produce a
        coherent start→heartbeat→recovery triple."""
        if self._cache_stale_episode_started_at > 0.0:
            if self._cache_stale_episode_logged:
                episode_age = (
                    time.time() - self._cache_stale_episode_started_at)
                logging.warning(
                    "CACHE_FRESH: active_windows recovered after "
                    "%.0fs stale episode — scan re-enabled.",
                    episode_age)
            self._cache_stale_episode_started_at = 0.0
            self._cache_stale_last_heartbeat_at = 0.0
            self._cache_stale_episode_logged = False

    def _tick(self):
        now = time.time()

        # Update peak balance from main thread (dashboard reads only)
        cached_bal = self.scanner._balance_cache[0]
        if cached_bal is not None:
            bal_dollars = cached_bal / 100.0
            if bal_dollars > self._peak_balance:
                self._peak_balance = bal_dollars

        # Per-task timing — emits PERIODIC_TASK_SLOW when any single task
        # exceeds 1.5s. Apr 25 00:30 UTC SLOW_SCAN_TICK still firing after
        # the WS drift probe was threaded — there's another dominant
        # blocker in this cluster. This instrumentation names which task
        # is to blame in the next round of logs.
        _PERIODIC_SLOW_THRESHOLD_S = 1.5

        # Refresh market list periodically — runs in a daemon worker
        # thread so 9+ Kalshi REST calls don't block the main scan
        # loop. _active_windows reassignment is atomic (Python ref
        # write); readers see either the prior list or the new one.
        # `_market_refresh_running` prevents thread pile-up if a
        # cycle exceeds MARKET_REFRESH_SECONDS. (Apr 25 00:45 fix.)
        if (now - self._last_market_refresh >= MARKET_REFRESH_SECONDS
                and not self._market_refresh_running):
            self._last_market_refresh = now
            self._market_refresh_running = True

            def _market_refresh_worker():
                try:
                    _t = time.perf_counter()
                    self._refresh_active_windows()
                    _dt = time.perf_counter() - _t
                    if _dt > _PERIODIC_SLOW_THRESHOLD_S:
                        logging.warning(
                            "PERIODIC_TASK_SLOW: refresh_active_windows "
                            "took %.2fs (worker thread)", _dt)
                    _t = time.perf_counter()
                    self._subscribe_discovery_orderbooks()
                    _dt = time.perf_counter() - _t
                    if _dt > _PERIODIC_SLOW_THRESHOLD_S:
                        logging.warning(
                            "PERIODIC_TASK_SLOW: "
                            "subscribe_discovery_orderbooks took %.2fs "
                            "(worker thread)", _dt)
                except Exception:
                    logging.error(
                        "market_refresh_worker failed", exc_info=True)
                finally:
                    self._market_refresh_running = False

            try:
                threading.Thread(
                    target=_market_refresh_worker,
                    daemon=True,
                    name="market_refresh",
                ).start()
            except Exception:
                self._market_refresh_running = False
                logging.debug(
                    "market_refresh worker spawn failed",
                    exc_info=True)

        # Phase 2: periodic WS re-snapshot of currently-subscribed
        # 15M tickers. Insurance against H3' (msg loss without seq
        # increment) per ws-cache-drift-investigation.md. Industry
        # standard practice: snapshot reset is the only reliable
        # cure for cumulative WS cache drift.
        #
        # We read subscribed tickers from the WS feed via
        # `get_subscribed_tickers()` (R1/A3 [P1] — locked snapshot
        # so the WS thread can mutate the set concurrently). This
        # loop:
        #   (a) doesn't depend on active_windows freshness — runs
        #       even if the staleness gate trips
        #   (b) targets the actual set of tickers whose WS cache
        #       could be drifted
        #   (c) doesn't trigger the Step #5 AST tripwire that
        #       prevents iterating stale window data before vol.update
        #
        # R1 / A2 [P0]: pass purge_cache=False so the periodic sweep
        # does NOT simultaneously evict caches for every 15M ticker
        # (that would create a system-wide ~5s gap every 5 min).
        # Detected-drift callers (flag_ticker_drifted) keep the
        # default purge=True since we KNOW that cache is corrupt.
        #
        # R1 / A7 [P1]: pass bypass_cooldown=True so periodic
        # insurance always runs, even if flag_ticker_drifted fired
        # for the same ticker within WS_FORCE_RESUB_COOLDOWN_S.
        if (now - self._last_ws_periodic_resnap
                >= WS_PERIODIC_RESNAPSHOT_INTERVAL_S):
            self._last_ws_periodic_resnap = now
            if (self.kalshi_feed is not None
                    and self.kalshi_feed.is_connected):
                try:
                    _subscribed = (
                        self.kalshi_feed.get_subscribed_tickers())
                    _resnap_tickers = [
                        t for t in _subscribed if "15M" in t.upper()]
                    for t in _resnap_tickers:
                        self.kalshi_feed.force_resubscribe(
                            t,
                            purge_cache=False,
                            bypass_cooldown=True,
                            track_recovery=False,
                        )
                    if _resnap_tickers:
                        logging.info(
                            "WS_PERIODIC_RESNAP: requested fresh "
                            "snapshots for %d 15M tickers "
                            "(interval=%ds, purge=False)",
                            len(_resnap_tickers),
                            int(WS_PERIODIC_RESNAPSHOT_INTERVAL_S))
                except Exception:
                    logging.warning(
                        "WS_PERIODIC_RESNAP loop failed",
                        exc_info=True)

        # Check settlements periodically (self-throttled)
        _t = time.perf_counter()
        self.tracker.tick()
        _dt = time.perf_counter() - _t
        if _dt > _PERIODIC_SLOW_THRESHOLD_S:
            logging.warning(
                "PERIODIC_TASK_SLOW: tracker_tick took %.2fs", _dt)

        self._log_daily_summary()

        # Periodic WAL checkpoint (every 60s) — prevents WAL bloat that causes
        # "database is locked" across shadow engines with 7 concurrent connections.
        # Uses PASSIVE (not TRUNCATE) — TRUNCATE requires exclusive lock that
        # deadlocks with supabase_sync reader + settlement writer. PASSIVE
        # checkpoints whatever pages it can without blocking. (Mar 16 2026)
        if now - self._last_wal_checkpoint >= 60.0:
            self._last_wal_checkpoint = now  # Update BEFORE attempt — prevents hot retry loop
            _t = time.perf_counter()
            try:
                self.state.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception:
                self._db_locked_count += 1
                logging.debug("WAL checkpoint failed (busy)", exc_info=True)
            _dt = time.perf_counter() - _t
            if _dt > _PERIODIC_SLOW_THRESHOLD_S:
                logging.warning(
                    "PERIODIC_TASK_SLOW: wal_checkpoint took %.2fs", _dt)

        # DB health watchdog (every 5 minutes)
        if now - self._last_db_health_check >= 300.0:
            try:
                self._check_db_health()
            except Exception:
                logging.debug("DB health check failed", exc_info=True)
            self._last_db_health_check = now

        # Periodic calibration retrain check
        _t = time.perf_counter()
        if self.calibration:
            self.calibration.maybe_retrain()
        for _rk, _eng in self._cal_engines.items():
            _eng.maybe_retrain()
        _dt = time.perf_counter() - _t
        if _dt > _PERIODIC_SLOW_THRESHOLD_S:
            logging.warning(
                "PERIODIC_TASK_SLOW: calibration_retrain took %.2fs", _dt)

        # Periodic EGARCH MLE refit — runs in a daemon worker thread.
        # Apr 25 01:02: scipy L-BFGS-B fit across 4 assets blocked the
        # main thread for 53.30s. Same threading pattern as
        # SettlementTracker (8114ddc) and market refresh (f216a8d).
        # `_egarch_refit_running` prevents pile-up if a refit cycle
        # exceeds the next _tick() interval. Thread safety: maybe_refit
        # writes `self._params[asset] = new_params` (atomic dict-item
        # assignment under GIL); EGARCH readers in update() see either
        # old or new params, never partial.
        if self.egarch_estimator and not self._egarch_refit_running:
            self._egarch_refit_running = True

            def _egarch_refit_worker():
                _wt = time.perf_counter()
                try:
                    self.egarch_estimator.maybe_refit()
                    _wdt = time.perf_counter() - _wt
                    if _wdt > _PERIODIC_SLOW_THRESHOLD_S:
                        logging.warning(
                            "PERIODIC_TASK_SLOW: egarch_refit took "
                            "%.2fs (worker thread)", _wdt)
                except Exception:
                    logging.error(
                        "egarch_refit worker thread failed",
                        exc_info=True)
                finally:
                    self._egarch_refit_running = False

            try:
                threading.Thread(
                    target=_egarch_refit_worker,
                    daemon=True,
                    name="egarch_refit",
                ).start()
            except Exception:
                self._egarch_refit_running = False
                logging.debug(
                    "egarch_refit worker spawn failed", exc_info=True)

        # Recompute seconds_to_close and log each window (skip hourly vol diagnostics)
        utc_now = datetime.datetime.now(timezone.utc)
        prices = self.feed.get_all_prices()

        # Feed price data to hourly alt shadow engine for HAR-RV return computation
        if self.hourly_alt_shadow:
            _alt_ts = time.time()
            for _alt_asset, _alt_price in prices.items():
                if _alt_price is not None and _alt_price > 0:
                    try:
                        self.hourly_alt_shadow.ingest_price(_alt_asset, _alt_price, _alt_ts)
                    except Exception:
                        pass

        # Feed SPX price to HAR-RV shadow engine for return computation
        if self.spx_harrv_shadow and self.spx_engine:
            try:
                _spx_spot = self.spx_engine.get_spot_price("SPX")
                if _spx_spot is not None and _spx_spot > 0:
                    self.spx_harrv_shadow.ingest_price(_spx_spot, time.time())
            except Exception:
                pass

        # Per-window iteration (vol.update + scan_journal) is now
        # gated below alongside scanner.scan() so a stale window
        # list can't feed VolatilityEngine negative STCs or pollute
        # the scan journal with stale `seconds_to_close` values.
        # See `_active_windows_is_stale` block below.
        # Poll active executor orders (maker fill check — one per asset)
        self.executor.tick()

        # SOL Path C shadow: check orderbook every tick during escalation window
        try:
            self.executor._tick_sol_pathc_observations()
        except Exception:
            logging.debug("sol_pathc_obs tick failed", exc_info=True)

        # Check confirmation addon opportunities on open positions
        try:
            self.executor._check_addon_opportunities()
        except Exception:
            logging.debug("addon check failed", exc_info=True)

        # Check dip addon opportunities on open positions
        try:
            self.executor._check_dip_addon_opportunities()
        except Exception:
            logging.debug("dip addon check failed", exc_info=True)

        # Process DC IOC retry queue (non-blocking — each retry is <1s)
        try:
            self.executor.process_dc_retries()
        except Exception:
            logging.warning("DC retry processing failed", exc_info=True)

        # Record CASH balance for HWM tracking (once per tick).
        # Uses available cash only — NOT portfolio value (cash + positions).
        # Kalshi available_balance does NOT drop when collateral is locked, so
        # cash-only is safe. Portfolio tracking caused HWM inflation when DC
        # positions opened, compressing ds even on profitable accounts.
        # (Learned: portfolio HWM $2,035 vs cash $1,485 → ds=0.25 for 7 days. Mar 29 2026)
        # Must use full cash balance, NOT fractional bankroll (hourly 10%, SPX 15%).
        # (Learned: fractional bankroll in compute() poisoned HWM for 18h, Mar 25-26 2026)
        try:
            _hwm_balance = self.scanner._get_balance_cached()
            if _hwm_balance and _hwm_balance > 0:
                self.sizer.record_balance(_hwm_balance)
                # Alert on 3+ consecutive spike rejections
                if self.sizer._consecutive_spike_rejections >= 3:
                    if self.sizer._consecutive_spike_rejections == 3:
                        _msg = (
                            "\u26a0\ufe0f HWM spike alert: 3 consecutive rejections. "
                            f"Balance={_hwm_balance}c, "
                            f"last_accepted={self.sizer._balance_history[-1][1] if self.sizer._balance_history else 'none'}c"
                        )
                        logging.warning(_msg)
                        if _telegram_state._TELEGRAM:
                            _telegram_state._TELEGRAM.send(_msg)
        except Exception:
            logging.debug("HWM balance recording failed", exc_info=True)

        # Step #5 cache staleness watchdog: gates ALL code that
        # reads `self._active_windows` for trading decisions. The
        # gated block contains BOTH the per-window iteration (which
        # calls `vol.update(seconds_to_close=...)` — feeding stale
        # negative STC values would corrupt the vol engine) AND the
        # scanner.scan() call. Settlement, executor.tick(), PPO, and
        # weather PPO are NOT gated — they read different state and
        # are time-sensitive in their own right (settlement is
        # idempotent at the DB layer; PPO is observation-only).
        # Scope: covers Kalshi 15M/hourly windows (the cache that
        # `_active_windows_updated_at` actually tracks). SPX and
        # weather merges share the same timestamp but have no
        # independent freshness signal — see Round 2 [A3] follow-up.
        candidates = None
        if self._active_windows_is_stale():
            self._maybe_log_cache_stale()
        else:
            self._maybe_log_cache_fresh_recovery()
            # Take a stable local snapshot of the window list for
            # this tick. The refresh worker does an atomic ref-swap
            # (`_refresh_active_windows` builds locally then
            # publishes — no in-place extend), so the snapshot
            # pins the iterator+scanner to whichever list version
            # was current at load time. A subsequent ref-swap mid-
            # tick produces no surprise: for-loop iterates the OLD
            # list, scan() gets the OLD list, both consistent. The
            # NEXT tick's snapshot picks up the new ref. CPython's
            # GIL makes the ref-load atomic, so `_local_windows` is
            # never partially observed.
            _local_windows = self._active_windows
            for window in _local_windows:
                seconds_to_close = (window["close_time"] - utc_now).total_seconds()
                window["seconds_to_close"] = seconds_to_close

                if window.get("product_type") == "hourly":
                    continue  # volume control: skip scan journal writes for hourly

                in_range = (
                    MIN_SECONDS_BEFORE_CLOSE
                    <= seconds_to_close
                    <= MAX_SECONDS_BEFORE_CLOSE
                )

                asset = window["asset"]
                vol_estimate = self.vol.update(asset, seconds_to_close=seconds_to_close)

                scan_entry = {
                    "asset": asset,
                    "event_ticker": window["event_ticker"],
                    "seconds_to_close": round(seconds_to_close, 1),
                    "in_trading_range": in_range,
                    "num_markets": len(window["markets"]),
                    "spot_price": prices.get(asset),
                    "buffer_len": len(self.feed.get_buffer(asset)),
                }
                if vol_estimate:
                    scan_entry.update({
                        "rv_1min": round(vol_estimate["rv_1min"], 8),
                        "rv_5min": round(vol_estimate["rv_5min"], 8),
                        "rv_15min": round(vol_estimate["rv_15min"], 8),
                        "blended_rv": round(vol_estimate["blended_rv"], 8),
                        "vol_regime": vol_estimate["regime"],
                        "vol_returns": vol_estimate["num_returns"],
                        "bv_1min": round(vol_estimate.get("bv_1min", 0), 8),
                        "jump_component": round(vol_estimate.get("jump_component", 0), 8),
                        "dvol_5s": round(vol_estimate["dvol_5s"], 8) if vol_estimate.get("dvol_5s") is not None else None,
                        "iv_rv_blend_method": vol_estimate.get("iv_rv_blend_method"),
                        "fixed_blend_rv": round(vol_estimate.get("fixed_blend_rv", 0), 8),
                        "jump_multiplier": vol_estimate.get("jump_multiplier", 1.0),
                        "jump_event_count": vol_estimate.get("jump_event_count", 0),
                        "egarch_sigma": round(vol_estimate["egarch_sigma"], 8) if vol_estimate.get("egarch_sigma") is not None else None,
                        "egarch_n_updates": vol_estimate.get("egarch_n_updates", 0),
                        "egarch_log_var": round(vol_estimate.get("egarch_log_var", 0), 4) if vol_estimate.get("egarch_log_var") is not None else None,
                        "egarch_vs_rv_ratio": round(vol_estimate["egarch_sigma"] / vol_estimate["blended_rv"], 4) if vol_estimate.get("egarch_sigma") and vol_estimate.get("blended_rv") and vol_estimate["blended_rv"] > 0 else None,
                        # Adaptive RK bandwidth
                        "omega_sq": vol_estimate.get("omega_sq"),
                        "rk_H_adaptive_5": vol_estimate.get("rk_H_adaptive_5"),
                        "rk_H_adaptive_15": vol_estimate.get("rk_H_adaptive_15"),
                        "rk_H_fixed_5": vol_estimate.get("rk_H_fixed_5"),
                        "rk_H_fixed_15": vol_estimate.get("rk_H_fixed_15"),
                        "ark_5min": round(vol_estimate.get("ark_5min", 0), 8) if vol_estimate.get("ark_5min") is not None else None,
                        "ark_15min": round(vol_estimate.get("ark_15min", 0), 8) if vol_estimate.get("ark_15min") is not None else None,
                        "rk_adaptive_delta_5": vol_estimate.get("rk_adaptive_delta_5", 0),
                        "rk_adaptive_delta_15": vol_estimate.get("rk_adaptive_delta_15", 0),
                        # DVOL diagnostics
                        "dvol_sq_hourly": round(vol_estimate["dvol_sq_hourly"], 10) if vol_estimate.get("dvol_sq_hourly") is not None else None,
                        "vrp": round(vol_estimate["vrp"], 10) if vol_estimate.get("vrp") is not None else None,
                        # Shadow TV RK weights
                        "shadow_tv_blend_rv": round(vol_estimate["shadow_tv_blend_rv"], 8) if vol_estimate.get("shadow_tv_blend_rv") is not None else None,
                        "shadow_tv_weights": vol_estimate.get("shadow_tv_weights"),
                    })
                # Order flow snapshot
                if self.order_flow is not None:
                    try:
                        ofa = self.order_flow.get_signals(asset)
                        scan_entry["ofa_adjustment"] = round(ofa["prob_adjustment"], 6)
                        scan_entry["ofa_confidence"] = ofa["confidence"]
                        cx = ofa["signals"].get("cross_exchange", {})
                        scan_entry["cross_ex_consensus"] = cx.get("consensus_direction")
                        scan_entry["cross_ex_above"] = cx.get("exchanges_above", 0)
                        fn = ofa["signals"].get("funding", {})
                        scan_entry["funding_rate"] = fn.get("rate")
                        scan_entry["funding_level"] = fn.get("level")
                    except Exception:
                        pass
                self.logger.log_scan(scan_entry)

            # Run opportunity scanner (always — execute() rejects if asset already active)
            # Body-duration timing — SCAN_BODY_SLOW fires when scan()'s own
            # execution exceeds 1.5s. Distinguishes "scan body is slow"
            # from "work between scan calls is slow". The latter would
            # show in the SLOW_SCAN_TICK gap measurement without showing
            # here. Apr 25 00:55 incident: SLOW_SCAN_TICK 6.13s with zero
            # PERIODIC_TASK_SLOW means the blocker is in scan() body
            # itself OR in tick body outside the periodic-task cluster.
            _scan_body_start_perf = time.perf_counter()
            try:
                candidates = self.scanner.scan(_local_windows)
            finally:
                _scan_body_dt = time.perf_counter() - _scan_body_start_perf
                if _scan_body_dt > 1.5:
                    logging.warning(
                        "SCAN_BODY_SLOW: scanner.scan body took %.2fs",
                        _scan_body_dt)
            if self.scanner._last_scan_stats:
                try:
                    self.logger.log_scan({
                        "type": "scan_summary",
                        "per_asset": self.scanner._last_scan_stats,
                        "had_candidate": candidates is not None,
                    })
                except Exception:
                    pass
            if candidates:
                for candidate in candidates:
                    logging.info(
                        f"Opportunity: {candidate['ticker']} "
                        f"ask={candidate['best_yes_ask']}¢ "
                        f"edge={candidate['edge']:.2%} "
                        f"size={candidate['position_size']} "
                        f"prob={candidate['calibrated_prob']:.2%}"
                    )
                    self.executor.execute(candidate)

        # ── Post-entry position price monitor (v2: spot-price primary) ─────
        # Logs spot price + Kalshi quotes for held 15M positions every tick.
        # Spot from CoinbaseFeed (always available). Kalshi quotes best-effort.
        # NEVER skips — logs spot even when Kalshi books are empty.
        if POSITION_PRICE_MONITOR_ENABLED:
            try:
                _ppo_positions = self.state.get_open_positions()
                _ppo_wrote = False
                for pos in _ppo_positions:
                    if pos.get("status") != "open":
                        continue
                    _ppo_ticker = pos["ticker"]
                    _ppo_asset = pos.get("asset", "")

                    # Only 15M positions
                    if "15M" not in _ppo_ticker.upper():
                        continue

                    # Ensure held-position ticker is WS-subscribed for orderbook data
                    if self.kalshi_feed and self.kalshi_feed.is_connected:
                        try:
                            self.kalshi_feed.subscribe_ticker(_ppo_ticker)
                        except Exception:
                            pass

                    # 1. SPOT PRICE (always available from CoinbaseFeed)
                    _ppo_spot = None
                    try:
                        _ppo_spot = self.feed.get_price(_ppo_asset)
                    except Exception:
                        pass
                    if _ppo_spot is None:
                        continue  # CoinbaseFeed disconnected — only skip condition

                    # 2. THRESHOLD (cached per position lifecycle)
                    if not hasattr(self, "_ppo_thresholds"):
                        self._ppo_thresholds = {}
                    if _ppo_ticker not in self._ppo_thresholds:
                        try:
                            _t_row = self.state.conn.execute(
                                "SELECT threshold FROM evaluated_opportunities WHERE ticker=? AND threshold IS NOT NULL LIMIT 1",
                                (_ppo_ticker,)).fetchone()
                            self._ppo_thresholds[_ppo_ticker] = _t_row[0] if _t_row else None
                        except Exception:
                            self._ppo_thresholds[_ppo_ticker] = None
                    _ppo_threshold = self._ppo_thresholds.get(_ppo_ticker)

                    # 3. STC (parse from event_ticker — deterministic, no _active_windows dependency)
                    _ppo_stc = None
                    try:
                        _ppo_et = pos.get("event_ticker") or ""
                        _ppo_ts_str = _ppo_et.split("-")[1] if "-" in _ppo_et else ""
                        if len(_ppo_ts_str) >= 11:
                            _ppo_yr = int("20" + _ppo_ts_str[:2])
                            _ppo_mon_str = _ppo_ts_str[2:5].upper()
                            _ppo_months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                                           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
                            _ppo_mon = _ppo_months.get(_ppo_mon_str, 1)
                            _ppo_day = int(_ppo_ts_str[5:7])
                            _ppo_hr = int(_ppo_ts_str[7:9])
                            _ppo_min = int(_ppo_ts_str[9:11])
                            # Ticker time is ET (UTC-4 during EDT). Convert to UTC.
                            _ppo_close_et = datetime.datetime(_ppo_yr, _ppo_mon, _ppo_day,
                                                              _ppo_hr, _ppo_min, 0)
                            _ppo_close_utc = _ppo_close_et.replace(tzinfo=timezone.utc) + datetime.timedelta(hours=4)
                            _ppo_stc = (_ppo_close_utc - datetime.datetime.now(timezone.utc)).total_seconds()
                    except Exception:
                        pass

                    # 4. KALSHI QUOTES (best effort — often None near settlement)
                    _ppo_ask = None
                    _ppo_bid = None
                    _ppo_source = "spot_only"
                    try:
                        _ppo_ob = self.kalshi_feed.get_orderbook(_ppo_ticker) if self.kalshi_feed else None
                        if _ppo_ob and (time.time() - _ppo_ob.get("ts", 0)) < POSITION_PRICE_MONITOR_WS_STALE_SEC:
                            _ppo_ask = OpportunityScanner._best_yes_ask_cents(_ppo_ob)
                            _ppo_bid = OrderExecutor._best_yes_bid(_ppo_ob)
                            if _ppo_ask is not None:
                                _ppo_source = "ws"
                    except Exception:
                        pass
                    if _ppo_ask is None:
                        try:
                            _ppo_mkt = self.client.get_market(_ppo_ticker)
                            if _ppo_mkt:
                                _ppo_mkt_data = _ppo_mkt.get("market", _ppo_mkt)
                                # Use *_dollars fields (FP transition Feb 26 2026)
                                _ppo_ya = _ppo_mkt_data.get("yes_ask_dollars") or _ppo_mkt_data.get("yes_ask")
                                _ppo_yb = _ppo_mkt_data.get("yes_bid_dollars") or _ppo_mkt_data.get("yes_bid")
                                if _ppo_ya is not None:
                                    _ppo_ask = dollars_str_to_cents(_ppo_ya) if isinstance(_ppo_ya, str) else int(_ppo_ya)
                                if _ppo_yb is not None:
                                    _ppo_bid = dollars_str_to_cents(_ppo_yb) if isinstance(_ppo_yb, str) else int(_ppo_yb)
                                # Filter out zero prices (market not yet active)
                                if _ppo_ask == 0:
                                    _ppo_ask = None
                                if _ppo_bid == 0:
                                    _ppo_bid = None
                                if _ppo_ask is not None:
                                    _ppo_source = "rest"
                        except Exception:
                            pass

                    # 5. COMPUTED FIELDS
                    _ppo_buffer = None
                    if _ppo_threshold and _ppo_threshold > 0:
                        _ppo_buffer = round((_ppo_spot - _ppo_threshold) / _ppo_threshold * 100, 4)

                    # 6. INSERT — ALWAYS (never skip when we have spot)
                    _ppo_now_str = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    # Per-level orderbook ladder via shared cache + freshness gate.
                    # Stale entry → NULL (honest); 5-10 ticks of grace at 1-2s/tick.
                    _ppo_ob_ladder = self.state._get_fresh_ob_ladder(_ppo_ticker)
                    self.state.conn.execute(
                        """INSERT INTO position_price_observations
                           (ticker, asset, observation_time, seconds_to_close,
                            spot_price, threshold, spot_buffer_pct,
                            yes_ask_cents, yes_bid_cents,
                            entry_price_cents, position_count, source,
                            orderbook_levels_json)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (_ppo_ticker, _ppo_asset, _ppo_now_str,
                         round(_ppo_stc, 1) if _ppo_stc is not None else None,
                         round(_ppo_spot, 6), _ppo_threshold, _ppo_buffer,
                         _ppo_ask, _ppo_bid,
                         pos.get("avg_price_cents", 0), pos.get("count", 0),
                         _ppo_source, _ppo_ob_ladder))
                    _ppo_wrote = True

                    # 7. SHADOW EXIT SIGNAL — would early exit trigger here?
                    # Track rolling buffer observations per ticker for 30-obs window.
                    # Signal fires once per ticker per hold (deduped via _exit_signal_fired).
                    if _ppo_buffer is not None:
                        if not hasattr(self, "_ppo_buffer_history"):
                            self._ppo_buffer_history = {}
                        if not hasattr(self, "_exit_signal_fired"):
                            self._exit_signal_fired = set()
                        hist = self._ppo_buffer_history.setdefault(_ppo_ticker, [])
                        hist.append(_ppo_buffer)
                        # Keep only last 60 observations (memory bound)
                        if len(hist) > 60:
                            hist[:] = hist[-60:]

                        if _ppo_ticker not in self._exit_signal_fired:
                            _exit_signal = None
                            # Signal 1: buffer below -0.10% (high precision, 68%)
                            if _ppo_buffer < -0.10:
                                _exit_signal = "buffer_below_-0.10"
                            # Signal 2: buffer below -0.05% with STC > 150s gate
                            # Best net benefit (+$383/10d), catches 77% of losses,
                            # kills only 1.2% of wins. SOL uses -0.10% (wider
                            # threshold due to higher volatility / false exit rate).
                            elif _ppo_stc is not None and _ppo_stc > 150:
                                _exit_thresh = -0.10 if _ppo_asset == "SOL" else -0.05
                                if _ppo_buffer < _exit_thresh:
                                    _exit_signal = f"buffer_below_{_exit_thresh:.2f}_stc_gt_150"

                            if _exit_signal:
                                self._exit_signal_fired.add(_ppo_ticker)
                                _entry_cents = pos.get("avg_price_cents", 0)
                                _pos_count = pos.get("count", 0)
                                # Counterfactual: what would exiting at current bid save?
                                _cf_exit_pnl = None
                                if _ppo_bid and _entry_cents and _pos_count:
                                    _sell_revenue = _ppo_bid * _pos_count
                                    _buy_cost = _entry_cents * _pos_count
                                    _sell_fee = calculate_taker_fee(_pos_count, _ppo_bid)
                                    _cf_exit_pnl = _sell_revenue - _buy_cost - _sell_fee
                                _neg30 = sum(1 for b in hist[-30:] if b < 0) / min(len(hist), 30) if hist else None
                                try:
                                    self.state.conn.execute(
                                        """INSERT INTO exit_signal_shadow
                                           (ticker, asset, signal_time, signal_type,
                                            buffer_at_signal, pct_negative_30,
                                            entry_price_cents, yes_bid_at_signal,
                                            position_count, seconds_to_close,
                                            counterfactual_exit_pnl_cents)
                                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                                        (_ppo_ticker, _ppo_asset, _ppo_now_str, _exit_signal,
                                         _ppo_buffer, _neg30,
                                         _entry_cents, _ppo_bid,
                                         _pos_count, round(_ppo_stc, 1) if _ppo_stc else None,
                                         _cf_exit_pnl))
                                except Exception:
                                    logging.warning("exit_signal_shadow insert failed", exc_info=True)
                                logging.info(
                                    "EXIT_SIGNAL_SHADOW: %s %s @%dc signal=%s buf=%.4f%% bid=%s cf_pnl=%s",
                                    _ppo_asset, _ppo_ticker, _entry_cents, _exit_signal,
                                    _ppo_buffer, _ppo_bid, _cf_exit_pnl)

                if _ppo_wrote:
                    self.state.conn.commit()
            except Exception:
                logging.warning("position_price_monitor failed", exc_info=True)

        # ── Weather position price monitor (REST, 15-min cadence) ─────────
        # Separate from 15M PPO: weather holds 18-24h, WS books are empty,
        # must use REST. Runs every 15 min to match ensemble refresh cadence.
        # Zero impact on 15M PPO (different cadence, different code path).
        if POSITION_PRICE_MONITOR_ENABLED:
            _wx_ppo_now = time.time()
            if _wx_ppo_now - getattr(self, '_wx_ppo_last_poll', 0) >= 900:
                try:
                    _wx_positions = [
                        p for p in self.state.get_open_positions()
                        if p.get("status") == "open"
                        and (p.get("ticker") or "").startswith("KXHIGH")
                    ]
                    if _wx_positions:
                        _wx_ppo_wrote = False
                        for pos in _wx_positions:
                            _wx_ticker = pos["ticker"]
                            _wx_asset = pos.get("asset", "")
                            try:
                                _wx_mkt = self.client.get_market(_wx_ticker)
                                if not _wx_mkt:
                                    continue
                                _wx_yes_ask = _wx_mkt.get("yes_ask")
                                _wx_yes_bid = _wx_mkt.get("yes_bid")
                                _wx_no_ask = _wx_mkt.get("no_ask")
                                _wx_no_bid = _wx_mkt.get("no_bid")
                                # For NO-side positions, the relevant prices are NO ask/bid.
                                # Store in yes_ask/yes_bid columns (reuse schema) but tag source.
                                _wx_display_ask = _wx_no_ask if pos.get("side") == "no" else _wx_yes_ask
                                _wx_display_bid = _wx_no_bid if pos.get("side") == "no" else _wx_yes_bid
                                # Get ensemble data for weather-specific context
                                _wx_city = _wx_asset.replace("_TEMP", "")
                                _wx_ens = None
                                if self.weather_engine:
                                    _wx_ens = self.weather_engine._last_ensemble.get(_wx_city)
                                _wx_ens_mean = None
                                _wx_threshold = None
                                _wx_buffer = None
                                if _wx_ens and _wx_ens.get("combined_members"):
                                    _wx_members = _wx_ens["combined_members"]
                                    _wx_ens_mean = sum(_wx_members) / len(_wx_members)
                                # Parse threshold from ticker
                                try:
                                    _wx_threshold = float(
                                        self.state.conn.execute(
                                            "SELECT threshold FROM evaluated_opportunities "
                                            "WHERE ticker=? AND filter_stage='candidate' LIMIT 1",
                                            (_wx_ticker,)).fetchone()[0])
                                except Exception:
                                    pass
                                if _wx_ens_mean is not None and _wx_threshold and _wx_threshold > 0:
                                    _wx_buffer = round(
                                        (_wx_ens_mean - _wx_threshold) / _wx_threshold * 100, 4)
                                _wx_stc = None
                                try:
                                    _wx_close = _wx_mkt.get("close_time") or _wx_mkt.get("expiration_time")
                                    if _wx_close:
                                        _close_dt = datetime.datetime.fromisoformat(
                                            _wx_close.replace("Z", "+00:00"))
                                        _wx_stc = (_close_dt - datetime.datetime.now(timezone.utc)).total_seconds()
                                except Exception:
                                    pass
                                _wx_now_str = datetime.datetime.now(timezone.utc).strftime(
                                    "%Y-%m-%dT%H:%M:%S.%fZ")
                                # Per-level orderbook ladder: weather monitor runs every
                                # 900s, well outside _scan_ob_cache's 10s freshness window
                                # (15M scanner doesn't tick weather tickers). Cache would
                                # be ~always None — fetch fresh via REST instead. Cost: 1
                                # extra get_orderbook per weather position per 15min cycle
                                # (typically ≤5 positions → trivial).
                                _wx_ob_ladder = None
                                try:
                                    _wx_ob_data = self.client.get_orderbook(_wx_ticker)
                                    _wx_ob_ladder = OrderExecutor._extract_book_levels(_wx_ob_data)
                                except Exception:
                                    logging.debug("WEATHER_PPO ladder fetch failed for %s",
                                                  _wx_ticker, exc_info=True)
                                self.state.conn.execute(
                                    """INSERT INTO position_price_observations
                                       (ticker, asset, observation_time, seconds_to_close,
                                        spot_price, threshold, spot_buffer_pct,
                                        yes_ask_cents, yes_bid_cents,
                                        entry_price_cents, position_count, source,
                                        orderbook_levels_json)
                                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                    (_wx_ticker, _wx_asset, _wx_now_str,
                                     round(_wx_stc, 1) if _wx_stc else None,
                                     _wx_ens_mean, _wx_threshold, _wx_buffer,
                                     _wx_display_ask, _wx_display_bid,
                                     pos.get("avg_price_cents"),
                                     pos.get("count", 1),
                                     "rest_weather", _wx_ob_ladder))
                                _wx_ppo_wrote = True
                                logging.info(
                                    "WEATHER_PPO: %s %s ask=%s bid=%s ens=%.1fF thresh=%s buf=%s stc=%s",
                                    _wx_asset, _wx_ticker,
                                    _wx_display_ask, _wx_display_bid,
                                    _wx_ens_mean if _wx_ens_mean else 0,
                                    _wx_threshold,
                                    f"{_wx_buffer:.3f}%" if _wx_buffer is not None else "N/A",
                                    f"{_wx_stc/3600:.1f}h" if _wx_stc else "N/A")
                            except Exception:
                                logging.warning("WEATHER_PPO: failed for %s", _wx_ticker, exc_info=True)
                        if _wx_ppo_wrote:
                            self.state.conn.commit()
                except Exception:
                    logging.warning("weather_position_monitor failed", exc_info=True)
                self._wx_ppo_last_poll = _wx_ppo_now

    # ── Run ───────────────────────────────────────────────────────────────

    def run(self):
        self._setup_signals()
        self.startup()

        mode = "LIVE" if not OBSERVATION_MODE else "observation"
        logging.info(f"Entering main loop ({mode} mode)...")
        _consecutive_errors = 0
        _incident_alerted = False
        try:
            while not self._shutdown.is_set():
                loop_start = time.time()
                try:
                    self._tick()
                except Exception as e:
                    self._last_error = str(e)
                    self._last_error_time = time.time()
                    _consecutive_errors += 1
                    # Capture deepest frame so first-fire Telegram alerts
                    # are diagnosable without journalctl. Apr 27 2026
                    # incident: a single `tuple index out of range` fired,
                    # self-recovered, and the trace was unrecoverable from
                    # journal retention afterward.
                    err_loc = _extract_tick_error_location(e)
                    logging.error("Tick error (%d consecutive) at %s",
                                  _consecutive_errors, err_loc,
                                  exc_info=True)

                    if _telegram_state._TELEGRAM:
                        if _consecutive_errors <= 1:
                            # First error: standard warning with dedup
                            _telegram_state._TELEGRAM.send(
                                f"\u26a0\ufe0f Tick error at `{err_loc}`: "
                                f"{str(e)[:200]}",
                                dedup_key="tick_error")
                        elif _consecutive_errors == 3 and not _incident_alerted:
                            # 3 consecutive: CRITICAL escalation
                            _telegram_state._TELEGRAM.send(
                                f"\U0001f6a8 *INCIDENT: BOT BLOCKED*\n"
                                f"{_consecutive_errors} consecutive tick "
                                f"errors in {_consecutive_errors * 5}s\n"
                                f"At: `{err_loc}`\n"
                                f"Error: `{str(e)[:150]}`\n"
                                f"Auto-restart in 30s if not resolved.")
                            _incident_alerted = True
                        elif _consecutive_errors % 12 == 0 and _incident_alerted:
                            # Every 60s during sustained outage: update
                            _telegram_state._TELEGRAM.send(
                                f"\U0001f6a8 *INCIDENT ONGOING*: "
                                f"{_consecutive_errors} consecutive errors "
                                f"({_consecutive_errors * 5}s blocked)\n"
                                f"At: `{err_loc}`\n"
                                f"Error: `{str(e)[:150]}`")

                    # Auto-restart: 6 consecutive errors = 30s blocked
                    # systemd Restart=always brings us back up clean
                    if _consecutive_errors >= 6:
                        logging.critical(
                            "AUTO-RESTART: %d consecutive tick errors, "
                            "exiting for systemd restart",
                            _consecutive_errors)
                        if _telegram_state._TELEGRAM:
                            _telegram_state._TELEGRAM.send(
                                f"\U0001f504 *AUTO-RESTART*: "
                                f"{_consecutive_errors} consecutive errors "
                                f"({_consecutive_errors * 5}s blocked). "
                                f"Restarting now.")
                            time.sleep(1)  # let Telegram send
                        os._exit(1)

                    time.sleep(5)
                    continue

                # Successful tick — check for recovery
                if _consecutive_errors > 0:
                    if _incident_alerted and _telegram_state._TELEGRAM:
                        _telegram_state._TELEGRAM.send(
                            f"\u2705 *INCIDENT RECOVERED*: Bot resumed "
                            f"after {_consecutive_errors} consecutive "
                            f"errors ({_consecutive_errors * 5}s blocked)")
                    elif _consecutive_errors >= 2:
                        logging.warning(
                            "Recovered from %d consecutive tick errors",
                            _consecutive_errors)
                    _consecutive_errors = 0
                    _incident_alerted = False

                elapsed = time.time() - loop_start
                sleep_time = max(0, SCAN_INTERVAL_SECONDS - elapsed)
                self._shutdown.wait(timeout=sleep_time)
        finally:
            self._cleanup()

    def _cleanup(self):
        logging.info("Shutting down...")
        if hasattr(self, 'executor'):
            for asset in list(self.executor._active_orders):
                try:
                    self.executor._cancel_order(asset, "shutdown")
                except Exception:
                    pass
        if hasattr(self, 'egarch_estimator'):
            self.egarch_estimator._save_state()
            logging.info(
                "EGARCH buffer saved on shutdown: %s",
                ", ".join(f"{a}={len(self.egarch_estimator._returns[a])}" for a in ASSETS))
        if hasattr(self, 'mz_tracker'):
            self.mz_tracker.save_state()
            logging.info("MZ tracker state saved on shutdown")
        if hasattr(self, 'vol'):
            self.vol.save_rk_state()
            logging.info(
                "RK state saved on shutdown: %s returns",
                ", ".join(f"{a}={len(self.vol._returns[a])}" for a in ASSETS))
            self.vol._save_adaptive_state()
            logging.info(
                "Adaptive jump state saved on shutdown: %s obs",
                ", ".join(f"{a}={len(self.vol._adaptive_returns_15s[a])}" for a in ASSETS))
        if hasattr(self, 'kalshi_feed') and self.kalshi_feed:
            self.kalshi_feed.stop()
        # Stop H-3a snapshotter — it holds its OWN sqlite connection
        # (separate from self.state.conn), but if join times out the
        # daemon thread is force-killed at process exit which can leave
        # a partial WAL segment. Bumped to 25s (one tick + commit + slack)
        # and we log if still alive after timeout (round-1 wiring #4).
        if hasattr(self, 'market_obs_snapshotter') and self.market_obs_snapshotter is not None:
            try:
                self.market_obs_snapshotter.stop()
                self.market_obs_snapshotter.join(timeout=25.0)
                if self.market_obs_snapshotter.is_alive():
                    logging.warning(
                        "Market obs snapshotter join timed out — thread still "
                        "alive at shutdown. Daemon will be force-killed; "
                        "WAL recovery on next startup."
                    )
            except Exception as e:
                logging.warning(f"Market obs snapshotter stop failed: {e}")
        # snapshot_builder has no thread — nothing to stop
        if hasattr(self, 'supabase_syncer') and self.supabase_syncer:
            self.supabase_syncer.stop()
        if hasattr(self, 'coinglass'):
            self.coinglass.stop()
        if hasattr(self, 'cross_feed') and self.cross_feed:
            self.cross_feed.stop()
        if hasattr(self, 'dvol_fetcher'):
            self.dvol_fetcher.stop()
        self.feed.stop()
        # R-p7-deploy-r8: drain async predict pool BEFORE closing state.db so
        # in-flight UPDATEs land. Worker holds a sqlite connection that will
        # error if state.close() runs first.
        try:
            _calmlp_drain_pool(timeout_sec=5.0)
        except Exception as e:
            logging.warning(f"cal_mlp pool drain failed: {e}")
        self.state.close()
        if _telegram_state._TELEGRAM:
            _telegram_state._TELEGRAM.send("\U0001f534 Bot shutting down")
        logging.info("Bot stopped.")

