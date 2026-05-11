#!/usr/bin/env python3
"""Kalshi cryptocurrency prediction market trading bot."""

# R-p7-deploy-r7 CRITICAL: pin OMP/MKL/OpenBLAS to 1 thread BEFORE numpy /
# scipy / torch / sklearn / pandas C-extensions load. These libraries cache
# their thread-pool size at LIBRARY LOAD TIME — setting OMP_NUM_THREADS=1
# AFTER `import numpy` is a no-op. See bot/_thread_env.py and
# CLAUDE.md "Critical rules". AST regression in test_cal_mlp_invariants.py.
import os
import sys
import bot._thread_env  # noqa: F401, E402 — side-effect: sets OMP_NUM_THREADS=1 before numpy below
# `scripts/cal_mlp/` on sys.path for the bare `from integration import` calls
# below. Placed AFTER bot._thread_env so OMP_NUM_THREADS=1 is already set when
# integration.py later loads numpy/scipy/torch transitively.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'cal_mlp'))
import re
import time
import json
import uuid
import signal
import sqlite3
import math
import heapq
import base64
import functools
import datetime
from datetime import timezone
import threading
import asyncio
import random
import logging
import inspect
import traceback
from collections import deque
from typing import Optional, Dict, List, Set, Tuple, Any
# Phase 7 cal_mlp deploy prerequisites (R-p7-deploy-r1#C1).
import numpy as np
from pathlib import Path

import requests
import websockets
# scipy.stats import (student_t, norminvgauss) was the sole consumer of scipy in
# bot/_impl.py — used only by ProbabilityEngine for Student-t/NIG CDF. Both names
# moved to bot/engines/probability.py in Bit 6.2; removed here as dead-import
# cleanup. Adding scipy back here would re-import scipy.stats in the
# bot/_thread_env-pinned chain (see kb/failures/cal-mlp-torch-thread-contention-apr29.md).
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

from market_config import get_market_config, get_cal_excluded_types, validate_market_configs, MARKET_CONFIGS
from config import *  # noqa: F401,F403 — shared constants (single source of truth)
from bot.infra.circuit_breaker import REGISTRY as _BREAKER_REGISTRY  # circuit breaker for KalshiClient REST GETs (Sprint 10.5a sibling-reorg 2026-05-11)

# Phase 7: cal_mlp integration (single import surface).
# `from integration import ...` resolves via the `sys.path.insert(...,
# 'scripts/cal_mlp')` at the top of this file — placed there alongside
# `import bot._thread_env` so OMP_NUM_THREADS=1 is set before integration.py
# transitively loads numpy/scipy/torch.
from integration import (  # noqa: E402
    CalMLPError, CalMLPParityError, CalMLPSchemaError,
    migrate_schema as _calmlp_migrate_schema,
    parity_assert as _calmlp_parity_assert_impl,
    sizing_parity_assert as _calmlp_sizing_parity_assert_impl,
    make_compute_for_15m_main_path,
    CalMLPPredictor,
    annotate_evaluation_kwargs as _calmlp_annotate_kwargs,
    annotate_evaluation_async_enqueue as _calmlp_annotate_async,
    stop_post_hoc_processor as _calmlp_drain_pool,
    _calmlp_predictors,
    warmup_predictor_cache as _calmlp_warmup_cache,
)










from bot.models import (  # noqa: F401 — extracted pure-math classes
    EGARCHEstimator, MincerZarnowitzTracker, PositionSizer,
    calculate_fee, calculate_taker_fee, calculate_maker_fee,
    compute_tv_rk_weights, _student_t_e_abs_z, _compute_qlike,
    strategy_to_group,
)

from bot.constants import *  # noqa: F401,F403 — module-level constants extracted per Bit 3.1
from bot.constants import _CROSS_EXCHANGE_FEEDS_ACTIVE  # noqa: F401 — underscore-prefixed names that star-import skips; explicit re-export so bot._impl namespace contains them too

from bot.helpers import *  # noqa: F401,F403 — feature/sizing/cell-block helpers per Bit 3.2
from bot.helpers.validators import (  # noqa: F401 — underscore-prefixed; star-import skips them
    _validate_bleeders_against_runtime_registry,
    _validate_high_price_stc_block_bleeder_strings,
    _validate_bleed_block_bleeder_strings,
)
from bot.helpers.breakers import (  # noqa: F401 — underscore-prefixed; star-import skips them
    _extract_tick_error_location,
    _kalshi_breaker_success,
    _kalshi_series_key,
    _kalshi_breaker,
    _breaker_config,
)

