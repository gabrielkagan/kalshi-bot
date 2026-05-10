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
import bot.notifier as _telegram_state  # Bit 8.1 path-A++ (2026-05-10): alias for `_telegram_state._TELEGRAM` module-attribute access. The singleton lives in bot/notifier.py alongside TelegramNotifier; the module-attribute access pattern preserves mutation freshness across all three consumers post-Bit-9.1 (this module + bot/scanner/__init__.py + bot/executor.py — verify counts with `grep -c '_telegram_state\._TELEGRAM' bot/_impl.py bot/scanner/__init__.py bot/executor.py` — every reader through this alias sees writes immediately because we go through the module reference, NOT a captured-by-value binding). Mirrors the Bit 6.3 path-B `_cal_state` pattern. MainLoop.__init__ writes via `_telegram_state._TELEGRAM = self.telegram` (drops the previous `global _TELEGRAM` declaration). NOTE: explicit `import bot.notifier as ...` (NOT `from bot import notifier as ...`) — the latter form goes through `_BotProxy.__getattr__` and triggers a partial-module ImportError of bot._impl from inside bot.scanner during its load.
from bot.kalshi_client import KalshiClient  # noqa: F401 — Bit 4.3 leaf extraction; re-export so MainLoop construction (search "self.client = KalshiClient") + type annotations on reconcile_with_api/_reconcile_positions/_reconcile_orders/OpportunityScanner/OrderExecutor/SettlementTracker/discover_active_windows resolve via bot._impl namespace.
from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher  # noqa: F401 — Bit 4.4 leaf extraction; re-export so MainLoop construction (search "self.dvol_fetcher = DeribitDVOLFetcher" and "self.coinglass = CoinGlassFetcher") + the Optional[DeribitDVOLFetcher] type annotation on VolatilityEngine.__init__ (now in bot/engines/volatility.py per Bit 6.1) resolve via bot._impl namespace.
from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError  # noqa: F401 — Bit 4.5a + 4.5b leaf extraction; re-export so MainLoop construction (search "self.feed = CoinbaseFeed", "self.cross_feed = CrossExchangeFeed", and "self.kalshi_feed = KalshiFeed") + the `feed: CoinbaseFeed` type annotations on VolatilityEngine.__init__ (now in bot/engines/volatility.py per Bit 6.1) and OpportunityScanner.__init__ + the OrderbookSchemaError raises inside KalshiFeed (now sibling-imported from bot.feeds.orderbook_schema) all resolve via bot._impl namespace.
from bot.engines import VolatilityEngine, ProbabilityEngine, CalibrationEngine  # noqa: F401 — Bit 6.1 + 6.2 + 6.3 leaf extractions; re-export so MainLoop construction (search "self.vol = VolatilityEngine" and "self.calibration = CalibrationEngine()") + the `vol: VolatilityEngine` type annotation on OpportunityScanner.__init__ + the bare-name `ProbabilityEngine.X(...)` call sites (scan-loop edge computation, counterfactual probability, dynamic cap lookup) + the bare-name `CalibrationEngine(...)` construction sites in MainLoop.__init__ + the static-method calls in tests/test_vol_engine.py / tests/test_probability_engine.py / tests/test_calibration_engine.py (`from bot import VolatilityEngine, ProbabilityEngine, CalibrationEngine`) all resolve via bot._impl namespace. The Optional['EGARCHEstimator'] / Optional['MincerZarnowitzTracker'] forward-refs on VolatilityEngine.__init__ remain string-quoted because both classes still live in models.py. **Bit 6.3 path-B refactor (2026-05-10)**: the `_cal_state._CALIBRATION_ENGINE` singleton + `_cal_state._CAL_REGISTRY` dict + `_cal_state._derive_subtype`/`_cal_state._derive_asset_filter`/`_cal_state._resolve_cal_engine` helpers all moved to `bot/engines/calibration.py` alongside the class. Both this module and `bot/engines/probability.py` reach them via `_cal_state.X` (see the `from bot.engines import calibration as _cal_state` alias below). The path-B move lifted the previous Bit 6.2 late-binding `from bot import _impl as _bot_impl` pattern inside ProbabilityEngine — top-level imports work because `bot.engines.calibration` is a leaf (does NOT import bot._impl). Removes the `.importlinter` `bot.engines.probability -> bot._impl` carve-out shipped in Pillar 2.
from bot.engines import calibration as _cal_state  # Bit 6.3 path-B: alias for _cal_state._CALIBRATION_ENGINE / _cal_state._CAL_REGISTRY / _cal_state._derive_subtype / _cal_state._derive_asset_filter / _cal_state._resolve_cal_engine which all live in bot/engines/calibration.py post-Bit-6.3. Module-attribute access pattern (e.g., `_cal_state._CALIBRATION_ENGINE = self.calibration`) preserves singleton mutation semantics — every reader through this alias sees writes immediately because we go through the module reference, not a captured-by-value binding.
from bot.state import StateManager  # noqa: F401 — Bit 7.1 leaf extraction (2026-05-10); re-export so MainLoop construction (`self.state = StateManager()`) + 3 consumer-class type annotations (`OpportunityScanner.__init__`, `OrderExecutor.__init__`, `SettlementTracker.__init__`: `state: StateManager`) + ~50 test instantiation sites (`bot.StateManager(...)` via _BotProxy → bot._impl.StateManager → bot.state.StateManager) all resolve. **Path-A++ deviation note**: bot/state.py introduces a `_get_compute_for_15m_main_path()` single-name late-binding helper (returns `bot._impl.compute_for_15m_main_path` bound at line 350 below). Sister Bit 7.1 also refactored `parity_assert` and `sizing_parity_assert` in scripts/cal_mlp/integration.py to drop their `bot_globals` parameter and import constants directly — the previous `globals()` smell at the StateManager.__init__ call sites is fixed in-Bit per the modularization strategic goal of reducing code smells. Sister Bit 7.2 ships in lock-step with this commit (agent_docs/db_schema.md refresh).
from bot.scanner import OpportunityScanner  # noqa: F401 — Bit 8.1 leaf extraction (2026-05-10, path-A++); re-export so MainLoop construction (`self.scanner = OpportunityScanner(..., main_loop=self)`) + ~13 consumer call sites (12 OrderExecutor static-method calls + 1 MainLoop static-method call referencing `OpportunityScanner._best_yes_ask_cents` / `_convert_orderbook_fp`) + ~30 test instantiation sites (`bot.OpportunityScanner(...)` via _BotProxy → bot._impl.OpportunityScanner → bot.scanner.OpportunityScanner) all resolve. **Path-A++ deviation note (post-Bit-9.1, 2026-05-10)**: (1) the `_get_order_executor()` late-binding helper in bot/scanner/__init__.py was RETIRED in Bit 9.1 atomically with the OrderExecutor extraction (see line-116 `from bot.executor import OrderExecutor` re-export below) — bot/scanner now uses a top-level `from bot.executor import OrderExecutor` directly; the `scanner-no-impl-toplevel` `.importlinter` contract dropped in the same commit (net contracts: 6 → 5); (2) the `Optional["OrderFlowEngine"]` and `Optional["KalshiOrderFlowTracker"]` quoted forward-refs in `__init__` signature for the 2 sister-class type annotations stay quoted (cycle avoidance — both classes still in this file; search anchors: ``class OrderFlowEngine:`` and ``class KalshiOrderFlowTracker:``). Sister Bit 8.1 ALSO relocated the `_TELEGRAM` module-level singleton from this file to `bot/notifier.py` (path-A++ relocation), reached via `_telegram_state._TELEGRAM` module-attribute access (parallel to Bit 6.3 path-B `_cal_state._CALIBRATION_ENGINE` pattern; preserves mutation freshness across consumers). Sprint 8 closed at Bit 8.2 (`bot/scanner/CLAUDE.md`).
from bot.executor import OrderExecutor  # noqa: F401 — Bit 9.1 leaf extraction (2026-05-10, path-A++); re-export so MainLoop construction (`self.executor = OrderExecutor(self.client, self.state, self.logger, main_loop=self, kalshi_feed=self.kalshi_feed)`) + ~12 test instantiation sites (`bot.OrderExecutor(...)` via _BotProxy → bot._impl.OrderExecutor → bot.executor.OrderExecutor) all resolve. **Path-A++ deviations**: (1) `_append_raw_api_journal` relocated to bot/helpers/raw_api_journal.py (consumed via L81 alias-import below at line ~282); (2) 4 latent `OpportunityScanner._best_ask_depth(...)` AttributeErrors fixed (closes ticket 86b9vn9r5; `_best_ask_depth` is a staticmethod on OrderExecutor itself). Sister cleanup atomic in this Bit: `_get_order_executor()` helper retired from bot/scanner/__init__.py + `scanner-no-impl-toplevel` `.importlinter` contract dropped (net contracts: 6 → 5).
from bot.db_writer_registry import tracked_write, snapshot_active, recent_writes  # ops: db-locked RCA instrumentation 2026-05-08 — track every write across all 8 sqlite3 connections so the failure-path log can identify which OTHER writer was holding the writer lock at db-locked failure time. recent_writes() captures the JUST-FINISHED holder (FAST-fail path: BEGIN IMMEDIATE returns SQLITE_BUSY in <1ms when intra-process lock-holder releases right before our retry).




























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
# `_CALIBRATION_ENGINE` alongside its class. Reads inside this module
# all use `_telegram_state._TELEGRAM` module-attribute access (verify
# post-Bit-9.1 with `grep -c '_telegram_state._TELEGRAM' bot/_impl.py
# bot/scanner/__init__.py bot/executor.py`; mirrors the
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







