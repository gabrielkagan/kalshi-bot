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
from circuit_breaker import REGISTRY as _BREAKER_REGISTRY  # circuit breaker for KalshiClient REST GETs

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










from models import (  # noqa: F401 — extracted pure-math classes
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
import bot.notifier as _telegram_state  # Bit 8.1 path-A++ (2026-05-10) + Bit 9.3 atomic narrative update (2026-05-10): alias for `_telegram_state._TELEGRAM` module-attribute access. The singleton lives in bot/notifier.py alongside TelegramNotifier. **Post-Bit-9.3, the only remaining `_telegram_state._TELEGRAM` consumer block in bot/_impl.py is the orphan-DB watchdog helpers** (`_alert_orphan_db_holder` + the `detect_orphan_db_holders` lsof-not-found Telegram alert branch — search anchor: `def _alert_orphan_db_holder` and `def detect_orphan_db_holders`) — MainLoop reads MOVED with the class extraction to bot/main_loop.py. The module-attribute access pattern preserves mutation freshness across all five consumers post-Bit-9.3 (this module for `_alert_orphan_db_holder` + bot/main_loop.py for MainLoop reads + the singleton WRITE in `__init__` + bot/scanner/__init__.py + bot/executor.py + bot/settlement.py — verify counts with `grep -c '_telegram_state\._TELEGRAM' bot/_impl.py bot/main_loop.py bot/scanner/__init__.py bot/executor.py bot/settlement.py`). Mirrors the Bit 6.3 path-B `_cal_state` pattern. NOTE: explicit `import bot.notifier as ...` (NOT `from bot import notifier as ...`) — the latter form goes through `_BotProxy.__getattr__` and triggers a partial-module ImportError of bot._impl from inside bot.scanner during its load.
from bot.kalshi_client import KalshiClient  # noqa: F401 — Bit 4.3 leaf extraction; re-export so MainLoop construction (search "self.client = KalshiClient") + type annotations on reconcile_with_api/_reconcile_positions/_reconcile_orders/OpportunityScanner/OrderExecutor/SettlementTracker/discover_active_windows resolve via bot._impl namespace.
from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher  # noqa: F401 — Bit 4.4 leaf extraction; re-export so MainLoop construction (search "self.dvol_fetcher = DeribitDVOLFetcher" and "self.coinglass = CoinGlassFetcher") + the Optional[DeribitDVOLFetcher] type annotation on VolatilityEngine.__init__ (now in bot/engines/volatility.py per Bit 6.1) resolve via bot._impl namespace.
from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError  # noqa: F401 — Bit 4.5a + 4.5b leaf extraction; re-export so MainLoop construction (search "self.feed = CoinbaseFeed", "self.cross_feed = CrossExchangeFeed", and "self.kalshi_feed = KalshiFeed") + the `feed: CoinbaseFeed` type annotations on VolatilityEngine.__init__ (now in bot/engines/volatility.py per Bit 6.1) and OpportunityScanner.__init__ + the OrderbookSchemaError raises inside KalshiFeed (now sibling-imported from bot.feeds.orderbook_schema) all resolve via bot._impl namespace.
from bot.engines import VolatilityEngine, ProbabilityEngine, CalibrationEngine  # noqa: F401 — Bit 6.1 + 6.2 + 6.3 leaf extractions; re-export so MainLoop construction (search "self.vol = VolatilityEngine" and "self.calibration = CalibrationEngine()") + the `vol: VolatilityEngine` type annotation on OpportunityScanner.__init__ + the bare-name `ProbabilityEngine.X(...)` call sites (scan-loop edge computation, counterfactual probability, dynamic cap lookup) + the bare-name `CalibrationEngine(...)` construction sites in MainLoop.__init__ + the static-method calls in tests/test_vol_engine.py / tests/test_probability_engine.py / tests/test_calibration_engine.py (`from bot import VolatilityEngine, ProbabilityEngine, CalibrationEngine`) all resolve via bot._impl namespace. The Optional['EGARCHEstimator'] / Optional['MincerZarnowitzTracker'] forward-refs on VolatilityEngine.__init__ remain string-quoted because both classes still live in models.py. **Bit 6.3 path-B refactor (2026-05-10)**: the `_cal_state._CALIBRATION_ENGINE` singleton + `_cal_state._CAL_REGISTRY` dict + `_cal_state._derive_subtype`/`_cal_state._derive_asset_filter`/`_cal_state._resolve_cal_engine` helpers all moved to `bot/engines/calibration.py` alongside the class. Both this module and `bot/engines/probability.py` reach them via `_cal_state.X` (see the `from bot.engines import calibration as _cal_state` alias below). The path-B move lifted the previous Bit 6.2 late-binding `from bot import _impl as _bot_impl` pattern inside ProbabilityEngine — top-level imports work because `bot.engines.calibration` is a leaf (does NOT import bot._impl). Removes the `.importlinter` `bot.engines.probability -> bot._impl` carve-out shipped in Pillar 2.
from bot.engines import calibration as _cal_state  # Bit 6.3 path-B: alias for _cal_state._CALIBRATION_ENGINE / _cal_state._CAL_REGISTRY / _cal_state._derive_subtype / _cal_state._derive_asset_filter / _cal_state._resolve_cal_engine which all live in bot/engines/calibration.py post-Bit-6.3. Module-attribute access pattern (e.g., `_cal_state._CALIBRATION_ENGINE = self.calibration`) preserves singleton mutation semantics — every reader through this alias sees writes immediately because we go through the module reference, not a captured-by-value binding.
from bot.state import StateManager  # noqa: F401 — Bit 7.1 leaf extraction (2026-05-10); re-export so MainLoop construction (`self.state = StateManager()`) + 3 consumer-class type annotations (`OpportunityScanner.__init__`, `OrderExecutor.__init__`, `SettlementTracker.__init__`: `state: StateManager`) + ~50 test instantiation sites (`bot.StateManager(...)` via _BotProxy → bot._impl.StateManager → bot.state.StateManager) all resolve. **Path-A++ deviation note**: bot/state.py introduces a `_get_compute_for_15m_main_path()` single-name late-binding helper (returns `bot._impl.compute_for_15m_main_path` bound at line 350 below). Sister Bit 7.1 also refactored `parity_assert` and `sizing_parity_assert` in scripts/cal_mlp/integration.py to drop their `bot_globals` parameter and import constants directly — the previous `globals()` smell at the StateManager.__init__ call sites is fixed in-Bit per the modularization strategic goal of reducing code smells. Sister Bit 7.2 ships in lock-step with this commit (agent_docs/db_schema.md refresh).
from bot.scanner import OpportunityScanner  # noqa: F401 — Bit 8.1 leaf extraction (2026-05-10, path-A++); re-export so MainLoop construction (`self.scanner = OpportunityScanner(..., main_loop=self)`) + ~13 consumer call sites (12 OrderExecutor static-method calls + 1 MainLoop static-method call referencing `OpportunityScanner._best_yes_ask_cents` / `_convert_orderbook_fp`) + ~30 test instantiation sites (`bot.OpportunityScanner(...)` via _BotProxy → bot._impl.OpportunityScanner → bot.scanner.OpportunityScanner) all resolve. **Path-A++ deviation note (post-Bit-9.1, 2026-05-10)**: (1) the `_get_order_executor()` late-binding helper in bot/scanner/__init__.py was RETIRED in Bit 9.1 atomically with the OrderExecutor extraction (see line-116 `from bot.executor import OrderExecutor` re-export below) — bot/scanner now uses a top-level `from bot.executor import OrderExecutor` directly; the `scanner-no-impl-toplevel` `.importlinter` contract dropped in the same commit (net contracts: 6 → 5); (2) the `Optional["OrderFlowEngine"]` and `Optional["KalshiOrderFlowTracker"]` quoted forward-refs in `__init__` signature for the 2 sister-class type annotations stay quoted (cycle avoidance — both classes still in this file; search anchors: ``class OrderFlowEngine:`` and ``class KalshiOrderFlowTracker:``). Sister Bit 8.1 ALSO relocated the `_TELEGRAM` module-level singleton from this file to `bot/notifier.py` (path-A++ relocation), reached via `_telegram_state._TELEGRAM` module-attribute access (parallel to Bit 6.3 path-B `_cal_state._CALIBRATION_ENGINE` pattern; preserves mutation freshness across consumers). Sprint 8 closed at Bit 8.2 (`bot/scanner/CLAUDE.md`).
from bot.executor import OrderExecutor  # noqa: F401 — Bit 9.1 leaf extraction (2026-05-10, path-A++); re-export so MainLoop construction (`self.executor = OrderExecutor(self.client, self.state, self.logger, main_loop=self, kalshi_feed=self.kalshi_feed)`) + ~12 test instantiation sites (`bot.OrderExecutor(...)` via _BotProxy → bot._impl.OrderExecutor → bot.executor.OrderExecutor) all resolve. **Path-A++ deviations**: (1) `_append_raw_api_journal` relocated to bot/helpers/raw_api_journal.py (the L81 alias-import at line ~282 RETIRED in Bit 9.2 atomically with the SettlementTracker extraction — zero callers remain in bot/_impl.py); (2) 4 latent `OpportunityScanner._best_ask_depth(...)` AttributeErrors fixed (closes ticket 86b9vn9r5; `_best_ask_depth` is a staticmethod on OrderExecutor itself). Sister cleanup atomic in this Bit: `_get_order_executor()` helper retired from bot/scanner/__init__.py + `scanner-no-impl-toplevel` `.importlinter` contract dropped (net contracts: 6 → 5).
from bot.settlement import SettlementTracker, discover_active_windows  # noqa: F401 — Bit 9.2 leaf extraction (2026-05-10, path-A++); re-export so MainLoop construction (search anchor: `self.tracker = SettlementTracker(`) + the single MainLoop call site (search anchor: `discover_active_windows(self.client)`) resolve via bot._impl namespace. **Path-A++ deviation**: the 2 SettlementTracker call sites that read the L81-aliased underscore-prefix name now use the public `append_raw_api_journal` directly (matches bot/executor.py:98 convention); the L81 alias-import at line ~282 RETIRED atomically (zero callers remain). Bundled atomic cleanup: bot/notifier.py docstring 3→4 consumers; bot/__init__.py extended; bot/CLAUDE.md Deploy step 3 catalog gains SettlementTracker paragraph; tests/test_state_extraction.py quadruple-walk extension (BOT_PY + SCANNER_PY + EXECUTOR_PY + SETTLEMENT_PY); test_low_price_shadow.py + test_stacking.py + test_tm_sweep_shadow.py + test_regression.py _read_bot()/_paths helpers extended to concat bot/settlement.py; test_tracker_tick_threaded.py BOT_PY → SETTLEMENT_PY; test_order_outcome_vocab.py SCANNED_PATHS extended; tests/test_executor_extraction.py L81 positive pin flipped to negative; agent_docs/bot_layout.md class table loses the `~1010–~2179 SettlementTracker 1179` row; discover_active_windows narrative flips from Bit 9.3 to Bit 9.2 across all parallel sites. Bundled bug fix (ticket 86b9vppn3): pre-existing UnboundLocalError 'best_ask' in OpportunityScanner.scan() low_probability_15m insert_rejection branch (predates Bit 8.1 per git blame) — initialize best_ask=None at iteration start.
from bot.db_writer_registry import tracked_write, snapshot_active, recent_writes  # ops: db-locked RCA instrumentation 2026-05-08 — track every write across all 8 sqlite3 connections so the failure-path log can identify which OTHER writer was holding the writer lock at db-locked failure time. recent_writes() captures the JUST-FINISHED holder (FAST-fail path: BEGIN IMMEDIATE returns SQLITE_BUSY in <1ms when intra-process lock-holder releases right before our retry).
from bot.main_loop import MainLoop  # noqa: F401 — Bit 9.3 leaf extraction (2026-05-10); re-export so bot/__main__.py `from bot._impl import MainLoop` continues to resolve at 9.3-i; bot/__main__.py swap to direct `from bot.main_loop import MainLoop` deferred to Bit 9.3-ii per master plan.




























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
# `_CALIBRATION_ENGINE` alongside its class. Reads inside THIS module are
# now scoped to the orphan-DB watchdog block (`_alert_orphan_db_holder` +
# the `detect_orphan_db_holders` lsof-not-found Telegram alert path) post-
# Bit-9.3 (MainLoop reads moved to bot/main_loop.py with the class
# extraction). Verify count with `grep -c '_telegram_state._TELEGRAM'
# bot/_impl.py bot/main_loop.py bot/scanner/__init__.py bot/executor.py
# bot/settlement.py` — 5 consumers post-Bit-9.3 (mirrors the
# `_cal_state._CALIBRATION_ENGINE` pattern). The plain
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

# Vestigial post-Bit-3.0.5: registry-membership validator can't fail with a
# FileNotFoundError (no filesystem read). Kept as None for the HPSB_GATE_STATE
# log consumer ("validator_unavailable=no" output at MainLoop.__init__,
# search "HPSB_GATE_STATE:" for the current line).
_HPSB_VALIDATOR_UNAVAILABLE_REASON: Optional[str] = None









# Boot-time validation — runs after all registry sources are in scope.
_HPSB_MISSING_BLEEDERS = _validate_high_price_stc_block_bleeder_strings()
_BLEED_BLOCK_MISSING_BLEEDERS = _validate_bleed_block_bleeder_strings()




# KalshiClient → bot/kalshi_client.py (Bit 4.3, 2026-05-08).
# Re-imported above via `from bot.kalshi_client import KalshiClient`.


# ═════════════════════════════════════════════════════════════════════════════
#  StateManager
# ═════════════════════════════════════════════════════════════════════════════

# Phase 7 Edit 2: static reimplementation of 15M main-path sizing for parity-assert.
# DO NOT use in production trading — only consumed by sizing_parity_assert at startup.
# Bit 7.1 fu (Smell 4, ticket 86b9vhccw): make_compute_for_15m_main_path() now imports
# its dependent names directly from bot.constants + config inside the function body
# (mirrors Bit 7.1 path-A++ parity_assert / sizing_parity_assert). The previous
# `make_compute_for_15m_main_path(globals())` closure-over-bot._impl-namespace pattern
# is gone; SIZING_TIERS / DRAWDOWN_* / per-asset risk caps / STC scaler / DRAWDOWN_HALT_FLOOR
# literal fallback all resolve at call-time via direct imports.
compute_for_15m_main_path = make_compute_for_15m_main_path()


# ── Orphan-DB watchdog (Layer 3 of orphan prevention) ─────────────────
#
# May 3 2026 incident: a `cryptocompare_news_backfill.py` subprocess
# orphaned itself and held state.db's writer lock for 2h42m, eventually
# wedging the live bot through a 6-minute crash-loop on restart. Layers
# 1+2 (wrapper signal-handling + script SIGALRM hard timeout) close the
# orphan-creation paths from the H-4 cron infra. Layer 3 is detection
# at bot startup: enumerate non-bot PIDs touching state.db and alert
# the operator. Detection-only by design — auto-killing is too risky
# (could kill legitimate manually-launched migrations or debug
# sessions); the operator triages from the Telegram alert.
#
# Postmortem: kb/failures/shape-d-contention-explosion-may03.md.

def _run_lsof_for_db(db_path: str) -> List[int]:
    """Return PIDs that have `db_path` open. Implementation: shells
    out to `lsof -t <db_path>`. Separated into its own function so
    tests can stub it out without mocking subprocess globally."""
    import subprocess as _sub
    out = _sub.check_output(
        ["lsof", "-t", db_path], timeout=10, text=True,
    )
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _get_pid_cmdline(pid: int) -> str:
    """Best-effort fetch of the command line for `pid`. Reads
    `/proc/<pid>/cmdline` on Linux, falls back to `ps -p <pid> -o
    command=` on other platforms. Returns empty string on failure
    (the operator still has the PID even without cmdline)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    try:
        import subprocess as _sub
        out = _sub.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            timeout=5, text=True,
        )
        return out.strip()
    except Exception:
        return ""


def _alert_orphan_db_holder(*, pid: int, cmdline: str) -> None:
    """Send a Telegram alert about an orphan PID holding state.db.
    Best-effort — failure to send must not block bot startup."""
    try:
        if _telegram_state._TELEGRAM is None:
            return
        msg = (
            f"⚠️ ORPHAN DB HOLDER detected at startup\n"
            f"pid={pid}\n"
            f"cmd: {cmdline[:300] or '(unknown)'}\n"
            f"This process is holding state.db's lock from outside the "
            f"bot. Investigate via `ssh kalshi-vps; ps -p {pid}` and kill "
            f"if stale. May 3 2026 incident: an H-4c backfill orphan "
            f"wedged the bot for 6 min via this exact pattern."
        )
        _telegram_state._TELEGRAM.send(msg, dedup_key=f"orphan_db_pid_{pid}")
    except Exception:
        logging.debug("orphan-DB Telegram alert failed", exc_info=True)


# Positive-list of cmdline substrings that indicate an orphan we care
# about. We deliberately do NOT alert on legitimate cron-spawned
# cohabitants (watchdog.py, auditor.py, audit_cron.py,
# dashboard_snapshot.py — see adversarial review C-1) because their
# overlap with bot startup is routine and would habituate the operator
# to ignore alerts. The May 3 2026 incident was an H-4 backfill orphan,
# and that's the specific class we're guarding against.
_ORPHAN_DB_WATCHDOG_PATTERNS: List[str] = [
    "gdelt_backfill",
    "cryptocompare_news_backfill",
    "glassnode_backfill",
]


def detect_orphan_db_holders(
    db_path: str, self_pid: Optional[int] = None,
) -> List[Dict[str, object]]:
    """Layer 3 orphan-prevention watchdog.

    Enumerates PIDs holding `db_path` open via `lsof -t`. Filters out
    `self_pid` (defaults to `os.getpid()`). For each remaining PID,
    captures cmdline. Alerts ONLY on PIDs whose cmdline matches a
    known H-4 backfill script (positive-list — see
    `_ORPHAN_DB_WATCHDOG_PATTERNS`). Returns the list of offender
    records `{"pid": int, "cmdline": str}` (the full filtered list,
    pre-alert) for caller-side logging / tests.

    Why positive-list and not "anything not bot/_impl.py": the live VPS has
    several legitimate cron-spawned `state.db` openers (watchdog.py
    every 2 min, auditor.py hourly, audit_cron.py every 30 min,
    operator-run dashboard_snapshot.py). Any of them can collide
    with the watchdog's lsof probe at bot startup. A negative-list
    design would generate alerts on every overlap → alert fatigue →
    the operator stops looking at the channel. Positive-list keeps
    signal high.

    Detection-only — does NOT call `os.kill`. The operator triages
    from the alert.

    Also probes `db_path` + `db_path-wal` so SQLite writers that have
    only the WAL FD open (rare, possible during shutdown races) are
    caught. Adversarial-review C-7."""
    if self_pid is None:
        self_pid = os.getpid()
    try:
        pids = _run_lsof_for_db(db_path)
    except FileNotFoundError as e:
        # Adversarial-review C-5: lsof binary not installed → watchdog
        # is silently no-op for the entire deploy lifetime. Log loud
        # AND emit a one-shot Telegram so the operator knows the
        # safety net is off.
        logging.warning(
            "orphan_db_watchdog: lsof not found (%s); watchdog DISABLED. "
            "Install lsof to re-enable.", e,
        )
        try:
            if _telegram_state._TELEGRAM is not None:
                _telegram_state._TELEGRAM.send(
                    "⚠️ orphan-DB watchdog DISABLED: lsof not installed "
                    "on VPS. May 3 2026 orphan-class incidents are "
                    "undetected until lsof is available.",
                    dedup_key="orphan_watchdog_disabled",
                )
        except Exception:
            pass
        return []
    except Exception as e:
        # Includes CalledProcessError (lsof exit 1 = "no holders found",
        # which on macOS is exit 0 + empty stdout, and on Linux is
        # exit 1) — both indicate "no PIDs," not a failure mode.
        logging.warning(
            "orphan_db_watchdog: lsof probe failed (%s); skipping check",
            e,
        )
        return []
    # Adversarial-review C-7: also probe the WAL sibling so a writer
    # holding only the WAL FD is caught. Union with main probe.
    try:
        wal_pids = _run_lsof_for_db(db_path + "-wal")
        for wp in wal_pids:
            if wp not in pids:
                pids.append(wp)
    except Exception:
        # WAL probe failure is non-fatal; we still have main-DB pids.
        pass
    # Adversarial-review C-4: dedup PIDs (lsof currently dedups for
    # single-file probes but the WAL union above can re-introduce dupes).
    pids = list(dict.fromkeys(pids))
    offenders: List[Dict[str, object]] = []
    for pid in pids:
        if pid == self_pid:
            continue
        cmdline = _get_pid_cmdline(pid)
        # Adversarial-review C-9: the PID may have exited between the
        # lsof snapshot and now — `os.kill(pid, 0)` is a stat-cheap
        # liveness probe; if it raises ProcessLookupError, the orphan
        # is already gone and no alert is needed.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError:
            # EPERM (different uid) — process is alive but we can't
            # signal it. Continue with the alert flow.
            pass
        offenders.append({"pid": pid, "cmdline": cmdline})
        # Adversarial-review C-1: only alert on PIDs that match the
        # known orphan-creator patterns. Otherwise log debug-only and
        # move on — legitimate cron processes (watchdog.py, auditor.py,
        # audit_cron.py, dashboard_snapshot.py) routinely collide with
        # bot startup.
        is_orphan_class = any(
            pat in cmdline for pat in _ORPHAN_DB_WATCHDOG_PATTERNS
        )
        if is_orphan_class:
            logging.error(
                "ORPHAN_DB_HOLDER: pid=%d cmd=%s holds state.db at "
                "startup — investigate (May 3 2026 incident pattern)",
                pid, cmdline[:300] or "(unknown)",
            )
            _alert_orphan_db_holder(pid=pid, cmdline=cmdline)
        else:
            logging.debug(
                "orphan_db_watchdog: pid=%d cmd=%s holds state.db but "
                "is NOT in the orphan-creator allow-list; skipping alert",
                pid, cmdline[:200],
            )
    return offenders


# StateManager → bot/state.py (Bit 7.1, 2026-05-10).
# Re-imported above via `from bot.state import StateManager`.
# Path-A++ extraction (NOT byte-for-byte): the two `globals()` calls inside
# StateManager.__init__ pre-extraction were refactored in-Bit — see
# scripts/cal_mlp/integration.py `parity_assert(conn) -> tuple[str, int]` and
# `sizing_parity_assert(conn, *, rowid, compute_for_15m_main_path)` for the
# new explicit signatures. The `_get_compute_for_15m_main_path()` single-name
# late-binding helper inside bot/state.py wraps the closure created at
# `compute_for_15m_main_path = make_compute_for_15m_main_path()` (Bit 7.1 fu /
# Smell 4, ticket 86b9vhccw, dropped the `globals()` arg + closes over
# function-scoped imports instead). Sister Bit 7.2 (agent_docs/db_schema.md)
# shipped in the same atomic commit as Bit 7.1. Sprint 7 closes here.


# cal_mlp predictor cache + warmup → scripts/cal_mlp/integration.py
# (Smell 3 fu, 86b9vhcat, 2026-05-10). Kill-switch contract preserved:
# predictor INSTANCES always constructed at integration.py module-import time;
# warmup() gated on CALMLP_ENABLED. Module-scoped logger (NOT bare
# `logging.info`) — pinned by tests/regression/test_no_basicconfig_in_bot_impl.py.
_calmlp_enabled_at_boot, _calmlp_warmed = _calmlp_warmup_cache()
if _calmlp_enabled_at_boot:
    logging.getLogger(__name__).info(
        "[CALMLP] enabled=1 at boot, predictors_warmed=%d/4", _calmlp_warmed)
else:
    logging.getLogger(__name__).info(
        "[CALMLP] enabled=0 at boot — predictors constructed but not warmed; "
        "hot env flip to 1 will lazy-load on first scan tick")

# R-p7-deploy-r9: post-hoc processor lifecycle imported here; STARTED later
# from MainLoop.startup() AFTER StateManager + migrate_schema have run.
# Round-1#3: starting at module-import time raced StateManager construction;
# moved to MainLoop.startup() so the cal_mlp_request_id column + partial
# index exist before the first poll.
from integration import (  # noqa: E402
    start_post_hoc_processor as _calmlp_start_posthoc,
)


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
#  OrderFlowEngine
# ═════════════════════════════════════════════════════════════════════════════

class OrderFlowEngine:
    """Aggregates cross-exchange and derivatives signals into a probability adjustment."""

    def __init__(self, cross_feed=None, coinglass=None, kalshi_oft=None):
        self._cross = cross_feed
        self._coinglass = coinglass
        self._kalshi_oft = kalshi_oft

    def get_signals(self, asset: str, **kwargs) -> Dict:
        """Compute order flow adjustment for the given asset.

        Returns:
            {
                "prob_adjustment": float,
                "confidence": "high"|"moderate"|"low"|"none",
                "signals": {
                    "cross_exchange": {...lead_lag dict...},
                    "funding": {"rate": float|None, "level": str},
                },
                "adjustments_applied": [str, ...],
            }
        """
        adjustments: List[Tuple[str, float]] = []
        cross_exchange = {}
        funding_info = {"rate": None, "level": "unknown"}

        # 1. Cross-exchange consensus
        if self._cross is not None:
            try:
                lead_lag = self._cross.get_lead_lag(asset)
                cross_exchange = lead_lag
                direction = lead_lag.get("consensus_direction", "none")
                above = lead_lag.get("exchanges_above", 0)
                below = lead_lag.get("exchanges_below", 0)

                if direction == "above" and above >= CROSS_EXCHANGE_CONSENSUS_MIN:
                    adjustments.append((
                        f"consensus_above_{above}ex",
                        OFA_CONSENSUS_BOOST,
                    ))
                elif direction == "below" and below >= CROSS_EXCHANGE_CONSENSUS_MIN:
                    adjustments.append((
                        f"consensus_below_{below}ex",
                        OFA_CONSENSUS_REDUCE,
                    ))
                elif direction == "mixed":
                    # Weaker signal: at least one exchange leads
                    if above > below:
                        adjustments.append(("lead_above_mixed", OFA_LEAD_BOOST))
                    elif below > above:
                        adjustments.append(("lead_below_mixed", -OFA_LEAD_BOOST))
            except Exception:
                logging.debug("CrossExchangeFeed.get_lead_lag failed", exc_info=True)

        # 2. Funding rate
        if self._coinglass is not None:
            try:
                rate = self._coinglass.get_funding_rate(asset)
                if rate is not None:
                    abs_rate = abs(rate)
                    if abs_rate >= FUNDING_RATE_EXTREME:
                        funding_info = {"rate": rate, "level": "extreme"}
                        adjustments.append((
                            f"extreme_funding_{rate:+.6f}",
                            OFA_EXTREME_FUNDING_REDUCE,
                        ))
                    elif abs_rate >= FUNDING_RATE_ELEVATED:
                        funding_info = {"rate": rate, "level": "elevated"}
                        adjustments.append((
                            f"elevated_funding_{rate:+.6f}",
                            OFA_ELEVATED_FUNDING_REDUCE,
                        ))
                    else:
                        funding_info = {"rate": rate, "level": "normal"}
                else:
                    funding_info = {"rate": None, "level": "unknown"}
            except Exception:
                logging.debug("CoinGlassFetcher.get_funding_rate failed", exc_info=True)

        # 3. Kalshi orderbook flow
        kalshi_flow = {}
        if self._kalshi_oft is not None:
            try:
                ticker = kwargs.get("ticker")
                if ticker:
                    koft = self._kalshi_oft.get_signals(ticker)
                    if koft is not None:
                        kalshi_flow = koft
                        if not KALSHI_OFT_SHADOW_MODE and koft["prob_adjustment"] != 0:
                            adjustments.append(("kalshi_oft", koft["prob_adjustment"]))
            except Exception:
                logging.debug("KalshiOFT.get_signals failed", exc_info=True)

        # 4. Sum and clamp
        total = sum(v for _, v in adjustments)
        total = max(-OFA_MAX_ADJUSTMENT, min(OFA_MAX_ADJUSTMENT, total))

        # 5. Confidence
        abs_total = abs(total)
        if abs_total >= 0.015:
            confidence = "high"
        elif abs_total >= 0.005:
            confidence = "moderate"
        elif abs_total > 0:
            confidence = "low"
        else:
            confidence = "none"

        return {
            "prob_adjustment": total,
            "confidence": confidence,
            "signals": {
                "cross_exchange": cross_exchange,
                "funding": funding_info,
                "kalshi_orderbook": kalshi_flow,
            },
            "adjustments_applied": [
                f"{name}: {val:+.3f}" for name, val in adjustments
            ],
        }


class KalshiOrderFlowTracker:
    """Tracks Kalshi orderbook snapshots over time for flow signals.

    Records full depth-5 snapshots from the scanner's existing orderbook
    fetches (no additional API calls). Computes:
    - Bid/ask imbalance ratio (YES depth vs total)
    - Depth velocity (total depth change rate)
    - Spread dynamics (bid-ask spread trend)
    - Ask convergence velocity (cents/sec)
    """

    def __init__(self):
        self._snapshots: Dict[str, deque] = {}
        self._last_seen: Dict[str, float] = {}
        self._last_log: Dict[str, float] = {}

    def record_snapshot(self, ticker: str, ob_data: Dict, best_ask: int):
        """Record orderbook snapshot. Called from scanner after each OB fetch.

        ob_data format: {"no": [[price_cents, qty], ...], "yes": [[price_cents, qty], ...]}
        """
        now = time.time()
        if ticker not in self._snapshots:
            self._snapshots[ticker] = deque(maxlen=KALSHI_OFT_BUFFER_SIZE)

        # Sum depth per side
        yes_total_qty = 0
        no_total_qty = 0
        best_yes_bid_price = 0

        for entry in (ob_data.get("yes") or []):
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = int(entry[0]), int(entry[1])
                yes_total_qty += qty
                if price > best_yes_bid_price:
                    best_yes_bid_price = price

        for entry in (ob_data.get("no") or []):
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                no_total_qty += int(entry[1])

        spread = (best_ask - best_yes_bid_price) if best_yes_bid_price > 0 else 99

        self._snapshots[ticker].append({
            "ts": now,
            "best_ask": best_ask,
            "best_yes_bid": best_yes_bid_price,
            "yes_total_qty": yes_total_qty,
            "no_total_qty": no_total_qty,
            "total_depth": yes_total_qty + no_total_qty,
            "spread": spread,
        })
        self._last_seen[ticker] = now

    def get_signals(self, ticker: str) -> Optional[Dict]:
        """Compute order flow signals from snapshot history. Returns None if insufficient data."""
        snaps = self._snapshots.get(ticker)
        if not snaps or len(snaps) < KALSHI_OFT_MIN_SNAPSHOTS:
            return None

        snap_list = list(snaps)
        latest = snap_list[-1]
        earliest = snap_list[0]
        time_span = latest["ts"] - earliest["ts"]
        if time_span <= 0:
            return None

        # 1. Imbalance: YES bids / total depth
        total_qty = latest["yes_total_qty"] + latest["no_total_qty"]
        imbalance = latest["yes_total_qty"] / total_qty if total_qty > 0 else 0.5

        if imbalance >= KALSHI_OFT_IMBALANCE_STRONG:
            imbalance_level = "strong_buy"
        elif imbalance <= KALSHI_OFT_IMBALANCE_WEAK:
            imbalance_level = "strong_sell"
        else:
            imbalance_level = "neutral"

        # 2. Depth velocity
        depth_velocity = (latest["total_depth"] - earliest["total_depth"]) / time_span
        depth_pct_change = ((latest["total_depth"] - earliest["total_depth"])
                           / earliest["total_depth"]) if earliest["total_depth"] > 0 else 0.0
        depth_drain = depth_pct_change < KALSHI_OFT_DEPTH_DRAIN_PCT

        # 3. Spread trend
        spread_trend = (latest["spread"] - earliest["spread"]) / time_span

        # 4. Ask velocity
        ask_velocity = (latest["best_ask"] - earliest["best_ask"]) / time_span

        # 5. Prob adjustment (shadow or live)
        adjustments = []
        if imbalance_level == "strong_buy":
            adjustments.append(("kalshi_imbalance_buy", OFA_KALSHI_IMBALANCE_BOOST))
        elif imbalance_level == "strong_sell":
            adjustments.append(("kalshi_imbalance_sell", OFA_KALSHI_IMBALANCE_REDUCE))
        if depth_drain and ask_velocity > 0:
            adjustments.append(("kalshi_depth_drain", OFA_KALSHI_DEPTH_DRAIN_BOOST))
        if ask_velocity > 0.5:
            adjustments.append(("kalshi_convergence", OFA_KALSHI_CONVERGENCE_BOOST))

        total_adj = max(-0.02, min(0.02, sum(v for _, v in adjustments)))

        # Confidence
        n_snaps = len(snap_list)
        if n_snaps >= 30 and total_qty >= 20:
            confidence = "high"
        elif n_snaps >= 15 or total_qty >= 10:
            confidence = "moderate"
        else:
            confidence = "low"

        result = {
            "imbalance_ratio": round(imbalance, 4),
            "imbalance_level": imbalance_level,
            "depth_velocity": round(depth_velocity, 2),
            "depth_drain": depth_drain,
            "depth_pct_change": round(depth_pct_change, 4),
            "spread_current": latest["spread"],
            "spread_trend": round(spread_trend, 4),
            "ask_velocity": round(ask_velocity, 4),
            "prob_adjustment": round(total_adj, 6),
            "adjustments_applied": [f"{n}: {v:+.3f}" for n, v in adjustments],
            "n_snapshots": n_snaps,
            "confidence": confidence,
        }

        # Periodic per-ticker diagnostic logging
        now = time.time()
        last_log = self._last_log.get(ticker, 0.0)
        if now - last_log >= KALSHI_OFT_LOG_INTERVAL:
            self._last_log[ticker] = now
            adj_str = ", ".join(f"{n}: {v:+.3f}" for n, v in adjustments) if adjustments else "none"
            logging.info(
                "KalshiOFT %s: imbal=%.3f (%s) depth_vel=%.1f depth_pct=%.1f%% "
                "spread=%d trend=%.3f ask_vel=%.3f adj=%.4f [%s] snaps=%d conf=%s shadow=%s",
                ticker, imbalance, imbalance_level, depth_velocity,
                depth_pct_change * 100, latest["spread"], spread_trend,
                ask_velocity, total_adj, adj_str, n_snaps, confidence,
                KALSHI_OFT_SHADOW_MODE,
            )

        return result

    def cleanup_stale(self, active_tickers: Set[str]):
        """Evict tickers no longer in active windows."""
        now = time.time()
        stale = [t for t, ts in self._last_seen.items()
                 if now - ts > KALSHI_OFT_STALE_SECONDS or t not in active_tickers]
        for t in stale:
            self._snapshots.pop(t, None)
            self._last_seen.pop(t, None)

    def get_tracked_count(self) -> int:
        return len(self._snapshots)


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