from bot.logger import Logger  # noqa: F401 — Bit 4.1 leaf extraction; re-export so MainLoop construction + type annotations on OpportunityScanner/OrderExecutor/SettlementTracker resolve
from bot.notifier import TelegramNotifier  # noqa: F401 — Bit 4.2 leaf extraction; re-export so the runtime construction in MainLoop.__init__ resolves.
import bot.notifier as _telegram_state  # Bit 8.1 path-A++ (2026-05-10) + Bit 9.3-ii atomic narrative update (2026-05-10): alias for `_telegram_state._TELEGRAM` module-attribute access. The singleton lives in bot/notifier.py alongside TelegramNotifier. **Post-Bit-9.3-ii, bot/_impl.py has ZERO `_telegram_state._TELEGRAM` consumers** — the orphan-DB watchdog block (which was the last remaining consumer post-Bit-9.3) relocated to `bot/orphan_db_watchdog.py` clean leaf. This module retains the alias only to support the re-export `from bot.orphan_db_watchdog import (...)` proxy chain (post-9.3-ii the alias's only role here is documentation of the load-order convention for sister modules that DO consume it). The module-attribute access pattern preserves mutation freshness across all five consumers post-Bit-9.3-ii (bot/orphan_db_watchdog.py for `_alert_orphan_db_holder` + the `detect_orphan_db_holders` lsof-not-found alert branch + bot/main_loop.py for MainLoop reads + the singleton WRITE in `__init__` + bot/scanner/__init__.py + bot/executor.py + bot/settlement.py — verify counts with `grep -c '_telegram_state\._TELEGRAM' bot/orphan_db_watchdog.py bot/main_loop.py bot/scanner/__init__.py bot/executor.py bot/settlement.py`). Mirrors the Bit 6.3 path-B `_cal_state` pattern. NOTE: explicit `import bot.notifier as ...` (NOT `from bot import notifier as ...`) — the latter form goes through `_BotProxy.__getattr__` and triggers a partial-module ImportError of bot._impl from inside bot.scanner during its load.
from bot.kalshi_client import KalshiClient  # noqa: F401 — Bit 4.3 leaf extraction; re-export so MainLoop construction (search "self.client = KalshiClient") + type annotations on reconcile_with_api/_reconcile_positions/_reconcile_orders/OpportunityScanner/OrderExecutor/SettlementTracker/discover_active_windows resolve via bot._impl namespace.
from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher  # noqa: F401 — Bit 4.4 leaf extraction; re-export so MainLoop construction (search "self.dvol_fetcher = DeribitDVOLFetcher" and "self.coinglass = CoinGlassFetcher") + the Optional[DeribitDVOLFetcher] type annotation on VolatilityEngine.__init__ (now in bot/engines/volatility.py per Bit 6.1) resolve via bot._impl namespace.
from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError  # noqa: F401 — Bit 4.5a + 4.5b leaf extraction; re-export so MainLoop construction (search "self.feed = CoinbaseFeed", "self.cross_feed = CrossExchangeFeed", and "self.kalshi_feed = KalshiFeed") + the `feed: CoinbaseFeed` type annotations on VolatilityEngine.__init__ (now in bot/engines/volatility.py per Bit 6.1) and OpportunityScanner.__init__ + the OrderbookSchemaError raises inside KalshiFeed (now sibling-imported from bot.feeds.orderbook_schema) all resolve via bot._impl namespace.
from bot.engines import VolatilityEngine, ProbabilityEngine, CalibrationEngine  # noqa: F401 — Bit 6.1 + 6.2 + 6.3 leaf extractions; re-export so MainLoop construction (search "self.vol = VolatilityEngine" and "self.calibration = CalibrationEngine()") + the `vol: VolatilityEngine` type annotation on OpportunityScanner.__init__ + the bare-name `ProbabilityEngine.X(...)` call sites (scan-loop edge computation, counterfactual probability, dynamic cap lookup) + the bare-name `CalibrationEngine(...)` construction sites in MainLoop.__init__ + the static-method calls in tests/test_vol_engine.py / tests/test_probability_engine.py / tests/test_calibration_engine.py (`from bot import VolatilityEngine, ProbabilityEngine, CalibrationEngine`) all resolve via bot._impl namespace. The Optional['EGARCHEstimator'] / Optional['MincerZarnowitzTracker'] forward-refs on VolatilityEngine.__init__ remain string-quoted because both classes still live in models.py. **Bit 6.3 path-B refactor (2026-05-10)**: the `_cal_state._CALIBRATION_ENGINE` singleton + `_cal_state._CAL_REGISTRY` dict + `_cal_state._derive_subtype`/`_cal_state._derive_asset_filter`/`_cal_state._resolve_cal_engine` helpers all moved to `bot/engines/calibration.py` alongside the class. Both this module and `bot/engines/probability.py` reach them via `_cal_state.X` (see the `from bot.engines import calibration as _cal_state` alias below). The path-B move lifted the previous Bit 6.2 late-binding `from bot import _impl as _bot_impl` pattern inside ProbabilityEngine — top-level imports work because `bot.engines.calibration` is a leaf (does NOT import bot._impl). Removes the `.importlinter` `bot.engines.probability -> bot._impl` carve-out shipped in Pillar 2.
from bot.engines import calibration as _cal_state  # Bit 6.3 path-B: alias for _cal_state._CALIBRATION_ENGINE / _cal_state._CAL_REGISTRY / _cal_state._derive_subtype / _cal_state._derive_asset_filter / _cal_state._resolve_cal_engine which all live in bot/engines/calibration.py post-Bit-6.3. Module-attribute access pattern (e.g., `_cal_state._CALIBRATION_ENGINE = self.calibration`) preserves singleton mutation semantics — every reader through this alias sees writes immediately because we go through the module reference, not a captured-by-value binding.
from bot.state import StateManager  # noqa: F401 — Bit 7.1 leaf extraction (2026-05-10); re-export so MainLoop construction (`self.state = StateManager()`) + 3 consumer-class type annotations (`OpportunityScanner.__init__`, `OrderExecutor.__init__`, `SettlementTracker.__init__`: `state: StateManager`) + ~50 test instantiation sites (`bot.StateManager(...)` via _BotProxy → bot._impl.StateManager → bot.state.StateManager) all resolve. **Path-A++ deviation note (Bit 7.1, historical)**: bot/state.py originally introduced a `_get_compute_for_15m_main_path()` single-name late-binding helper to reach `compute_for_15m_main_path` (closure bound below the bot.state re-export in this file). Sister Bit 7.1 also refactored `parity_assert` and `sizing_parity_assert` in scripts/cal_mlp/integration.py to drop their `bot_globals` parameter and import constants directly — the previous `globals()` smell at the StateManager.__init__ call sites was fixed in-Bit per the modularization strategic goal of reducing code smells. Sister Bit 7.2 shipped in lock-step (agent_docs/db_schema.md refresh). **Post-Bit-9.3-iii.a (2026-05-11)**: `compute_for_15m_main_path` relocated to clean-leaf bot/boot.py and the `_get_compute_for_15m_main_path()` helper RETIRED — bot/state.py now top-imports the callable directly. The .importlinter `state-no-impl-toplevel` carve-out also retired (net contracts 7 → 6).
from bot.scanner import OpportunityScanner  # noqa: F401 — Bit 8.1 leaf extraction (2026-05-10, path-A++); re-export so MainLoop construction (`self.scanner = OpportunityScanner(..., main_loop=self)`) + ~13 consumer call sites (12 OrderExecutor static-method calls + 1 MainLoop static-method call referencing `OpportunityScanner._best_yes_ask_cents` / `_convert_orderbook_fp`) + ~30 test instantiation sites (`bot.OpportunityScanner(...)` via _BotProxy → bot._impl.OpportunityScanner → bot.scanner.OpportunityScanner) all resolve. **Path-A++ deviation note (post-Bit-9.1, 2026-05-10)**: (1) the `_get_order_executor()` late-binding helper in bot/scanner/__init__.py was RETIRED in Bit 9.1 atomically with the OrderExecutor extraction (see line-116 `from bot.executor import OrderExecutor` re-export below) — bot/scanner now uses a top-level `from bot.executor import OrderExecutor` directly; the `scanner-no-impl-toplevel` `.importlinter` contract dropped in the same commit (net contracts: 6 → 5); (2) the `Optional[OrderFlowEngine]` and `Optional[KalshiOrderFlowTracker]` annotations in `__init__` signature are UNQUOTED post-Bit-9.3.5 (2026-05-10) — both classes live in `bot/order_flow.py` post-extraction; bot/scanner has a top-level `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker` that resolves cleanly because bot/order_flow.py is a clean leaf (stdlib + bot.constants only) with zero bot.scanner edges. Sister Bit 8.1 ALSO relocated the `_TELEGRAM` module-level singleton from this file to `bot/notifier.py` (path-A++ relocation), reached via `_telegram_state._TELEGRAM` module-attribute access (parallel to Bit 6.3 path-B `_cal_state._CALIBRATION_ENGINE` pattern; preserves mutation freshness across consumers). Sprint 8 closed at Bit 8.2 (`bot/scanner/CLAUDE.md`).
from bot.executor import OrderExecutor  # noqa: F401 — Bit 9.1 leaf extraction (2026-05-10, path-A++); re-export so MainLoop construction (`self.executor = OrderExecutor(self.client, self.state, self.logger, main_loop=self, kalshi_feed=self.kalshi_feed)`) + ~12 test instantiation sites (`bot.OrderExecutor(...)` via _BotProxy → bot._impl.OrderExecutor → bot.executor.OrderExecutor) all resolve. **Path-A++ deviations**: (1) `_append_raw_api_journal` relocated to bot/helpers/raw_api_journal.py (the L81 alias-import at line ~282 RETIRED in Bit 9.2 atomically with the SettlementTracker extraction — zero callers remain in bot/_impl.py); (2) 4 latent `OpportunityScanner._best_ask_depth(...)` AttributeErrors fixed (closes ticket 86b9vn9r5; `_best_ask_depth` is a staticmethod on OrderExecutor itself). Sister cleanup atomic in this Bit: `_get_order_executor()` helper retired from bot/scanner/__init__.py + `scanner-no-impl-toplevel` `.importlinter` contract dropped (net contracts: 6 → 5).
from bot.settlement import SettlementTracker, discover_active_windows  # noqa: F401 — Bit 9.2 leaf extraction (2026-05-10, path-A++); re-export so MainLoop construction (search anchor: `self.tracker = SettlementTracker(`) + the single MainLoop call site (search anchor: `discover_active_windows(self.client)`) resolve via bot._impl namespace. **Path-A++ deviation**: the 2 SettlementTracker call sites that read the L81-aliased underscore-prefix name now use the public `append_raw_api_journal` directly (matches bot/executor.py:98 convention); the L81 alias-import at line ~282 RETIRED atomically (zero callers remain). Bundled atomic cleanup: bot/notifier.py docstring 3→4 consumers; bot/__init__.py extended; bot/CLAUDE.md Deploy step 3 catalog gains SettlementTracker paragraph; tests/test_state_extraction.py quadruple-walk extension (BOT_PY + SCANNER_PY + EXECUTOR_PY + SETTLEMENT_PY); test_low_price_shadow.py + test_stacking.py + test_tm_sweep_shadow.py + test_regression.py _read_bot()/_paths helpers extended to concat bot/settlement.py; test_tracker_tick_threaded.py BOT_PY → SETTLEMENT_PY; test_order_outcome_vocab.py SCANNED_PATHS extended; tests/test_executor_extraction.py L81 positive pin flipped to negative; agent_docs/bot_layout.md class table loses the `~1010–~2179 SettlementTracker 1179` row; discover_active_windows narrative flips from Bit 9.3 to Bit 9.2 across all parallel sites. Bundled bug fix (ticket 86b9vppn3): pre-existing UnboundLocalError 'best_ask' in OpportunityScanner.scan() low_probability_15m insert_rejection branch (predates Bit 8.1 per git blame) — initialize best_ask=None at iteration start.
from bot.db_writer_registry import tracked_write, snapshot_active, recent_writes  # ops: db-locked RCA instrumentation 2026-05-08 — track every write across all 8 sqlite3 connections so the failure-path log can identify which OTHER writer was holding the writer lock at db-locked failure time. recent_writes() captures the JUST-FINISHED holder (FAST-fail path: BEGIN IMMEDIATE returns SQLITE_BUSY in <1ms when intra-process lock-holder releases right before our retry).
from bot.main_loop import MainLoop  # noqa: F401 — Bit 9.3 leaf extraction (2026-05-10); re-export so bot/__main__.py `from bot._impl import MainLoop` continues to resolve at 9.3-i; bot/__main__.py swap to direct `from bot.main_loop import MainLoop` deferred to Bit 9.3-ii per master plan.
from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker  # noqa: F401 — Bit 9.3.5 leaf extraction (2026-05-10); re-export so the proxy chain (bot.X → bot._impl.X → bot.order_flow.X) stays stable. Clean leaf — bot/order_flow.py imports only stdlib + bot.constants (zero bot._impl edge, zero _telegram_state, zero _cal_state).
from bot.orphan_db_watchdog import (  # noqa: F401 — Bit 9.3-ii leaf extraction (2026-05-10); re-export so the proxy chain (bot.X → bot._impl.X → bot.orphan_db_watchdog.X) stays stable for the 11 `monkeypatch.setattr(bot, ...)` sites in tests/test_orphan_db_watchdog.py at Option A scope (full proxy retirement deferred to Bit 9.3-iii). Clean leaf — bot/orphan_db_watchdog.py imports only stdlib + bot.notifier alias (zero bot._impl edge, becomes the 5th _telegram_state._TELEGRAM consumer REPLACING this module in the count — net stays at 5). Sprint 9 closes here.
    detect_orphan_db_holders,
    _run_lsof_for_db,
    _get_pid_cmdline,
    _alert_orphan_db_holder,
    _ORPHAN_DB_WATCHDOG_PATTERNS,
)




