from bot.helpers.raw_api_journal import append_raw_api_journal as _append_raw_api_journal  # Bit 9.1 path-A++ relocation: pure leaf helper moved to bot/helpers/raw_api_journal.py (3 callers total — 1 in OrderExecutor now in bot/executor.py, 2 in SettlementTracker below — search anchors: `_append_raw_api_journal({` inside class SettlementTracker). L81 underscore alias keeps bot._impl.__dict__ snapshot byte-stable for the SettlementTracker callers until Bit 9.2 retires them.







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
# (consumed here via the L81 alias-import at line ~282; SettlementTracker callers
# at lines ~1116/~1358 unchanged until Bit 9.2 retires them — search anchors:
# `_append_raw_api_journal({` inside class SettlementTracker).
# Sister cleanup: `_get_order_executor()` retired from bot/scanner/__init__.py;
# `scanner-no-impl-toplevel` `.importlinter` contract dropped (net contracts: 6 → 5).
# 4 latent `OpportunityScanner._best_ask_depth(...)` AttributeErrors fixed in this Bit
# (closes ticket 86b9vn9r5).


# ═════════════════════════════════════════════════════════════════════════════
#  SettlementTracker
# ═════════════════════════════════════════════════════════════════════════════

class SettlementTracker:
    """Incremental settlement poller. API is the single source of truth.

    Polls GET /portfolio/settlements?min_ts={last_check} every 30 seconds.
    On settlement: look up trade in SQLite, record WIN/LOSS from market_result,
    calculate net P&L from revenue, log to settlement_journal.jsonl, update
    running balance.

    Never uses z-score heuristics or balance deltas to determine outcomes.
    """

    def __init__(self, client: KalshiClient, state: StateManager,
                 logger: Logger, main_loop=None):
        self._client = client
        self._state = state
        self._logger = logger
        self._ml = main_loop
        self._last_check_ts: int = 0
        self._last_poll_time: float = 0.0
        self._last_fallback_sweep: float = 0.0
        self._last_order_cleanup: float = 0.0
        self._processed_tickers: Set[str] = set()
        self._pending_rejection_tickers: Set[str] = set()
        self._settled_rejection_tickers: Set[str] = set()
        # Re-entry guard for tick() worker thread. Prevents thread
        # pile-up if a settlement cycle takes longer than
        # SETTLEMENT_CHECK_SECONDS. Apr 25 00:39 incident: synchronous
        # tick was 4.84-5.54s per cycle; threading the body restores
        # main-loop cadence.
        self._worker_running: bool = False

    # ── Startup ──────────────────────────────────────────────────────────

    def startup(self):
        """Initialize watermark to 24h ago, load dedup set, sweep once."""
        self._last_check_ts = int(
            (datetime.datetime.now(timezone.utc) - datetime.timedelta(hours=24)).timestamp()
        )
        self._load_processed_tickers()
        self._load_pending_rejections()
        self._poll()

    def _load_pending_rejections(self):
        """Load unsettled rejected tickers from DB."""
        rows = self._state.get_unsettled_rejections()
        self._pending_rejection_tickers = {r["ticker"] for r in rows}
        logging.info(
            f"SettlementTracker: loaded {len(self._pending_rejection_tickers)} "
            f"pending rejected opportunities"
        )

    def _load_processed_tickers(self):
        """Load already-settled tickers from DB for deduplication."""
        rows = self._state.conn.execute(
            "SELECT ticker FROM settled_trades"
        ).fetchall()
        self._processed_tickers = {row["ticker"] for row in rows}
        logging.info(
            f"SettlementTracker: loaded {len(self._processed_tickers)} "
            f"previously settled tickers"
        )

    # ── Tick (called every main-loop iteration) ──────────────────────────

    def tick(self):
        """Self-throttled: only polls every SETTLEMENT_CHECK_SECONDS.

        The tick body runs in a daemon worker thread to keep the main
        scan loop unblocked. With 400+ pending evaluated_opportunities
        rows, the synchronous version was 4.84-5.54s per cycle (Apr 25
        00:39 SLOW_SCAN_TICK incident). The `_worker_running` flag
        prevents thread pile-up if a cycle exceeds the throttle.
        """
        now = time.time()
        if now - self._last_poll_time < SETTLEMENT_CHECK_SECONDS:
            return
        if self._worker_running:
            # Previous worker still running; skip this cycle. The
            # next call after _last_poll_time advances will spawn fresh.
            return
        self._last_poll_time = now
        self._worker_running = True

        def _worker():
            try:
                self._poll()
                self._poll_rejections()
                self._poll_evaluated_opportunities()
                # Fallback: sweep for positions stuck past market close (every 5 min)
                _wn = time.time()
                if _wn - self._last_fallback_sweep >= 300.0:
                    self._last_fallback_sweep = _wn
                    self._sweep_stuck_positions()
                # Cleanup expired resting orders (every 60s)
                if _wn - self._last_order_cleanup >= 60.0:
                    self._last_order_cleanup = _wn
                    try:
                        self._state.cleanup_expired_resting_orders()
                    except Exception:
                        logging.debug("cleanup_expired_resting_orders failed",
                                      exc_info=True)
            except Exception:
                logging.error("SettlementTracker worker thread failed",
                              exc_info=True)
            finally:
                self._worker_running = False

        try:
            threading.Thread(
                target=_worker,
                daemon=True,
                name="settlement_tracker",
            ).start()
        except Exception:
            self._worker_running = False
            logging.debug(
                "SettlementTracker worker thread spawn failed",
                exc_info=True)

    # ── Core poll ────────────────────────────────────────────────────────

    def _poll(self):
        """Fetch new settlements from API and process them."""
        unsettled = self._state.get_unsettled_positions()
        if not unsettled:
            return

        resp = self._client.get_settlements(min_ts=self._last_check_ts)
        if LOG_RAW_SETTLEMENTS and resp:
            _append_raw_api_journal({
                "kind": "settlements",
                "min_ts": self._last_check_ts,
                "resp": resp,
            })
        if not resp or "settlements" not in resp:
            return

        settlements = resp["settlements"]
        if not settlements:
            return

        our_tickers = {p["ticker"] for p in unsettled}
        processed_any = False

        for s in settlements:
            ticker = s.get("ticker", "")

            # Skip if already processed (dedup)
            if ticker in self._processed_tickers:
                continue

            # Only process settlements for our open positions
            if ticker not in our_tickers:
                continue

            try:
                self._process_settlement(s)
                processed_any = True
            except Exception as e:
                logging.error(f"Settlement processing failed for {ticker}: {e}", exc_info=True)

        # Advance watermark to now (even if nothing processed, to shrink window)
        self._last_check_ts = int(datetime.datetime.now(timezone.utc).timestamp())

        # Refresh balance after processing settlements
        if processed_any:
            balance_resp = self._client.get_balance()
            if balance_resp:
                new_balance = balance_resp.get("balance") or 0
                logging.info(
                    f"Balance after settlements: ${new_balance / 100:.2f}"
                )
            # Invalidate scanner balance cache so next tick's record_balance()
            # gets post-settlement balance. Without this, the 10s cache TTL
            # causes record_balance to record stale pre-settlement balance,
            # compressing drawdown_scaler for one tick. (Learned: 33% of
            # candidates got ds<1.0 from stale cache, Mar 29-30 2026.)
            try:
                if self._ml and hasattr(self._ml, 'scanner'):
                    self._ml.scanner._balance_cache = (None, 0.0)
            except Exception:
                pass

    # ── Fallback sweep for stuck positions ───────────────────────────────

    def _sweep_stuck_positions(self):
        """Detect positions whose market close time has passed and settle via
        individual market lookup.  This catches positions that were skipped by
        the watermark-based settlement poll (e.g. unknown market_result at the
        time, revenue=0 on WIN timing race, etc.).
        """
        import re
        _MONTH_MAP = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
        unsettled = self._state.get_unsettled_positions()
        if not unsettled:
            return
        now_utc = datetime.datetime.now(timezone.utc).replace(tzinfo=None)
        for pos in unsettled:
            ticker = pos["ticker"]
            if ticker in self._processed_tickers:
                continue
            # Parse close time from 15M ticker format
            m = re.match(r'KX\w+15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-', ticker)
            if not m:
                continue  # Non-15M — hourly/weather have different settlement paths
            yy, mon, dd, hh, mm = m.groups()
            mon_num = _MONTH_MAP.get(mon)
            if not mon_num:
                continue
            try:
                close_et = datetime.datetime(2000 + int(yy), mon_num, int(dd), int(hh), int(mm))
                close_utc = close_et + datetime.timedelta(hours=4)  # ET→UTC (EDT)
            except (ValueError, OverflowError):
                continue
            # Only sweep if market closed >5 min ago (allow normal settlement path time)
            if now_utc < close_utc + datetime.timedelta(minutes=5):
                continue
            # Fetch market result directly from API
            try:
                mkt = self._client.get_market(ticker)
                if not mkt or "market" not in mkt:
                    continue
                market_data = mkt["market"]
                result = market_data.get("result", "")
                if not result:
                    continue  # Not yet settled on Kalshi
                # Capture expiration_value (CFB RTI settlement price) for divergence analysis
                _exp_val = market_data.get("expiration_value")
                logging.warning(
                    "sweep_stuck_positions: recovering %s (result=%s, "
                    "close_utc=%s, stuck >5min, expiration_value=%s)",
                    ticker, result, close_utc, _exp_val)
                # Build a synthetic settlement dict and process it.
                # _from_sweep=True tells _process_settlement to skip the
                # revenue=0/WIN guard (we compute PnL from first principles).
                settlement = {
                    "ticker": ticker,
                    "market_result": result,
                    "revenue_dollars": None,
                    "revenue": 0,
                    "settled_time": market_data.get("close_time", ""),
                    "_from_sweep": True,
                    "_expiration_value": _exp_val,
                }
                self._process_settlement(settlement)
            except Exception as e:
                logging.error("sweep_stuck_positions failed for %s: %s", ticker, e, exc_info=True)

    # ── Process a single settlement ──────────────────────────────────────

    def _process_settlement(self, settlement: Dict):
        """Record outcome, P&L, and log to journal.

        Handles stacked positions: fetches ALL position rows for the ticker,
        computes per-row PnL from first principles, records each to settled_trades,
        then marks all settled in one UPDATE.
        """
        ticker = settlement["ticker"]
        market_result = settlement.get("market_result", "")
        rev_d = settlement.get("revenue_dollars")
        revenue = dollars_str_to_cents(rev_d) if rev_d else (settlement.get("revenue") or 0)

        # Look up ALL position rows for this ticker
        pos_rows = self._state.conn.execute(
            "SELECT * FROM positions WHERE ticker=?", (ticker,)
        ).fetchall()
        if not pos_rows:
            logging.warning(
                f"SettlementTracker: no position found for {ticker}"
            )
            return
        positions = [dict(r) for r in pos_rows]
        is_stacked = len(positions) > 1

        # Determine WIN/LOSS from market_result only (API is truth)
        side = positions[0]["side"]
        if market_result == "yes":
            outcome = "WIN" if side == "yes" else "LOSS"
        elif market_result == "no":
            outcome = "WIN" if side == "no" else "LOSS"
        elif market_result == "all_no":
            outcome = "WIN" if side == "no" else "LOSS"
        elif market_result == "all_yes":
            outcome = "WIN" if side == "yes" else "LOSS"
        else:
            outcome = "UNKNOWN"
            logging.critical(
                f"UNKNOWN market_result '{market_result}' for {ticker} "
                f"— skipping settlement to prevent bad P&L recording"
            )
            return

        # Aggregate count across all position rows for cross-checks
        aggregate_count = sum(p["count"] for p in positions)
        aggregate_cost = sum(p["total_cost_cents"] for p in positions)

        # Cross-check: revenue=0 on a WIN is almost certainly a false position.
        # Skip this guard for sweep-recovered settlements — they always have
        # revenue=0 and compute PnL from first principles.
        from_sweep = settlement.get("_from_sweep", False)
        if outcome == "WIN" and revenue == 0 and aggregate_count > 0 and not from_sweep:
            logging.critical(
                f"SETTLEMENT REVENUE ZERO ON WIN {ticker}: "
                f"market_result={market_result} side={side} count={aggregate_count} "
                f"cost={aggregate_cost}¢ fill_source={positions[0].get('fill_source')} — "
                f"Kalshi likely has no matching position. "
                f"Skipping settlement to prevent false -{aggregate_cost}¢ loss.")
            return

        # Cross-check: detect count mismatch between internal tracking
        # and Kalshi settlement.  For YES wins, revenue = real_count * 100.
        if revenue > 0 and outcome == "WIN" and side == "yes":
            implied_count = revenue // 100
            if implied_count != aggregate_count:
                logging.error(
                    f"SETTLEMENT COUNT MISMATCH {ticker}: "
                    f"internal={aggregate_count} kalshi={implied_count} "
                    f"revenue={revenue}¢ n_rows={len(positions)}")
                if implied_count == 0 and aggregate_count > 0:
                    # Sub-dollar revenue (1-99¢) floors to 0 contracts under
                    # `revenue // 100`. Auto-zeroing a confirmed-filled
                    # position on the basis of a sub-dollar revenue value is
                    # almost always wrong: it fabricates a $0 settled_trade
                    # and silently diverges local cost tracking from reality.
                    # Trust the local fill record; alert and fall through to
                    # per-row PnL computed from first principles.
                    # (Learned: KXXRP15M-26APR241200-00 141ct WIN and
                    # KXSOL15M-26APR230200-00 32ct WIN both silently zeroed
                    # Apr 23-24 2026.)
                    logging.critical(
                        f"SETTLEMENT_REVENUE_SUB_DOLLAR {ticker}: "
                        f"kalshi_revenue={revenue}¢ implied_count=0 vs "
                        f"internal={aggregate_count} — REFUSING to auto-zero. "
                        f"Trusting local count; investigate Kalshi payload.")
                    if _telegram_state._TELEGRAM:
                        try:
                            _telegram_state._TELEGRAM.send(
                                f"🚨 SETTLEMENT_REVENUE_SUB_DOLLAR {ticker}: "
                                f"Kalshi revenue={revenue}¢ on "
                                f"{aggregate_count}ct WIN — refused auto-zero. "
                                f"Check journal for payload.")
                        except Exception:
                            pass
                elif len(positions) == 1:
                    # Single row: auto-correct with strategy_group
                    p = positions[0]
                    sg = p.get("strategy_group", "main")
                    corrected_cost = implied_count * p["avg_price_cents"]
                    self._state.conn.execute(
                        "UPDATE positions SET count=?, total_cost_cents=? "
                        "WHERE ticker=? AND strategy_group=?",
                        (implied_count, corrected_cost, ticker, sg))
                    p["count"] = implied_count
                    p["total_cost_cents"] = corrected_cost
                    aggregate_count = implied_count
                    aggregate_cost = corrected_cost
                else:
                    logging.warning(
                        "SETTLEMENT_MULTI_MISMATCH: %s — NOT auto-correcting stacked positions",
                        ticker)

        # Cross-check on LOSSES: revenue=0 gives no count info, so fetch the
        # authoritative fill count from Kalshi's fills API. Catches IOC-path
        # double-count bugs that the WIN-side check can't see.
        # (Learned: XRP 06:15 Apr 19 2026 recorded 208ct local vs 104ct Kalshi
        # -> $99 over-reported loss; 1 of 2 divergent in 30d/48 IOC losses.)
        if outcome == "LOSS" and len(positions) == 1 and positions[0].get("is_taker"):
            try:
                _fresp = self._client.get_fills(ticker=ticker, limit=200)
                if LOG_RAW_IOC_FILLS and _fresp:
                    _append_raw_api_journal({
                        "kind": "loss_check_fills",
                        "ticker": ticker,
                        "aggregate_count": aggregate_count,
                        "resp": _fresp,
                    })
                if _fresp and _fresp.get("fills"):
                    _local_order_ids = set()
                    for _r in self._state.conn.execute(
                        "SELECT order_id FROM pending_orders WHERE ticker=?",
                        (ticker,)
                    ).fetchall():
                        _oid = _r["order_id"] if isinstance(_r, sqlite3.Row) else _r[0]
                        if _oid:
                            _local_order_ids.add(_oid)
                    _kalshi_count = 0
                    for _f in _fresp["fills"]:
                        if _f.get("order_id") in _local_order_ids:
                            _c = fp_str_to_int(_f.get("count_fp")) or int(_f.get("count") or 0)
                            _kalshi_count += _c
                    if _kalshi_count > 0 and _kalshi_count != aggregate_count:
                        logging.error(
                            f"SETTLEMENT_LOSS_COUNT_MISMATCH {ticker}: "
                            f"internal={aggregate_count} kalshi={_kalshi_count} "
                            f"(loss-side cross-check) — auto-correcting")
                        p = positions[0]
                        sg = p.get("strategy_group", "main")
                        corrected_cost = _kalshi_count * p["avg_price_cents"]
                        self._state.conn.execute(
                            "UPDATE positions SET count=?, total_cost_cents=? "
                            "WHERE ticker=? AND strategy_group=?",
                            (_kalshi_count, corrected_cost, ticker, sg))
                        p["count"] = _kalshi_count
                        p["total_cost_cents"] = corrected_cost
                        aggregate_count = _kalshi_count
                        aggregate_cost = corrected_cost
            except Exception:
                logging.warning(
                    "Loss-side count cross-check failed for %s", ticker,
                    exc_info=True)

        # Process each position row independently
        combined_pnl = 0
        combined_fee = 0
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        for pos in positions:
            row_count = pos["count"]
            row_cost = pos["total_cost_cents"]
            # Revenue from first principles: WIN yes-side → count*100, LOSS → 0
            if outcome == "WIN":
                if side == "yes":
                    row_revenue = row_count * 100
                else:
                    row_revenue = row_count * 100  # NO-side win: paid (100-p), get 100
            else:
                row_revenue = 0
            row_pnl = row_revenue - row_cost
            row_is_taker = bool(pos.get("is_taker"))
            row_fee = calculate_fee(row_count, pos["avg_price_cents"], is_taker=row_is_taker)

            combined_pnl += row_pnl
            combined_fee += row_fee

            # Record each row to settled_trades (revenue_override prevents
            # stacked positions from each getting the full API aggregate revenue)
            self._state.record_settlement(
                settlement, revenue_override=row_revenue,
                pnl_override=row_pnl, fee_override=row_fee, pos=pos)

        # Mark ALL positions for this ticker as settled (once, outside loop)
        self._state.conn.execute("""
            UPDATE positions SET status='settled', updated_at=?
            WHERE ticker=?
        """, (now, ticker))
        self._state.conn.commit()

        # Mark as processed for dedup (once)
        self._processed_tickers.add(ticker)

        # Fetch expiration_value (CFB RTI settlement price) for divergence analysis.
        # For sweep settlements, it's already in the settlement dict.
        # For normal settlements, one extra API call (non-blocking, after PnL recorded).
        _exp_val = settlement.get("_expiration_value")
        if _exp_val is None:
            try:
                _exp_mkt = self._client.get_market(ticker)
                if _exp_mkt:
                    _exp_val = _exp_mkt.get("market", _exp_mkt).get("expiration_value")
            except Exception:
                pass
        if _exp_val is not None:
            logging.info("EXPIRATION_VALUE: %s expiration_value=%s asset=%s",
                         ticker, _exp_val, positions[0]["asset"])

        # Rich journal entry with combined PnL
        self._logger.log_settlement({
            "ticker": ticker,
            "event_ticker": positions[0]["event_ticker"],
            "asset": positions[0]["asset"],
            "outcome": outcome,
            "market_result": market_result,
            "side": side,
            "count": aggregate_count,
            "entry_price_cents": positions[0]["avg_price_cents"],
            "total_cost_cents": aggregate_cost,
            "revenue_cents": revenue,
            "fee_cents": combined_fee,
            "pnl_cents": combined_pnl,
            "pnl_net_cents": combined_pnl - combined_fee,
            "settled_time": settlement.get("settled_time", ""),
            "is_stacked": is_stacked,
            "n_positions": len(positions),
            "expiration_value": _exp_val,
        })

        stacked_tag = f" [STACKED x{len(positions)}]" if is_stacked else ""
        logging.info(
            f"Settlement: {ticker} -> {outcome}{stacked_tag} "
            f"(market_result={market_result}, "
            f"revenue={revenue}¢, cost={aggregate_cost}¢, "
            f"pnl={combined_pnl}¢, fee={combined_fee}¢)"
        )
        if _telegram_state._TELEGRAM:
            emoji = "\u2705" if outcome == "WIN" else "\u274c"
            sign = "+" if combined_pnl >= 0 else ""
            pnl_dollars = combined_pnl / 100
            bal_str = ""
            try:
                bal_resp = self._client.get_balance()
                if bal_resp:
                    bal_str = f" | Balance: ${bal_resp.get('balance', 0) / 100:.2f}"
            except Exception:
                pass
            _telegram_state._TELEGRAM.send(
                f"{emoji} {outcome} {positions[0]['asset']} {aggregate_count}ct "
                f"@{positions[0]['avg_price_cents']}c {sign}${abs(pnl_dollars):.2f}"
                f"{stacked_tag}{bal_str}"
            )

    # ── Rejection Settlement ─────────────────────────────────────────────

    def register_rejection_ticker(self, ticker: str):
        """Called by scanner when a new rejection is recorded."""
        self._pending_rejection_tickers.add(ticker)

    def _poll_rejections(self):
        """Check if any rejected-opportunity tickers have settled."""
        # Refresh from DB to pick up rejections inserted by scanner since last poll
        db_rows = self._state.get_unsettled_rejections()
        for r in db_rows:
            self._pending_rejection_tickers.add(r["ticker"])

        if not self._pending_rejection_tickers:
            return

        # Snapshot to iterate safely
        tickers_to_check = list(
            self._pending_rejection_tickers - self._settled_rejection_tickers
        )
        for ticker in tickers_to_check:
            try:
                resp = self._client.get_market(ticker)
                if not resp:
                    continue
                market = resp.get("market", resp)
                result = market.get("result", "")
                if result:
                    self._process_rejection_settlement(market, ticker)
            except Exception as e:
                logging.warning(
                    f"Rejection settlement check failed for {ticker}: {e}", exc_info=True)

    def _process_rejection_settlement(self, market: Dict, ticker: str):
        """Compute counterfactual P&L for a rejected opportunity that settled."""
        result = market.get("result", "")

        # Look up the rejection row from SQLite
        row = self._state.conn.execute(
            "SELECT * FROM rejected_opportunities WHERE ticker=?", (ticker,)
        ).fetchone()
        if not row:
            return

        entry_price = row["market_price"]
        # If we never had a market price (pre-filter rejection), skip P&L calc
        if entry_price is None:
            would_have_profit = None
            assumed_fee = 0
            counterfactual_outcome = "unknown_no_price"
        else:
            # Counterfactual: bought 1 YES contract at entry_price (include taker fee)
            # Note: rejected_opportunities are always YES-side. NO-side goes through
            # evaluated_opportunities which has its own side-aware settlement in
            # _poll_evaluated_opportunities().
            assumed_fee = calculate_taker_fee(1, int(entry_price))
            if result in ("yes", "all_yes"):
                would_have_profit = (100 - entry_price) - assumed_fee  # cents
                counterfactual_outcome = "would_have_won"
            elif result in ("no", "all_no"):
                would_have_profit = -(entry_price + assumed_fee)  # cents
                counterfactual_outcome = "would_have_lost"
            else:
                would_have_profit = None
                counterfactual_outcome = f"unknown_result_{result}"

        cf_json = json.dumps({
            "outcome": counterfactual_outcome,
            "would_have_profit_cents": would_have_profit,
            "assumed_fee_cents": assumed_fee,
            "entry_price": entry_price,
        })

        self._logger.log_rejection({
            "type": "rejection_settlement",
            "ticker": ticker,
            "event_ticker": row["event_ticker"],
            "asset": row["asset"],
            "rejection_reason": row["rejection_reason"],
            "market_result": result,
            "entry_price_if_traded": entry_price,
            "counterfactual_outcome": counterfactual_outcome,
            "would_have_profit_cents": would_have_profit,
            "assumed_fee_cents": assumed_fee,
            "assumed_contracts": 1,
            "z_score": row["z_score"],
            "spot_price": row["spot_price"],
            "threshold": row["threshold"],
        })

        self._state.mark_rejection_settled(ticker, market_result=result,
                                           counterfactual=cf_json)
        self._settled_rejection_tickers.add(ticker)
        self._pending_rejection_tickers.discard(ticker)

        logging.info(
            f"Rejection settled: {ticker} -> {counterfactual_outcome} "
            f"(result={result}, would_have_profit={would_have_profit}¢)"
        )

    # ── Evaluated Opportunity Settlement ──────────────────────────────────

    @staticmethod
    def _parse_weather_market_date(ticker: str) -> Optional[str]:
        """Extract the market date from a weather ticker as YYYY-MM-DD.

        Ticker format: KXHIGHNY-26FEB28-T50 → date segment '26FEB28' → '2026-02-28'
        """
        parts = ticker.split("-")
        if len(parts) < 2:
            return None
        raw = parts[1]  # e.g. '26FEB28', '26MAR01', '26MAR03'
        if len(raw) < 7:
            return None
        try:
            dt = datetime.datetime.strptime(raw, "%y%b%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    @staticmethod
    def _estimate_actual_temp_from_bracket(ticker, threshold, spot_price=None):
        """Estimate actual temperature from a settled weather bracket for bias update.

        B-type: midpoint of bracket (threshold is floor_strike, brackets ~2°F wide)
        T-type: threshold ± 2°F based on tail direction (uses spot_price to disambiguate)
        Returns estimated actual temperature or None.
        """
        parts = ticker.split("-")
        if len(parts) < 3:
            return None
        strike_part = parts[-1]
        if strike_part.startswith("B"):
            # Bracket: threshold is floor_strike, bracket ~2°F wide → midpoint
            return threshold + 1.0
        elif strike_part.startswith("T"):
            # Tail: use spot_price (ensemble mean at evaluation time) to determine direction
            if spot_price is not None:
                if threshold < spot_price:
                    return threshold - 2.0  # lower tail: actual below threshold
                else:
                    return threshold + 2.0  # upper tail: actual above threshold
            return None  # can't determine tail direction without spot_price
        return None

    def _poll_evaluated_opportunities(self):
        """Check if any evaluated opportunities have settled for counterfactual tracking.
        Groups rows by ticker to avoid redundant API calls and batches DB commits."""
        try:
            rows = self._state.get_unsettled_evaluated_opportunities()
        except Exception as e:
            logging.warning(f"get_unsettled_evaluated_opportunities failed: {e}", exc_info=True)
            return

        if not rows:
            return

        # Volume warning — high pending count means observation modes are flooding the table
        if len(rows) > 50:
            logging.warning(
                "eval_opp_settlement: %d pending rows (>50 threshold) — "
                "check observation mode volume (weather=%d, hourly=%d, spx=%d, sports=%d, 15m=%d)",
                len(rows),
                sum(1 for r in rows if r.get("product_type") == "weather"),
                sum(1 for r in rows if r.get("product_type") == "hourly"),
                sum(1 for r in rows if r.get("product_type") == "spx_hourly"),
                sum(1 for r in rows if r.get("product_type") == "sports"),
                sum(1 for r in rows if r.get("product_type") == "15m"),
            )

        # Group rows by ticker — one API call per unique ticker
        from collections import defaultdict
        ticker_groups: dict = defaultdict(list)
        for row in rows:
            ticker_groups[row["ticker"]].append(row)

        # Fetch market result once per unique ticker
        ticker_results: dict = {}
        for ticker in ticker_groups:
            try:
                resp = self._client.get_market(ticker)
                if not resp:
                    continue
                market = resp.get("market", resp)
                result = market.get("result", "")
                if result:
                    ticker_results[ticker] = result
            except Exception as e:
                logging.warning(f"get_market failed for {ticker}: {e}")

        if not ticker_results:
            return

        logging.info("eval_opp_settlement: %d unique tickers settled (from %d pending rows)",
                     len(ticker_results), len(rows))

        # ── Phase 1: Compute settlement results in memory (NO DB writes) ──
        # This avoids holding a write lock during the computation + JSONL logging.
        # Each entry: (opp_id, ticker, result, row, would_have_profit, counterfactual_outcome,
        #              count, taker_fee, maker_fee, pnl_taker, pnl_maker)
        _settlement_batch: list = []
        _cal_observations: list = []  # (raw_p, cal_binary, _opp_pt, asset, filter_stage)
        _weather_updates: list = []   # (opp_id, ticker, row) — need API calls, done after commit
        for ticker, result in ticker_results.items():
            for row in ticker_groups[ticker]:
                opp_id = row["id"]
                try:
                    entry_price = row["market_price"]
                    _opp_pt = row.get("product_type")
                    if entry_price is None:
                        would_have_profit = None
                        taker_fee = 0
                        maker_fee = 0
                        pnl_taker = None
                        pnl_maker = None
                        count = row.get("position_size") or 1
                        counterfactual_outcome = "unknown_no_price"
                    elif _opp_pt == "weather" and entry_price < WEATHER_MIN_ENTRY_PRICE:
                        count = row.get("position_size") or 1
                        would_have_profit = 0
                        counterfactual_outcome = "untradeable_price"
                        taker_fee = 0
                        maker_fee = 0
                        pnl_taker = 0
                        pnl_maker = 0
                    else:
                        count = row.get("position_size") or 1
                        taker_fee = calculate_taker_fee(count, int(entry_price))
                        maker_fee = calculate_maker_fee(count, int(entry_price))
                        _opp_side = row.get("side") or "yes"
                        if _opp_side == "no":
                            _is_win = result in ("no", "all_no")
                            _is_loss = result in ("yes", "all_yes")
                        else:
                            _is_win = result in ("yes", "all_yes")
                            _is_loss = result in ("no", "all_no")
                        if _is_win:
                            pnl_taker = (100 - entry_price) * count - taker_fee
                            pnl_maker = (100 - entry_price) * count - maker_fee
                            counterfactual_outcome = "would_have_won"
                        elif _is_loss:
                            pnl_taker = -(entry_price * count + taker_fee)
                            pnl_maker = -(entry_price * count + maker_fee)
                            counterfactual_outcome = "would_have_lost"
                        else:
                            pnl_taker = None
                            pnl_maker = None
                            taker_fee = 0
                            maker_fee = 0
                            counterfactual_outcome = f"unknown_result_{result}"
                        would_have_profit = pnl_taker

                    # JSONL logging (no DB write)
                    self._logger.log_rejection({
                        "type": "evaluated_settlement",
                        "ticker": ticker,
                        "event_ticker": row["event_ticker"],
                        "asset": row["asset"],
                        "filter_stage": row["filter_stage"],
                        "rejection_reason": row.get("rejection_reason"),
                        "market_result": result,
                        "entry_price_if_traded": entry_price,
                        "counterfactual_outcome": counterfactual_outcome,
                        "would_have_profit_cents": would_have_profit,
                        "assumed_contracts": count,
                        "taker_fee_cents": taker_fee,
                        "maker_fee_cents": maker_fee,
                        "pnl_taker_cents": pnl_taker,
                        "pnl_maker_cents": pnl_maker,
                        "calibrated_prob": row.get("calibrated_prob"),
                        "edge": row.get("edge"),
                        "strategy": row.get("strategy"),
                        "position_size": row.get("position_size"),
                        "kelly_f": row.get("kelly_f"),
                        "vol_regime": row.get("vol_regime"),
                        "z_score": row.get("z_score"),
                        "raw_prob": row.get("raw_prob"),
                        "calibration_method": row.get("calibration_method"),
                        "fee_adjusted_edge": row.get("fee_adjusted_edge"),
                        "old_system_prob": row.get("old_system_prob"),
                    })

                    _settlement_batch.append((opp_id, ticker, result, row,
                                              would_have_profit, counterfactual_outcome))

                    # Prepare CalEngine observations
                    raw_p = row.get("raw_prob")
                    filter_stage = row.get("filter_stage", "")
                    _opp_side = row.get("side") or "yes"
                    if (raw_p is not None and _opp_side == "yes"
                            and result in ("yes", "all_yes", "no", "all_no")
                            and not filter_stage.endswith("_v2")):
                        cal_binary = 1 if result in ("yes", "all_yes") else 0
                        _cal_observations.append((raw_p, cal_binary, _opp_pt,
                                                  row.get("asset"), filter_stage,
                                                  row.get("seconds_to_close")))

                    # Queue weather temp fetches for after commit
                    if (_opp_pt == "weather" and result in ("yes", "all_yes", "no", "all_no")
                            and row.get("wx_actual_high_temp") is None):
                        _weather_updates.append((opp_id, ticker, row))

                    logging.info(
                        f"Evaluated opp settled: {ticker} ({row['filter_stage']}) "
                        f"-> {counterfactual_outcome} (profit={would_have_profit}¢)"
                    )
                except Exception as e:
                    logging.warning(f"Evaluated opp settlement check failed for {ticker}: {e}", exc_info=True)

        # ── Phase 2: Fast DB writes (short lock, no API calls) ──
        # Commit in chunks of 50 to keep write-lock duration short.
        # Large batches (200+) hold the lock long enough to deadlock with
        # supabase_sync reader + WAL checkpoint. (Mar 16 2026)
        _SETTLEMENT_BATCH_SIZE = 50
        _settled_count = 0
        if _settlement_batch:
            for _chunk_start in range(0, len(_settlement_batch), _SETTLEMENT_BATCH_SIZE):
                _chunk = _settlement_batch[_chunk_start:_chunk_start + _SETTLEMENT_BATCH_SIZE]
                try:
                    for (opp_id, ticker, result, row,
                         would_have_profit, counterfactual_outcome) in _chunk:
                        self._state.mark_evaluated_opportunity_settled(
                            opp_id, market_result=result,
                            counterfactual_pnl=would_have_profit,
                            commit=False)
                        _settled_count += 1
                    self._state.conn.commit()
                except Exception as e:
                    try:
                        self._state.conn.rollback()
                    except Exception:
                        pass
                    logging.warning("eval_opp_settlement batch commit failed (chunk %d-%d): %s",
                                    _chunk_start, _chunk_start + len(_chunk), e, exc_info=True)
            if _settled_count:
                logging.info("eval_opp_settlement: committed %d rows in %d chunks",
                             _settled_count,
                             (len(_settlement_batch) + _SETTLEMENT_BATCH_SIZE - 1) // _SETTLEMENT_BATCH_SIZE)

        # ── Phase 3: Post-commit work (CalEngine, shadow settlement, weather) ──
        # These run AFTER the write lock is released.

        # Feed CalEngine observations
        for _cal_item in _cal_observations:
            raw_p, cal_binary, _opp_pt, _asset, filter_stage = _cal_item[0], _cal_item[1], _cal_item[2], _cal_item[3], _cal_item[4]
            _cal_stc = _cal_item[5] if len(_cal_item) > 5 else None
            _settle_engine = _cal_state._resolve_cal_engine(_opp_pt, _asset)
            if _settle_engine is not None:
                _settle_engine.add_observation(raw_p, cal_binary, filter_stage=filter_stage,
                                              seconds_to_close=_cal_stc)
            # Dual-feed: 15M per-asset engines AND global engine (keeps shadow pipeline working)
            if _opp_pt in (None, "15m") and _cal_state._CALIBRATION_ENGINE is not None:
                _cal_state._CALIBRATION_ENGINE.add_observation(raw_p, cal_binary, seconds_to_close=_cal_stc)
            elif (_settle_engine is None
                  and filter_stage in ("candidate", "observation_trade",
                                       "hourly_observation", "spx_observation",
                                       "weather_observation")
                  and get_market_config(_opp_pt).cal_eligible):
                if _cal_state._CALIBRATION_ENGINE is not None:
                    _cal_state._CALIBRATION_ENGINE.add_observation(raw_p, cal_binary)

        # Settle shadow signals (per unique settled ticker)
        for ticker, result in ticker_results.items():
            if result not in ("yes", "all_yes", "no", "all_no"):
                continue
            if self._ml and getattr(self._ml, "fifteenm_shadow", None):
                try:
                    self._ml.fifteenm_shadow.settle_signals(ticker, result)
                except Exception:
                    logging.warning("fifteenm_shadow settle failed for %s", ticker, exc_info=True)

            if self._ml and getattr(self._ml, "hourly_alt_shadow", None):
                try:
                    self._ml.hourly_alt_shadow.settle_signals(ticker, result)
                except Exception:
                    logging.warning("hourly_alt_shadow settle failed for %s", ticker, exc_info=True)

            if self._ml and getattr(self._ml, "spx_harrv_shadow", None):
                try:
                    self._ml.spx_harrv_shadow.settle_signals(ticker, result)
                except Exception:
                    logging.warning("spx_harrv_shadow settle failed for %s", ticker, exc_info=True)

            # Settle SOL Path C shadow entry for this ticker
            try:
                _pc_row = self._state.conn.execute(
                    "SELECT * FROM sol_pathc_shadow WHERE ticker=? AND status='pending'",
                    (ticker,)).fetchone()
                if _pc_row:
                    _pc = dict(_pc_row)
                    _is_win = result in ("yes", "all_yes")
                    _live_price = _pc["live_entry_price"]
                    _live_contracts = _pc["live_contracts"]
                    _pos_size = _pc["position_size"]

                    _live_fee = calculate_taker_fee(_live_contracts, _live_price)
                    if _is_win:
                        _live_pnl = (100 - _live_price) * _live_contracts - _live_fee
                    else:
                        _live_pnl = -(_live_price * _live_contracts + _live_fee)

                    _maker_price = _pc["pathc_maker_price"]
                    _maker_depth = _pc["pathc_depth_at_maker"] or 0
                    _maker_touched = _pc["obs_maker_price_touched"] or 0
                    _maker_contracts = min(_pos_size, _maker_depth) if _maker_touched else 0
                    _maker_fee = calculate_maker_fee(_maker_contracts, _maker_price) if _maker_contracts > 0 else 0
                    if _maker_contracts > 0:
                        if _is_win:
                            _maker_pnl = (100 - _maker_price) * _maker_contracts - _maker_fee
                        else:
                            _maker_pnl = -(_maker_price * _maker_contracts + _maker_fee)
                    else:
                        _maker_pnl = 0

                    _esc_ask = _pc["pathc_esc_ask"]
                    _esc_depth = _pc["pathc_esc_depth"] or 0
                    if _esc_ask is not None and _esc_depth > 0:
                        _esc_contracts = min(_pos_size, _esc_depth)
                        _esc_fee = calculate_taker_fee(_esc_contracts, _esc_ask)
                        if _is_win:
                            _esc_pnl = (100 - _esc_ask) * _esc_contracts - _esc_fee
                        else:
                            _esc_pnl = -(_esc_ask * _esc_contracts + _esc_fee)
                    else:
                        _esc_contracts = 0
                        _esc_pnl = 0

                    if _maker_touched and _maker_contracts > 0:
                        _remainder = max(0, _pos_size - _maker_contracts)
                        if _remainder > 0 and _esc_ask is not None and _esc_depth > 0:
                            _rem_contracts = min(_remainder, _esc_depth)
                            _rem_fee = calculate_taker_fee(_rem_contracts, _esc_ask)
                            if _is_win:
                                _rem_pnl = (100 - _esc_ask) * _rem_contracts - _rem_fee
                            else:
                                _rem_pnl = -(_esc_ask * _rem_contracts + _rem_fee)
                        else:
                            _rem_pnl = 0
                        _best_pnl = _maker_pnl + _rem_pnl
                    else:
                        _best_pnl = _esc_pnl

                    self._state.settle_sol_pathc_shadow(
                        ticker=ticker, market_result=result,
                        live_pnl=_live_pnl,
                        pathc_maker_pnl=_maker_pnl,
                        pathc_maker_contracts=_maker_contracts,
                        pathc_esc_pnl=_esc_pnl,
                        pathc_esc_contracts=_esc_contracts,
                        pathc_best_pnl=_best_pnl)
                    logging.info(
                        "sol_pathc_settled: %s result=%s live_pnl=%d maker_pnl=%d esc_pnl=%d best_pnl=%d",
                        ticker, result, _live_pnl, _maker_pnl, _esc_pnl, _best_pnl)
            except Exception:
                logging.warning("sol_pathc_shadow settle failed for %s", ticker, exc_info=True)

        # Settle low_price_shadow_signals
        for ticker, result in ticker_results.items():
            if result not in ("yes", "all_yes", "no", "all_no"):
                continue
            try:
                _lps_rows = self._state.conn.execute(
                    "SELECT id, market_price, full_kelly_contracts, capped_contracts "
                    "FROM low_price_shadow_signals WHERE ticker=? AND status='open'",
                    (ticker,)).fetchall()
                for _lps in _lps_rows:
                    _lps_id = _lps["id"]
                    _lps_price = _lps["market_price"]
                    _is_win = result in ("yes", "all_yes")
                    # Full Kelly PnL
                    _full_ct = _lps["full_kelly_contracts"] or 1
                    _full_fee = calculate_taker_fee(_full_ct, _lps_price)
                    if _is_win:
                        _full_pnl = (100 - _lps_price) * _full_ct - _full_fee
                    else:
                        _full_pnl = -(_lps_price * _full_ct + _full_fee)
                    # Capped Kelly PnL
                    _cap_ct = _lps["capped_contracts"] or 1
                    _cap_fee = calculate_taker_fee(_cap_ct, _lps_price)
                    if _is_win:
                        _cap_pnl = (100 - _lps_price) * _cap_ct - _cap_fee
                    else:
                        _cap_pnl = -(_lps_price * _cap_ct + _cap_fee)
                    self._state.conn.execute(
                        "UPDATE low_price_shadow_signals SET status='settled', "
                        "market_result=?, counterfactual_pnl_full=?, counterfactual_pnl_capped=?, "
                        "settled_at=? WHERE id=?",
                        (result, _full_pnl, _cap_pnl,
                         datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         _lps_id))
                if _lps_rows:
                    self._state.conn.commit()
            except Exception:
                logging.warning("low_price_shadow settle failed for %s", ticker, exc_info=True)

        # Settle tm_sweep_shadow rows. Reuses the same ticker_results aggregation
        # as low_price_shadow above. Idempotent — only acts on rows with
        # status='open', so safe if this method is invoked repeatedly.
        if TM_SWEEP_SHADOW_ENABLED:
            for ticker, result in ticker_results.items():
                if result not in ("yes", "all_yes", "no", "all_no"):
                    continue
                try:
                    self._state.update_tm_sweep_shadow_on_settlement(ticker, result)
                except Exception:
                    logging.warning("tm_sweep_shadow settle failed for %s", ticker, exc_info=True)

        # Weather: fetch actual temps (API calls — after lock released)
        _wx_dirty = False
        for (opp_id, ticker, row) in _weather_updates:
            try:
                _wx_city = row["asset"].replace("_TEMP", "")
                _market_date = self._parse_weather_market_date(ticker)
                if _market_date:
                    _today = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    if _market_date < _today:
                        _wx_eng = getattr(self._ml, "weather_engine", None) if self._ml else None
                        if _wx_eng:
                            _obs_high = _wx_eng._fetcher.fetch_observed_high(_wx_city, _market_date)
                            if _obs_high is not None:
                                self._state.conn.execute(
                                    "UPDATE evaluated_opportunities SET wx_actual_high_temp=? WHERE id=?",
                                    (_obs_high, opp_id))
                                _wx_dirty = True
                                logging.info("weather_observed_temp: %s %s %.1fF",
                                             _wx_city, _market_date, _obs_high)
                                forecast_mean = row.get("spot_price")
                                if forecast_mean:
                                    _wx_eng._model.update_bias(
                                        _wx_city, _obs_high, forecast_mean,
                                        market_date=_market_date)
                                    logging.info("weather_bias_update: %s %s actual=%.1fF forecast=%.1fF",
                                                 _wx_city, _market_date, _obs_high, forecast_mean)
            except Exception as e:
                logging.warning("weather_observed_temp fetch failed for %s: %s", ticker, e)
        if _wx_dirty:
            self._state.conn.commit()

        # Backfill wx_actual_high_temp for settled weather entries that missed it
        self._backfill_weather_actual_temps()

    def _backfill_weather_actual_temps(self):
        """Retry archive API fetch for settled weather entries missing wx_actual_high_temp."""
        try:
            rows = self._state.conn.execute(
                "SELECT id, ticker, asset, spot_price FROM evaluated_opportunities "
                "WHERE product_type='weather' AND status='settled' "
                "AND wx_actual_high_temp IS NULL LIMIT 10"
            ).fetchall()
        except Exception:
            return
        if not rows:
            return
        _today = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _wx_eng = getattr(self._ml, "weather_engine", None) if self._ml else None
        if not _wx_eng:
            return
        for r in rows:
            opp_id, ticker, asset, forecast_mean = r
            try:
                _wx_city = asset.replace("_TEMP", "")
                _market_date = self._parse_weather_market_date(ticker)
                if not _market_date or _market_date >= _today:
                    continue
                _obs_high = _wx_eng._fetcher.fetch_observed_high(_wx_city, _market_date)
                if _obs_high is not None:
                    self._state.conn.execute(
                        "UPDATE evaluated_opportunities SET wx_actual_high_temp=? WHERE id=?",
                        (_obs_high, opp_id))
                    self._state.conn.commit()
                    logging.info("weather_backfill_temp: %s %s %.1fF", _wx_city, _market_date, _obs_high)
                    # Bias update with real observed temp
                    if forecast_mean:
                        _wx_eng._model.update_bias(
                            _wx_city, _obs_high, forecast_mean,
                            market_date=_market_date)
            except Exception as e:
                logging.warning("weather_backfill failed for %s: %s", ticker, e)


# ═════════════════════════════════════════════════════════════════════════════
#  Market Discovery
# ═════════════════════════════════════════════════════════════════════════════

def discover_active_windows(client: KalshiClient) -> List[Dict]:
    """
    Query Kalshi for currently open crypto windows (15M + hourly).

    Uses the events endpoint (GET /events) with status=open and
    with_nested_markets=true to find tradeable markets. The markets
    endpoint (GET /markets) with series_ticker only returns pre-created
    'initialized' markets on production, missing the active ones.

    Returns list of dicts with asset, event_ticker, close_time,
    seconds_to_close, markets list, and product_type.
    """
    now = datetime.datetime.now(timezone.utc)
    windows: List[Dict] = []

    # Build combined series list: 15M always, hourly when enabled
    series_list = [(a, s, "15m") for a, s in SERIES_TICKERS.items()]
    if HOURLY_OBSERVATION_ENABLED:
        series_list += [(a, s, "hourly") for a, s in HOURLY_SERIES_TICKERS.items()]

    for asset, series, product_type in series_list:
        result = client.get_events(
            series_ticker=series,
            status="open",
            with_nested_markets=True,
            limit=100,
        )
        events = result.get("events") if result else None
        if not events:
            logging.warning(
                f"Market discovery: {asset} ({series}) — API returned no data"
            )
            continue
        market_count = 0

        for event in events:
            event_ticker = event.get("event_ticker", "")
            nested_markets = event.get("markets", [])
            if not isinstance(nested_markets, list):
                continue

            # Filter to actual market dicts (not string references)
            mkts = [m for m in nested_markets if isinstance(m, dict)]
            if not mkts:
                continue

            market_count += len(mkts)

            close_time_str = mkts[0].get("close_time", "")
            try:
                close_time = datetime.datetime.fromisoformat(
                    close_time_str.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                continue

            seconds_to_close = (close_time - now).total_seconds()
            if seconds_to_close < 0:
                continue
            windows.append({
                "asset": asset,
                "event_ticker": event_ticker,
                "close_time": close_time,
                "seconds_to_close": seconds_to_close,
                "markets": mkts,
                "product_type": product_type,
            })

        if market_count == 0:
            logging.info(
                f"Market discovery: {asset} ({series}) — 0 open markets"
            )
        else:
            logging.info(
                f"Market discovery: {asset} ({series}) — "
                f"{market_count} markets in {len(events)} windows"
            )

    return windows


# ═════════════════════════════════════════════════════════════════════════════
#  MainLoop
# ═════════════════════════════════════════════════════════════════════════════

class MainLoop:
    """Continuous observation loop. Scans active windows every second."""

    def __init__(self):
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
                     SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE)
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

        # Backward compat for dashboard_snapshot.py
        self.hourly_calibration = self._cal_engines.get("hourly")
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.telegram = TelegramNotifier(tg_token, tg_chat)
        # Bit 8.1 path-A++ (2026-05-10): write through to the source-of-truth
        # singleton in bot/notifier.py so all consumers (this module + bot/scanner)
        # reading via `_telegram_state._TELEGRAM` see the mutation immediately.
        # Drops the previous `global _TELEGRAM` declaration since we no longer
        # rebind a module-level name in this file.
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
                from spx_engine import SPXEngine
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
                from weather_engine import WeatherEngine
                self.weather_engine = WeatherEngine(db_path=DB_PATH)
                logging.info("Weather engine initialized")
            except Exception as e:
                logging.warning(f"Weather engine unavailable: {e}")

        # ── 15M Shadow Engine (recalibrated EGARCH + LightGBM) ─────────
        self.fifteenm_shadow = None
        try:
            from fifteenm_shadow import FifteenMShadowEngine, FIFTEENM_SHADOW_ENABLED
            if FIFTEENM_SHADOW_ENABLED:
                self.fifteenm_shadow = FifteenMShadowEngine(db_path=DB_PATH)
                logging.info("15M shadow engine initialized (recalibrated EGARCH + LightGBM)")
        except Exception as e:
            logging.warning(f"15M shadow engine unavailable: {e}")

        # ── Hourly Alt Shadow Engine (ETH/SOL/XRP shadow strategies) ──────
        self.hourly_alt_shadow = None
        try:
            from hourly_alt_shadow import HourlyAltShadowEngine, HOURLY_ALT_SHADOW_ENABLED
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
            from market_observations_snapshotter import (
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
                    "market_observations_snapshotter.py contract)"
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
                from spx_harrv_shadow import SPXHARRVShadowEngine, SPX_HARRV_SHADOW_ENABLED
                if SPX_HARRV_SHADOW_ENABLED:
                    self.spx_harrv_shadow = SPXHARRVShadowEngine(db_path=DB_PATH)
                    logging.info("SPX HAR-RV shadow engine initialized")
            except Exception as e:
                logging.warning(f"SPX HAR-RV shadow engine unavailable: {e}")

        # ── Sports Engine (conditional) ────────────────────────────────────
        self.sports_engine = None
        if SPORTS_ENABLED:
            try:
                from sports_engine import SportsEngine
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
            from capital_allocator import CapitalAllocator
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
            from bot_state_snapshot import compute_bot_state_snapshot as _moc_snap
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
            from dashboard_snapshot import DashboardSnapshotBuilder
            self.snapshot_builder = DashboardSnapshotBuilder(self)
        except Exception as e:
            logging.info(f"Dashboard snapshot builder not available: {e}")
            self.snapshot_builder = None

        # Start Supabase syncer (if configured)
        try:
            from supabase_sync import SupabaseSyncer
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
                "EGARCH buffer saved on shutdown: BTC=%d ETH=%d SOL=%d XRP=%d",
                len(self.egarch_estimator._returns["BTC"]),
                len(self.egarch_estimator._returns["ETH"]),
                len(self.egarch_estimator._returns["SOL"]),
                len(self.egarch_estimator._returns["XRP"]))
        if hasattr(self, 'mz_tracker'):
            self.mz_tracker.save_state()
            logging.info("MZ tracker state saved on shutdown")
        if hasattr(self, 'vol'):
            self.vol.save_rk_state()
            logging.info(
                "RK state saved on shutdown: BTC=%d ETH=%d SOL=%d XRP=%d returns",
                len(self.vol._returns["BTC"]), len(self.vol._returns["ETH"]),
                len(self.vol._returns["SOL"]), len(self.vol._returns["XRP"]))
            self.vol._save_adaptive_state()
            logging.info(
                "Adaptive jump state saved on shutdown: BTC=%d ETH=%d SOL=%d XRP=%d obs",
                len(self.vol._adaptive_returns_15s["BTC"]),
                len(self.vol._adaptive_returns_15s["ETH"]),
                len(self.vol._adaptive_returns_15s["SOL"]),
                len(self.vol._adaptive_returns_15s["XRP"]))
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


# ═════════════════════════════════════════════════════════════════════════════
#  Entrypoint moved to bot/__main__.py — `python -m bot` invokes it.
# ═════════════════════════════════════════════════════════════════════════════