# _swallow_persist_exception → bot/feeds/coinbase.py (Bit 4.5a, 2026-05-08).
# Helper moved alongside its sole consumer (CoinbaseFeed._snapshot_loop).




# EGARCH, MZ, blend constants → config.py (imported via `from config import *`)


# annualized → per-5-second: 1/sqrt(SECONDS_PER_YEAR / VOL_RETURN_INTERVAL)
# (computed after SECONDS_PER_YEAR is defined below)







# Probability engine constants (SECONDS_PER_YEAR, DVOL_ANNUALIZED_TO_5S, STUDENT_T_DF,
# BETA_SLOPE, MAX_EFFECTIVE_PROB, NUMERICAL_SAFETY_CEILING) → config.py







# DIST_CONFIG, _load_dist_config → config.py

# _CALIBRATION_ENGINE singleton + _CAL_REGISTRY dict + _derive_subtype +
# _derive_asset_filter + _resolve_cal_engine relocated to
# bot/engines/calibration.py in Bit 6.3 path-B refactor (2026-05-10).
# Both this module and bot/engines/probability.py reach them via
# `_cal_state.X` (search anchor: `from bot.engines import calibration as
# _cal_state`). The path-B move removed the .importlinter
# `bot.engines.probability -> bot._impl` carve-out.
#
# _TELEGRAM relocated to bot/notifier.py per Bit 8.1 path-A++
# (2026-05-10) — see the `import bot.notifier as _telegram_state` alias
# near the top of this file. The relocation lifted ~86
# `@patch("bot._TELEGRAM", ...)` patch-target retargets onto
# `bot.notifier._TELEGRAM`, parallel to how Bit 6.3 path-B relocated
# `_CALIBRATION_ENGINE` alongside its class. **Post-Bit-9.3-ii (2026-05-10),
# bot/_impl.py has ZERO `_telegram_state._TELEGRAM` executable consumers —
# the orphan-DB watchdog block (the last remaining consumer post-Bit-9.3)
# relocated to `bot/orphan_db_watchdog.py` clean leaf alongside the
# bot/__main__.py swap per master plan L2197.** Verify count with
# `grep -c '_telegram_state._TELEGRAM' bot/orphan_db_watchdog.py
# bot/main_loop.py bot/scanner/__init__.py bot/executor.py
# bot/settlement.py` — 5 consumers post-Bit-9.3-ii (bot/orphan_db_watchdog.py
# REPLACES bot/_impl.py in the slot). The plain
# `from bot.notifier import _TELEGRAM` form would NOT propagate runtime
# mutation because `from`-imports capture by value at import time.































# Bit 3.0.5: bleeder validators (HPSB + BLEED_BLOCK) and their boot-time
# bindings (_HPSB_MISSING_BLEEDERS, _BLEED_BLOCK_MISSING_BLEEDERS) live AFTER
# the strategy-registry sources (STRATEGY_CLAMP_POLICY, MAKER_TAIL_*,
# TM_LIVE_*, STRATEGY_LIMIT_BUMP_*, STRATEGY_* string constants,
# KNOWN_DC_STRATEGIES) are all in scope — see "Bleeder validators
# (Bit 3.0.5)" section below the Strategy constants block.
# kb/decisions/bit-3.0.5-validator-decoupling.md.


# ─── New bleed-cell predicates (R-bleed-1) ─────────────────────────────────





# Bit 3.0.5: _validate_bleed_block_bleeder_strings + _BLEED_BLOCK_MISSING_BLEEDERS
# moved alongside HPSB validator to the "Bleeder validators (Bit 3.0.5)" section
# below the Strategy constants block (after STRATEGY_PANIC_CAPTURE), where the
# strategy-registry sources are all in scope.




# ─── Extended Feature Instrumentation (Tier 4 + Tier 5) ───────────────────
# Feature helpers for per-scan logging to evaluated_opportunities.
# See kb-research/bot/buffer-rescue-analysis.md for motivation and schema.









# Position sizing constants (SIZING_TIERS, DRAWDOWN_*, MAX_RISK_PER_TRADE) → config.py





                                  # Set to 0: taker allowed at all STC (data: 14W/0L, 100% taker WR)
                                  # Was 90.0 — removed after verifying taker has zero losses

# Per-asset taker concurrency cap removed 2026-05-04 — was dead code
# (counter never incremented anywhere). Per-asset concurrency under
# single-thread architecture is already enforced by IOC_TICKER_COOLDOWN
# and the sequential scan loop. If parallel submission is ever
# introduced, real concurrency control needs designing (locks, atomic
# counters), not retrofitted onto a counter pattern.
# See kb/failures/active-taker-count-dead-cap-may04.md







# Bit 9.2 (2026-05-10) RETIRED the L81 alias-import for `_append_raw_api_journal`.
# All 3 historical callers (1 in OrderExecutor, 2 in SettlementTracker) now live
# in extracted modules (bot/executor.py + bot/settlement.py respectively) and
# import the public name `append_raw_api_journal` directly from bot/helpers/raw_api_journal.py.
# bot/_impl.py has zero callers post-Bit-9.2.







# ─── Stacking ─────────────────────────────────────────────────────────
# STACKING_ENABLED defined above (env var gated) — see Stacking Infrastructure section




# Fee helpers, compute_tv_rk_weights → imported from models.py









# ═════════════════════════════════════════════════════════════════════════════
#  Execution Strategy Engine
# ═════════════════════════════════════════════════════════════════════════════



# ═════════════════════════════════════════════════════════════════════════════
#  Bleeder validators (Bit 3.0.5 — registry-membership)
# ═════════════════════════════════════════════════════════════════════════════
# Pre-Bit-3.0.5 these validators self-grepped bot/_impl.py source and required
# >= 2 occurrences of each bleeder string. Three strategies passed for the
# wrong reasons: scan-site usages of MAKER_PATIENT / TAKER_NOW route through
# STRATEGY_* symbol bindings (not literals); terminal_momentum_98 is
# f-string-built at runtime so no literal scan-site emission exists. Bit 3.0.5
# replaces the heuristic with runtime-registry membership.
# RCA + decision: kb/decisions/bit-3.0.5-validator-decoupling.md.

# ─── Boot-time bindings — relocated to bot/boot.py (Bit 9.3-iii.a, 2026-05-11) ─
# The 4 module-level boot bindings and the cal_mlp warmup boot log were
# relocated to a NEW clean-leaf module `bot/boot.py` to break the last
# structural dependency that bot/main_loop.py and bot/state.py had on
# bot._impl. bot/boot.py imports only stdlib + bot.helpers.validators +
# scripts/cal_mlp/integration — zero bot._impl edge. The re-export below
# preserves `tests/contracts/public_api.json` byte-identical until full
# proxy retirement in Bit 9.3-iii.b/c.
from bot.boot import (  # noqa: F401
    _HPSB_VALIDATOR_UNAVAILABLE_REASON,
    _HPSB_MISSING_BLEEDERS,
    _BLEED_BLOCK_MISSING_BLEEDERS,
    compute_for_15m_main_path,
)









# Boot-time validation now lives in bot/boot.py (Bit 9.3-iii.a) — re-exported above.




# KalshiClient → bot/kalshi_client.py (Bit 4.3, 2026-05-08).
# Re-imported above via `from bot.kalshi_client import KalshiClient`.


# ═════════════════════════════════════════════════════════════════════════════
#  StateManager
# ═════════════════════════════════════════════════════════════════════════════

# Phase 7 Edit 2 closure `compute_for_15m_main_path` lives in bot/boot.py
# (Bit 9.3-iii.a) — re-exported above alongside the HPSB validator bindings.


# Orphan-DB Layer-3 watchdog → bot/orphan_db_watchdog.py (Bit 9.3-ii, 2026-05-10).
# Was bot/_impl.py:375-578 pre-9.3-ii (4 functions + _ORPHAN_DB_WATCHDOG_PATTERNS
# list). Clean leaf — stdlib + bot.notifier alias only. The block became the
# 5th `_telegram_state._TELEGRAM` consumer (REPLACING this module — net stays
# at 5: bot/orphan_db_watchdog.py + bot/main_loop.py + bot/scanner/__init__.py
# + bot/executor.py + bot/settlement.py). MainLoop.startup() late-binding
# retargeted in the same atomic commit. Re-imported above via
# `from bot.orphan_db_watchdog import (detect_orphan_db_holders, _run_lsof_for_db,
# _get_pid_cmdline, _alert_orphan_db_holder, _ORPHAN_DB_WATCHDOG_PATTERNS)` so
# the proxy chain `bot.X → bot._impl.X → bot.orphan_db_watchdog.X` resolves
# (proxy retirement deferred to Bit 9.3-iii). Postmortem:
# kb/failures/shape-d-contention-explosion-may03.md.


# StateManager → bot/state.py (Bit 7.1, 2026-05-10).
# Re-imported above via `from bot.state import StateManager`.
# Path-A++ extraction (NOT byte-for-byte): the two `globals()` calls inside
# StateManager.__init__ pre-extraction were refactored in-Bit — see
# scripts/cal_mlp/integration.py `parity_assert(conn) -> tuple[str, int]` and
# `sizing_parity_assert(conn, *, rowid, compute_for_15m_main_path)` for the
# new explicit signatures (Bit 7.1 fu / Smell 4, ticket 86b9vhccw, also dropped
# the `globals()` arg from `make_compute_for_15m_main_path`). The original
# Bit 7.1 `_get_compute_for_15m_main_path()` late-binding helper inside bot/state.py
# was RETIRED in Bit 9.3-iii.a (2026-05-11) — `compute_for_15m_main_path` relocated
# to clean-leaf bot/boot.py and bot/state.py top-imports the callable directly.
# The .importlinter `state-no-impl-toplevel` carve-out retired in the same atomic
# commit (net contracts 7 → 6). Sister Bit 7.2 (agent_docs/db_schema.md) shipped
# in the same atomic commit as Bit 7.1. Sprint 7 closes here.


# cal_mlp warmup cache + boot log relocated to bot/boot.py (Bit 9.3-iii.a, 2026-05-11).
# `_calmlp_start_posthoc` is top-imported directly by bot/main_loop.py (search
# anchor `start_post_hoc_processor as _calmlp_start_posthoc`) — the previous
# bot/_impl.py re-import (Plan-agent M1) was shadowed dead weight and is
# dropped here.


# ═════════════════════════════════════════════════════════════════════════════
#  CoinbaseFeed
# ═════════════════════════════════════════════════════════════════════════════

# CoinbaseFeed → bot/feeds/coinbase.py (Bit 4.5a, 2026-05-08).
# Re-imported above via `from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError`.

# ═════════════════════════════════════════════════════════════════════════════
#  KalshiFeed — Kalshi WebSocket for fills + orderbook deltas
# ═════════════════════════════════════════════════════════════════════════════

# KalshiFeed → bot/feeds/kalshi.py (Bit 4.5b, 2026-05-09).
# OrderbookSchemaError → bot/feeds/orderbook_schema.py (Bit 4.5a, 2026-05-08).
# Re-imported above via `from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError`.


# ═════════════════════════════════════════════════════════════════════════════
#  DeribitDVOLFetcher
# ═════════════════════════════════════════════════════════════════════════════

# DeribitDVOLFetcher → bot/fetchers/deribit.py (Bit 4.4, 2026-05-08).
# Re-imported above via `from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher`.


# ═════════════════════════════════════════════════════════════════════════════
#  CoinGlassFetcher
# ═════════════════════════════════════════════════════════════════════════════

# CoinGlassFetcher → bot/fetchers/coinglass.py (Bit 4.4, 2026-05-08).
# Re-imported above via `from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher`.


# CrossExchangeFeed → bot/feeds/cross_exchange.py (Bit 4.5a, 2026-05-08).
# Re-imported above via `from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError`.


# ═════════════════════════════════════════════════════════════════════════════
#  OrderFlowEngine + KalshiOrderFlowTracker → bot/order_flow.py (Bit 9.3.5, 2026-05-10)
# ═════════════════════════════════════════════════════════════════════════════
# Sprint 9 closing sister leaf. Both classes extracted verbatim from
# bot/_impl.py:658-934 to bot/order_flow.py. Re-imported above via
# `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker`
# (search anchor at the line-120 re-export block). Clean leaf — no
# carve-out, no late-binding; bot/order_flow.py imports only stdlib +
# bot.constants. Sister cleanup: bot/main_loop.py late-binding block
# shrunk from 4 names to 2 (HPSB only); bot/scanner/__init__.py
# Optional["OrderFlowEngine"] / Optional["KalshiOrderFlowTracker"]
# forward-refs UNQUOTED. Sprint 9 closes here.


# ═════════════════════════════════════════════════════════════════════════════
#  Sprint 6 engines — extracted to bot/engines/
# ═════════════════════════════════════════════════════════════════════════════
# All three Sprint 6 math-layer engines have been extracted from this file:
#
# VolatilityEngine → bot/engines/volatility.py (Bit 6.1, 2026-05-09).
#   ~960-line byte-for-byte move. Realized Kernel + DVOL blend +
#   adaptive jump detector + EGARCH variance-space blend.
# ProbabilityEngine → bot/engines/probability.py (Bit 6.2, 2026-05-09).
#   ~197-line move. Bit 6.2 originally used a `from bot import _impl as
#   _bot_impl` late-binding pattern inside compute() and
#   counterfactual_prob() to access the mutable _CALIBRATION_ENGINE
#   singleton + _resolve_cal_engine function (which lived in this
#   file, defined BELOW the line-109 engines re-export — top-level
#   import would have ImportErrored or captured a stale `None`). The
#   .importlinter shipped a `bot.engines.probability -> bot._impl`
#   ignore_imports carve-out for this. Bit 6.3 path-B (2026-05-10)
#   relocated the singleton + resolver out of this file and lifted
#   the late-binding (see CalibrationEngine entry below) — carve-out
#   removed in the same commit.
# CalibrationEngine → bot/engines/calibration.py (Bit 6.3, 2026-05-10).
#   ~1,004-line class body BYTE-FOR-BYTE move + path-B refactor:
#   _CALIBRATION_ENGINE singleton, _CAL_REGISTRY dict, and the three
#   helpers _derive_subtype / _derive_asset_filter / _resolve_cal_engine
#   ALL relocated to bot/engines/calibration.py. The class itself never
#   read these names (free-variable analysis returned zero hits); they're
#   written by MainLoop.__init__ and read by callers OUTSIDE the class
#   (OpportunityScanner.scan() and downstream sites). Both this module
#   and bot/engines/probability.py now reach them via top-level
#   `from bot.engines import calibration as _cal_state` plus
#   `_cal_state.X` attribute access — module-attribute access pattern,
#   no late-binding needed. Sprint 6 closes here. (Bit 8.1 path-A++
#   subsequently relocated _TELEGRAM to bot/notifier.py — see the
#   `import bot.notifier as _telegram_state` alias near the top of this
#   file; reads use `_telegram_state._TELEGRAM` module-attribute access,
#   parallel to the `_cal_state._CALIBRATION_ENGINE` pattern.)
# All three classes are re-imported via the
# `from bot.engines import VolatilityEngine, ProbabilityEngine, CalibrationEngine`
# at the top of this file (search anchor:
# "from bot.engines import VolatilityEngine").

# EGARCHEstimator, MincerZarnowitzTracker, PositionSizer, _student_t_e_abs_z,
# _compute_qlike, fee helpers, compute_tv_rk_weights → imported from models.py
# ═════════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════════
#  OpportunityScanner
# ═════════════════════════════════════════════════════════════════════════════

# OpportunityScanner → bot/scanner/__init__.py (Bit 8.1, 2026-05-10).
# Re-imported above via `from bot.scanner import OpportunityScanner`.
# Sprint 8 closes here. (Bit 8.2 `bot/scanner/CLAUDE.md` ships separately.)


# ═════════════════════════════════════════════════════════════════════════════
#  OrderExecutor
# ═════════════════════════════════════════════════════════════════════════════

# OrderExecutor → bot/executor.py (Bit 9.1, 2026-05-10).
# Re-imported above via `from bot.executor import OrderExecutor`.
# Path-A++: `_append_raw_api_journal` relocated to bot/helpers/raw_api_journal.py
# (Bit 9.2 RETIRED the L81 alias-import — bot/_impl.py has zero callers post-Bit-9.2;
# the only remaining caller is in bot/executor.py via the public name `append_raw_api_journal`).
# Sister cleanup: `_get_order_executor()` retired from bot/scanner/__init__.py;
# `scanner-no-impl-toplevel` `.importlinter` contract dropped (net contracts: 6 → 5).
# 4 latent `OpportunityScanner._best_ask_depth(...)` AttributeErrors fixed in this Bit
# (closes ticket 86b9vn9r5).


# ═════════════════════════════════════════════════════════════════════════════
#  SettlementTracker + Market Discovery
# ═════════════════════════════════════════════════════════════════════════════
# SettlementTracker → bot/settlement.py (Bit 9.2, 2026-05-10).
# Re-imported above via `from bot.settlement import SettlementTracker, discover_active_windows`.
# Path-A++: 2 SettlementTracker call sites moved with the class and now use
# the public name `append_raw_api_journal` (matching bot/executor.py:98).
#
# discover_active_windows → bot/settlement.py (Bit 9.2, bundled per master plan
# Phase Z+AA bundle decision; settlement-adjacent in source layout, sole caller
# is MainLoop._refresh_active_windows below — search anchor: `discover_active_windows(self.client)`).

# ═════════════════════════════════════════════════════════════════════════════
#  MainLoop → bot/main_loop.py (Bit 9.3, 2026-05-10)
# ═════════════════════════════════════════════════════════════════════════════

# MainLoop class body extracted to bot/main_loop.py per Sprint 9 Bit 9.3.
# Path-A method-body late-binding for bot._impl access (see bot/main_loop.py
# module docstring "Path-A architecture" section). Re-imported above via
# `from bot.main_loop import MainLoop` near the line-119 re-export block —
# search anchor: ``from bot.main_loop import MainLoop``. bot/__main__.py
# swap from `from bot._impl import MainLoop` to `from bot.main_loop import
# MainLoop` is deferred to Bit 9.3-ii per master plan two-step atomic
# discipline (≥7d soak window between ships per master plan L2195-2216).


# ═════════════════════════════════════════════════════════════════════════════
#  Entrypoint moved to bot/__main__.py — `python -m bot` invokes it.
# ═════════════════════════════════════════════════════════════════════════════
