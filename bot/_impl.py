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
import bot.notifier as _telegram_state  # Bit 8.1 path-A++ (2026-05-10): alias for `_telegram_state._TELEGRAM` module-attribute access. The singleton lives in bot/notifier.py alongside TelegramNotifier; the module-attribute access pattern preserves mutation freshness across consumers (both this module's read sites and bot/scanner/__init__.py's read sites — verify counts with `grep -c '_telegram_state\._TELEGRAM' bot/_impl.py bot/scanner/__init__.py` — every reader through this alias sees writes immediately because we go through the module reference, NOT a captured-by-value binding). Mirrors the Bit 6.3 path-B `_cal_state` pattern. MainLoop.__init__ writes via `_telegram_state._TELEGRAM = self.telegram` (drops the previous `global _TELEGRAM` declaration). NOTE: explicit `import bot.notifier as ...` (NOT `from bot import notifier as ...`) — the latter form goes through `_BotProxy.__getattr__` and triggers a partial-module ImportError of bot._impl from inside bot.scanner during its load.
from bot.kalshi_client import KalshiClient  # noqa: F401 — Bit 4.3 leaf extraction; re-export so MainLoop construction (search "self.client = KalshiClient") + type annotations on reconcile_with_api/_reconcile_positions/_reconcile_orders/OpportunityScanner/OrderExecutor/SettlementTracker/discover_active_windows resolve via bot._impl namespace.
from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher  # noqa: F401 — Bit 4.4 leaf extraction; re-export so MainLoop construction (search "self.dvol_fetcher = DeribitDVOLFetcher" and "self.coinglass = CoinGlassFetcher") + the Optional[DeribitDVOLFetcher] type annotation on VolatilityEngine.__init__ (now in bot/engines/volatility.py per Bit 6.1) resolve via bot._impl namespace.
from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError  # noqa: F401 — Bit 4.5a + 4.5b leaf extraction; re-export so MainLoop construction (search "self.feed = CoinbaseFeed", "self.cross_feed = CrossExchangeFeed", and "self.kalshi_feed = KalshiFeed") + the `feed: CoinbaseFeed` type annotations on VolatilityEngine.__init__ (now in bot/engines/volatility.py per Bit 6.1) and OpportunityScanner.__init__ + the OrderbookSchemaError raises inside KalshiFeed (now sibling-imported from bot.feeds.orderbook_schema) all resolve via bot._impl namespace.
from bot.engines import VolatilityEngine, ProbabilityEngine, CalibrationEngine  # noqa: F401 — Bit 6.1 + 6.2 + 6.3 leaf extractions; re-export so MainLoop construction (search "self.vol = VolatilityEngine" and "self.calibration = CalibrationEngine()") + the `vol: VolatilityEngine` type annotation on OpportunityScanner.__init__ + the bare-name `ProbabilityEngine.X(...)` call sites (scan-loop edge computation, counterfactual probability, dynamic cap lookup) + the bare-name `CalibrationEngine(...)` construction sites in MainLoop.__init__ + the static-method calls in tests/test_vol_engine.py / tests/test_probability_engine.py / tests/test_calibration_engine.py (`from bot import VolatilityEngine, ProbabilityEngine, CalibrationEngine`) all resolve via bot._impl namespace. The Optional['EGARCHEstimator'] / Optional['MincerZarnowitzTracker'] forward-refs on VolatilityEngine.__init__ remain string-quoted because both classes still live in models.py. **Bit 6.3 path-B refactor (2026-05-10)**: the `_cal_state._CALIBRATION_ENGINE` singleton + `_cal_state._CAL_REGISTRY` dict + `_cal_state._derive_subtype`/`_cal_state._derive_asset_filter`/`_cal_state._resolve_cal_engine` helpers all moved to `bot/engines/calibration.py` alongside the class. Both this module and `bot/engines/probability.py` reach them via `_cal_state.X` (see the `from bot.engines import calibration as _cal_state` alias below). The path-B move lifted the previous Bit 6.2 late-binding `from bot import _impl as _bot_impl` pattern inside ProbabilityEngine — top-level imports work because `bot.engines.calibration` is a leaf (does NOT import bot._impl). Removes the `.importlinter` `bot.engines.probability -> bot._impl` carve-out shipped in Pillar 2.
from bot.engines import calibration as _cal_state  # Bit 6.3 path-B: alias for _cal_state._CALIBRATION_ENGINE / _cal_state._CAL_REGISTRY / _cal_state._derive_subtype / _cal_state._derive_asset_filter / _cal_state._resolve_cal_engine which all live in bot/engines/calibration.py post-Bit-6.3. Module-attribute access pattern (e.g., `_cal_state._CALIBRATION_ENGINE = self.calibration`) preserves singleton mutation semantics — every reader through this alias sees writes immediately because we go through the module reference, not a captured-by-value binding.
from bot.state import StateManager  # noqa: F401 — Bit 7.1 leaf extraction (2026-05-10); re-export so MainLoop construction (`self.state = StateManager()`) + 3 consumer-class type annotations (`OpportunityScanner.__init__`, `OrderExecutor.__init__`, `SettlementTracker.__init__`: `state: StateManager`) + ~50 test instantiation sites (`bot.StateManager(...)` via _BotProxy → bot._impl.StateManager → bot.state.StateManager) all resolve. **Path-A++ deviation note**: bot/state.py introduces a `_get_compute_for_15m_main_path()` single-name late-binding helper (returns `bot._impl.compute_for_15m_main_path` bound at line 350 below). Sister Bit 7.1 also refactored `parity_assert` and `sizing_parity_assert` in scripts/cal_mlp/integration.py to drop their `bot_globals` parameter and import constants directly — the previous `globals()` smell at the StateManager.__init__ call sites is fixed in-Bit per the modularization strategic goal of reducing code smells. Sister Bit 7.2 ships in lock-step with this commit (agent_docs/db_schema.md refresh).
from bot.scanner import OpportunityScanner  # noqa: F401 — Bit 8.1 leaf extraction (2026-05-10, path-A++); re-export so MainLoop construction (`self.scanner = OpportunityScanner(..., main_loop=self)`) + ~13 consumer call sites (12 OrderExecutor static-method calls + 1 MainLoop static-method call referencing `OpportunityScanner._best_yes_ask_cents` / `_convert_orderbook_fp`) + ~30 test instantiation sites (`bot.OpportunityScanner(...)` via _BotProxy → bot._impl.OpportunityScanner → bot.scanner.OpportunityScanner) all resolve. **Path-A++ deviation note**: bot/scanner/__init__.py introduces (1) a `_get_order_executor()` single-name late-binding helper for the 34 `OrderExecutor.X(...)` static-method call sites in scan() — Sprint 9 Bit 9.1 will resolve this when OrderExecutor extracts to bot/executor.py; (2) the `Optional["OrderFlowEngine"]` and `Optional["KalshiOrderFlowTracker"]` quoted forward-refs in `__init__` signature for the 2 sister-class type annotations (cycle avoidance — both classes still in this file at lines 656 + 778). Sister Bit 8.1 ALSO relocated the `_TELEGRAM` module-level singleton from this file to `bot/notifier.py` (path-A++ relocation), reached via `_telegram_state._TELEGRAM` module-attribute access (parallel to Bit 6.3 path-B `_cal_state._CALIBRATION_ENGINE` pattern; preserves mutation freshness across consumers). Sprint 8 closes here. Sister Bit 8.2 (`bot/scanner/CLAUDE.md`) ships separately per master plan.
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
# with `grep -c '_telegram_state._TELEGRAM' bot/_impl.py`; mirrors the
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







def _append_raw_api_journal(entry: Dict) -> None:
    """Append one JSON line to the raw-API journal. Never raises."""
    try:
        entry["ts"] = datetime.datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        with open(RAW_API_JOURNAL_PATH, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logging.warning("raw_api_journal write failed: %s", e)







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

class OrderExecutor:
    """Maker-first executor with adaptive taker escalation.

    Always enters via a maker limit order (1-2¢ below fair value).
    tick() polls for fills and, if unfilled, escalates to a taker order
    after an urgency-based wait window:
      - 60-300s to close → wait 15s
      - 30-60s  to close → wait 10s
      - <30s    to close → wait 5s

    On escalation: cancel maker, re-fetch orderbook, validate price
    is in [MIN_ENTRY_PRICE, ESCALATION_MAX_ENTRY], and submit taker.

    UUID client_order_id, persist to SQLite before submission,
    log fills to trade_journal.jsonl, record position on fill.
    """

    def __init__(self, client: KalshiClient, state: StateManager,
                 logger: Logger, main_loop=None, kalshi_feed=None):
        self._client = client
        self._state = state
        self._logger = logger
        self._ml = main_loop
        self._kalshi_feed = kalshi_feed
        self._active_orders: Dict[str, Dict] = {}  # asset → order dict
        # Maker-tail tracking: order_id → record. Each record:
        #   {asset, ticker, strategy, count, price_cents,
        #    posted_monotonic, expires_monotonic}
        # Populated by _maybe_post_maker_tail, swept by
        # _sweep_maker_tails (called from tick()).
        self._maker_tails: Dict[str, Dict] = {}
        self._session_maker_tails_posted: int = 0
        self._session_maker_tails_skipped_cap: int = 0
        self._session_maker_tails_cancelled_ttl: int = 0
        # Ladder-escalation session counter. Mirrors maker-tail.
        # Increments on attempt (not on fill) — pair with success-rate
        # by comparing to settlement-level ladder fill counts.
        self._session_ladder_escalations: int = 0
        self._recent_taker_tickers: Dict[str, float] = {}  # ticker → timestamp (cooldown after IOC)
        # Session counters for execution engine stats
        self._session_amend_attempts: int = 0
        self._session_amend_successes: int = 0
        self._session_ioc_fills: int = 0
        self._session_ioc_unfilled: int = 0
        self._session_ws_fills: int = 0
        self._session_rest_fills: int = 0
        self._session_post_only_rejections: int = 0
        # Post-only rejection → taker escalation tracking
        self._post_only_rejections: Dict[str, Tuple[int, float]] = {}  # ticker → (count, first_rejection_ts)
        # Per-ticker API error cap: stop hammering after 3 consecutive api_errors
        self._ticker_api_errors: Dict[str, int] = {}  # ticker → consecutive error count
        self.TICKER_API_ERROR_CAP = 3
        # Rolling buffer of recent REST best-ask depth observations
        # per ticker, used to smooth the IOC drift-check clamp. Each
        # entry is (ts, depth); samples older than
        # IOC_DRIFT_CHECK_REST_WINDOW_S are pruned at observation time.
        # The clamp authority is `max(depths in window)` rather than
        # a single REST sample — see _rest_best_ask_depth_smoothed.
        self._rest_depth_observations: Dict[str, deque] = {}
        self._session_post_only_degraded_attempts: int = 0
        self._session_post_only_taker_escalations: int = 0
        self._session_post_only_taker_fills: int = 0
        # Direct taker counters (for <60s candidates)
        self._session_direct_taker_attempts: int = 0
        self._session_direct_taker_fills: int = 0
        self._session_direct_taker_unfilled: int = 0
        self._session_direct_taker_skipped: int = 0
        # Confirmation addon state
        self._addon_eligible: Dict[str, Dict] = {}   # ticker → metadata
        self._addon_completed: set = set()            # tickers already addon'd
        self._session_addon_attempts: int = 0
        self._session_addon_fills: int = 0
        self._session_addon_unfilled: int = 0
        self._session_addon_skipped: int = 0
        # Dip addon state
        self._dip_addon_completed: set = set()         # tickers already dip-addon'd
        self._session_dip_addon_attempts: int = 0
        self._session_dip_addon_fills: int = 0
        self._session_dip_addon_shadow: int = 0
        self._session_dip_addon_skipped: int = 0
        self._escalating_assets: set = set()  # Fix 5: guard against re-entry during escalation
        # Order suppression tracking — every gate logs when it blocks
        self._session_suppressed_asset_lock: int = 0
        self._session_suppressed_ticker_cooldown: int = 0
        self._session_suppressed_no_asks: int = 0
        self._session_nbbo_fallback_attempts: int = 0
        self._session_nbbo_fallback_blocked: int = 0
        self._session_suppressed_edge_recalc: int = 0
        self._session_suppressed_zero_size: int = 0
        # SOL empty-book maker fallback counters
        self._session_sol_empty_maker_attempt: int = 0
        self._session_sol_empty_maker_skip_price: int = 0
        self._session_sol_empty_maker_skip_stc: int = 0
        self._session_ioc_retries: int = 0
        self._session_ioc_retry_fills: int = 0
        self._kalshi_oft = None  # populated from scanner if available
        # SOL Path C shadow: pending observations {ticker → dict}
        self._sol_pathc_pending: Dict[str, Dict] = {}
        # DC IOC retry queue: non-blocking retries between scan cycles
        # Each entry: {candidate, original_count, total_filled, remaining, attempt, next_retry_ts, strategy}
        self._dc_retry_queue: List[Dict] = []
        self._session_dc_retries: int = 0
        self._session_dc_retry_fills: int = 0
        # Cancel-404 session counter. Explicit init removes the
        # attribute-missing race the prior `getattr(...)` lazy pattern
        # in `_handle_cancel_404` carried (`+= 1` is still non-atomic
        # under any future multi-thread refactor — explicit init
        # narrows the surface, doesn't make the counter thread-safe).
        # See kb/decisions/cancel-404-fix-v2-design-may04.md
        # "Counter initialization".
        self._cancel_404_count: int = 0

    @property
    def _active_order(self) -> Optional[Dict]:
        """Backwards compat for dashboard_snapshot.py."""
        if not self._active_orders:
            return None
        return next(iter(self._active_orders.values()))

    @property
    def has_active_order(self) -> bool:
        return len(self._active_orders) > 0

    # ── Post-only rejection tracking ────────────────────────────────────

    def _get_post_only_rejection_count(self, ticker: str) -> int:
        """Get active rejection count for ticker. Returns 0 if expired or missing."""
        entry = self._post_only_rejections.get(ticker)
        if entry is None:
            return 0
        count, first_ts = entry
        if time.time() - first_ts > POST_ONLY_REJECTION_EXPIRY:
            self._post_only_rejections.pop(ticker, None)
            return 0
        return count

    def _record_post_only_rejection(self, ticker: str):
        """Increment rejection count for ticker. Starts fresh if expired."""
        now = time.time()
        entry = self._post_only_rejections.get(ticker)
        if entry is None or (now - entry[1] > POST_ONLY_REJECTION_EXPIRY):
            self._post_only_rejections[ticker] = (1, now)
        else:
            self._post_only_rejections[ticker] = (entry[0] + 1, entry[1])

    # ── Pre-submit settlement-race gate ─────────────────────────────────

    def _should_skip_near_close(self, candidate: Dict) -> bool:
        """Return True when candidate STC is too close to settlement
        for a submission to land cleanly. None / non-numeric STC →
        return False (no info, allow submit — this path is shared with
        weather/sports where seconds_to_close may be unset)."""
        stc = candidate.get("seconds_to_close")
        try:
            stc_f = float(stc)
        except (TypeError, ValueError):
            return False
        return stc_f < MIN_ORDER_SUBMIT_STC_S

    def _abort_near_close(self, candidate: Dict, path: str) -> None:
        """Record the skip in evaluated_opportunities + log so the
        forensic trail makes the abort discoverable (a missing
        place_order would otherwise look like 'we never tried')."""
        ticker = candidate.get("ticker", "?")
        stc = candidate.get("seconds_to_close")
        logging.warning(
            "ORDER_ABORT_NEAR_CLOSE: %s path=%s stc=%s "
            "threshold=%.1fs (skip to avoid 409/404 race)",
            ticker, path, stc, MIN_ORDER_SUBMIT_STC_S)
        try:
            self._state.update_evaluated_opportunity_order(
                ticker, order_outcome="skipped_near_close")
        except Exception:
            logging.warning(
                "update_evaluated_opportunity_order(skipped_near_close) "
                "failed for %s", ticker, exc_info=True)

    # ── Hourly taker-only execution ─────────────────────────────────────

    def _execute_hourly_taker(self, candidate: Dict) -> Optional[Dict]:
        """Hourly-only IOC execution. No per-asset lock, no maker, no escalation.

        Completely isolated from 15M execution path:
        - Does NOT write to _active_orders (no maker resting)
        - Does NOT write to _escalating_assets (no escalation)
        - Calls _submit_taker() directly → IOC resolves in <1s
        """
        ticker = candidate["ticker"]
        asset = candidate["asset"]
        best_ask = candidate["best_yes_ask"]
        count = min(candidate["position_size"], HOURLY_FIXED_CONTRACTS)  # Hard cap

        # Ticker cooldown (shared with all products — IOC-specific, safe)
        cooldown_ts = self._recent_taker_tickers.get(ticker)
        if cooldown_ts is not None:
            _cd_remaining = IOC_TICKER_COOLDOWN - (time.time() - cooldown_ts)
            if _cd_remaining > 0:
                logging.info("HOURLY_TAKER: %s cooldown %.0fs remaining", ticker, _cd_remaining)
                return None

        candidate["entry_path"] = "hourly_taker"

        # Apply ask+1c offset for fill certainty (same pattern as SOL taker-first).
        # At sub-60c, 1c worse entry is trivial vs the 20c+ per-trade edge.
        # Verify edge is still positive after the offset before submitting.
        _h_mcfg = get_market_config("hourly")
        ioc_price = min(best_ask + IOC_RETRY_OFFSET, HOURLY_MAX_ENTRY_PRICE)
        if ioc_price != best_ask:
            cal_prob = candidate.get("calibrated_prob", 0)
            _offset_fee = calculate_fee(HOURLY_FIXED_CONTRACTS, ioc_price, is_taker=True,
                                        fee_mult_taker=_h_mcfg.fee_multiplier_taker,
                                        fee_mult_maker=_h_mcfg.fee_multiplier_maker)
            _offset_edge = cal_prob - (ioc_price / 100.0) - (_offset_fee / (HOURLY_FIXED_CONTRACTS * 100.0))
            if _offset_edge >= HOURLY_MIN_EDGE_PCT / 100.0:
                candidate["best_yes_ask"] = ioc_price
                best_ask = ioc_price
            else:
                logging.info("HOURLY_TAKER: %s offset %d→%dc kills edge (%.4f < %.4f), using ask",
                             ticker, best_ask, ioc_price, _offset_edge, HOURLY_MIN_EDGE_PCT / 100.0)

        logging.info("HOURLY_TAKER: %s %dx@%dc edge=%.2f%% prob=%.1f%% stc=%.0fs",
                     ticker, count, best_ask,
                     candidate.get("fee_adjusted_edge", 0) * 100,
                     candidate.get("calibrated_prob", 0) * 100,
                     candidate.get("seconds_to_close", 0))

        if _telegram_state._TELEGRAM:
            _telegram_state._TELEGRAM.send(
                f"HOURLY: {asset} {count}x@{best_ask}c "
                f"edge={candidate.get('fee_adjusted_edge', 0):.2%} "
                f"stc={candidate.get('seconds_to_close', 0):.0f}s")

        result = self._submit_taker(candidate)
        if result is None:
            self._recent_taker_tickers[ticker] = time.time()
        return result

    def _execute_weather_no_taker(self, candidate: Dict) -> Optional[Dict]:
        """Weather NO-side IOC execution. Direct taker, no maker, no escalation.

        Weather NO books are structurally empty — resting NO asks at 30-40c don't
        exist. Maker-first always cancels. Small fixed sizing (WEATHER_NO_CONTRACT_COUNT)
        at ~39-40c keeps taker fee negligible vs the 30%+ assumed-prob edge.
        """
        ticker = candidate["ticker"]
        asset = candidate["asset"]
        best_ask = candidate["best_yes_ask"]  # NO price for NO-side
        count = candidate["position_size"]     # WEATHER_NO_CONTRACT_COUNT (gated, ex-LAS)

        # Ticker cooldown (shared with all products)
        cooldown_ts = self._recent_taker_tickers.get(ticker)
        if cooldown_ts is not None:
            _cd_remaining = IOC_TICKER_COOLDOWN - (time.time() - cooldown_ts)
            if _cd_remaining > 0:
                logging.info("WEATHER_NO_TAKER: %s cooldown %.0fs remaining", ticker, _cd_remaining)
                return None

        candidate["entry_path"] = "weather_no_taker"

        logging.info(
            "WEATHER_NO_TAKER: %s %dx@%dc edge=%.2f%% prob=%.0f%% stc=%.0fs",
            ticker, count, best_ask,
            candidate.get("fee_adjusted_edge", 0) * 100,
            candidate.get("calibrated_prob", 0) * 100,
            candidate.get("seconds_to_close", 0))

        if _telegram_state._TELEGRAM:
            _telegram_state._TELEGRAM.send(
                f"\u2601\ufe0f WX NO: {asset} {count}x@{best_ask}c "
                f"edge={candidate.get('fee_adjusted_edge', 0):.2%} "
                f"stc={candidate.get('seconds_to_close', 0) / 3600:.0f}h")

        result = self._submit_taker(candidate)
        if result is None:
            self._recent_taker_tickers[ticker] = time.time()
        return result

    def _execute_hourly_no_taker(self, candidate: Dict) -> Optional[Dict]:
        """Hourly NO-side IOC execution. 1-contract verification mode.

        Mirrors weather NO taker — direct IOC, no maker, no escalation.
        Completely isolated from 15M and hourly YES execution paths.
        """
        ticker = candidate["ticker"]
        asset = candidate["asset"]
        best_ask = candidate["best_yes_ask"]  # NO price for NO-side
        count = min(candidate["position_size"], HOURLY_NO_FIXED_CONTRACTS)

        # Ticker cooldown
        cooldown_ts = self._recent_taker_tickers.get(ticker)
        if cooldown_ts is not None:
            _cd_remaining = IOC_TICKER_COOLDOWN - (time.time() - cooldown_ts)
            if _cd_remaining > 0:
                logging.info("HOURLY_NO_TAKER: %s cooldown %.0fs remaining", ticker, _cd_remaining)
                return None

        candidate["entry_path"] = "hourly_no_taker"

        logging.info(
            "HOURLY_NO_TAKER: %s %s %dx@%dc edge=%.2f%% no_prob=%.1f%% stc=%.0fs",
            ticker, asset, count, best_ask,
            candidate.get("fee_adjusted_edge", 0) * 100,
            candidate.get("calibrated_prob", 0) * 100,
            candidate.get("seconds_to_close", 0))

        if _telegram_state._TELEGRAM:
            _telegram_state._TELEGRAM.send(
                f"HOURLY NO: {asset} {count}x@{best_ask}c "
                f"edge={candidate.get('fee_adjusted_edge', 0):.2%} "
                f"stc={candidate.get('seconds_to_close', 0):.0f}s")

        result = self._submit_taker(candidate)
        if result is None:
            self._recent_taker_tickers[ticker] = time.time()
        return result

    # ── Public interface ──────────────────────────────────────────────────

    @staticmethod
    def _existing_window_cost_for_timeslot(positions: List[Dict],
                                           timeslot: str) -> int:
        """Sum total_cost_cents across positions whose event_ticker shares
        `timeslot`. Defends against a transiently-None event_ticker (the
        race documented at `_window_timeslot`'s `WINDOW_TIMESLOT_NULL`
        warning and in `kb/failures/transient-none-event-ticker-may08.md`).

        Tick error 2026-05-08 14:44:36 fired here when an inline genexpr
        chained `.split(...)` directly off `p.get("event_ticker", "")` —
        `dict.get` returns the value (None) when the key is present, so
        the default-empty-string never coerced None.

        total_cost_cents is also coerced defensively: schema is INTEGER
        NOT NULL, but the same race that surfaced None event_ticker can
        plausibly surface other transiently-None columns. Skip the row
        rather than TypeError on `total += None`.
        """
        total = 0
        for p in positions:
            et = p.get("event_ticker")
            if not et:
                logging.warning(
                    "WINDOW_CAP_NULL_EVENT_TICKER: ticker=%s asset=%s "
                    "side=%s status=%s cost=%s",
                    p.get("ticker"), p.get("asset"), p.get("side"),
                    p.get("status"), p.get("total_cost_cents"))
                continue
            if et.split("-", 1)[-1] == timeslot:
                cost = p.get("total_cost_cents")
                if cost is None:
                    logging.warning(
                        "WINDOW_CAP_NULL_TOTAL_COST: ticker=%s event_ticker=%s",
                        p.get("ticker"), et)
                    continue
                total += cost
        return total

    def execute(self, candidate: Dict) -> Optional[Dict]:
        """Always submit maker order. Escalation to taker happens in tick()."""
        # Observation safety belt — should never reach here for obs-only types
        # Exceptions:
        #   - weather NO-side bypasses observation_only when WEATHER_NO_SIDE_LIVE=True
        #   - hourly NO-side bypasses observation_only when HOURLY_NO_SIDE_LIVE=True
        #     (NO-side verification runs independently of the YES-side kill switch)
        _exec_cfg = get_market_config(candidate.get("product_type"))
        if _exec_cfg.observation_only:
            _is_weather_no_live = (candidate.get("product_type") == "weather"
                                  and candidate.get("side") == "no"
                                  and WEATHER_NO_SIDE_LIVE)
            _is_hourly_no_live = (candidate.get("product_type") == "hourly"
                                 and candidate.get("side") == "no"
                                 and HOURLY_NO_SIDE_LIVE)
            if not (_is_weather_no_live or _is_hourly_no_live):
                logging.error("SAFETY: %s candidate reached execute() — should never happen. Ticker=%s",
                              _exec_cfg.product_type, candidate.get("ticker"))
                return None

        asset = candidate["asset"]
        ticker = candidate["ticker"]

        # ── Unified exposure caps (always active) ─────────────────────
        _fresh_balance = None
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                _fresh_balance = self._ml.scanner._get_balance_cached()
            if not isinstance(_fresh_balance, (int, float)) or _fresh_balance <= 0:
                _fresh_balance = candidate.get("balance_at_scan")
        except Exception:
            _fresh_balance = candidate.get("balance_at_scan")

        if isinstance(_fresh_balance, (int, float)) and _fresh_balance > 0:
            # Per-ticker cap: 20% of balance
            _existing_ticker_cost = sum(
                p["total_cost_cents"]
                for p in self._state.get_open_positions()
                if p["ticker"] == ticker
            )
            _candidate_price = candidate.get("best_yes_ask",
                               candidate.get("best_ask", 96))
            _candidate_cost = candidate["position_size"] * _candidate_price
            _ticker_cap = _fresh_balance * MAX_TICKER_RISK

            if _existing_ticker_cost + _candidate_cost > _ticker_cap:
                _remaining = _ticker_cap - _existing_ticker_cost
                _reduced = max(0, int(_remaining / _candidate_price)) if _candidate_price > 0 else 0
                if _reduced <= 0:
                    logging.info(
                        "TICKER_CAP_SKIPPED: %s %s existing=%dc candidate=%dc cap=%dc",
                        ticker, candidate.get("strategy"),
                        _existing_ticker_cost, _candidate_cost, _ticker_cap)
                    return None
                else:
                    logging.info(
                        "TICKER_CAP_REDUCED: %s %s %d->%dct existing=%dc cap=%dc",
                        ticker, candidate.get("strategy"),
                        candidate["position_size"], _reduced,
                        _existing_ticker_cost, _ticker_cap)
                    candidate["position_size"] = _reduced
                    _candidate_cost = _reduced * _candidate_price

            # Per-window cap: cross-asset — sum ALL positions in the same 15-min timeslot
            _event_ticker = candidate.get("event_ticker")
            if _event_ticker:
                # Extract timeslot for cross-asset matching
                # Event tickers: KXBTC15M-26APR021000, KXETH15M-26APR021000 → timeslot=26APR021000
                _et_parts = _event_ticker.split("-", 1)
                _timeslot = _et_parts[1] if len(_et_parts) > 1 else _event_ticker
                _existing_window_cost = (
                    self._existing_window_cost_for_timeslot(
                        self._state.get_open_positions(), _timeslot))
                if _existing_window_cost > 0:
                    logging.debug("WINDOW_XASSET: timeslot=%s existing=$%.2f",
                                  _timeslot, _existing_window_cost / 100)
                _window_cap = _fresh_balance * MAX_WINDOW_RISK
                if _existing_window_cost + _candidate_cost > _window_cap:
                    _w_remaining = _window_cap - _existing_window_cost
                    _w_reduced = max(0, int(_w_remaining / _candidate_price)) if _candidate_price > 0 else 0
                    if _w_reduced <= 0:
                        logging.info(
                            "WINDOW_CAP_SKIPPED: %s %s window=%dc candidate=%dc cap=%dc",
                            _event_ticker, candidate.get("strategy"),
                            _existing_window_cost, _candidate_cost, _window_cap)
                        return None
                    else:
                        logging.info(
                            "WINDOW_CAP_REDUCED: %s %s %d->%dct window=%dc cap=%dc",
                            _event_ticker, candidate.get("strategy"),
                            candidate["position_size"], _w_reduced,
                            _existing_window_cost, _window_cap)
                        candidate["position_size"] = _w_reduced

        # ── HOURLY TAKER-ONLY PATH ──
        # Hourly uses IOC exclusively. No per-asset lock, no maker orders, no escalation.
        # This guarantees zero contention with 15M execution. Gated on product_type == "hourly".
        # Exception: hourly DC uses the DC taker path (strategy="hourly_dc"), not the hourly taker.
        if (candidate.get("product_type") == "hourly"
                and HOURLY_TAKER_ONLY
                and candidate.get("strategy") != "hourly_dc"
                and candidate.get("side") != "no"):
            return self._execute_hourly_taker(candidate)

        # ── HOURLY NO TAKER-ONLY PATH ──
        # Hourly NO-side verification: 1-contract IOC at NO ask price.
        # Same isolation as hourly YES taker — no per-asset lock, no maker, no escalation.
        if (candidate.get("product_type") == "hourly"
                and candidate.get("side") == "no"):
            return self._execute_hourly_no_taker(candidate)

        # ── WEATHER NO TAKER-ONLY PATH ──
        # Weather NO orderbooks are structurally empty — nobody posts resting NO
        # asks at 30-40c. Maker-first always cancels unfilled after escalation
        # timeout (3/3 canceled, 0% fill rate, Apr 12 2026). Direct IOC at ask.
        # 1 contract × 35c = $0.35 cost; ~30% assumed edge makes taker fee trivial.
        if (candidate.get("product_type") == "weather"
                and candidate.get("side") == "no"):
            return self._execute_weather_no_taker(candidate)

        # Gate 1: Per-asset lock for maker-first assets only.
        # Taker-first (SOL): IOC resolves synchronously (<1s), no concurrent order risk.
        # Maker-first (BTC/ETH/XRP): per-asset lock prevents two resting makers.
        if asset not in TAKER_FIRST_ASSETS:
            if asset in self._active_orders or asset in self._escalating_assets:
                logging.warning(
                    "ORDER_SUPPRESSED asset_lock: %s %s active_ticker=%s escalating=%s",
                    asset, ticker,
                    self._active_orders.get(asset, {}).get("ticker", "none"),
                    asset in self._escalating_assets)
                self._session_suppressed_asset_lock += 1
                return None

        # Gate 2: Ticker cooldown — skip tickers recently attempted via IOC
        cooldown_ts = self._recent_taker_tickers.get(ticker)
        if cooldown_ts is not None:
            _cd_remaining = IOC_TICKER_COOLDOWN - (time.time() - cooldown_ts)
            if _cd_remaining > 0:
                logging.warning(
                    "ORDER_SUPPRESSED ticker_cooldown: %s remaining=%.0fs",
                    ticker, _cd_remaining)
                self._session_suppressed_ticker_cooldown += 1
                return None
            del self._recent_taker_tickers[ticker]

        # Gate 3: Per-ticker API error cap — stop hammering after 3 consecutive failures.
        # Prevents hot retry loops on expired/closed markets (21 api_errors in 30s, Mar 23).
        _api_err_count = self._ticker_api_errors.get(ticker, 0)
        if _api_err_count >= self.TICKER_API_ERROR_CAP:
            logging.warning("ORDER_SUPPRESSED api_error_cap: %s errors=%d (capped at %d)",
                            ticker, _api_err_count, self.TICKER_API_ERROR_CAP)
            return None

        if OBSERVATION_MODE:
            logging.info(
                f"OBSERVATION MODE: Would place maker for {candidate['ticker']} "
                f"at {candidate.get('best_yes_ask', '?')}¢ for "
                f"{candidate.get('position_size', '?')} contracts"
            )
            try:
                _fv = candidate.get("best_yes_ask")
                _obs_offset = (MAKER_PRICE_OFFSET if _fv and _fv >= 90
                               else MAKER_PRICE_OFFSET + 1) if _fv else MAKER_PRICE_OFFSET
                self._logger.log_execution({
                    "action": "observation_would_trade",
                    "ticker": candidate["ticker"],
                    "asset": candidate["asset"],
                    "event_ticker": candidate["event_ticker"],
                    "best_yes_ask": candidate.get("best_yes_ask"),
                    "position_size": candidate.get("position_size"),
                    "edge": candidate.get("edge"),
                    "calibrated_prob": candidate.get("calibrated_prob"),
                    "strategy": candidate.get("strategy"),
                    "seconds_to_close": candidate.get("seconds_to_close"),
                    "vol_regime": candidate.get("vol_regime"),
                    "ofa_adjustment": candidate.get("ofa_adjustment"),
                    "balance_at_scan": candidate.get("balance_at_scan"),
                    "execution_params": {
                        "maker_price": (_fv - _obs_offset) if _fv else None,
                        "maker_offset": _obs_offset,
                        "post_only": True,
                        "escalation_strategy": "cancel_replace_ioc",
                        "taker_time_in_force": "ioc",
                    },
                })
                if _telegram_state._TELEGRAM:
                    _ba = candidate.get("best_yes_ask", "?")
                    _edge = candidate.get("edge")
                    _prob = candidate.get("calibrated_prob")
                    _sz = candidate.get("position_size", "?")
                    _asset = candidate.get("asset", "?")
                    _edge_s = f"{_edge:.1%}" if _edge is not None else "?"
                    _prob_s = f"{_prob:.0%}" if _prob is not None else "?"
                    _cost = (_ba * _sz / 100) if isinstance(_ba, (int, float)) and isinstance(_sz, (int, float)) else 0
                    _telegram_state._TELEGRAM.send(
                        f"\U0001f4ca {_asset} {_sz}ct @ {_ba}c "
                        f"(${_cost:.2f}) edge={_edge_s} prob={_prob_s}",
                        dedup_key=candidate["ticker"],
                    )
                if not hasattr(self, '_last_obs_ticker') or self._last_obs_ticker != candidate['ticker']:
                    self._last_obs_ticker = candidate['ticker']
                    _ba = candidate.get("best_yes_ask")
                    _cp = candidate.get("calibrated_prob")
                    _obs_fee_cfg = get_market_config(candidate.get("product_type"))
                    _fee1 = calculate_fee(1, _ba, is_taker=True, fee_mult_taker=_obs_fee_cfg.fee_multiplier_taker) if _ba else 0
                    _ev = (_cp * (100 - _ba)) - ((1 - _cp) * _ba) - _fee1 if (_ba and _cp) else None
                    self._state.insert_evaluated_opportunity(
                        candidate["ticker"], candidate["event_ticker"],
                        candidate["asset"], "observation_trade",
                        spot_price=candidate.get("spot"),
                        threshold=candidate.get("threshold"),
                        volatility=candidate.get("blended_rv"),
                        market_price=_ba,
                        seconds_to_close=candidate.get("seconds_to_close"),
                        calibrated_prob=_cp,
                        edge=candidate.get("edge"),
                        ofa_adjustment=candidate.get("ofa_adjustment"),
                        strategy=candidate.get("strategy"),
                        position_size=candidate.get("position_size"),
                        kelly_f=candidate.get("kelly_f"),
                        z_score=candidate.get("z_score"),
                        vol_regime=candidate.get("vol_regime"),
                        calibrated_prob_raw=candidate.get("calibrated_prob_raw"),
                        breakeven_wr=_ba / 100.0 if _ba else None,
                        expected_value=round(_ev, 2) if _ev is not None else None,
                        drawdown_scaler=candidate.get("drawdown_scaler"),
                        ask_depth=candidate.get("ob_snapshot", {}).get("ask_depth"),
                        best_ask_source=candidate.get("best_ask_source"),
                        ofa_confidence=candidate.get("ofa_confidence"),
                        raw_prob=candidate.get("raw_prob"),
                        calibration_method=candidate.get("calibration_method"),
                        old_system_prob=candidate.get("old_system_prob"),
                        fee_adjusted_edge=candidate.get("fee_adjusted_edge"),
                        egarch_sigma=candidate.get("egarch_sigma"),
                        egarch_blend_sigma=candidate.get("egarch_blend_sigma"),
                        egarch_blend_weight=candidate.get("egarch_blend_weight"),
                        mz_r_squared=candidate.get("mz_r_squared"),
                        shadow_tv_blend_rv=candidate.get("shadow_tv_blend_rv"),
                        mz_shadow_sigmoid_w=candidate.get("mz_shadow_sigmoid_w"),
                        mz_baseline_qlike=candidate.get("mz_baseline_qlike"),
                        mz_qlike=candidate.get("mz_qlike"),
                        counterfactual=candidate.get("counterfactual_json"),
                        shadow_cal_prob=candidate.get("shadow_cal_prob"),
                        shadow_cal_fee_edge=candidate.get("shadow_cal_fee_edge"),
                        shadow_cal_temperature=candidate.get("shadow_cal_temperature"),
                        oft_prob_adjustment=candidate.get("oft_prob_adjustment"),
                        oft_imbalance_ratio=candidate.get("oft_imbalance_ratio"),
                        oft_n_snapshots=candidate.get("oft_n_snapshots"),
                        product_type=candidate.get("product_type"),
                        wx_ensemble_mean=candidate.get("wx_ensemble_mean"),
                        wx_ensemble_std=candidate.get("wx_ensemble_std"),
                        wx_bias_correction=candidate.get("wx_bias_correction"),
                        wx_n_members=candidate.get("wx_n_members"),
                        wx_market_type=candidate.get("wx_market_type"),
                        wx_hrrr_temp=candidate.get("wx_hrrr_temp"),
                        wx_corrected_mean=candidate.get("wx_corrected_mean"),
                        hourly_pre_temp_prob=candidate.get("hourly_pre_temp_prob"),
                        hourly_applied_temp_t=candidate.get("hourly_applied_temp_t"),
                        hourly_shadow_temp_2_0=candidate.get("hourly_shadow_temp_2_0"),
                        hourly_shadow_temp_1_0=candidate.get("hourly_shadow_temp_1_0"),
                        hourly_shadow_temp_2_5=candidate.get("hourly_shadow_temp_2_5"),
                        hourly_shadow_blend_50=candidate.get("hourly_shadow_blend_50"),
                        hourly_shadow_temp_1_75=candidate.get("hourly_shadow_temp_1_75"),
                        hourly_shadow_temp_3_0=candidate.get("hourly_shadow_temp_3_0"),
                        hourly_shadow_blend_20=candidate.get("hourly_shadow_blend_20"),
                        hourly_shadow_blend_30=candidate.get("hourly_shadow_blend_30"),
                        hourly_shadow_blend_60=candidate.get("hourly_shadow_blend_60"),
                        hourly_post_temp_prob=candidate.get("hourly_post_temp_prob"),
                        available_balance_cents=candidate.get("balance_at_scan"),
                        # cal_mlp_* propagation: candidate dict carries these via
                        # **_shadow_diag splat at line ~15425. Without these kwargs
                        # the post-hoc processor's WHERE cal_mlp_request_id IS NOT NULL
                        # never matches the row → 0% annotation on real trades.
                        cal_mlp_request_id=candidate.get("cal_mlp_request_id"),
                        cal_mlp_skipped_reason=candidate.get("cal_mlp_skipped_reason"),
                        cal_mlp_p_mean=candidate.get("cal_mlp_p_mean"),
                        cal_mlp_p_std=candidate.get("cal_mlp_p_std"),
                        cal_mlp_final_lo=candidate.get("cal_mlp_final_lo"),
                        cal_mlp_final_hi=candidate.get("cal_mlp_final_hi"),
                        cal_mlp_train_id=candidate.get("cal_mlp_train_id"))
            except Exception as e:
                logging.error(f"OBSERVATION_DB_INSERT_FAILED: {candidate.get('ticker')}: {e}")
            return None

        # ── Log candidate to evaluated_opportunities (live mode) ──
        try:
            _ba = candidate.get("best_yes_ask")
            _cp = candidate.get("calibrated_prob")
            _cand_fee_cfg = get_market_config(candidate.get("product_type"))
            _fee1 = calculate_fee(1, _ba, is_taker=True, fee_mult_taker=_cand_fee_cfg.fee_multiplier_taker) if _ba else 0
            _ev = (_cp * (100 - _ba)) - ((1 - _cp) * _ba) - _fee1 if (_ba and _cp) else None
            self._state.insert_evaluated_opportunity(
                candidate["ticker"], candidate["event_ticker"],
                candidate["asset"], "candidate",
                spot_price=candidate.get("spot"),
                threshold=candidate.get("threshold"),
                volatility=candidate.get("blended_rv"),
                market_price=_ba,
                seconds_to_close=candidate.get("seconds_to_close"),
                calibrated_prob=_cp,
                edge=candidate.get("edge"),
                ofa_adjustment=candidate.get("ofa_adjustment"),
                strategy=candidate.get("strategy"),
                position_size=candidate.get("position_size"),
                kelly_f=candidate.get("kelly_f"),
                z_score=candidate.get("z_score"),
                vol_regime=candidate.get("vol_regime"),
                calibrated_prob_raw=candidate.get("calibrated_prob_raw"),
                breakeven_wr=_ba / 100.0 if _ba else None,
                expected_value=round(_ev, 2) if _ev is not None else None,
                drawdown_scaler=candidate.get("drawdown_scaler"),
                ask_depth=candidate.get("ob_snapshot", {}).get("ask_depth"),
                best_ask_source=candidate.get("best_ask_source"),
                ofa_confidence=candidate.get("ofa_confidence"),
                raw_prob=candidate.get("raw_prob"),
                calibration_method=candidate.get("calibration_method"),
                old_system_prob=candidate.get("old_system_prob"),
                fee_adjusted_edge=candidate.get("fee_adjusted_edge"),
                egarch_sigma=candidate.get("egarch_sigma"),
                egarch_blend_sigma=candidate.get("egarch_blend_sigma"),
                egarch_blend_weight=candidate.get("egarch_blend_weight"),
                mz_r_squared=candidate.get("mz_r_squared"),
                shadow_tv_blend_rv=candidate.get("shadow_tv_blend_rv"),
                mz_shadow_sigmoid_w=candidate.get("mz_shadow_sigmoid_w"),
                mz_baseline_qlike=candidate.get("mz_baseline_qlike"),
                mz_qlike=candidate.get("mz_qlike"),
                counterfactual=candidate.get("counterfactual_json"),
                shadow_cal_prob=candidate.get("shadow_cal_prob"),
                shadow_cal_fee_edge=candidate.get("shadow_cal_fee_edge"),
                shadow_cal_temperature=candidate.get("shadow_cal_temperature"),
                oft_prob_adjustment=candidate.get("oft_prob_adjustment"),
                oft_imbalance_ratio=candidate.get("oft_imbalance_ratio"),
                oft_n_snapshots=candidate.get("oft_n_snapshots"),
                product_type=candidate.get("product_type"),
                wx_ensemble_mean=candidate.get("wx_ensemble_mean"),
                wx_ensemble_std=candidate.get("wx_ensemble_std"),
                wx_bias_correction=candidate.get("wx_bias_correction"),
                wx_n_members=candidate.get("wx_n_members"),
                wx_market_type=candidate.get("wx_market_type"),
                wx_no_side_edge=candidate.get("wx_no_side_edge"),
                wx_hrrr_temp=candidate.get("wx_hrrr_temp"),
                wx_corrected_mean=candidate.get("wx_corrected_mean"),
                hourly_pre_temp_prob=candidate.get("hourly_pre_temp_prob"),
                hourly_applied_temp_t=candidate.get("hourly_applied_temp_t"),
                hourly_shadow_temp_2_0=candidate.get("hourly_shadow_temp_2_0"),
                hourly_shadow_temp_1_0=candidate.get("hourly_shadow_temp_1_0"),
                hourly_shadow_temp_2_5=candidate.get("hourly_shadow_temp_2_5"),
                hourly_shadow_blend_50=candidate.get("hourly_shadow_blend_50"),
                hourly_shadow_temp_1_75=candidate.get("hourly_shadow_temp_1_75"),
                hourly_shadow_temp_3_0=candidate.get("hourly_shadow_temp_3_0"),
                hourly_shadow_blend_20=candidate.get("hourly_shadow_blend_20"),
                hourly_shadow_blend_30=candidate.get("hourly_shadow_blend_30"),
                hourly_shadow_blend_60=candidate.get("hourly_shadow_blend_60"),
                hourly_post_temp_prob=candidate.get("hourly_post_temp_prob"),
                available_balance_cents=candidate.get("balance_at_scan"),
                cal_mlp_request_id=candidate.get("cal_mlp_request_id"),
                cal_mlp_skipped_reason=candidate.get("cal_mlp_skipped_reason"),
                cal_mlp_p_mean=candidate.get("cal_mlp_p_mean"),
                cal_mlp_p_std=candidate.get("cal_mlp_p_std"),
                cal_mlp_final_lo=candidate.get("cal_mlp_final_lo"),
                cal_mlp_final_hi=candidate.get("cal_mlp_final_hi"),
                cal_mlp_train_id=candidate.get("cal_mlp_train_id"))
        except Exception as e:
            logging.error(f"CANDIDATE_DB_INSERT_FAILED: {candidate.get('ticker')}: {e}")

        seconds_to_close = candidate.get("seconds_to_close")

        # ── Decided contract taker override ─────────────────────────
        # Must be checked BEFORE SOL taker-first and direct taker paths,
        # which apply MIN_EDGE_PCT (0.25%). DC uses -0.01 threshold.
        # Bug fix: SOL DC candidates were hitting SOL taker-first path
        # first, getting edge-gated at 0.25% when DC allows -1%.
        #
        # Non-blocking retry: Kalshi IOCs partial fill (fill whatever's on
        # the book, cancel the rest). On unfilled/partial, queue a retry
        # entry — processed at the TOP of next _tick() cycle (~5s later).
        # Data: 24% of DC tickers recover within 8-32s.
        _dc_strategy = candidate.get("strategy")
        if _dc_strategy in ("decided_t1", "decided_t1b", "decided_t2",
                            "decided_t2_z2", "decided_t2_z25", "hourly_dc"):
            return self._execute_dc_taker(candidate, asset, seconds_to_close)

        # ── Terminal momentum taker override ──────────────────────────
        if _dc_strategy and _dc_strategy.startswith("terminal_momentum"):
            return self._execute_tm_taker(candidate, asset, seconds_to_close)

        # ── LPNE taker override ──────────────────────────────────────
        if _dc_strategy == "low_price_near_expiry":
            return self._execute_lpne_taker(candidate, asset, seconds_to_close)

        # ── Bracket NO taker override ────────────────────────────────
        if _dc_strategy == "bracket_no":
            return self._execute_bracket_no_taker(candidate, asset, seconds_to_close)

        # ── SOL taker-first override ──────────────────────────────
        # SOL: bypass maker entirely, go direct IOC — UNLESS book is empty.
        # Data: 44.7% maker fill rate, $101/wk missed, 95% unfilled WR.
        # Empty-book fallback: post maker bid to attract counterparties (like BTC/ETH).
        # Data: 400 unfilled SOL depth=0 candidates at 87c+ have 95% hypothetical WR.
        _sol_empty_book_fallback = False
        if SOL_TAKER_FIRST and candidate.get("asset") == "SOL":
            _sol_depth = candidate.get("ob_snapshot", {}).get("ask_depth", 0)
            _sol_price = candidate.get("best_yes_ask", 0)
            _sol_stc = seconds_to_close or 0

            # Empty book + price >= 87c: fall through to maker path
            if _sol_depth == 0 and _sol_price >= SOL_EMPTY_BOOK_MAKER_MIN_PRICE:
                if _sol_stc < SOL_EMPTY_BOOK_MIN_STC:
                    logging.info(
                        "sol_empty_book_SKIP_STC: %s price=%dc depth=0 stc=%.0fs < %.0fs",
                        ticker, _sol_price, _sol_stc, SOL_EMPTY_BOOK_MIN_STC)
                    self._session_sol_empty_maker_skip_stc += 1
                    return None
                # Fall through to maker path (PATH 5)
                logging.info(
                    "sol_empty_book_MAKER_FALLBACK: %s price=%dc depth=0 stc=%.0fs",
                    ticker, _sol_price, _sol_stc)
                self._session_sol_empty_maker_attempt += 1
                _sol_empty_book_fallback = True
                # Need per-asset lock check (SOL normally skips it as taker-first)
                if asset in self._active_orders or asset in self._escalating_assets:
                    logging.warning(
                        "ORDER_SUPPRESSED asset_lock: %s %s (sol_empty_book_maker) active=%s",
                        asset, ticker, self._active_orders.get(asset, {}).get("ticker", "none"))
                    self._session_suppressed_asset_lock += 1
                    return None
                # Fall through — will hit PATH 5 (maker) below
            elif _sol_depth == 0:
                # Empty book but price < 87c: skip entirely
                logging.info(
                    "sol_empty_book_SKIP_PRICE: %s price=%dc depth=0 (< %dc floor)",
                    ticker, _sol_price, SOL_EMPTY_BOOK_MAKER_MIN_PRICE)
                self._session_sol_empty_maker_skip_price += 1
                return None

        if SOL_TAKER_FIRST and candidate.get("asset") == "SOL" and not _sol_empty_book_fallback:
            count = candidate["position_size"]
            price = candidate["best_yes_ask"]
            cal_prob = candidate["calibrated_prob"]

            if count <= 0:
                logging.warning("ORDER_SUPPRESSED zero_size: %s asset=SOL price=%d",
                                candidate["ticker"], price)
                self._session_suppressed_zero_size += 1
                return None

            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                logging.warning(
                    "ORDER_SUPPRESSED edge_taker_fee: %s asset=SOL net_edge=%.4f < min=%.4f "
                    "price=%d taker_fee=%d¢ stc=%.0f",
                    candidate["ticker"], net_edge, MIN_EDGE_PCT / 100.0, price, taker_fee,
                    seconds_to_close or 0)
                self._session_suppressed_edge_recalc += 1
                return None

            fresh_ask = self._get_addon_best_ask(candidate["ticker"])
            if fresh_ask is None:
                fresh_ask = self._nbbo_fallback_price(candidate)
                if fresh_ask is None:
                    logging.warning("ORDER_SUPPRESSED no_asks: %s asset=SOL price=%d stc=%.0f",
                                    candidate["ticker"], price, seconds_to_close or 0)
                    self._session_suppressed_no_asks += 1
                    return None

            if fresh_ask != price:
                logging.info("sol_taker_override_price_update: %s scanner=%d¢ fresh=%d¢",
                             candidate["ticker"], price, fresh_ask)
                price = fresh_ask
                candidate["best_yes_ask"] = fresh_ask
                taker_fee = calculate_taker_fee(count, price)
                net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    logging.warning(
                        "ORDER_SUPPRESSED edge_recalc: %s asset=SOL fresh_ask=%d net_edge=%.4f < min=%.4f",
                        candidate["ticker"], price, net_edge, MIN_EDGE_PCT / 100.0)
                    self._session_suppressed_edge_recalc += 1
                    return None

            # Apply ask+1c offset for fill certainty on taker-first
            ioc_price = min(price + IOC_RETRY_OFFSET, 99)
            if ioc_price != price:
                _offset_fee = calculate_taker_fee(count, ioc_price)
                _offset_edge = cal_prob - (ioc_price / 100.0) - (_offset_fee / (count * 100.0))
                if _offset_edge >= MIN_EDGE_PCT / 100.0:
                    price = ioc_price
                    candidate["best_yes_ask"] = ioc_price
                    net_edge = _offset_edge
                    taker_fee = _offset_fee

            logging.info(
                "sol_taker_override_ENTRY: %s %dx @ %d¢ "
                "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
                candidate["ticker"], count, price,
                seconds_to_close or 0, net_edge, cal_prob, taker_fee)

            candidate["entry_path"] = "sol_taker_override"
            candidate["escalation_type"] = "sol_taker_override"
            self._recent_taker_tickers[candidate["ticker"]] = time.time()
            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self._session_direct_taker_attempts += 1
            result = self._submit_taker(candidate)
            if result is not None:
                logging.info("sol_taker_override_FILLED: %s", candidate["ticker"])
                self._session_direct_taker_fills += 1
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    candidate["ticker"], order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled",
                    taker_ask_at_submit=candidate.get("best_yes_ask"))
            else:
                # IOC retry: refresh ask, try once more at fresh_ask + offset
                _retry_ask = self._get_addon_best_ask(candidate["ticker"])
                _retry_result = None
                if _retry_ask is not None:
                    _retry_price = min(_retry_ask + IOC_RETRY_OFFSET, 99)
                    # Don't chase more than 2c above original submission price
                    if _retry_price <= price + 2:
                        _retry_fee = calculate_taker_fee(count, _retry_price)
                        _retry_edge = cal_prob - (_retry_price / 100.0) - (_retry_fee / (count * 100.0))
                        if _retry_edge >= MIN_EDGE_PCT / 100.0:
                            logging.info("sol_taker_IOC_RETRY: %s retry_price=%d¢ retry_edge=%.4f",
                                         candidate["ticker"], _retry_price, _retry_edge)
                            candidate["best_yes_ask"] = _retry_price
                            candidate["escalation_type"] = "ioc_retry"
                            self._session_ioc_retries += 1
                            _retry_result = self._submit_taker(candidate)
                            if _retry_result is not None:
                                logging.info("sol_taker_IOC_RETRY_FILLED: %s", candidate["ticker"])
                                self._session_ioc_retry_fills += 1
                                result = _retry_result
                                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                                self._state.update_evaluated_opportunity_order(
                                    candidate["ticker"], order_id=_taker_oid,
                                    order_submitted_at=_order_submit_ts, order_outcome="filled",
                                    taker_ask_at_submit=candidate.get("best_yes_ask"))
                if _retry_result is None:
                    logging.warning("sol_taker_override_UNFILLED: %s", candidate["ticker"])
                    self._session_direct_taker_unfilled += 1
                    self._state.update_evaluated_opportunity_order(
                        candidate["ticker"], order_submitted_at=_order_submit_ts,
                        order_outcome="unfilled",
                        taker_ask_at_submit=candidate.get("best_yes_ask"))

            # ── SOL Path C shadow: log what maker path would have done ──
            try:
                _pathc_fv = price  # current best ask (possibly refreshed)
                _pathc_offset = MAKER_PRICE_OFFSET if _pathc_fv >= 90 else MAKER_PRICE_OFFSET + 1
                _pathc_maker_price = _pathc_fv - _pathc_offset

                # Get depth at the hypothetical maker price level
                _pathc_depth = 0
                try:
                    scanner = self._ml.scanner if self._ml else None
                    if scanner:
                        _pc_ob, _ = scanner._get_orderbook_cached(candidate["ticker"])
                        if _pc_ob:
                            _pathc_depth = OpportunityScanner._best_ask_depth(_pc_ob)
                except Exception:
                    pass

                _pathc_pos_size = candidate["position_size"]
                _eval_time = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

                self._state.insert_sol_pathc_shadow(
                    ticker=candidate["ticker"],
                    evaluation_time=_eval_time,
                    live_ask=price,
                    live_depth=_pathc_depth,
                    live_edge=net_edge,
                    live_stc=seconds_to_close or 0,
                    live_contracts=count,
                    live_entry_price=price,
                    live_cal_prob=cal_prob,
                    pathc_maker_price=_pathc_maker_price,
                    pathc_maker_offset=_pathc_offset,
                    pathc_depth_at_maker=_pathc_depth,
                    position_size=_pathc_pos_size,
                )

                # Schedule for deferred observation (check every tick during escalation window)
                _esc_wait = ESCALATION_WAIT_LONG  # SOL uses default 15s
                self._sol_pathc_pending[candidate["ticker"]] = {
                    "start_time": time.time(),
                    "escalation_wait": _esc_wait,
                    "maker_price": _pathc_maker_price,
                    "position_size": _pathc_pos_size,
                    "cal_prob": cal_prob,
                    "touched": False,
                }
                logging.info(
                    "sol_pathc_shadow_LOGGED: %s maker_price=%d¢ offset=%d depth=%d pos_size=%d",
                    candidate["ticker"], _pathc_maker_price, _pathc_offset, _pathc_depth, _pathc_pos_size)
            except Exception:
                logging.warning("sol_pathc_shadow logging failed", exc_info=True)

            return result

        # ── Direct taker for <180s candidates ───────────────────────
        # Maker-only below 90s: block direct taker, fall through to maker
        if (seconds_to_close is not None
                and seconds_to_close < MAKER_ONLY_THRESHOLD
                and seconds_to_close < DIRECT_TAKER_THRESHOLD):
            logging.info(
                "direct_taker_BLOCKED_maker_only: %s seconds_to_close=%.0f",
                candidate["ticker"], seconds_to_close)
        elif seconds_to_close is not None and seconds_to_close < DIRECT_TAKER_THRESHOLD:
            count = candidate["position_size"]
            price = candidate["best_yes_ask"]
            cal_prob = candidate["calibrated_prob"]

            if count <= 0:
                logging.warning("ORDER_SUPPRESSED zero_size: %s asset=%s path=direct_taker price=%d",
                                candidate["ticker"], asset, price)
                self._session_suppressed_zero_size += 1
                self._session_direct_taker_skipped += 1
                return None

            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                logging.warning(
                    "ORDER_SUPPRESSED edge_taker_fee: %s asset=%s path=direct_taker "
                    "net_edge=%.4f < min=%.4f price=%d taker_fee=%d¢ stc=%.0f",
                    candidate["ticker"], asset, net_edge, MIN_EDGE_PCT / 100.0,
                    price, taker_fee, seconds_to_close)
                self._session_suppressed_edge_recalc += 1
                self._session_direct_taker_skipped += 1
                return None

            # Verify actual liquidity before submitting IOC
            fresh_ask = self._get_addon_best_ask(candidate["ticker"])
            if fresh_ask is None:
                fresh_ask = self._nbbo_fallback_price(candidate)
                if fresh_ask is None:
                    logging.warning(
                        "ORDER_SUPPRESSED no_asks: %s asset=%s path=direct_taker stc=%.0f",
                        candidate["ticker"], asset, seconds_to_close)
                    self._session_suppressed_no_asks += 1
                    self._session_direct_taker_skipped += 1
                    return None

            # Use fresh ask if it differs from scanner's (may be stale NBBO)
            if fresh_ask != price:
                logging.info(
                    "direct_taker_price_update: %s scanner=%d¢ fresh=%d¢",
                    candidate["ticker"], price, fresh_ask)
                price = fresh_ask
                candidate["best_yes_ask"] = fresh_ask
                taker_fee = calculate_taker_fee(count, price)
                net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    logging.info(
                        "direct_taker_SKIPPED: %s fresh_ask=%d¢ net_edge=%.4f < min",
                        candidate["ticker"], price, net_edge)
                    self._session_direct_taker_skipped += 1
                    return None

            self._session_direct_taker_attempts += 1
            logging.info(
                "direct_taker_ENTRY: %s %dx @ %d¢ "
                "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
                candidate["ticker"], count, price,
                seconds_to_close, net_edge, cal_prob, taker_fee)

            candidate["entry_path"] = "direct_taker"
            candidate["escalation_type"] = "direct_taker"
            self._recent_taker_tickers[candidate["ticker"]] = time.time()
            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            result = self._submit_taker(candidate)
            if result is not None:
                self._session_direct_taker_fills += 1
                logging.info("direct_taker_FILLED: %s", candidate["ticker"])
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    candidate["ticker"], order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
            else:
                self._session_direct_taker_unfilled += 1
                logging.warning("direct_taker_UNFILLED: %s", candidate["ticker"])
                self._state.update_evaluated_opportunity_order(
                    candidate["ticker"], order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled")
            return result

        # ── Three-tier post_only rejection escalation ──────────────
        ticker = candidate["ticker"]
        rejections = self._get_post_only_rejection_count(ticker)

        # Tier 3: Taker escalation (2 same-price + 1 degraded all failed)
        # Maker-only below 90s: block post-only taker escalation
        if (rejections >= POST_ONLY_MAX_SAME_PRICE + 1  # 3+
                and not (seconds_to_close is not None and seconds_to_close < MAKER_ONLY_THRESHOLD)):
            count = candidate["position_size"]
            price = candidate["best_yes_ask"]
            cal_prob = candidate["calibrated_prob"]

            if count <= 0:
                logging.warning("post_only_taker_SKIPPED: %s position_size=%d", ticker, count)
                self._post_only_rejections.pop(ticker, None)
                return None

            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                logging.info(
                    "post_only_taker_SKIPPED: %s net_edge=%.4f < min=%.4f "
                    "taker_fee=%d¢ count=%d price=%d¢",
                    ticker, net_edge, MIN_EDGE_PCT / 100.0, taker_fee, count, price)
                self._post_only_rejections.pop(ticker, None)
                return None

            # Verify actual liquidity before submitting IOC
            fresh_ask = self._get_addon_best_ask(ticker)
            if fresh_ask is None:
                logging.info(
                    "post_only_taker_SKIPPED: %s no asks on orderbook", ticker)
                self._post_only_rejections.pop(ticker, None)
                return None

            if fresh_ask != price:
                logging.info(
                    "post_only_taker_price_update: %s scanner=%d¢ fresh=%d¢",
                    ticker, price, fresh_ask)
                price = fresh_ask
                candidate["best_yes_ask"] = fresh_ask
                taker_fee = calculate_taker_fee(count, price)
                net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    logging.info(
                        "post_only_taker_SKIPPED: %s fresh_ask=%d¢ net_edge=%.4f < min",
                        ticker, price, net_edge)
                    self._post_only_rejections.pop(ticker, None)
                    return None

            logging.info(
                "post_only_taker_ESCALATION: %s %dx @ %d¢ "
                "rejections=%d net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
                ticker, count, price, rejections, net_edge, cal_prob, taker_fee)
            self._session_post_only_taker_escalations += 1
            candidate["entry_path"] = "post_only_taker"
            candidate["escalation_type"] = "post_only_taker"
            self._recent_taker_tickers[ticker] = time.time()
            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            result = self._submit_taker(candidate)
            if result is not None:
                self._post_only_rejections.pop(ticker, None)
                self._session_post_only_taker_fills += 1
                logging.info("post_only_taker_FILLED: %s", ticker)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
            else:
                # Clear rejections to prevent hot retry loop on persistent API errors
                self._post_only_rejections.pop(ticker, None)
                logging.warning("post_only_taker_UNFILLED: %s (cleared rejections, will re-evaluate)", ticker)
                self._state.update_evaluated_opportunity_order(
                    ticker, order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled")
            return result

        # Tier 2: Degraded maker (1¢ worse, one attempt)
        if rejections == POST_ONLY_MAX_SAME_PRICE:  # 2
            logging.info(
                "post_only_degraded_maker: %s rejections=%d, trying %d¢ worse",
                ticker, rejections, POST_ONLY_DEGRADED_EXTRA_OFFSET)
            self._session_post_only_degraded_attempts += 1
            self._submit_maker(candidate, degraded=True)
            _active = self._active_orders.get(candidate["asset"])
            if _active:
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_active["order_id"],
                    order_submitted_at=datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    taker_ask_at_submit=candidate.get("best_yes_ask"))
            return None

        # Tier 1: Normal maker (attempt 1 or 2)
        self._submit_maker(candidate)
        _active = self._active_orders.get(candidate["asset"])
        if _active:
            self._state.update_evaluated_opportunity_order(
                candidate["ticker"], order_id=_active["order_id"],
                order_submitted_at=datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                taker_ask_at_submit=candidate.get("best_yes_ask"))
        return None

    def tick(self) -> Optional[Dict]:
        """Called each main-loop tick.  Iterates all active orders,
        polls for fills, and handles escalation independently per order.
        Also sweeps maker-tail orders for TTL expiry — must run even
        when _active_orders is empty, since tails outlive the IOC.
        """
        # Maker-tail TTL sweep runs unconditionally (before the
        # active_orders early-out below). A tail can outlive its
        # parent IOC's _active_orders entry, so gating the sweep on
        # active_orders being non-empty would let tails leak past TTL.
        if MAKER_TAIL_AFTER_IOC_PARTIAL and self._maker_tails:
            try:
                self._sweep_maker_tails()
            except Exception:
                logging.warning(
                    "_sweep_maker_tails raised", exc_info=True)
        if not self._active_orders:
            return None

        # Drain WS fills once, group by order_id
        ws_fills_by_oid: Dict[str, list] = {}
        if self._kalshi_feed and self._kalshi_feed.is_connected:
            try:
                for ws_fill in self._kalshi_feed.pop_fills():
                    oid = ws_fill.get("order_id", "")
                    ws_fills_by_oid.setdefault(oid, []).append(ws_fill)
            except Exception:
                logging.warning("WS fill drain failed", exc_info=True)

        result = None
        for asset in list(self._active_orders):
            order = self._active_orders.get(asset)
            if order is None:
                continue  # removed by a prior iteration's escalation
            order_ws = ws_fills_by_oid.get(order.get("order_id", ""), [])
            r = self._tick_one(order, asset, order_ws)
            if r is not None:
                result = r
        return result

    def _tick_sol_pathc_observations(self):
        """Check orderbook every tick for pending SOL Path C shadow entries.

        During the escalation window (default 15s), continuously monitor the
        orderbook. If best ask ever touches the hypothetical maker price,
        set obs_maker_price_touched=1 (sticky). After escalation window expires,
        write final observation snapshot and remove from pending.
        """
        if not self._sol_pathc_pending:
            return

        now = time.time()
        completed = []

        for ticker, info in self._sol_pathc_pending.items():
            elapsed = now - info["start_time"]
            maker_price = info["maker_price"]

            # Fetch current orderbook
            obs_ask = self._get_addon_best_ask(ticker)
            obs_depth = 0
            if obs_ask is not None:
                try:
                    scanner = self._ml.scanner if self._ml else None
                    if scanner:
                        _ob, _ = scanner._get_orderbook_cached(ticker)
                        if _ob:
                            obs_depth = OpportunityScanner._best_ask_depth(_ob)
                except Exception:
                    pass

            # Check if ask has touched maker price (sticky boolean)
            if obs_ask is not None and obs_ask <= maker_price:
                if not info["touched"]:
                    info["touched"] = True
                    try:
                        self._state.update_sol_pathc_touch(ticker)
                    except Exception:
                        logging.warning("sol_pathc_touch update failed for %s", ticker, exc_info=True)

            maker_would_fill = 1 if (obs_ask is not None and obs_ask <= maker_price) else 0

            # After escalation window: write final observation and compute escalation snapshot
            if elapsed >= info["escalation_wait"]:
                # Compute escalation taker edge
                esc_edge = None
                if obs_ask is not None:
                    esc_taker_fee = calculate_taker_fee(info["position_size"], obs_ask)
                    esc_edge = info["cal_prob"] - (obs_ask / 100.0) - (esc_taker_fee / (info["position_size"] * 100.0))

                obs_time = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                try:
                    self._state.update_sol_pathc_observation(
                        ticker=ticker,
                        obs_time=obs_time,
                        obs_elapsed=round(elapsed, 1),
                        obs_best_ask=obs_ask,
                        obs_depth=obs_depth,
                        obs_maker_would_fill=maker_would_fill,
                        obs_maker_price_touched=1 if info["touched"] else 0,
                        pathc_esc_ask=obs_ask,
                        pathc_esc_depth=obs_depth,
                        pathc_esc_edge=esc_edge,
                    )
                    logging.info(
                        "sol_pathc_obs_FINAL: %s elapsed=%.1fs ask=%s depth=%d touched=%s esc_edge=%s",
                        ticker, elapsed, obs_ask, obs_depth, info["touched"],
                        f"{esc_edge:.4f}" if esc_edge is not None else "None")
                except Exception:
                    logging.warning("sol_pathc_observation write failed for %s", ticker, exc_info=True)

                completed.append(ticker)

        for ticker in completed:
            self._sol_pathc_pending.pop(ticker, None)

    def _tick_one(self, order: Dict, asset: str,
                  ws_fills: list) -> Optional[Dict]:
        """Handle one active order: poll for fill, escalate if needed."""
        now = time.time()

        # Backstop: if more than 60s past expected close and order still
        # tracked, some path failed to clean up (404-variant the
        # targeted fix doesn't anticipate, missed WS expiry, etc.).
        # Force-pop with WARNING. See kb/failures/cancel-404-asset-lockout-may04.md
        elapsed = now - order["submit_time"]
        remaining = order["seconds_to_close_at_submit"] - elapsed
        if remaining < -60.0:
            filled = order.get("filled_so_far", 0)
            if filled > 0:
                db_status = "partial_canceled"
                outcome = "partial_filled"
                fm_label = "partial_canceled"
            else:
                db_status = "expired"
                outcome = "expired"
                fm_label = "expired"
            backstop_reason = (
                f"force_pop_after_close"
                f"_pending={order.get('cancel_pending', False)}"
                f"_esc={order.get('escalated', False)}"
            )
            # POP FIRST. Single outer try wraps audit writes — see
            # _handle_cancel_404 docstring for rationale.
            self._active_orders.pop(asset, None)
            try:
                self._state.mark_order_status(order["order_id"], db_status)
                self._logger.log_order({
                    "action": "maker_canceled",
                    "ticker": order["ticker"],
                    "order_id": order["order_id"],
                    "reason": backstop_reason,
                    "elapsed": round(elapsed, 1),
                    "filled_so_far": filled,
                })
                self._log_fill_model_sample(
                    order, fm_label, cancel_reason=backstop_reason)
                if asset not in self._escalating_assets:
                    self._state.update_evaluated_opportunity_order(
                        order["ticker"], order_outcome=outcome)
            except Exception:
                logging.error(
                    f"force_pop_audit_failed: {order['ticker']} "
                    f"{order['order_id']} — pop complete, audit "
                    f"incomplete.",
                    exc_info=True)
            logging.warning(
                f"force_pop_after_close: {order['ticker']} "
                f"{order['order_id']} remaining={remaining:.1f}s "
                f"filled={filled}/{order['count']} "
                f"reason={backstop_reason} — backstop fired.")
            return None

        if now - order["_last_poll"] < MAKER_POLL_INTERVAL:
            return None
        order["_last_poll"] = now

        # Reconcile cancel_pending orders: retry cancel via Kalshi API
        if order.get("cancel_pending"):
            try:
                cancel_resp = self._client.cancel_order(order["order_id"])
                # 404 sentinel — Kalshi has aged it; route through helper.
                if isinstance(cancel_resp, dict) and cancel_resp.get("_status_code") == 404:
                    self._handle_cancel_404(
                        order, asset, "cancel_pending_retry",
                        source="reconciliation")
                    return None
                if cancel_resp is not None:
                    logging.info(f"cancel_pending resolved: {order['ticker']} cancel succeeded on retry")
                    order.pop("cancel_pending", None)
                    self._state.mark_order_status(order["order_id"], "canceled")
                    self._active_orders.pop(asset, None)
                    self._state.update_evaluated_opportunity_order(
                        order["ticker"], order_outcome="canceled")
                    return None
                # Cancel still failing — check if order was already filled
                fills_resp = self._client.get_fills(ticker=order["ticker"])
                if fills_resp:
                    fills = fills_resp.get("fills", [])
                    for f in fills:
                        if f.get("order_id") == order["order_id"]:
                            logging.info(f"cancel_pending resolved: {order['ticker']} was filled")
                            order.pop("cancel_pending", None)
                            break  # Let normal fill detection handle it below
            except Exception as e:
                logging.error(f"cancel_pending reconciliation error for {order['ticker']}: {e}")

        # 0. Check WebSocket fills (pre-drained, zero API cost)
        for ws_fill in ws_fills:
            order["fill_source"] = "websocket"
            self._session_ws_fills += 1
            latency_ms = round((now - order["submit_time"]) * 1000, 1)
            logging.info(
                f"kalshi_ws_fill: {order['ticker']} order={order['order_id']} "
                f"latency={latency_ms}ms")
            self._on_fill(ws_fill, order)
            ws_trade_id = ws_fill.get("trade_id") or ws_fill.get("id")
            if not ws_trade_id:
                # Synthetic dedup key when trade_id missing — prevents REST double-count
                self._ws_fill_seq = getattr(self, '_ws_fill_seq', 0) + 1
                ws_trade_id = f"syn_{ws_fill.get('order_id','')}_{ws_fill.get('count','')}_{ws_fill.get('price','')}_{self._ws_fill_seq}"
                logging.warning(f"WS fill missing trade_id for {order['ticker']}, using synthetic key: {ws_trade_id}")
            order.setdefault("_seen_fill_ids", set()).add(ws_trade_id)
            if order.get("filled_so_far", 0) >= order["count"]:
                self._active_orders.pop(asset, None)
                self._state.update_evaluated_opportunity_order(
                    order["ticker"], order_outcome="filled")
                return ws_fill
            logging.info(
                f"Partial WS fill — keeping order active "
                f"({order['filled_so_far']}/{order['count']})")

        # 1. Check for maker fill via REST
        fill = self._check_for_fill(order)
        if fill:
            order["fill_source"] = "rest_poll"
            self._session_rest_fills += 1
            self._on_fill(fill, order)
            if order.get("filled_so_far", 0) >= order["count"]:
                self._active_orders.pop(asset, None)
                self._state.update_evaluated_opportunity_order(
                    order["ticker"], order_outcome="filled")
                return fill
            logging.info(
                f"Partial REST fill — keeping order active "
                f"({order['filled_so_far']}/{order['count']})")

        elapsed = now - order["submit_time"]
        remaining = order["seconds_to_close_at_submit"] - elapsed

        # 2. Too close to expiry — cancel, don't escalate
        if remaining < MIN_SECONDS_BEFORE_CLOSE:
            self._cancel_order(asset, "close_approaching")
            return None

        # 2.5 Queue position polling (~every 5s, rate-limit friendly)
        if now - order["_last_queue_poll"] >= 5.0:
            order["_last_queue_poll"] = now
            try:
                qpos = self._client.get_queue_position(order["order_id"])
                if qpos is not None:
                    order["queue_position"] = qpos
                    logging.debug(
                        f"queue_position_check: {order['ticker']} "
                        f"order={order['order_id']} position={qpos}")
            except Exception:
                pass  # Non-critical, don't disrupt flow

        # 3. Escalation: maker waited long enough? (skip if already escalated)
        # Maker-only below 90s: no taker escalation, let maker fill or expire
        if not order.get("escalated") and remaining >= MAKER_ONLY_THRESHOLD:
            # ── Early escalation: ask confirms thesis ──────────────
            current_ask = self._get_addon_best_ask(order["ticker"])
            if current_ask is not None:
                order["_ask_history"].append((now, current_ask))
                ask_move = current_ask - order["price_cents"]
                if ask_move >= 2 and ask_move < EARLY_ESCALATION_MIN_MOVE:
                    # Shadow: log skipped early escalations (2-4c) for data collection
                    if not order.get("_ask_confirmed_skipped_logged"):
                        order["_ask_confirmed_skipped_logged"] = True
                        _skip_candidate = order["candidate"]
                        _skip_count = order["count"]
                        _skip_fee = calculate_taker_fee(_skip_count, current_ask)
                        _skip_edge = _skip_candidate["calibrated_prob"] - (current_ask / 100.0) - (_skip_fee / (_skip_count * 100.0))
                        logging.info(
                            "ask_confirmed_SKIPPED: %s ask=%d¢ (maker=%d¢ +%d¢) "
                            "net_edge=%.4f elapsed=%.1fs threshold=%d¢",
                            order["ticker"], current_ask, order["price_cents"],
                            ask_move, _skip_edge, elapsed, EARLY_ESCALATION_MIN_MOVE)
                if ask_move >= EARLY_ESCALATION_MIN_MOVE:
                    candidate = order["candidate"]
                    cal_prob = candidate["calibrated_prob"]
                    count = order["count"]
                    taker_fee = calculate_taker_fee(count, current_ask)
                    net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (count * 100.0))
                    if net_edge >= MIN_EDGE_PCT / 100.0:
                        logging.info(
                            "early_escalation_TRIGGER: %s ask=%d¢ (maker=%d¢ +%d¢) "
                            "net_edge=%.4f elapsed=%.1fs",
                            order["ticker"], current_ask, order["price_cents"],
                            ask_move, net_edge, elapsed)
                        return self._escalate_to_taker(order, remaining,
                                                       reason="ask_confirmed")

            # ── Standard time-based escalation (existing code) ─────
            escalation_wait = self._escalation_wait(remaining, asset=order.get("asset", ""))
            # Queue-aware: escalate earlier if deep in queue and time is short
            queue_pos = order.get("queue_position")
            if queue_pos is not None and queue_pos > 20 and remaining < 60:
                escalation_wait = min(escalation_wait, 5.0)
            if elapsed >= escalation_wait:
                return self._escalate_to_taker(order, remaining)

        # 4. Hard timeout fallback
        if elapsed >= MAKER_TIMEOUT_SECONDS:
            self._cancel_order(asset, "timeout")

        return None

    @staticmethod
    def _escalation_wait(remaining: float, asset: str = "") -> float:
        """Urgency-based maker wait before escalating to taker."""
        if remaining >= 180:
            # BTC: shorter wait (7s vs 15s) — ask_confirmed avg 2.7s, slip 3.4c
            if asset == "BTC" and BTC_ESCALATION_WAIT_OVERRIDE is not None:
                return BTC_ESCALATION_WAIT_OVERRIDE
            return ESCALATION_WAIT_LONG     # 15s — ample time, let maker fill
        elif remaining >= 120:
            return ESCALATION_WAIT_MEDIUM   # 7s — 86% of fills happen within 7s
        else:
            return ESCALATION_WAIT_SHORT    # 5s — tight, quick escalation

    # ── Market Intelligence Helpers ───────────────────────────────────────

    @staticmethod
    def _best_ask_depth(ob_data: Dict) -> int:
        """Depth (contracts) at the best YES ask (= highest NO bid level)."""
        no_bids = ob_data.get("no", [])
        if not no_bids:
            return 0
        best_price = -1
        best_qty = 0
        for entry in no_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = entry[0], int(entry[1])
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
                qty = int(entry.get("quantity", 0))
            else:
                continue
            if isinstance(price, float) and price < 1.0:
                price_cents = round(price * 100)
            else:
                price_cents = int(price)
            if price_cents > best_price:
                best_price = price_cents
                best_qty = qty
        return best_qty

    @staticmethod
    def _compute_ladder_diag(live_ob) -> Dict:
        """F/U TM_99 zero-fill diagnostic. Extract yes_asks_top and
        no_bid_top (price + qty) from cached orderbook, compute the
        cross-side derivation `100 - no_bid_top` and whether the two
        ladders diverge.

        Distinguishes which hypothesis is right when ETH TM_99 IOCs
        fail to fill at 99c:
          - HYP A: Kalshi's matching engine fills only against the
            explicit yes_asks ladder, not synthetic cross-side. If
            yes_asks_top > our bid AND ladders diverge, our IOC
            can't cross.
          - HYP B: Kalshi matches both ladders, but no_bid is too
            thin and gets sniped before our IOC arrives.
        Logged at IOC submit. Pure observability — no behavior change.

        Returns: {yes_ask_top_price, yes_ask_top_qty, no_bid_top_price,
                  no_bid_top_qty, cross_side_ask, diverges}
        See kb/failures (when written).
        """
        out = {
            "yes_ask_top_price": None, "yes_ask_top_qty": 0,
            "no_bid_top_price": None, "no_bid_top_qty": 0,
            "cross_side_ask": None, "diverges": False,
            "one_side_empty": False,
        }
        if not live_ob:
            return out

        def _to_cents(p):
            # Handle: int (already cents), float < 1.0 (dollar format,
            # e.g. 0.99 = 99c), float in [1.0, 100.0] (could be dollar
            # 1.00 = 100c OR cents 1.0 = 1c — Kalshi never sends
            # "1.00 dollars" for binary 0-100c contracts, so treat as
            # cents), string (cast through float first — schema drift
            # defense, see MEMORY: feedback_kalshi_schema_drift).
            try:
                if isinstance(p, str):
                    p = float(p)
                if isinstance(p, float) and 0 < p < 1.0:
                    return round(p * 100)
                return int(p)
            except (TypeError, ValueError):
                return None

        # yes_asks: pick LOWEST price (best ask for buyer).
        for entry in (live_ob.get("yes") or []):
            if not (isinstance(entry, (list, tuple)) and len(entry) >= 2):
                continue
            p = _to_cents(entry[0])
            if p is None:
                continue
            try:
                q = int(entry[1])
            except (TypeError, ValueError):
                q = 0
            if (out["yes_ask_top_price"] is None
                    or p < out["yes_ask_top_price"]):
                out["yes_ask_top_price"] = p
                out["yes_ask_top_qty"] = q

        # no_bids: pick HIGHEST price (best NO bid → best cross-side).
        for entry in (live_ob.get("no") or []):
            if not (isinstance(entry, (list, tuple)) and len(entry) >= 2):
                continue
            p = _to_cents(entry[0])
            if p is None:
                continue
            try:
                q = int(entry[1])
            except (TypeError, ValueError):
                q = 0
            if (out["no_bid_top_price"] is None
                    or p > out["no_bid_top_price"]):
                out["no_bid_top_price"] = p
                out["no_bid_top_qty"] = q

        if out["no_bid_top_price"] is not None:
            out["cross_side_ask"] = 100 - out["no_bid_top_price"]

        if (out["yes_ask_top_price"] is not None
                and out["cross_side_ask"] is not None):
            out["diverges"] = (
                out["yes_ask_top_price"] != out["cross_side_ask"])

        # one_side_empty: tri-state signal for grep — captures the
        # case where one ladder is missing entirely. R-review [A4]:
        # `diverges=False + one_side_empty=False` means real
        # alignment; `diverges=False + one_side_empty=True` means
        # uninformative. Don't conflate.
        out["one_side_empty"] = (
            (out["yes_ask_top_price"] is None)
            != (out["no_bid_top_price"] is None)
        )

        return out

    @staticmethod
    def _pick_ioc_limit_for_depth(
            ob_data: Dict,
            best_yes_ask: int,
            target_qty: int,
            max_bump_cents: int,
            edge_ceiling_price: int,
            max_price: int = 99) -> int:
        """Walk the orderbook from `best_yes_ask` upward, return the
        smallest YES limit price where cumulative fillable depth
        meets `target_qty`. Hard-capped at:
          - `best_yes_ask + max_bump_cents` (operational ceiling)
          - `edge_ceiling_price` (EV ceiling — caller computes
            from `floor(calibrated_prob*100) - fee - reserve_cents`,
            where reserve_cents is per-strategy
            (STRATEGY_LIMIT_BUMP_RESERVE_CENTS, default 0). At
            limit = ceiling, worst-case fill has edge = reserve.
            Default reserve=0 means break-even after fee on worst
            fill; aggressive overrides (-1) tolerate ~1c negative
            edge on worst fill. NOTE: this no longer respects
            MIN_EDGE_PCT — that floor was a SCAN-time gate, not a
            submit-time gate. The submit gate uses per-strategy
            reserve directly.)
          - `max_price` (defaults to MAX_ENTRY_PRICE = 99)

        If no level inside the cap delivers `target_qty`, returns
        the highest level inside the cap (still better than
        best_yes_ask alone — Kalshi auto-cancels surplus at $0).

        If the orderbook has no fillable depth at any level inside
        the cap, returns `best_yes_ask` unchanged (caller will
        discover empty book via PHANTOM_ABORT).

        WHY (Apr 25 2026):
        Pre-Apr 23 the WS schema bug masked the orderbook → bot
        fell back to NBBO yes_ask (typically wider than orderbook
        best_ask) → IOC swept multiple price levels → 64-82ct
        avg fills. Post-Apr 23 fix made the bot use orderbook
        best_ask exactly → matches only top-of-book → 33ct avg.
        Liquidity didn't disappear — just sat 1-3c above our
        IOC limit. Production sample: 1ct at 66c, 151ct at 69c.
        This helper restores access to the deep level when the
        candidate's edge can absorb the bump.

        Mechanics: Kalshi orderbooks store YES asks via the NO
        bid stack — NO bid at price P = YES ask at (100 - P).
        We walk YES ask prices ascending from best_yes_ask, sum
        qty, return first price where cumul ≥ target.

        Sub-floor levels (YES asks below best_yes_ask) are NOT
        included in the walk because the picker only chooses
        the LIMIT, not the fill source — Kalshi's matching engine
        will sweep sub-floor asks at any limit ≥ them, but that's
        Variant B behavior intentional under the no_clamp policy."""
        # Caller's edge_ceiling_price might be below best_yes_ask
        # (defensive — candidate shouldn't have been generated, but
        # never return a price below best_yes_ask).
        if max_bump_cents <= 0 or target_qty <= 0:
            return best_yes_ask
        cap = min(
            best_yes_ask + max_bump_cents,
            max(edge_ceiling_price, best_yes_ask),
            max_price,
        )
        if cap < best_yes_ask:
            return best_yes_ask
        # Build the YES-ask ladder from the NO bid stack, filter to
        # prices in [best_yes_ask, cap], sort ascending.
        no_bids = ob_data.get("no") or []
        levels: list = []
        for entry in no_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = entry[0], entry[1]
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
                qty = entry.get("quantity", 0)
            else:
                continue
            try:
                qty_int = int(qty)
            except (TypeError, ValueError):
                continue
            if qty_int <= 0:
                continue
            # Normalize price to cents.
            if isinstance(price, float) and price < 1.0:
                price_cents = round(price * 100)
            else:
                try:
                    price_cents = int(price)
                except (TypeError, ValueError):
                    continue
            yes_ask = 100 - price_cents
            if best_yes_ask <= yes_ask <= cap:
                levels.append((yes_ask, qty_int))
        if not levels:
            return best_yes_ask
        levels.sort()  # ascending YES price
        cumul = 0
        for yes_ask, qty in levels:
            cumul += qty
            if cumul >= target_qty:
                return yes_ask
        # Walked everything inside cap without hitting target.
        # Return highest level we reached — better than best_ask.
        return levels[-1][0]

    @staticmethod
    def _total_ob_depth(ob_data: Dict) -> int:
        """Total depth (contracts) across all orderbook levels."""
        total = 0
        for side in ("no", "yes"):
            for entry in (ob_data.get(side) or []):
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    total += int(entry[1])
                elif isinstance(entry, dict):
                    total += int(entry.get("quantity", 0))
        return total

    @staticmethod
    def _best_yes_bid(ob_data: Dict) -> Optional[int]:
        """Highest YES bid price in cents."""
        yes_bids = ob_data.get("yes", [])
        best = None
        for entry in yes_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price = entry[0]
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
            else:
                continue
            price_cents = round(price * 100) if isinstance(price, float) and price < 1.0 else int(price)
            if best is None or price_cents > best:
                best = price_cents
        return best

    @staticmethod
    def _best_yes_bid_depth(ob_data: Dict) -> int:
        """Depth at the highest YES bid."""
        yes_bids = ob_data.get("yes", [])
        best_price = -1
        best_qty = 0
        for entry in yes_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = entry[0], int(entry[1])
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
                qty = int(entry.get("quantity", 0))
            else:
                continue
            price_cents = round(price * 100) if isinstance(price, float) and price < 1.0 else int(price)
            if price_cents > best_price:
                best_price = price_cents
                best_qty = qty
        return best_qty

    @staticmethod
    def _extract_book_levels(ob_data: Optional[Dict], n: int = 10) -> Optional[str]:
        """Top-N YES-side ladder as compact JSON for forensic logging.

        Returns: '{"yes_bids":[[p,q],...],"yes_asks":[[p,q],...]}' or None.
        yes_bids sorted desc by price (best bid first).
        yes_asks derived from raw NO bids via 100-p, sorted asc (best ask first).

        Input contract:
        - ob_data must be a coalesced book (dict), not a delta frame.
          Non-dict input (None, list, str) returns None.
        - Float price <= 1.0 treated as probability (× 100 → cents).
          Float price > 1.0 treated as already-cents.

        Dropped (silently): NaN/Inf/negative/missing/bool qty,
        price < 0 or > 100, bool price, malformed entry shapes,
        duplicate price levels are merged (sum qty).
        """
        if not isinstance(ob_data, dict):
            return None

        def _parse_and_merge(entries):
            """Parse entries to {price_cents: total_qty} dict, merging duplicates."""
            out: Dict[int, int] = {}
            for entry in entries or []:
                if isinstance(entry, (list, tuple)):
                    if len(entry) < 2:
                        continue
                    price, qty = entry[0], entry[1]
                elif isinstance(entry, dict):
                    if "quantity" not in entry:
                        continue
                    price = entry.get("price")
                    qty = entry.get("quantity")
                else:
                    continue
                # Reject bools (subclass of int — silently poisons output)
                if isinstance(price, bool) or isinstance(qty, bool):
                    continue
                try:
                    if isinstance(qty, float) and not math.isfinite(qty):
                        continue
                    qty_int = int(qty)
                    if qty_int <= 0:
                        continue
                    if isinstance(price, float):
                        if not math.isfinite(price):
                            continue
                        if price <= 1.0:
                            price_cents = round(price * 100)
                        else:
                            price_cents = int(price)
                    else:
                        price_cents = int(price)
                except (TypeError, ValueError, OverflowError):
                    continue
                if price_cents < 0 or price_cents > 100:
                    continue
                out[price_cents] = out.get(price_cents, 0) + qty_int
            return out

        yes_bids_merged = _parse_and_merge(ob_data.get("yes"))
        no_bids_merged = _parse_and_merge(ob_data.get("no"))

        yes_bids = heapq.nlargest(n, yes_bids_merged.items(), key=lambda kv: kv[0])
        # NO bid >= 100c → derived YES ask <= 0, drop as nonsensical
        yes_asks_iter = ((100 - p, q) for p, q in no_bids_merged.items() if p < 100)
        yes_asks = heapq.nsmallest(n, yes_asks_iter, key=lambda pq: pq[0])

        return json.dumps(
            {"yes_bids": [[p, q] for p, q in yes_bids],
             "yes_asks": [[p, q] for p, q in yes_asks]},
            separators=(",", ":"),
        )

    # ── Repricing ─────────────────────────────────────────────────────────

    def _reprice_maker(self, new_price: int) -> bool:
        """Amend maker order to a new price. Returns True on success."""
        if self._active_order is None:
            return False
        order = self._active_order
        self._session_amend_attempts += 1
        try:
            _side = order.get("side", "yes")
            _price_kwarg = {"no_price": new_price} if _side == "no" else {"yes_price": new_price}
            resp = self._client.amend_order(
                order_id=order["order_id"], ticker=order["ticker"],
                side=_side, action="buy", count=order["count"],
                **_price_kwarg)
            if resp is None:
                logging.warning(
                    f"amend_failed_fallback: {order['ticker']} "
                    f"old={order['price_cents']}¢ new={new_price}¢")
                return False
            old_price = order["price_cents"]
            order["price_cents"] = new_price
            self._session_amend_successes += 1
            logging.info(
                f"amend_success: {order['ticker']} "
                f"{old_price}¢ → {new_price}¢ order={order['order_id']}")
            return True
        except Exception:
            logging.warning("Amend failed with exception", exc_info=True)
            return False

    def _escalate_to_taker(self, order: Dict, remaining: float,
                           reason: str = "escalation_wait") -> Optional[Dict]:
        """Escalate maker to taker via cancel-replace IOC."""
        ticker = order["ticker"]
        asset = order["asset"]
        self._escalating_assets.add(asset)
        try:
            return self._escalate_to_taker_inner(order, remaining, reason)
        finally:
            self._escalating_assets.discard(asset)

    def _escalate_to_taker_inner(self, order: Dict, remaining: float,
                                  reason: str = "escalation_wait") -> Optional[Dict]:
        """Inner escalation logic (guarded by _escalating_assets)."""
        ticker = order["ticker"]
        elapsed = time.time() - order["submit_time"]

        # Re-fetch orderbook for current best ask
        ob_raw = self._client.get_orderbook(ticker, depth=5)
        if ob_raw is None:
            logging.warning(f"Escalation aborted: orderbook fetch failed for {ticker}")
            self._cancel_order(order["asset"], reason)
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Unwrap response envelope (same as _get_orderbook_cached)
        ob_fp = ob_raw.get("orderbook_fp") if ob_raw else None
        if ob_fp:
            ob_data = OpportunityScanner._convert_orderbook_fp(ob_fp)
        else:
            ob_data = ob_raw.get("orderbook") or ob_raw

        best_ask = OpportunityScanner._best_yes_ask_cents(ob_data)
        if best_ask is None:
            logging.warning(f"Escalation aborted: no asks on orderbook for {ticker}")
            self._cancel_order(order["asset"], reason)
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Per-asset price floor for escalation (mirrors scanner + maker)
        _esc_floor = MIN_ENTRY_PRICE
        _esc_asset = order.get("asset")
        if _esc_asset == "BTC":
            _esc_floor = BTC_MIN_ENTRY_PRICE
        elif _esc_asset == "ETH":
            _esc_floor = ETH_MIN_ENTRY_PRICE
        elif _esc_asset == "SOL":
            _esc_floor = SOL_MIN_ENTRY_PRICE
        elif _esc_asset == "XRP":
            _esc_floor = XRP_MIN_ENTRY_PRICE
        if best_ask < _esc_floor or best_ask > ESCALATION_MAX_ENTRY:
            logging.warning(
                f"Escalation aborted: price {best_ask}¢ out of range "
                f"[{_esc_floor}-{ESCALATION_MAX_ENTRY}¢] for {ticker}"
            )
            self._cancel_order(order["asset"], reason)
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Determine urgency tier for logging
        if remaining >= 180:
            tier = "long"
        elif remaining >= 120:
            tier = "medium"
        else:
            tier = "short"

        price_slip = best_ask - order["price_cents"]
        self._logger.log_order({
            "action": "escalate_to_taker",
            "reason": reason,
            "ticker": ticker,
            "maker_price": order["price_cents"],
            "taker_price": best_ask,
            "price_slip": price_slip,
            "wait_time": round(elapsed, 1),
            "urgency_tier": tier,
            "remaining": round(remaining, 1),
            "execution_method": "cancel_replace_ioc",
        })

        # ── Edge recheck at escalated price ──────────────────────────
        # The candidate was evaluated with edge at the maker price. If the
        # taker price is higher, the edge may have evaporated or gone negative.
        # Data: 10 MAKER_PATIENT losses with drift>0 cost $704/2wk.
        _esc_prob = order["candidate"].get("calibrated_prob", 0)
        _esc_fee_1c = calculate_taker_fee(1, best_ask)
        _esc_edge = _esc_prob - best_ask / 100.0 - _esc_fee_1c / 100.0
        _esc_maker_price = order["price_cents"]
        if _esc_edge < 0 and best_ask > _esc_maker_price:
            logging.warning(
                "ESCALATION_EDGE_ABORT: %s prob=%.4f price=%d→%dc edge=%.4f "
                "(negative at escalated price, canceling)",
                ticker, _esc_prob, _esc_maker_price, best_ask, _esc_edge)
            self._cancel_order(order["asset"], "escalation_edge_abort")
            self._state.update_evaluated_opportunity_order(
                ticker, order_outcome="escalation_edge_abort")
            return None

        # Cancel maker + submit taker IOC
        logging.info(
            f"escalation_cancel_replace: {ticker} "
            f"(maker={order['price_cents']}¢ → taker={best_ask}¢ edge={_esc_edge:.4f})")
        cancel_ok = self._cancel_order(order["asset"], reason)
        if not cancel_ok:
            logging.error(f"Cancel failed for {ticker} — NOT submitting taker to prevent double position")
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Build modified candidate with fresh best ask
        candidate = dict(order["candidate"])
        candidate["best_yes_ask"] = best_ask
        candidate["entry_path"] = "escalation_ioc"
        candidate["escalation_type"] = reason
        candidate["maker_price_cents"] = order["price_cents"]
        candidate["maker_wait_seconds"] = round(elapsed, 1)
        filled = order.get("filled_so_far", 0)
        if filled > 0:
            candidate["position_size"] = max(1, candidate["position_size"] - filled)
            logging.info(
                f"escalation_partial_adjust: {ticker} "
                f"original={order['count']} filled={filled} "
                f"ioc_count={candidate['position_size']}")
        self._recent_taker_tickers[ticker] = time.time()

        result = self._submit_taker(candidate)
        if result is not None:
            _esc_oid = result.get("order_id") if isinstance(result, dict) else None
            self._state.update_evaluated_opportunity_order(
                ticker, order_id=_esc_oid, order_outcome="filled")
        else:
            self._state.update_evaluated_opportunity_order(
                ticker, order_outcome="unfilled")
        return result

    # ── DC Taker with Non-Blocking Retry Queue ─────────────────────────

    @staticmethod
    def _dc_retry_delay(seconds_to_close: float) -> float:
        """Adaptive retry delay based on urgency (STC). Shorter near settlement."""
        if seconds_to_close > 600:
            return 20.0
        elif seconds_to_close > 300:
            return 12.0
        elif seconds_to_close > 120:
            return 6.0
        elif seconds_to_close > 30:
            return 3.0
        else:
            return 1.0

    def _rest_best_ask_depth(self, ticker: str) -> Optional[int]:
        """Force-fetch best YES ask depth via REST /orderbook.

        Used as a pre-IOC drift check: the WS cache can diverge from
        Kalshi's real book (see kb/failures/kalshi-ws-schema-drift.md
        § "WS delta underflow"). REST is the ground truth. Returns None
        on any error so caller falls back to cached depth.

        Adds ~30-50ms latency per call. Should only be invoked from IOC
        submit paths where cache claims non-trivial depth — see
        IOC_DRIFT_CHECK_MIN_CACHED_DEPTH.

        Note: callers should prefer `_rest_best_ask_depth_smoothed`
        which records this single sample into the rolling-window
        buffer and returns the peak across recent observations.
        Single REST samples are themselves volatile — see
        WS_DRIFT_PROBE_REST_STABILITY.
        """
        try:
            ob_resp = self._client.get_orderbook(ticker, depth=5)
            if not ob_resp:
                return None
            ob_fp = ob_resp.get("orderbook_fp")
            if ob_fp and self._ml and hasattr(self._ml, 'scanner'):
                fresh_ob = self._ml.scanner._convert_orderbook_fp(ob_fp)
            else:
                fresh_ob = ob_resp.get("orderbook")
            if not fresh_ob:
                return None
            return OrderExecutor._best_ask_depth(fresh_ob)
        except Exception:
            return None

    # Sanity bound for a recorded depth value. Kalshi best-ask
    # depths are typically <50k contracts; rejecting anything past
    # 100k catches realistic schema-drift bugs (e.g., REST returning
    # sum-of-levels rather than top-of-book = ~10–50× inflation).
    # Round 1 P1 + Round 2 [A5] tightening — 1M was too loose to
    # catch any plausible bug class.
    _REST_DEPTH_SAMPLE_MAX = 100_000

    # Minimum samples in the rolling window before the smoothed peak
    # is trusted as the clamp authority. With only 1 sample, the
    # smoothed helper degenerates to single-sample clamping — the
    # exact pre-fix bug. Round 2 [A1] cold-start gate: if the buffer
    # has <2 samples within the window, the drift-check skips the
    # clamp altogether and falls through to the existing policy
    # (cached `_ask_depth` + STRATEGY_CLAMP_POLICY), which is the
    # behavior that worked for months pre-a56ecc7. PHANTOM_ABORT
    # still fires on fresh=0 regardless of cold-start state.
    _REST_DEPTH_MIN_SAMPLES_FOR_CLAMP = 2

    # Threading: `_rest_depth_observations` is read/written ONLY from
    # the main thread (executor's `_submit_taker` and helpers). No
    # WS thread, refresh worker, or engine thread touches it today.
    # If a future engine ever calls into `_submit_taker` from a
    # different thread, wrap the deque ops in a lock — `popleft` is
    # atomic individually but the prune-then-append pattern in
    # `_record_rest_depth_observation` is not. (Round 2 [A3].)

    def _record_rest_depth_observation(self, ticker: str,
                                       depth: int) -> None:
        """Append (monotonic_now, depth) to the per-ticker rolling
        buffer and prune any samples older than
        IOC_DRIFT_CHECK_REST_WINDOW_S. Drops the dict entry entirely
        when the deque becomes empty after prune — bounds memory
        growth across the lifetime of the process (15M markets
        cycle every 15 min × 4 assets = ~16/hr new tickers; without
        cleanup the dict would leak indefinitely).

        Round 1 hardening:
          - `time.monotonic()` (not `time.time()`) — wall-clock NTP
            jumps backwards corrupt window math; the VPS has been
            logging 5–14s clock_drift_detected warnings every 30s.
          - Bounds-check on `depth`: out-of-bound values (negative
            or > _REST_DEPTH_SAMPLE_MAX) are dropped AND a
            once-per-ticker WARNING fires for diagnostics. Round 2
            [A7]: silent drop with no observability would mask a
            schema-drift bug that produced consistently-bad samples."""
        if depth is None or depth < 0 or depth > self._REST_DEPTH_SAMPLE_MAX:
            # Round 2 [A7]: log once per ticker so operators see a
            # signal if schema drift is poisoning the buffer.
            if not hasattr(self, "_rest_depth_drop_logged"):
                self._rest_depth_drop_logged = set()
            if ticker not in self._rest_depth_drop_logged:
                self._rest_depth_drop_logged.add(ticker)
                logging.warning(
                    "REST_DEPTH_SAMPLE_DROPPED: %s depth=%r — out of "
                    "bounds [0, %d]; smoothing buffer not updated. "
                    "Possible schema drift in REST /orderbook.",
                    ticker, depth, self._REST_DEPTH_SAMPLE_MAX)
            return
        now = time.monotonic()
        cutoff = now - IOC_DRIFT_CHECK_REST_WINDOW_S
        buf = self._rest_depth_observations.get(ticker)
        if buf is None:
            buf = deque()
            self._rest_depth_observations[ticker] = buf
        # Prune expired samples from the left.
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        buf.append((now, int(depth)))

    def _rest_depth_window_count(self, ticker: str) -> int:
        """Return the number of unexpired samples in the per-ticker
        buffer. Used as the cold-start gate — when count < 2 the
        smoothed peak is just a rename of the single fresh sample
        and provides no actual smoothing. Round 2 [A1].
        Side-effect-free."""
        now = time.monotonic()
        cutoff = now - IOC_DRIFT_CHECK_REST_WINDOW_S
        buf = self._rest_depth_observations.get(ticker)
        if not buf:
            return 0
        return sum(1 for ts, _ in buf if ts >= cutoff)

    def _rest_depth_window_max(self, ticker: str) -> Optional[int]:
        """Return the peak depth observed for `ticker` within the
        last IOC_DRIFT_CHECK_REST_WINDOW_S seconds, or None if no
        samples are in the window. Pure read — does not record.

        Round 1 hardening: prunes expired samples in-place and
        DELETES the dict entry when its deque becomes empty. This
        bounds memory growth (settled tickers stop sending samples,
        their deque ages out, then this read drops the entry).

        Peak (not mean/median) is the right statistic for this
        clamp because:
          - real phantom WS → REST stays consistently low → peak stays low
          - transient REST noise → some samples high, some low →
            peak preserves the high reading and avoids false-clamp"""
        now = time.monotonic()
        cutoff = now - IOC_DRIFT_CHECK_REST_WINDOW_S
        buf = self._rest_depth_observations.get(ticker)
        if not buf:
            return None
        # Prune expired samples in-place so memory is reclaimed.
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        if not buf:
            # All samples expired — drop the dict entry entirely.
            del self._rest_depth_observations[ticker]
            return None
        return max(d for _, d in buf)

    def _rest_best_ask_depth_smoothed(
            self, ticker: str) -> Tuple[Optional[int], Optional[int]]:
        """REST best-ask depth, smoothed over a rolling window.

        Returns a 2-tuple `(peak, fresh)`:
          - `peak`: max depth observed within
            IOC_DRIFT_CHECK_REST_WINDOW_S, after recording the
            fresh sample. Used as the clamp authority for IOC
            sizing — peak protects against single-sample REST
            volatility (delta_qty=-900 in 1s).
          - `fresh`: the just-fetched REST sample (or None on
            REST error). Required for PHANTOM_ABORT decisions —
            when fresh==0, the book IS empty right now regardless
            of historical peak, so the abort path must check fresh
            independently. Round 1 [P0 #2] regression guard.

        If REST errors, fresh=None and peak falls back to the
        historical buffer (or None if both are absent). The caller
        falls back to cached depth in that case — existing behavior."""
        fresh = self._rest_best_ask_depth(ticker)
        if fresh is not None:
            self._record_rest_depth_observation(ticker, fresh)
        peak = self._rest_depth_window_max(ticker)
        return peak, fresh

    def _dc_get_ask_with_depth(self, ticker: str, candidate: Dict):
        """Get best ask price AND depth for DC execution decisions.

        Returns (price, depth, source) where:
        - price: best YES ask in cents, or None
        - depth: contracts at best ask level, 0 if unknown
        - source: 'orderbook' or 'market_nbbo'
        """
        try:
            scanner = self._ml.scanner if self._ml else None
            if scanner:
                ob_data, _ = scanner._get_orderbook_cached(ticker)
                if ob_data:
                    price = OpportunityScanner._best_yes_ask_cents(ob_data)
                    if price is not None:
                        depth = OpportunityScanner._best_ask_depth(ob_data)
                        return price, depth, "orderbook"
        except Exception:
            pass

        # REST fallback
        try:
            ob_resp = self._client.get_orderbook(ticker, depth=5)
            if ob_resp:
                ob_fp = ob_resp.get("orderbook_fp")
                if ob_fp and self._ml and hasattr(self._ml, 'scanner'):
                    ob_data = self._ml.scanner._convert_orderbook_fp(ob_fp)
                else:
                    ob_data = ob_resp.get("orderbook", ob_resp)
                if ob_data:
                    price = OpportunityScanner._best_yes_ask_cents(ob_data)
                    if price is not None:
                        depth = OpportunityScanner._best_ask_depth(ob_data)
                        return price, depth, "orderbook"
        except Exception:
            pass

        # NBBO fallback
        nbbo_price = self._nbbo_fallback_price(candidate)
        if nbbo_price is not None:
            return nbbo_price, 0, "market_nbbo"

        return None, 0, "none"

    def _execute_dc_taker(self, candidate: Dict, asset: str, seconds_to_close) -> Optional[Dict]:
        """Submit DC IOC. On unfilled/partial, queue non-blocking retry."""
        _dc_strategy = candidate.get("strategy")
        ticker = candidate["ticker"]
        count = candidate["position_size"]
        price = candidate["best_yes_ask"]
        cal_prob = candidate["calibrated_prob"]

        if count <= 0:
            logging.warning("ORDER_SUPPRESSED zero_size: %s asset=%s strategy=%s price=%d",
                            ticker, asset, _dc_strategy, price)
            self._session_suppressed_zero_size += 1
            return None

        taker_fee = calculate_taker_fee(count, price)
        net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

        if net_edge < -0.01:
            logging.warning(
                "ORDER_SUPPRESSED edge_taker_fee: %s asset=%s strategy=%s net_edge=%.4f < -0.01 "
                "price=%d¢ cal_prob=%.4f taker_fee=%d¢",
                ticker, asset, _dc_strategy, net_edge, price, cal_prob, taker_fee)
            self._session_suppressed_edge_recalc += 1
            return None

        # Fresh ask check with depth — verify price, depth, and source
        _dc_scan_price = price  # preserve original scan price for drift check
        fresh_ask, fresh_depth, fresh_source = self._dc_get_ask_with_depth(ticker, candidate)

        if fresh_ask is None:
            logging.warning("ORDER_SUPPRESSED no_asks: %s asset=%s strategy=%s price=%d stc=%.0f",
                            ticker, asset, _dc_strategy, price, seconds_to_close or 0)
            self._session_suppressed_no_asks += 1
            if self._ml and hasattr(self._ml, "scanner"):
                self._ml.scanner._dc_skip_cooldown[ticker] = time.time() + 60
            # Queue retry — book may appear later
            _retry_delay = self._dc_retry_delay(seconds_to_close or 0)
            self._dc_retry_queue.append({
                "candidate": candidate.copy(),
                "original_count": count,
                "total_filled": 0,
                "remaining": count,
                "attempt": 1,
                "next_retry_ts": time.time() + _retry_delay,
                "strategy": _dc_strategy,
                "original_price": _dc_scan_price,
                "_queue_ts": time.time(),
            })
            logging.info("dc_retry_QUEUED: %s %s no_asks attempt=1/%d next_retry=%.0fs",
                         _dc_strategy, ticker, 1 + DC_IOC_MAX_RETRIES, _retry_delay)
            return None

        # Layer 1: Phantom depth flag — LOG ONLY, never block
        # Depth can appear between our check and the IOC hitting the matching engine.
        # Blocking here would kill real fills. Flag for analysis, submit IOC regardless.
        _phantom_depth = (fresh_depth == 0 and fresh_source == "market_nbbo")
        if _phantom_depth:
            logging.info("dc_taker_PHANTOM_FLAG: %s %s fresh_ask=%d¢ depth=0 source=nbbo — submitting anyway",
                         _dc_strategy, ticker, fresh_ask)

        # Price floor gate: refuse if fresh ask dropped below DC qualifying floor
        if fresh_ask < DECIDED_CONTRACT_MIN_PRICE:
            logging.warning("dc_taker_ABORT_PRICE_BELOW_FLOOR: %s fresh_ask=%d¢ < floor=%d¢ (scan=%d¢)",
                            ticker, fresh_ask, DECIDED_CONTRACT_MIN_PRICE, _dc_scan_price)
            return None

        if fresh_ask != price:
            logging.info("dc_taker_price_update: %s scanner=%d¢ fresh=%d¢ depth=%d src=%s",
                         ticker, price, fresh_ask, fresh_depth, fresh_source)
            price = fresh_ask
            candidate["best_yes_ask"] = fresh_ask
            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
            if net_edge < -0.01:
                logging.info("dc_taker_SKIPPED: %s fresh_ask=%d¢ net_edge=%.4f < -0.01",
                             ticker, price, net_edge)
                return None

        self._session_direct_taker_attempts += 1
        candidate["entry_path"] = "dc_taker"
        candidate["escalation_type"] = "direct_taker"
        self._recent_taker_tickers[ticker] = time.time()

        logging.info(
            "dc_taker_ENTRY: %s %s %dx @ %d¢ "
            "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢ attempt=1/%d",
            _dc_strategy, ticker, count, price,
            seconds_to_close or 0, net_edge, cal_prob, taker_fee, 1 + DC_IOC_MAX_RETRIES)

        _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result = self._submit_taker(candidate)

        if result is not None:
            fill_count = result.get("filled_count", 0)
            remaining = count - fill_count

            if remaining <= 0:
                # Fully filled on first attempt
                self._session_direct_taker_fills += 1
                logging.info("dc_taker_FILLED: %s %s %d/%d", _dc_strategy, ticker, fill_count, count)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
                return result

            # Partial fill — queue retry for remaining
            logging.info("dc_taker_PARTIAL: %s %s filled=%d remaining=%d — queuing retry",
                         _dc_strategy, ticker, fill_count, remaining)
            self._dc_retry_queue.append({
                "candidate": candidate.copy(),
                "original_count": count,
                "total_filled": fill_count,
                "remaining": remaining,
                "attempt": 1,
                "next_retry_ts": time.time() + DC_IOC_RETRY_DELAY,
                "strategy": _dc_strategy,
                "original_price": _dc_scan_price,
                "_queue_ts": time.time(),
                "last_order_submit_ts": _order_submit_ts,
                "last_order_id": result.get("order_id"),
            })
            # Return result so the partial fill is tracked
            self._state.update_evaluated_opportunity_order(
                ticker, order_id=result.get("order_id"),
                order_submitted_at=_order_submit_ts, order_outcome="partial_retry")
            return result
        else:
            # Zero fill — queue retry
            self._session_direct_taker_unfilled += 1
            logging.warning("dc_taker_UNFILLED: %s %s — queuing retry", _dc_strategy, ticker)
            self._dc_retry_queue.append({
                "candidate": candidate.copy(),
                "original_count": count,
                "total_filled": 0,
                "remaining": count,
                "attempt": 1,
                "next_retry_ts": time.time() + DC_IOC_RETRY_DELAY,
                "strategy": _dc_strategy,
                "original_price": _dc_scan_price,
                "_queue_ts": time.time(),
                "last_order_submit_ts": _order_submit_ts,
            })
            self._state.update_evaluated_opportunity_order(
                ticker, order_submitted_at=_order_submit_ts,
                order_outcome="unfilled_retry")
            return None

    def _execute_tm_taker(self, candidate: Dict, asset: str, seconds_to_close) -> Optional[Dict]:
        """Execute terminal momentum trade — direct taker, fixed contracts, no retry."""
        ticker = candidate["ticker"]
        count = candidate["position_size"]  # scan-time: tm_compute_contracts(price, stc, balance)
        price = candidate["best_yes_ask"]
        cal_prob = candidate["calibrated_prob"]

        if count <= 0:
            logging.warning("ORDER_SUPPRESSED zero_size: %s asset=%s strategy=terminal_momentum price=%d",
                            ticker, asset, price)
            self._session_suppressed_zero_size += 1
            return None

        taker_fee = calculate_taker_fee(count, price)
        net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

        # Fresh ask check — verify price hasn't moved outside TM_PRICE_SET
        _tm_scan_price = price
        fresh_ask, fresh_depth, fresh_source = self._dc_get_ask_with_depth(ticker, candidate)

        if fresh_ask is None:
            logging.warning("ORDER_SUPPRESSED no_asks: %s asset=%s strategy=terminal_momentum price=%d stc=%.0f",
                            ticker, asset, price, seconds_to_close or 0)
            self._session_suppressed_no_asks += 1
            return None

        if fresh_ask not in TM_PRICE_SET:
            logging.info("tm_taker_SKIP_PRICE: %s fresh_ask=%d¢ not in TM_PRICE_SET (scan=%d¢)",
                         ticker, fresh_ask, _tm_scan_price)
            return None

        if fresh_ask != price:
            logging.info("tm_taker_price_update: %s scanner=%d¢ fresh=%d¢ depth=%d src=%s",
                         ticker, price, fresh_ask, fresh_depth, fresh_source)
            price = fresh_ask
            candidate["best_yes_ask"] = fresh_ask
            # Re-derive sizing from execution-time price (scan price may have drifted)
            _exec_bal = candidate.get("balance_at_scan") or 100000
            _exec_buf_pct = candidate.get("spot_buffer_pct")
            # Adversary A6: sweep-aware risk cap — sizing against worst-case
            # fill price keeps cents-at-risk within the per-asset cap.
            _exec_risk_price = MAX_ENTRY_PRICE if TM_SWEEP_LIVE_ENABLED else None
            count = tm_compute_contracts(fresh_ask, seconds_to_close or 200,
                                         _exec_bal, asset,
                                         buf_pct=_exec_buf_pct,
                                         risk_cap_price=_exec_risk_price)
            candidate["position_size"] = count
            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

        # Race condition guard: check position at this price level
        _tm_exec_group = f"terminal_momentum_{price}"
        if STACKING_ENABLED:
            from models import strategy_to_group
            if any(p.get("ticker") == ticker
                   and p.get("strategy_group", strategy_to_group(p.get("strategy", "")))
                       == _tm_exec_group
                   for p in self._state.get_open_positions()):
                logging.info("tm_taker_SKIP_POSITION: %s already held at %dc by TM", ticker, price)
                return None
        else:
            if any(p.get("ticker") == ticker for p in self._state.get_open_positions()):
                logging.info("tm_taker_SKIP_POSITION: %s already held", ticker)
                return None

        self._session_direct_taker_attempts += 1
        candidate["entry_path"] = "tm_taker"
        candidate["escalation_type"] = "direct_taker"
        self._recent_taker_tickers[ticker] = time.time()

        logging.info(
            "tm_taker_ENTRY: %s %dx @ %d¢ "
            "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
            ticker, count, price,
            seconds_to_close or 0, net_edge, cal_prob, taker_fee)

        # tm_sweep_shadow: snapshot pre-fill depths at all four TM-relevant
        # tiers BEFORE the IOC. Wrapped to never break the trade path.
        _tmss_pre = self._tm_sweep_snapshot_depths(ticker) if TM_SWEEP_SHADOW_ENABLED else None

        _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result = self._submit_taker(candidate)

        # tm_sweep_shadow: snapshot post-fill depths immediately (option A —
        # models what a real sweep would see firing right after the IOC ack).
        # Insert one row regardless of fill outcome (full/partial/zero).
        #
        # Post-fill depth at the entry tier is RACY: we don't know whether the
        # WS delta from our own fill has landed in the local cache yet (adversary
        # A1). Storing raw is safer than guessing — over- or under-correcting at
        # random is worse than a documented bias. cf_pnl is unaffected because
        # it only consults sweep tiers (98/99), and a single-price IOC at the
        # entry tier cannot consume from higher tiers.
        if TM_SWEEP_SHADOW_ENABLED:
            try:
                _tmss_post = self._tm_sweep_snapshot_depths(ticker)
                # Adversary A2: clamp filled_count to non-negative int so
                # neither None (error sentinel) nor a future negative sentinel
                # corrupts unfilled_count.
                _tmss_raw = result.get("filled_count") if isinstance(result, dict) else 0
                _tmss_filled = max(0, int(_tmss_raw or 0))
                # Adversary R3 A1: read direct-bump status from candidate
                # (set by _submit_taker after its gate ran). Single source
                # of truth — eliminates the predicate-duplication that R2
                # A1 caught (capture over-reporting when picker happens to
                # fire and bump).
                _direct_bump = int(bool(candidate.get("_tm_direct_bump_fired", False)))
                self._state.insert_tm_sweep_shadow_row(
                    ticker=ticker,
                    event_ticker=candidate.get("event_ticker", ""),
                    asset=asset,
                    entry_time=_order_submit_ts,
                    entry_price_cents=int(price),
                    requested_count=int(count),
                    filled_count=_tmss_filled,
                    unfilled_count=int(count) - _tmss_filled,
                    depth_at_entry_pre_fill=fresh_depth,
                    depth_96c_pre=(_tmss_pre or {}).get(96),
                    depth_97c_pre=(_tmss_pre or {}).get(97),
                    depth_98c_pre=(_tmss_pre or {}).get(98),
                    depth_99c_pre=(_tmss_pre or {}).get(99),
                    depth_96c_post=(_tmss_post or {}).get(96),
                    depth_97c_post=(_tmss_post or {}).get(97),
                    depth_98c_post=(_tmss_post or {}).get(98),
                    depth_99c_post=(_tmss_post or {}).get(99),
                    seconds_to_close=seconds_to_close,
                    calibrated_prob=cal_prob,
                    buf_pct=candidate.get("spot_buffer_pct"),
                    best_ask_source=fresh_source,
                    direct_bump_applied=_direct_bump)
            except Exception:
                logging.debug("tm_sweep_shadow capture failed", exc_info=True)

        if result is not None:
            fill_count = result.get("filled_count", 0)
            if fill_count > 0:
                self._session_direct_taker_fills += 1
                logging.info("tm_taker_FILLED: %s %d/%d @ %d¢", ticker, fill_count, count, price)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
                # Telegram alert
                try:
                    _telegram_state._TELEGRAM.send(
                        f"TM: {asset} {fill_count}ct @ {price}c "
                        f"prob={cal_prob:.1%} stc={seconds_to_close or 0:.0f}s "
                        f"edge={net_edge:.2%}",
                        dedup_key=f"tm_{ticker}")
                except Exception:
                    logging.debug("TM telegram alert failed", exc_info=True)
                return result
            else:
                # Zero fill — no retry for TM (next scan cycle will re-evaluate)
                self._session_direct_taker_unfilled += 1
                logging.info("tm_taker_UNFILLED: %s @ %d¢ depth=%d", ticker, price, fresh_depth)
                self._state.update_evaluated_opportunity_order(
                    ticker, order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled")
                return None
        return None

    def _tm_sweep_snapshot_depths(self, ticker: str) -> Optional[Dict[int, int]]:
        """Snapshot YES-ask depths at TM_SWEEP_CAPTURE_TIERS from the scanner's
        cached orderbook. Returns None on any error (caller treats as unknown).
        Never raises — instrumentation must not break the trade path."""
        try:
            scanner = self._ml.scanner if self._ml else None
            if not scanner:
                return None
            ob_data, _ = scanner._get_orderbook_cached(ticker)
            if not ob_data:
                return None
            ladder_json = OrderExecutor._extract_book_levels(ob_data, n=10)
            if not ladder_json:
                return None
            yes_asks = json.loads(ladder_json).get("yes_asks", [])
            return tm_sweep_extract_depths(yes_asks, TM_SWEEP_CAPTURE_TIERS)
        except Exception:
            return None

    def _execute_lpne_taker(self, candidate: Dict, asset: str, seconds_to_close) -> Optional[Dict]:
        """Execute low-price near-expiry trade — direct taker, fixed contracts, no retry.
        BTC 80-87c at STC<=120s. Mirrors _execute_tm_taker with LPNE constants."""
        ticker = candidate["ticker"]
        count = candidate["position_size"]  # LPNE_FIXED_CONTRACTS
        price = candidate["best_yes_ask"]
        cal_prob = candidate["calibrated_prob"]

        if count <= 0:
            logging.warning("ORDER_SUPPRESSED zero_size: %s asset=%s strategy=low_price_near_expiry price=%d",
                            ticker, asset, price)
            self._session_suppressed_zero_size += 1
            return None

        taker_fee = calculate_taker_fee(count, price)
        net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

        # Fresh ask check — verify price hasn't moved outside LPNE range
        _lpne_scan_price = price
        fresh_ask, fresh_depth, fresh_source = self._dc_get_ask_with_depth(ticker, candidate)

        if fresh_ask is None:
            logging.warning("ORDER_SUPPRESSED no_asks: %s asset=%s strategy=low_price_near_expiry price=%d stc=%.0f",
                            ticker, asset, price, seconds_to_close or 0)
            self._session_suppressed_no_asks += 1
            return None

        if not (LPNE_MIN_PRICE <= fresh_ask <= LPNE_MAX_PRICE):
            logging.info("lpne_taker_SKIP_PRICE: %s fresh_ask=%d¢ outside LPNE range %d-%d (scan=%d¢)",
                         ticker, fresh_ask, LPNE_MIN_PRICE, LPNE_MAX_PRICE, _lpne_scan_price)
            return None

        if fresh_ask != price:
            logging.info("lpne_taker_price_update: %s scanner=%d¢ fresh=%d¢ depth=%d src=%s",
                         ticker, price, fresh_ask, fresh_depth, fresh_source)
            price = fresh_ask
            candidate["best_yes_ask"] = fresh_ask
            candidate["position_size"] = LPNE_FIXED_CONTRACTS  # no per-price overrides for LPNE
            count = LPNE_FIXED_CONTRACTS
            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

        # Race condition guard: check position one more time
        if any(p.get("ticker") == ticker for p in self._state.get_open_positions()):
            logging.info("lpne_taker_SKIP_POSITION: %s already held", ticker)
            return None

        self._session_direct_taker_attempts += 1
        candidate["entry_path"] = "lpne_taker"
        candidate["escalation_type"] = "direct_taker"
        self._recent_taker_tickers[ticker] = time.time()

        logging.info(
            "lpne_taker_ENTRY: %s %dx @ %d¢ "
            "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
            ticker, count, price,
            seconds_to_close or 0, net_edge, cal_prob, taker_fee)

        _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result = self._submit_taker(candidate)

        if result is not None:
            fill_count = result.get("filled_count", 0)
            if fill_count > 0:
                self._session_direct_taker_fills += 1
                logging.info("lpne_taker_FILLED: %s %d/%d @ %d¢", ticker, fill_count, count, price)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
                try:
                    _telegram_state._TELEGRAM.send(
                        f"LPNE: {asset} {fill_count}ct @ {price}c "
                        f"prob={cal_prob:.1%} stc={seconds_to_close or 0:.0f}s "
                        f"edge={net_edge:.2%}",
                        dedup_key=f"lpne_{ticker}")
                except Exception:
                    logging.debug("LPNE telegram alert failed", exc_info=True)
                return result
            else:
                self._session_direct_taker_unfilled += 1
                logging.info("lpne_taker_UNFILLED: %s @ %d¢ depth=%d", ticker, price, fresh_depth)
                self._state.update_evaluated_opportunity_order(
                    ticker, order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled")
                return None
        return None

    def _execute_bracket_no_taker(self, candidate: Dict, asset: str, seconds_to_close) -> Optional[Dict]:
        """Execute bracket NO trade — buy NO via IOC taker at computed price."""
        ticker = candidate["ticker"]
        count = candidate["position_size"]  # BRACKET_NO_FIXED_CONTRACTS (5)
        no_cost = candidate["best_yes_ask"]  # NO cost in cents (100 - yes_ask)
        yes_price = candidate.get("_bracket_yes_price", 100 - no_cost)

        if count <= 0:
            logging.warning("ORDER_SUPPRESSED zero_size: %s asset=%s strategy=bracket_no no_cost=%d",
                            ticker, asset, no_cost)
            self._session_suppressed_zero_size += 1
            return None

        # Fresh price check: get current YES ask and re-derive NO cost
        fresh_ask = self._get_addon_best_ask(ticker)
        if fresh_ask is None:
            fresh_ask = self._nbbo_fallback_price(candidate)
        if fresh_ask is not None:
            _fresh_no_cost = 100 - fresh_ask
            if _fresh_no_cost <= 0 or fresh_ask < BRACKET_NO_YES_MIN or fresh_ask > BRACKET_NO_YES_MAX:
                logging.info("bracket_no_SKIP_PRICE: %s fresh_yes=%dc (outside %d-%dc range)",
                             ticker, fresh_ask, BRACKET_NO_YES_MIN, BRACKET_NO_YES_MAX)
                return None
            if _fresh_no_cost != no_cost:
                logging.info("bracket_no_price_update: %s no_cost %dc→%dc (yes %dc→%dc)",
                             ticker, no_cost, _fresh_no_cost, yes_price, fresh_ask)
                no_cost = _fresh_no_cost
                candidate["best_yes_ask"] = no_cost  # Update for _submit_taker
                yes_price = fresh_ask

        # Ceiling check: NO cost must be ≤ 15c (generous margin above 4-12c target)
        if no_cost > 15:
            logging.info("bracket_no_SKIP_EXPENSIVE: %s no_cost=%dc > 15c", ticker, no_cost)
            return None

        # Race condition guard: check position one more time
        if any(p.get("ticker") == ticker for p in self._state.get_open_positions()):
            logging.info("bracket_no_SKIP_POSITION: %s already held", ticker)
            return None

        taker_fee = calculate_taker_fee(count, no_cost)
        net_edge = BRACKET_NO_ASSUMED_PROB - no_cost / 100.0 - taker_fee / (count * 100.0)

        self._session_direct_taker_attempts += 1
        candidate["entry_path"] = "bracket_no_taker"
        candidate["escalation_type"] = "direct_taker"
        self._recent_taker_tickers[ticker] = time.time()

        logging.info(
            "bracket_no_ENTRY: %s %dct NO @ %dc (YES=%dc) "
            "stc=%.0fs edge=%.2f%% assumed_prob=%.0f%% fee=%dc",
            ticker, count, no_cost, yes_price,
            seconds_to_close or 0, net_edge * 100,
            BRACKET_NO_ASSUMED_PROB * 100, taker_fee)

        _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        result = self._submit_taker(candidate)

        if result is not None:
            fill_count = result.get("filled_count", 0)
            if fill_count > 0:
                self._session_direct_taker_fills += 1
                logging.info("bracket_no_FILLED: %s %d/%d NO @ %dc (YES=%dc)",
                             ticker, fill_count, count, no_cost, yes_price)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
                try:
                    _telegram_state._TELEGRAM.send(
                        f"BKT_NO: {ticker} {fill_count}ct NO @ {no_cost}c "
                        f"(YES@{yes_price}c) stc={seconds_to_close or 0:.0f}s",
                        dedup_key=f"bn_{ticker}")
                except Exception:
                    logging.debug("bracket_no telegram alert failed", exc_info=True)
                return result
            else:
                self._session_direct_taker_unfilled += 1
                logging.info("bracket_no_UNFILLED: %s NO @ %dc", ticker, no_cost)
                self._state.update_evaluated_opportunity_order(
                    ticker, order_submitted_at=_order_submit_ts, order_outcome="unfilled")
                return None
        return None

    def process_dc_retries(self):
        """Process queued DC IOC retries. Called at the top of each _tick().

        Non-blocking: each retry is a single IOC submission (<1s).
        Retries are spaced by DC_IOC_RETRY_DELAY (8s) via next_retry_ts.
        """
        if not self._dc_retry_queue:
            return

        now = time.time()
        still_pending = []

        for entry in self._dc_retry_queue:
            if now < entry["next_retry_ts"]:
                still_pending.append(entry)
                continue

            candidate = entry["candidate"]
            ticker = candidate["ticker"]
            _dc_strategy = entry["strategy"]
            attempt = entry["attempt"] + 1
            remaining = entry["remaining"]

            if attempt > 1 + DC_IOC_MAX_RETRIES:
                # Max retries exhausted
                if entry["total_filled"] > 0:
                    logging.info("dc_retry_DONE: %s %s partial_filled=%d/%d after %d attempts",
                                 _dc_strategy, ticker, entry["total_filled"],
                                 entry["original_count"], attempt - 1)
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="partial_filled")
                else:
                    logging.warning("dc_retry_DONE: %s %s unfilled after %d attempts",
                                    _dc_strategy, ticker, attempt - 1)
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="unfilled")
                continue

            # Fresh ask check with depth
            fresh_ask, fresh_depth, fresh_source = self._dc_get_ask_with_depth(ticker, candidate)
            # Coerce None / non-numeric → 0. dict.get only fills default
            # for MISSING key; an explicit None value still passes
            # through, and a JSON-parse string would crash later
            # comparisons. Pre-fix crash: `_dc_retry_delay(None)` raised
            # TypeError on `None > 600`.
            try:
                _stc_now = float(candidate.get("seconds_to_close") or 0)
            except (TypeError, ValueError):
                _stc_now = 0.0
            # Estimate current STC from original eval time
            # _queue_ts is set at every production append site (lines
            # 18833, 18905, 18927). Fallback `now - DC_IOC_RETRY_DELAY`
            # biases toward decay when missing (vs the previous `now`
            # default which kept _eval_age=0 → STC stuck at original
            # → entry could retry forever on a malformed entry). R1 [A5].
            _eval_age = now - entry.get(
                "_queue_ts", now - DC_IOC_RETRY_DELAY)
            if _stc_now and _stc_now > 0:
                _stc_now = max(0, _stc_now - _eval_age)
            _adaptive_delay = self._dc_retry_delay(_stc_now)

            # Apr 26 11:15 incident
            # (kb/failures/dc-retry-post-settlement-burn-2026-04-26.md):
            # candidate fired with STC=5s, hit IOC_ABORT_PHANTOM, queued
            # retry. Retries continued AFTER the 11:15 window close at
            # 1s adaptive delay (=1.0 when STC<30), each hitting
            # phantom + ABORT, burning scan-tick budget across all 11
            # attempts. Once the window has settled, no IOC will fill —
            # drop the entry and stop wasting scan-tick time.
            #
            # Guard: only drop if STC was ORIGINALLY positive AND has
            # decayed to ≤ 0. If `seconds_to_close` was missing or 0
            # at queue time, we cannot bound elapsed → we must NOT
            # drop on STC alone, because the queue's stated purpose
            # (line 18822: "book may appear later") is incompatible
            # with STC-based dropping when STC was never positive to
            # begin with. R1 [A1].
            try:
                _orig_stc = float(
                    candidate.get("seconds_to_close") or 0)
            except (TypeError, ValueError):
                _orig_stc = 0.0
            if _orig_stc > 0 and _stc_now <= 0:
                logging.info(
                    "dc_retry_DROP_WINDOW_CLOSED: %s %s "
                    "orig_stc=%.1fs eval_age=%.1fs (window settled); "
                    "dropping after %d attempts, total_filled=%d/%d",
                    _dc_strategy, ticker, _orig_stc, _eval_age,
                    attempt - 1,
                    entry["total_filled"], entry["original_count"])
                if entry["total_filled"] > 0:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="partial_filled")
                else:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="unfilled_window_closed")
                continue  # drop from queue

            if fresh_ask is None:
                logging.info("dc_retry_no_asks: %s %s attempt=%d/%d",
                             _dc_strategy, ticker, attempt, 1 + DC_IOC_MAX_RETRIES)
                entry["attempt"] = attempt
                entry["next_retry_ts"] = now + _adaptive_delay
                still_pending.append(entry)
                continue

            # Layer 1: Phantom depth flag — LOG ONLY, submit IOC regardless
            _phantom_depth = (fresh_depth == 0 and fresh_source == "market_nbbo")
            if _phantom_depth:
                logging.info("dc_retry_PHANTOM_FLAG: %s %s attempt=%d/%d depth=0 nbbo — submitting anyway",
                             _dc_strategy, ticker, attempt, 1 + DC_IOC_MAX_RETRIES)

            # Price floor gate: abort if ask dropped below DC qualifying floor
            if fresh_ask < DECIDED_CONTRACT_MIN_PRICE:
                _orig_p = entry.get("original_price", 0)
                logging.warning(
                    "dc_retry_ABORT_PRICE_COLLAPSED: %s %s fresh_ask=%d¢ < floor=%d¢ "
                    "(original=%d¢) — dropping from retry queue",
                    _dc_strategy, ticker, fresh_ask, DECIDED_CONTRACT_MIN_PRICE, _orig_p)
                if entry["total_filled"] > 0:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="partial_filled")
                else:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="unfilled_price_collapsed")
                continue  # Drop from queue

            # Price drift gate: abort if ask dropped 3c+ from original signal price
            _orig_price = entry.get("original_price", fresh_ask)
            if fresh_ask < (_orig_price - 3):
                logging.warning(
                    "dc_retry_ABORT_PRICE_DRIFT: %s %s fresh_ask=%d¢ original=%d¢ "
                    "(drift=%d¢) — dropping from retry queue",
                    _dc_strategy, ticker, fresh_ask, _orig_price, _orig_price - fresh_ask)
                if entry["total_filled"] > 0:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="partial_filled")
                else:
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_outcome="unfilled_price_drift")
                continue  # Drop from queue

            # Layer 5: Price tolerance escalation on later retries
            # Retries 0-2: exact price. Retry 3+: widen by 1c per retry, max 3c.
            _retry_num = attempt - 1  # 0-indexed retry count (attempt 2 = retry 1)
            _price_offset = 0
            if _retry_num >= DC_PRICE_TOLERANCE_START_RETRY:
                _price_offset = min(_retry_num - DC_PRICE_TOLERANCE_START_RETRY + 1,
                                    DC_PRICE_TOLERANCE_MAX)

            price = min(fresh_ask + _price_offset, MAX_ENTRY_PRICE)
            candidate["best_yes_ask"] = price
            candidate["position_size"] = remaining
            cal_prob = candidate["calibrated_prob"]

            taker_fee = calculate_taker_fee(remaining, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (remaining * 100.0))
            if net_edge < -0.01:
                logging.info("dc_retry_SKIPPED: %s %s price=%d¢ (ask=%d+%d) net_edge=%.4f attempt=%d",
                             _dc_strategy, ticker, price, fresh_ask, _price_offset, net_edge, attempt)
                continue  # Drop from queue

            self._session_dc_retries += 1
            self._session_direct_taker_attempts += 1
            self._recent_taker_tickers[ticker] = now

            _offset_label = f" (+{_price_offset}c)" if _price_offset > 0 else ""
            logging.info(
                "dc_retry_ENTRY: %s %s %dx @ %d¢%s attempt=%d/%d total_filled=%d/%d depth=%d src=%s",
                _dc_strategy, ticker, remaining, price, _offset_label,
                attempt, 1 + DC_IOC_MAX_RETRIES,
                entry["total_filled"], entry["original_count"],
                fresh_depth, fresh_source)

            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            result = self._submit_taker(candidate)

            if result is not None:
                fill_count = result.get("filled_count", 0)
                entry["total_filled"] += fill_count
                entry["remaining"] -= fill_count
                self._session_dc_retry_fills += 1

                logging.info("dc_retry_FILL: %s %s filled=%d total=%d/%d remaining=%d attempt=%d",
                             _dc_strategy, ticker, fill_count, entry["total_filled"],
                             entry["original_count"], entry["remaining"], attempt)

                if entry["remaining"] <= 0:
                    # Fully filled across retries
                    self._session_direct_taker_fills += 1
                    _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                    self._state.update_evaluated_opportunity_order(
                        ticker, order_id=_taker_oid,
                        order_submitted_at=_order_submit_ts, order_outcome="filled")
                    continue  # Done — don't re-queue

                # Still more remaining — queue another retry
                entry["attempt"] = attempt
                entry["next_retry_ts"] = now + _adaptive_delay
                entry["last_order_submit_ts"] = _order_submit_ts
                entry["last_order_id"] = result.get("order_id")
                still_pending.append(entry)
            else:
                # Zero fill on retry — queue again
                logging.info("dc_retry_UNFILLED: %s %s attempt=%d/%d",
                             _dc_strategy, ticker, attempt, 1 + DC_IOC_MAX_RETRIES)
                entry["attempt"] = attempt
                entry["next_retry_ts"] = now + _adaptive_delay
                entry["last_order_submit_ts"] = _order_submit_ts
                still_pending.append(entry)

        self._dc_retry_queue = still_pending

    # ── Maker ─────────────────────────────────────────────────────────────

    # Hourly series prefixes — maker orders must NEVER be placed on these tickers.
    _HOURLY_SERIES_PREFIXES = ("KXBTCD-", "KXETHD-", "KXSOLD-", "KXXRPD-")

    def _submit_maker(self, candidate: Dict, aggressive: bool = False, degraded: bool = False):
        """Submit maker limit order below fair value.

        Patient: 1-2¢ below fair value (wider spread).
        Aggressive: always 1¢ below (tighter, more likely to fill).
        Degraded: extra offset after post_only rejections (Tier 2).
        """
        ticker = candidate["ticker"]
        # Settlement-race gate — see MIN_ORDER_SUBMIT_STC_S.
        if self._should_skip_near_close(candidate):
            self._abort_near_close(candidate, path="maker")
            return
        # Block maker orders on hourly tickers — hourly must be taker-only (IOC).
        # Belt-and-suspenders: catches any code path that reaches maker with an hourly ticker.
        if any(ticker.startswith(p) for p in self._HOURLY_SERIES_PREFIXES):
            logging.warning("maker_blocked_hourly_ticker: %s — hourly tickers must use IOC only", ticker)
            return
        count = candidate["position_size"]
        fair_value = candidate["best_yes_ask"]
        balance = candidate["balance_at_scan"]

        if aggressive:
            offset = MAKER_PRICE_OFFSET  # always 1¢
        else:
            # 1¢ offset for prices ≥ 90¢, else 2¢
            offset = MAKER_PRICE_OFFSET if fair_value >= 90 else MAKER_PRICE_OFFSET + 1
        price = fair_value - offset
        if degraded:
            price -= POST_ONLY_DEGRADED_EXTRA_OFFSET
        # Per-asset price floor (mirrors scanner check at ~L6560)
        _pt = candidate.get("product_type")
        _asset = candidate.get("asset")
        _mcfg_exec = get_market_config(_pt)
        _floor = _mcfg_exec.min_entry_price
        if _pt in (None, "15m"):
            if _asset == "BTC":
                _floor = BTC_MIN_ENTRY_PRICE
            elif _asset == "ETH":
                _floor = ETH_MIN_ENTRY_PRICE
            elif _asset == "SOL":
                _floor = SOL_MIN_ENTRY_PRICE
            elif _asset == "XRP":
                _floor = XRP_MIN_ENTRY_PRICE
        if price < _floor:
            logging.warning("Maker price %dc below %s floor %dc for %s — skipping",
                            price, _asset, _floor, ticker)
            return

        client_oid = str(uuid.uuid4())

        # Persist before submission
        _side = candidate.get("side", "yes")
        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], _side, count, price, False
        )

        # Submit with post_only to guarantee maker fees (4x cheaper)
        _price_kwarg = {"no_price": price} if _side == "no" else {"yes_price": price}
        resp = self._client.place_order(
            ticker=ticker, side=_side, action="buy",
            count=count, client_order_id=client_oid,
            post_only=True, **_price_kwarg,
        )

        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            self._record_post_only_rejection(ticker)
            # Increment per-ticker api error counter (prevents hot retry loops)
            self._ticker_api_errors[ticker] = self._ticker_api_errors.get(ticker, 0) + 1
            rej_count = self._get_post_only_rejection_count(ticker)
            tier = "degraded" if degraded else "normal"
            logging.warning(
                "Maker order rejected (post_only): %s price=%d¢ tier=%s "
                "rej_count=%d/%d api_errors=%d fair=%d¢",
                ticker, price, tier, rej_count,
                POST_ONLY_MAX_SAME_PRICE + 1,
                self._ticker_api_errors[ticker], fair_value)
            self._session_post_only_rejections += 1
            return

        # Successful submission — reset api error counter for this ticker
        self._ticker_api_errors.pop(ticker, None)
        order_id = (resp.get("order") or {}).get("order_id", client_oid)
        self._state.confirm_order_submitted(client_oid, order_id)
        if self._ml:
            self._ml._session_maker_submissions += 1

        _now = time.time()
        order = {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "side": _side,
            "price_cents": price,
            "count": count,
            "is_taker": False,
            "submit_time": _now,
            "seconds_to_close_at_submit": candidate["seconds_to_close"],
            "candidate": candidate,
            "balance_at_entry": balance,
            "entry_path": "maker",
            "_last_poll": _now,
            "_ask_history": deque(maxlen=30),
            "_last_queue_poll": 0.0,
        }
        self._active_orders[candidate["asset"]] = order

        # Clear rejection tracker on successful maker submission
        self._post_only_rejections.pop(ticker, None)

        self._logger.log_order({
            "action": "maker_submitted",
            "ticker": ticker,
            "order_id": order_id,
            "client_order_id": client_oid,
            "price_cents": price,
            "count": count,
            "fair_value": fair_value,
        })
        tier = "degraded" if degraded else ("aggressive" if aggressive else "patient")
        logging.info(
            f"Maker order: {ticker} {count}x @ {price}¢ "
            f"(fair={fair_value}¢, tier={tier})"
        )

    # ── Taker ─────────────────────────────────────────────────────────────

    def _submit_taker(self, candidate: Dict) -> Optional[Dict]:
        """Submit taker order at best ask. Blocks briefly to verify fill."""
        ticker = candidate["ticker"]
        # Settlement-race gate — see MIN_ORDER_SUBMIT_STC_S.
        if self._should_skip_near_close(candidate):
            self._abort_near_close(candidate, path="taker")
            return None
        count = candidate["position_size"]
        price = candidate["best_yes_ask"]
        balance = candidate["balance_at_scan"]
        # Ladder retries: avoid double-counting session-level IOC
        # metrics. The retry IS a new IOC submission to Kalshi, but
        # for SIGNAL-level metrics it's the same trading signal as
        # the parent. Mirrors the existing 'confirmation_addon'
        # exclusion pattern.
        _is_ladder_retry = bool(candidate.get("_is_ladder_retry"))

        # ── Smart IOC limit picker (Apr 25 2026) ──────────────────────────
        # Kalshi's matching engine fills against ALL ask levels at-or-below
        # our IOC limit. Pre-Apr 23 the bot used NBBO yes_ask (typically
        # wider than orderbook best_ask) — IOCs swept multiple levels and
        # filled 64-82ct on average. The 0ddcaf8 schema fix (Apr 23) made
        # the bot use orderbook best_ask exactly, dropping fills to 33ct
        # because liquidity sat 1-3c above our limit.
        #
        # The picker walks the visible orderbook from best_yes_ask upward,
        # finds the smallest limit price where cumulative fillable depth
        # ≥ position_size. Hard caps:
        #   - max_bump = IOC_LIMIT_MAX_BUMP_CENTS (3c default)
        #   - edge_ceiling = floor(prob*100) - fee_1c - reserve_cents
        #     (per-strategy reserve; default 0 = break-even after fee)
        #   - max_price = MAX_ENTRY_PRICE (99)
        # If picker can't fetch the live orderbook (no scanner ref / WS
        # cache empty), or the ticker is currently WS-drift-flagged
        # (cache untrusted), falls through to original `price` — no
        # regression on broken-cache paths.
        # The bumped limit is computed into `_ioc_limit_price` and used
        # ONLY for the place_order call below. We do NOT mutate
        # candidate["best_yes_ask"] — that stays at the scan-time value
        # for downstream telemetry/audit/post-fill analysis.
        _ioc_limit_price = price  # default to original
        # Hoist _bump_strategy so the TM_SWEEP_DIRECT_BUMP branch below
        # can reference it even when the picker block doesn't enter
        # (orderbook unavailable — the exact case the direct-bump fixes).
        _bump_strategy = candidate.get("strategy") or ""
        # Round 2 [P0-A]: picker is YES-side only. For NO-side
        # candidates (bracket_no, hourly_no_live, weather_no_live,
        # dc_shadow_no_side), `candidate["best_yes_ask"]` is set
        # to no_price — feeding it to the YES-side ladder walker
        # produces meaningless results and the bumped price gets
        # submitted as no_price, potentially overpaying. Bypass.
        _is_no_side = candidate.get("side") == "no"
        try:
            _scanner = self._ml.scanner if self._ml else None
            _live_ob = None
            # Bypass picker on drift-flagged tickers — the WS cache
            # the picker reads is the same one that's been wrong
            # (WS_DRIFT_AUTO_FLAG). Don't bump based on phantom data.
            _is_drift_flagged = (
                _scanner is not None
                and ticker in getattr(_scanner, "_ws_drift_cooldown", {}))
            if (_scanner is not None
                    and not _is_drift_flagged
                    and not _is_no_side):
                try:
                    _live_ob, _src = _scanner._get_orderbook_cached(ticker)
                except Exception:
                    _live_ob = None
            if _live_ob and isinstance(price, int) and price > 0:
                _cal_prob = candidate.get("calibrated_prob")
                if _cal_prob is not None and 0 < _cal_prob < 1:
                    _fee_1c = calculate_taker_fee(1, price)
                    # Per-strategy edge reserve — see
                    # STRATEGY_LIMIT_BUMP_RESERVE_CENTS in bot/_impl.py
                    # constants. Default (B) is 0 (break-even after
                    # fee). High-conviction strategies (DC tiers,
                    # addons) override to -1 (tolerate fee-cost on
                    # worst-fill margin).
                    # _bump_strategy hoisted above (used by the TM direct-bump
                    # branch outside this picker block).
                    _reserve_cents = STRATEGY_LIMIT_BUMP_RESERVE_CENTS.get(
                        _bump_strategy,
                        STRATEGY_LIMIT_BUMP_DEFAULT_RESERVE)
                    _edge_ceiling = (
                        int(_cal_prob * 100) - _fee_1c - _reserve_cents)
                    # TM Sweep Live: override edge_ceiling for the exact
                    # terminal_momentum tiers in TM_LIVE_STRATEGIES so the
                    # picker can bump up to MAX_ENTRY_PRICE (99c).
                    #
                    # Ladder-retry guard (adversary A4): the override does
                    # NOT apply on _is_ladder_retry candidates. The ladder
                    # escalation already does limit+1; compounding it with
                    # a fresh smart-picker bump is untested and could
                    # produce IOCs at price levels neither path validated.
                    if (TM_SWEEP_LIVE_ENABLED
                            and _bump_strategy in TM_LIVE_STRATEGIES
                            and not _is_ladder_retry):
                        _edge_ceiling = MAX_ENTRY_PRICE
                    _smart_limit = OrderExecutor._pick_ioc_limit_for_depth(
                        _live_ob,
                        best_yes_ask=int(price),
                        target_qty=int(count),
                        max_bump_cents=IOC_LIMIT_MAX_BUMP_CENTS,
                        edge_ceiling_price=_edge_ceiling,
                        max_price=MAX_ENTRY_PRICE,
                    )
                    if _smart_limit > price:
                        logging.info(
                            "IOC_LIMIT_BUMPED: %s %d→%d¢ "
                            "(target_qty=%d, prob=%.4f, "
                            "edge_ceiling=%d, max_bump=%d, "
                            "reserve=%+d strategy=%s) — sweeping "
                            "deeper levels",
                            ticker, price, _smart_limit,
                            count, _cal_prob, _edge_ceiling,
                            IOC_LIMIT_MAX_BUMP_CENTS, _reserve_cents,
                            _bump_strategy)
                        _ioc_limit_price = _smart_limit
                    elif _smart_limit == price:
                        logging.debug(
                            "IOC_LIMIT_AT_BEST: %s %d¢ (no bump needed "
                            "or no benefit within caps)",
                            ticker, price)
            elif _is_drift_flagged:
                logging.debug(
                    "IOC_LIMIT_PICKER_BYPASS: %s — ws_drift_cooldown "
                    "active; using scan-time best_ask=%d¢ unchanged",
                    ticker, price)
            elif _is_no_side:
                logging.debug(
                    "IOC_LIMIT_PICKER_BYPASS: %s — NO-side IOC "
                    "(picker is YES-side only); using "
                    "scan-time price=%d¢ unchanged",
                    ticker, price)
        except Exception:
            logging.warning(
                "smart_ioc_limit_picker failed", exc_info=True)
        # ── TM Sweep Live: direct limit bump (no orderbook required) ─────
        # The smart picker above is gated on _live_ob from the WS cache
        # and has no REST fallback. TM tickers consistently lack fresh WS
        # cache when TM fires (488/488 production rows showed
        # best_ask_source='market_nbbo' as of 2026-04-28), so the picker
        # silently no-ops for TM and the edge_ceiling override never runs.
        # This branch sets the limit directly when the sweep gates are
        # satisfied — independent of orderbook availability. Kalshi
        # auto-cancels surplus at $0 on unfilled IOC tail.
        # Conditions:
        #   - TM_SWEEP_LIVE_ENABLED env-var-gated kill switch
        #   - exact-set strategy match (no startswith footgun)
        #   - not a ladder retry (don't compound with +1c escalation)
        #   - not _is_no_side (NO-side `best_yes_ask` is no_price; bumping
        #     would submit a 99c NO buy = ~$1/contract overpay; matches
        #     the picker's NO-side bypass)
        #   - price < MAX_ENTRY_PRICE (no bump possible at 99c entry)
        #   - _ioc_limit_price < MAX_ENTRY_PRICE (don't lower a higher
        #     picker-chosen limit on the rare path where picker did fire)
        # Adversary R3 A1: write the gate result to the candidate dict so
        # the shadow capture in _execute_tm_taker reads from a single
        # source of truth — eliminating the predicate-duplication
        # fragility that R2 caught.
        _direct_bump_fired = (
            TM_SWEEP_LIVE_ENABLED
            and _bump_strategy in TM_LIVE_STRATEGIES
            and not _is_ladder_retry
            and not _is_no_side
            and isinstance(price, int)
            and price < MAX_ENTRY_PRICE
            and _ioc_limit_price < MAX_ENTRY_PRICE)
        candidate["_tm_direct_bump_fired"] = _direct_bump_fired
        if _direct_bump_fired:
            # Adversary R2 A3: assign FIRST, log AFTER — log records fact,
            # not intent. A logging handler exception between the log call
            # and the assignment would have created a misleading audit
            # trail (claiming bump while actually submitting scan-time price).
            _prev_limit = _ioc_limit_price
            _ioc_limit_price = MAX_ENTRY_PRICE
            logging.info(
                "TM_SWEEP_DIRECT_BUMP: %s %d→%d¢ strategy=%s "
                "(picker bypassed; orderbook unavailable for TM)",
                ticker, _prev_limit, _ioc_limit_price, _bump_strategy)
        # `_ioc_limit_price` is the actual price submitted to Kalshi.
        # `price` and `candidate["best_yes_ask"]` remain at scan-time
        # values for the downstream drift-check + PHANTOM_ABORT logic
        # and for telemetry/audit.

        # ── Option X v2: per-strategy IOC clamp (Apr 24) ──────────────────
        # Kalshi IOC matches at BEST available price up to our limit,
        # sweeping through the ladder. Variant B (sub-floor phantom-ask
        # sweep) can hand us sub-floor fills when top-of-book is thin and
        # real asks sit way below. Original Option X (Apr 15) clamped ALL
        # orderbook-source IOCs to top-of-book depth to prevent this.
        #
        # Post-WS-fix (0ddcaf8, Apr 23), top-of-book on TM_99 markets is
        # routinely 1-2ct. Pre-fix the clamp was silently bypassed because
        # ob_data came up empty (schema drift) → best_ask_source='market_nbbo'
        # → blind IOC path. Now that orderbook is real, the clamp fires
        # correctly but at a thin level, killing strategies that previously
        # benefited from blind-firing into Kalshi's real book.
        #
        # v2 picks the clamp policy per-strategy (STRATEGY_CLAMP_POLICY):
        #   top_of_book — cap count at quoted best ask depth. Preserves
        #     Variant B protection for sub-floor-risk paths.
        #   no_clamp    — submit full Kelly count. Kalshi IOC auto-cancels
        #     the unfilled remainder ($0 charge), so oversizing is free.
        #     PHANTOM_ABORT (ask_depth=0) still fires as the catastrophic
        #     tail guard. Correct for ceiling-triggered strategies (TM) and
        #     floor-triggered discounts with empirically-zero sweep tail.
        # See kb/decisions/no-floor-relaxation-on-ws-fix.md.
        _ask_src = candidate.get("best_ask_source")
        _ob_snap = candidate.get("ob_snapshot") or {}
        _ask_depth = _ob_snap.get("ask_depth")
        _strategy = candidate.get("strategy") or ""
        _policy = STRATEGY_CLAMP_POLICY.get(_strategy, STRATEGY_CLAMP_DEFAULT)

        # WS cache drift defense: when cache claims non-trivial depth, verify
        # against a fresh REST /orderbook fetch. If REST materially disagrees,
        # prefer REST as authoritative. This is a data-layer correction (not
        # a policy change); a confirmed drift means the book really IS thin
        # regardless of strategy policy, so we clamp even on no_clamp paths.
        # If cache was already thin (< threshold) OR REST agrees, no change.
        # See kb/failures/kalshi-ws-schema-drift.md § "WS delta underflow".
        # Apr 25 2026: clamp uses windowed peak (not single REST sample) for
        # the size decision; PHANTOM_ABORT uses fresh sample so a real-time
        # empty book is still caught regardless of historical peak.
        _drift_corrected = False
        _rest_fresh = None  # for PHANTOM_ABORT (Round 1 P0 #2)
        if (IOC_DRIFT_CHECK_ENABLED
                and _ask_src == "orderbook"
                and isinstance(_ask_depth, int)
                and _ask_depth >= IOC_DRIFT_CHECK_MIN_CACHED_DEPTH):
            # Smoothed helper returns (peak, fresh):
            #   peak — windowed-max for the size clamp (anti-flicker)
            #   fresh — most-recent REST sample for PHANTOM_ABORT
            # See IOC_DRIFT_CHECK_REST_WINDOW_S. A single REST call is
            # volatile — WS_DRIFT_PROBE_REST_STABILITY shows two
            # back-to-back calls disagreeing by hundreds of contracts.
            # Apr 25 2026: single-sample clamp caused a 65% drop in
            # position size across all assets.
            _rest_peak, _rest_fresh = self._rest_best_ask_depth_smoothed(ticker)
            # Round 2 [A1] cold-start gate: if the buffer has <2
            # samples in the window, peak ≈ fresh and the "smoothing"
            # degenerates to single-sample clamping — the exact
            # pre-fix bug. Skip the divergence/clamp branch on cold
            # start and let the existing policy
            # (cached _ask_depth + STRATEGY_CLAMP_POLICY) handle it.
            # _rest_fresh stays exposed for PHANTOM_ABORT below.
            _sample_count = self._rest_depth_window_count(ticker)
            _cold_start = (
                _sample_count
                < OrderExecutor._REST_DEPTH_MIN_SAMPLES_FOR_CLAMP)
            if _rest_peak is not None and not _cold_start:
                if _rest_peak < _ask_depth * IOC_DRIFT_CHECK_DIVERGENCE_RATIO:
                    logging.warning(
                        "IOC_CACHE_DRIFT: %s %dc ws_cache=%d rest_peak=%d "
                        "rest_fresh=%s (ratio=%.2f, samples=%d) "
                        "strategy=%s policy=%s "
                        "— using REST peak as authoritative",
                        ticker, price, _ask_depth, _rest_peak,
                        ("?" if _rest_fresh is None else str(_rest_fresh)),
                        _rest_peak / max(_ask_depth, 1), _sample_count,
                        _strategy, _policy)
                    _ask_depth = _rest_peak  # authoritative for clamp logic below
                    _drift_corrected = True
                else:
                    logging.info(
                        "IOC_CACHE_OK: %s %dc ws_cache=%d rest_peak=%d "
                        "rest_fresh=%s (samples=%d, strategy=%s)",
                        ticker, price, _ask_depth, _rest_peak,
                        ("?" if _rest_fresh is None else str(_rest_fresh)),
                        _sample_count, _strategy)
            elif _rest_peak is not None and _cold_start:
                # Cold-start: smoothed-peak gate isn't authoritative yet
                # (samples < _REST_DEPTH_MIN_SAMPLES_FOR_CLAMP). Default
                # behavior is to fall through to cached-depth policy.
                # ESCAPE HATCH (Apr 25 2026): if the single fresh REST
                # sample shows CATASTROPHIC divergence from cache
                # (≥10× drift, IOC_DRIFT_CHECK_COLD_START_RATIO), apply
                # the clamp anyway. Single-sample flicker risk is real
                # but bounded by the strict ratio; the alternative is
                # what we just measured — Kelly-size IOCs into 1ct books
                # producing 50+ micro-fills/day across freshly-discovered
                # tickers (~16/hr, all hit cold-start path).
                if (_rest_peak
                        < _ask_depth * IOC_DRIFT_CHECK_COLD_START_RATIO):
                    logging.warning(
                        "IOC_CACHE_DRIFT_COLD: %s %dc ws_cache=%d "
                        "rest_fresh=%d ratio=%.3f (samples=%d, "
                        "threshold=%.2f) strategy=%s policy=%s — "
                        "catastrophic drift on cold-start; using REST "
                        "as authoritative",
                        ticker, price, _ask_depth, _rest_peak,
                        _rest_peak / max(_ask_depth, 1), _sample_count,
                        IOC_DRIFT_CHECK_COLD_START_RATIO,
                        _strategy, _policy)
                    _ask_depth = _rest_peak
                    _drift_corrected = True
                else:
                    # Cold-start with non-catastrophic divergence: fall
                    # through to cached policy. Log once for diagnostics
                    # — this branch hits constantly for newly-discovered
                    # 15M tickers, so use INFO not WARNING to avoid log
                    # spam.
                    logging.info(
                        "IOC_CACHE_COLD_START: %s %dc ws_cache=%d "
                        "rest_fresh=%s (samples=%d < %d, ratio=%.3f) "
                        "— falling through to cached-depth policy",
                        ticker, price, _ask_depth,
                        ("?" if _rest_fresh is None else str(_rest_fresh)),
                        _sample_count,
                        OrderExecutor._REST_DEPTH_MIN_SAMPLES_FOR_CLAMP,
                        _rest_peak / max(_ask_depth, 1))

        if _ask_src == "orderbook" and isinstance(_ask_depth, int):
            # Catastrophic tail guard — fires regardless of policy. Uses
            # BOTH the drift-corrected _ask_depth (cached/peak) AND the
            # fresh REST sample. Fresh==0 means the book is empty RIGHT
            # NOW, regardless of any historical peak — the smoothing
            # window must not mask this signal. Round 1 P0 #2.
            if _ask_depth == 0 or _rest_fresh == 0:
                _abort_reason = (
                    "rest_fresh=0" if _rest_fresh == 0 else
                    "ask_depth=0 (orderbook-confirmed)")
                logging.warning(
                    "IOC_ABORT_PHANTOM: %s %dc count=%d %s "
                    "strategy=%s policy=%s — refusing IOC to prevent "
                    "ladder sweep",
                    ticker, price, count, _abort_reason, _strategy, _policy)
                if not _is_ladder_retry:
                    self._session_ioc_unfilled += 1
                return None
            if _policy == "no_clamp":
                # Default no_clamp: submit full Kelly, trust Kalshi auto-cancel.
                # Exception: if drift check corrected depth downward, clamp to
                # the REST-verified depth — that's a data-correctness override.
                if _drift_corrected and _ask_depth < count:
                    if _ask_depth < IOC_MIN_COUNT_AFTER_CLAMP:
                        logging.warning(
                            "IOC_ABORT_THIN_CLAMP: %s %dc rest_depth=%d < min=%d "
                            "(policy=no_clamp + drift, original_count=%d, asset=%s, strategy=%s) "
                            "— book genuinely thin, skipping; next scan retries after cooldown",
                            ticker, price, _ask_depth, IOC_MIN_COUNT_AFTER_CLAMP,
                            count, candidate.get("asset", "?"), _strategy)
                        if not _is_ladder_retry:
                            self._session_ioc_unfilled += 1
                        return None
                    logging.warning(
                        "IOC_DRIFT_CLAMP: %s %dc count %d -> %d "
                        "(policy=no_clamp + REST drift correction, asset=%s, strategy=%s)",
                        ticker, price, count, _ask_depth,
                        candidate.get("asset", "?"), _strategy)
                    count = _ask_depth
                    candidate["position_size"] = count
                else:
                    logging.info(
                        "IOC_NO_CLAMP: %s %dc count=%d (cached_depth=%d, asset=%s, strategy=%s)",
                        ticker, price, count, _ask_depth,
                        candidate.get("asset", "?"), _strategy)
            else:
                # top_of_book (default, conservative). Clamp to verified depth
                # (drift-corrected if REST fired, else cached) to prevent
                # ladder sweeps into sub-floor prices.
                if _ask_depth < count:
                    logging.warning(
                        "IOC_SIZE_CLAMP: %s %dc count %d -> %d "
                        "(policy=top_of_book, verified_depth=%d, drift=%s, asset=%s, strategy=%s)",
                        ticker, price, count, _ask_depth, _ask_depth,
                        _drift_corrected, candidate.get("asset", "?"), _strategy)
                    count = _ask_depth
                    candidate["position_size"] = count
        elif _ask_src == "market_nbbo":
            # NBBO blind path: no orderbook data → no depth signal,
            # no PHANTOM_ABORT possible. Round 2 [A2] / Round 3 [A4]
            # known scope limit — the smoothed-clamp + fresh-zero
            # phantom guard only protects orderbook-source IOCs.
            # If NBBO blind becomes the dominant path again (it was
            # pre-WS-fix), a separate defense is needed here.
            logging.info(
                "IOC_BLIND_SUBMIT: %s %dc count=%d asset=%s strategy=%s (NBBO fallback — no depth)",
                ticker, price, count, candidate.get("asset", "?"), _strategy)
        # ── end Option X v2 ───────────────────────────────────────────────

        client_oid = str(uuid.uuid4())

        # Persist before submission. price_cents records the LIMIT
        # actually submitted to Kalshi (= _ioc_limit_price), not the
        # scan-time best_ask. Round 2 [P1-B]: audit trail must
        # reflect what was actually sent.
        _side = candidate.get("side", "yes")
        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], _side, count, _ioc_limit_price, True
        )

        # F/U TM_99 zero-fill diagnostic (Apr 26): pre-IOC ladder
        # snapshot. Pairs with post-IOC snapshot below (after place_order
        # returns) to discriminate HYP A (Kalshi only matches yes_asks
        # ladder — no_bid stays unchanged on fail) from HYP B (Kalshi
        # matches both, no_bid sniped before our IOC arrives — no_bid
        # qty drops between pre and post). R-review [A1] fix.
        # See tests/test_ioc_submit_ladder_diag.py.
        _diag_pre = None
        try:
            _diag_pre = OrderExecutor._compute_ladder_diag(_live_ob)
        except Exception:
            logging.debug("IOC_SUBMIT_LADDER_DIAG pre failed", exc_info=True)

        # Submit as IOC — exchange auto-cancels any unfilled remainder.
        # Uses _ioc_limit_price (smart picker output) for the actual
        # exchange submission, while `price` and candidate["best_yes_ask"]
        # remain at scan-time values for telemetry/audit/drift-check.
        _price_kwarg = (
            {"no_price": _ioc_limit_price} if _side == "no"
            else {"yes_price": _ioc_limit_price})
        resp = self._client.place_order(
            ticker=ticker, side=_side, action="buy",
            count=count, client_order_id=client_oid,
            time_in_force="immediate_or_cancel", **_price_kwarg,
        )

        # F/U TM_99 zero-fill diagnostic — post-IOC snapshot + outcome.
        # Includes fill_count so the divergence pattern can be
        # correlated with fill outcome via single-line grep
        # (R-review [A5]). Re-fetches the cached orderbook so we
        # observe post-fill state (WS push from Kalshi typically
        # arrives within ms of fill). If no_bid_qty dropped between
        # pre and post, the IOC matched against the no_bid → HYP B.
        # If no_bid_qty unchanged AND fill_count==0, our 99c bid
        # never reached the no_bid → HYP A.
        try:
            _diag_post = None
            if _scanner is not None:
                try:
                    _live_ob_post, _ = _scanner._get_orderbook_cached(ticker)
                    _diag_post = OrderExecutor._compute_ladder_diag(_live_ob_post)
                except Exception:
                    pass
            _fill_ct = 0
            if resp is not None:
                _fill_ct = (
                    fp_str_to_int(
                        (resp.get("order") or {}).get("fill_count_fp"))
                    or ((resp.get("order") or {}).get("fill_count") or 0)
                )
            _pre = _diag_pre or {}
            _post = _diag_post or {}
            logging.info(
                "IOC_SUBMIT_LADDER_DIAG: %s asset=%s strategy=%s "
                "bid=%d req=%d fill=%d "
                "PRE: yes=%s/%d no_bid=%s/%d cross=%s "
                "diverges=%s one_side_empty=%s "
                "POST: yes=%s/%d no_bid=%s/%d",
                ticker, candidate.get("asset", "?"),
                candidate.get("strategy", "?"),
                _ioc_limit_price, count, _fill_ct,
                _pre.get("yes_ask_top_price"), _pre.get("yes_ask_top_qty", 0),
                _pre.get("no_bid_top_price"), _pre.get("no_bid_top_qty", 0),
                _pre.get("cross_side_ask"),
                _pre.get("diverges"), _pre.get("one_side_empty"),
                _post.get("yes_ask_top_price"), _post.get("yes_ask_top_qty", 0),
                _post.get("no_bid_top_price"), _post.get("no_bid_top_qty", 0))
        except Exception:
            logging.debug("IOC_SUBMIT_LADDER_DIAG post failed", exc_info=True)

        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            self._ticker_api_errors[ticker] = self._ticker_api_errors.get(ticker, 0) + 1
            logging.error("Taker order submission failed: %s (api_errors=%d)",
                          ticker, self._ticker_api_errors[ticker])
            if (candidate.get("entry_path") != "confirmation_addon"
                    and not _is_ladder_retry):
                self._session_ioc_unfilled += 1
            return None

        # Successful submission — reset api error counter
        self._ticker_api_errors.pop(ticker, None)
        order_id = (resp.get("order") or {}).get("order_id", client_oid)
        remaining_count = (resp.get("order") or {}).get("remaining_count", count)
        _order_fill_count = fp_str_to_int((resp.get("order") or {}).get("fill_count_fp")) or (
            (resp.get("order") or {}).get("fill_count") or 0)
        self._state.confirm_order_submitted(client_oid, order_id)

        order_info = {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "side": _side,
            # `price_cents` = limit actually submitted to Kalshi
            # (post smart-picker bump, if any). Round 2 [P1-B].
            "price_cents": _ioc_limit_price,
            "scan_time_best_ask": price,  # original for forensics
            "count": count,
            "is_taker": True,
            "submit_time": time.time(),
            "seconds_to_close_at_submit": candidate["seconds_to_close"],
            "candidate": candidate,
            "balance_at_entry": balance,
            "execution_method": "ioc",
            "entry_path": candidate.get("entry_path", "direct_taker"),
        }

        self._logger.log_order({
            "action": "taker_submitted",
            "ticker": ticker,
            "order_id": order_id,
            "client_order_id": client_oid,
            "price_cents": _ioc_limit_price,
            "scan_time_best_ask": price,
            "count": count,
            "time_in_force": "ioc",
        })
        logging.info(
            "Taker IOC order: %s %dx @ %d¢%s",
            ticker, count, _ioc_limit_price,
            (f" (bumped from {price}¢)"
             if _ioc_limit_price != price else ""))

        # Lifecycle snapshot at IOC submit — captures the book the order
        # was sent into. Pairs with fill snapshots in _on_fill so we can
        # later answer "was the 1ct stub the entire book at submit time
        # or did it shrink between scan and submit?"
        try:
            self._state.insert_order_lifecycle_snapshot(
                order_id=order_id, ticker=ticker, event_type="submit",
                source=candidate.get("strategy"))
        except Exception:
            logging.warning("insert_order_lifecycle_snapshot (submit) failed",
                            exc_info=True)

        # IOC resolves instantly; brief wait + collect ALL fill events.
        # An IOC can match against multiple resting orders, generating
        # multiple fill events.  _check_for_fill() returns one unseen
        # fill per call (tracks seen IDs), so loop until exhausted.
        time.sleep(0.3)
        total_filled = 0
        while True:
            fill = self._check_for_fill(order_info)
            if not fill:
                break
            fill_count = self._on_fill(fill, order_info)
            total_filled += fill_count

        # Second poll pass: catch late fills that arrived after initial 0.3s
        if total_filled > 0:
            time.sleep(0.5)
            while True:
                fill = self._check_for_fill(order_info)
                if not fill:
                    break
                fill_count = self._on_fill(fill, order_info)
                total_filled += fill_count

        if total_filled > 0:
            if (candidate.get("entry_path") != "confirmation_addon"
                    and not _is_ladder_retry):
                self._session_ioc_fills += 1
            unfilled = count - total_filled
            logging.info(
                f"ioc_taker_result: {ticker} filled={total_filled} "
                f"remaining={unfilled}"
                f"{'' if unfilled == 0 else ' [PARTIAL]'}")
            if unfilled > 0:
                logging.warning(
                    f"IOC partial fill: {ticker} wanted {count} got "
                    f"{total_filled} — {unfilled} contracts unfilled")
                # Ladder escalation: actively retry once at +1¢ for the
                # remainder. Runs BEFORE the maker tail so coexistence
                # is layered: active reach first, passive rest second.
                # If escalation fully fills, the maker tail's MIN_
                # REMAINDER gate naturally suppresses the tail. If
                # escalation also partials, fall through to maker tail
                # at ORIGINAL price (where someone may return to).
                # Failures here MUST NOT break the IOC return path.
                _ladder_filled = 0
                if LADDER_ESCALATION_ENABLED:
                    try:
                        _ladder_result = self._maybe_ladder_escalate(
                            candidate=candidate,
                            original_limit=_ioc_limit_price,
                            remaining=unfilled,
                            ioc_filled=total_filled)
                        _ladder_filled = (
                            _ladder_result.get("escalated_filled", 0))
                        unfilled -= _ladder_filled
                    except Exception:
                        logging.warning(
                            "_maybe_ladder_escalate raised; IOC "
                            "result still returned to caller",
                            exc_info=True)
                # Maker-tail: post the unfilled remainder as a
                # post_only GTC limit so benign rotation flow can
                # still fill us. Eligibility, gates, caps all live in
                # _maybe_post_maker_tail. Failures here MUST NOT break
                # the IOC return path — this is a strict additive
                # behavior on top of the IOC outcome.
                if MAKER_TAIL_AFTER_IOC_PARTIAL:
                    try:
                        self._maybe_post_maker_tail(
                            candidate=candidate,
                            ioc_price=_ioc_limit_price,
                            remaining=unfilled,
                            ioc_filled=total_filled)
                    except Exception:
                        logging.warning(
                            "_maybe_post_maker_tail raised; IOC "
                            "result still returned to caller",
                            exc_info=True)
            order_info["filled_count"] = total_filled
            return order_info

        # ── Ghost fill detection (Layer A): remaining_count from order response ──
        # Kalshi's matching engine returns remaining_count=0 when the order was
        # fully matched. If fill polling found nothing, the fills API has latency
        # but the contracts DO exist. Register a defensive position at the limit
        # price (conservative — actual fills are ≤ limit). Reconciliation at next
        # startup will correct prices from Kalshi's positions API.
        #
        # CRITICAL: For IOC orders, remaining_count=0 can also mean the order was
        # auto-canceled with zero fills. Must verify fill_count > 0 from the order
        # response to distinguish real ghost fills from unfilled IOC cancellations.
        # (Bug: false ghost fill on KXSOL15M-26MAR061400-00 cost -$39.16, Mar 6 2026)
        if remaining_count == 0 and _order_fill_count > 0:
            logging.error(
                f"GHOST_FILL_DETECTED: {ticker} remaining_count=0 but no fill "
                f"events from API — Kalshi matched all {count} contracts. "
                f"Registering defensive position at limit price {price}¢")
            self._state.record_position_from_fill(
                ticker=ticker,
                event_ticker=candidate["event_ticker"],
                asset=candidate["asset"],
                side=_side,
                count=count,
                price_cents=price,
                strategy=candidate.get("strategy"),
                seconds_to_close=order_info.get("seconds_to_close_at_submit"),
                fill_latency=round(time.time() - order_info["submit_time"], 3),
                vol_regime=candidate.get("vol_regime"),
                calibrated_prob=candidate.get("calibrated_prob"),
                edge=candidate.get("edge"),
                kelly_f=candidate.get("kelly_f"),
                is_taker=True,
                fill_source="ghost_fill",
                execution_method="ioc",
                escalation_type=candidate.get("escalation_type"),
                maker_price_cents=candidate.get("maker_price_cents"),
                maker_wait_seconds=candidate.get("maker_wait_seconds"),
            )
            self._state.mark_order_status(order_id, "filled")
            if (candidate.get("entry_path") != "confirmation_addon"
                    and not _is_ladder_retry):
                self._session_ioc_fills += 1
            order_info["filled_count"] = count  # Ghost fill = assumed full fill
            return order_info

        # remaining_count=0 but fill_count=0: IOC was auto-canceled, not a ghost fill
        if remaining_count == 0 and _order_fill_count == 0:
            logging.info(
                f"IOC_CANCELED_NO_FILLS: {ticker} remaining_count=0 "
                f"fill_count=0 — order was canceled unfilled, not a ghost fill")

        # ── Ghost fill detection (Layer B): positions API verification ──
        # remaining_count > 0 suggests genuinely unfilled, but verify against
        # Kalshi's positions API in case of any untracked position.
        try:
            _pos_resp = self._client.get_positions()
            if _pos_resp and _pos_resp.get("market_positions"):
                for _pos in _pos_resp["market_positions"]:
                    if _pos.get("ticker") == ticker:
                        _pos_count = fp_str_to_int(_pos.get("position_fp")) or (_pos.get("position") or 0)
                        if _pos_count != 0:
                            _ghost_side = "yes" if _pos_count > 0 else "no"
                            _pos_abs = abs(_pos_count)
                            _pos_cost_d = _pos.get("market_exposure_dollars")
                            _pos_cost = dollars_str_to_cents(_pos_cost_d) if _pos_cost_d else (_pos.get("market_exposure") or 0)
                            _pos_avg = _pos_cost // _pos_abs if _pos_abs else price
                            logging.error(
                                f"GHOST_FILL_DETECTED_VIA_POSITIONS: {ticker} side={_ghost_side} "
                                f"fill polling found nothing, remaining_count={remaining_count}, "
                                f"but positions API shows {_pos_count} contracts "
                                f"(cost={_pos_cost}¢, avg={_pos_avg}¢)")
                            self._state.record_position_from_fill(
                                ticker=ticker,
                                event_ticker=candidate["event_ticker"],
                                asset=candidate["asset"],
                                side=_ghost_side,
                                count=_pos_abs,
                                price_cents=_pos_avg,
                                strategy=candidate.get("strategy"),
                                seconds_to_close=order_info.get("seconds_to_close_at_submit"),
                                fill_latency=round(time.time() - order_info["submit_time"], 3),
                                vol_regime=candidate.get("vol_regime"),
                                calibrated_prob=candidate.get("calibrated_prob"),
                                edge=candidate.get("edge"),
                                kelly_f=candidate.get("kelly_f"),
                                is_taker=True,
                                fill_source="ghost_fill_positions_api",
                                execution_method="ioc",
                                escalation_type=candidate.get("escalation_type"),
                                maker_price_cents=candidate.get("maker_price_cents"),
                                maker_wait_seconds=candidate.get("maker_wait_seconds"),
                            )
                            self._state.mark_order_status(order_id, "filled")
                            if (candidate.get("entry_path") != "confirmation_addon"
                                    and not _is_ladder_retry):
                                self._session_ioc_fills += 1
                            order_info["filled_count"] = _pos_abs  # Ghost fill from positions API
                            return order_info
        except Exception as e:
            logging.warning(f"Ghost fill positions API check failed for {ticker}: {e}")

        # IOC auto-cancels unfilled portion — no manual cancel needed
        self._state.mark_order_status(order_id, "canceled")
        if (candidate.get("entry_path") != "confirmation_addon"
                and not _is_ladder_retry):
            self._session_ioc_unfilled += 1
        self._logger.log_order({
            "action": "taker_ioc_unfilled",
            "ticker": ticker,
            "order_id": order_id,
            "remaining_count": remaining_count,
        })
        logging.warning(f"Taker IOC not filled: {ticker} (remaining={remaining_count})")
        return None

    # ── Maker-tail-after-IOC-partial ──────────────────────────────────────
    # Apr 25 2026: when IOC fills 9 of 50 because top of book is thin,
    # the unfilled 41 used to die on cancel ($0 EV). For high-conviction
    # strategies (DC tiers + TM-99/-98 + weekend/overnight discounts),
    # leaving a post_only=True GTC limit at the IOC price for a short TTL
    # gives benign rotation flow a chance to fill the remainder, with
    # adverse selection as the offsetting risk. Maker fee = $0, so the
    # only cost is escrowed capital + adverse-selection PnL.
    # Caps + min STC + min remainder bound the worst case.
    # See kb/decisions/maker-tail-after-ioc-partial.md (TBD).

    def _strategy_max_entry_price(self, strategy: str) -> int:
        """Per-strategy MAX_ENTRY_PRICE for ladder escalation cap.

        decided_t2 / decided_t2_z25 cap at DECIDED_CONTRACT_T2_MAX_PRICE
        (96¢) — escalating past it would put us in territory the
        strategy never endorsed (T2 only applies 93-96¢).
        All other eligible strategies cap at the global MAX_ENTRY_PRICE.
        """
        if strategy in ("decided_t2", "decided_t2_z25"):
            return DECIDED_CONTRACT_T2_MAX_PRICE
        return MAX_ENTRY_PRICE

    def _maybe_ladder_escalate(self, candidate: Dict, original_limit: int,
                               remaining: int, ioc_filled: int) -> Dict:
        """After an IOC partial fill, retry ONCE at +1¢ for the
        unfilled remainder.

        Returns dict with at least {"escalated": bool}; on success also
        carries {"escalated_filled": int, "escalated_limit": int}.

        Gates (any failure → silent skip, no exception):
          1. LADDER_ESCALATION_ENABLED kill switch
          2. ioc_filled > 0 (zero fill = phantom; don't push into another)
          3. remaining >= LADDER_ESCALATION_MIN_REMAINDER
          4. strategy in LADDER_ESCALATION_ELIGIBLE_STRATEGIES
          5. NOT already a ladder retry (recursion guard)
          6. escalated price ≤ strategy MAX_ENTRY_PRICE AND ≤ global cap
        """
        result = {"escalated": False}
        # Gate 1: kill switch.
        if not LADDER_ESCALATION_ENABLED:
            return result
        # Gate 2: zero fill = phantom-book signal; don't escalate.
        if ioc_filled <= 0:
            return result
        # Gate 3: min remainder.
        if remaining < LADDER_ESCALATION_MIN_REMAINDER:
            return result
        # Gate 4: eligible strategy.
        strategy = candidate.get("strategy") or ""
        if strategy not in LADDER_ESCALATION_ELIGIBLE_STRATEGIES:
            return result
        # Gate 5: recursion guard.
        if candidate.get("_is_ladder_retry"):
            return result
        # Gate 6: per-strategy + global price ceiling.
        strategy_max = self._strategy_max_entry_price(strategy)
        escalated_limit = original_limit + LADDER_ESCALATION_OFFSET
        if escalated_limit > strategy_max or escalated_limit > MAX_ENTRY_PRICE:
            logging.info(
                "LADDER_ESCALATION_AT_CAP: %s strategy=%s original=%dc "
                "would_escalate_to=%dc cap=%dc — skipping",
                candidate.get("ticker", "?"), strategy, original_limit,
                escalated_limit, min(strategy_max, MAX_ENTRY_PRICE))
            return result
        # Gate 7: re-check per-ticker risk cap. The retry adds size
        # to the same ticker; aggregate exposure (existing positions
        # which now include the parent's just-recorded fill +
        # remaining*escalated_limit) must remain inside MAX_TICKER_RISK.
        # Re-checking here is required because callers (execute(),
        # _execute_*_taker) gate at scan time before the parent IOC,
        # but we're inside _submit_taker by the time we reach here —
        # the caller's gate is bypassed for the retry. Fail-closed on
        # any error.
        ticker = candidate.get("ticker", "")
        try:
            balance = candidate.get("balance_at_scan") or 0
            if balance <= 0:
                # No balance signal — fail closed.
                logging.warning(
                    "LADDER_ESCALATION_NO_BALANCE: %s strategy=%s — "
                    "skipping (cannot validate ticker cap)",
                    ticker, strategy)
                return result
            existing_ticker_cost = sum(
                p.get("total_cost_cents", 0)
                for p in self._state.get_open_positions()
                if p.get("ticker") == ticker)
            ticker_cap_cents = balance * MAX_TICKER_RISK
            retry_cost = remaining * escalated_limit
            if existing_ticker_cost + retry_cost > ticker_cap_cents:
                logging.info(
                    "LADDER_ESCALATION_TICKER_CAP_BLOCKED: %s "
                    "strategy=%s existing=%dc retry_cost=%dc cap=%dc",
                    ticker, strategy, existing_ticker_cost,
                    retry_cost, int(ticker_cap_cents))
                return result
        except Exception:
            # Fail-closed on any error reading state.
            logging.warning(
                "LADDER_ESCALATION_CAP_CHECK_RAISED: %s strategy=%s — "
                "skipping defensively", ticker, strategy, exc_info=True)
            return result
        # Gate 8: explicit pre-retry phantom check. The recursive
        # _submit_taker's PHANTOM_ABORT branch only fires when
        # candidate.ob_snapshot.ask_depth is an int — but we set it to
        # None on the retry to avoid stale-depth artifacts (the parent
        # ob_snapshot referred to the original price level). That
        # silent skip would leave the retry with NO catastrophic-tail
        # guard. Round 3 fix: do an explicit fresh REST orderbook
        # fetch here and abort if the escalated level shows zero
        # depth. Failures (None / exception) → fail-closed skip.
        try:
            _ob_raw = self._client.get_orderbook(ticker)
            if not isinstance(_ob_raw, dict):
                logging.info(
                    "LADDER_ESCALATION_OB_UNAVAILABLE: %s strategy=%s "
                    "— skipping retry (cannot verify depth)",
                    ticker, strategy)
                return result
            # Kalshi REST returns three possible shapes (mirror prod
            # unwrap at bot/_impl.py:15810-15812 + 16419-16421):
            #   1. {"orderbook_fp": {"yes_dollars": [["0.99","48"]...]}}
            #      — current FP schema (Mar 2026 migration)
            #   2. {"orderbook": {"yes": [[99, 48], ...]}} — wrapped legacy
            #   3. {"yes": [[99, 48], ...]} — unwrapped (WS cache, older)
            # Try FP first (matches prod ordering), then wrapped/unwrapped.
            _ob_fp = _ob_raw.get("orderbook_fp")
            if _ob_fp:
                _ob = OpportunityScanner._convert_orderbook_fp(_ob_fp)
            else:
                _ob = _ob_raw.get("orderbook", _ob_raw)
            if not isinstance(_ob, dict):
                logging.info(
                    "LADDER_ESCALATION_OB_MALFORMED: %s strategy=%s — "
                    "skipping retry", ticker, strategy)
                return result
            # YES-side ladder. We're a YES BUYER with limit at
            # escalated_limit. Kalshi will match our IOC against any
            # YES ask priced AT-OR-BELOW our limit. Depth check sums
            # those levels.
            _yes_levels = _ob.get("yes") or []
            _depth_at_or_below_limit = 0
            for lvl in _yes_levels:
                # Tolerate malformed levels: must be (price, qty) pair.
                if not (isinstance(lvl, (list, tuple)) and len(lvl) >= 2):
                    continue
                try:
                    _lp = int(lvl[0])
                    _lq = int(lvl[1])
                except (TypeError, ValueError):
                    continue
                if _lp <= escalated_limit and _lq > 0:
                    _depth_at_or_below_limit += _lq
            if _depth_at_or_below_limit <= 0:
                logging.warning(
                    "LADDER_ESCALATION_PHANTOM_ABORT: %s strategy=%s "
                    "escalated=%dc — fresh orderbook shows 0 depth "
                    "at <= limit; refusing retry",
                    ticker, strategy, escalated_limit)
                return result
        except Exception:
            logging.warning(
                "LADDER_ESCALATION_OB_CHECK_RAISED: %s strategy=%s — "
                "skipping defensively", ticker, strategy, exc_info=True)
            return result
        # Build the retry candidate. Carry _is_ladder_retry=True to
        # block recursive escalation AND recursive maker-tail.
        # Replace position_size with remaining; reset best_yes_ask to
        # the escalated limit so the smart-IOC-picker / drift-checks
        # operate on the right reference price.
        # CRITICAL: do NOT rename strategy. STRATEGY_CLAMP_POLICY and
        # STRATEGY_LIMIT_BUMP_RESERVE_CENTS are looked up by exact
        # string match — renaming silently routes the retry through
        # the default policy, which is top_of_book (not no_clamp). The
        # per-strategy IOC mechanics MUST be preserved on the retry.
        # Telemetry separation lives in the _is_ladder_retry flag and
        # the LADDER_ESCALATION_ATTEMPT log line, NOT the strategy
        # column.
        retry_candidate = dict(candidate)
        retry_candidate["_is_ladder_retry"] = True
        retry_candidate["position_size"] = remaining
        retry_candidate["best_yes_ask"] = escalated_limit
        # Drop the parent's ob_snapshot — its ask_depth value applies
        # to the original price level, not the escalated one. The
        # downstream PHANTOM_ABORT in _submit_taker no-ops on None,
        # but Gate 8 above did the equivalent check explicitly.
        retry_candidate["ob_snapshot"] = None
        logging.info(
            "LADDER_ESCALATION_ATTEMPT: %s strategy=%s original=%dc "
            "escalated=%dc remaining=%d ioc_filled=%d",
            ticker, strategy, original_limit,
            escalated_limit, remaining, ioc_filled)
        self._session_ladder_escalations += 1
        # Submit the retry IOC. Returns None on failure / no fill — we
        # surface that as escalated=True (we attempted) but no fill so
        # the caller still falls through to maker tail at original.
        retry_result = self._submit_taker(retry_candidate)
        result["escalated"] = True
        result["escalated_limit"] = escalated_limit
        result["escalated_filled"] = (
            (retry_result or {}).get("filled_count", 0))
        return result

    def _maybe_post_maker_tail(self, candidate: Dict, ioc_price: int,
                               remaining: int,
                               ioc_filled: int = 1) -> bool:
        """Post the unfilled IOC remainder as a post_only GTC limit
        if all gates pass. Returns True iff a maker order was placed.

        Gates (any failure → silent skip, no exception):
          1. ioc_filled > 0 (zero fill = phantom book, don't rest)
          2. remaining >= MAKER_TAIL_MIN_REMAINDER
          3. STC >= MAKER_TAIL_MIN_STC_SECONDS
          4. strategy in MAKER_TAIL_ELIGIBLE_STRATEGIES
          5. per-asset cap not breached
          6. global cap not breached
          7. NOT a ladder retry (the original IOC owns the tail)
        """
        # Gate 1: zero fill = phantom-book IOC; don't rest into nothing.
        if ioc_filled <= 0:
            return False
        # Gate 7: ladder retries must not post their own maker tail.
        # The original (outer) IOC's _submit_taker will post the tail
        # at the ORIGINAL price after the retry returns. Letting the
        # retry post its own tail at the ESCALATED price would create
        # two overlapping tails on the same ticker — the spec is one
        # tail at the original price.
        if candidate.get("_is_ladder_retry"):
            return False
        # Gate 2: min remainder.
        if remaining < MAKER_TAIL_MIN_REMAINDER:
            return False
        # Gate 3: min STC.
        stc = candidate.get("seconds_to_close")
        if stc is None or stc < MAKER_TAIL_MIN_STC_SECONDS:
            return False
        # Gate 4: eligible strategy.
        strategy = candidate.get("strategy") or ""
        if strategy not in MAKER_TAIL_ELIGIBLE_STRATEGIES:
            return False
        asset = candidate.get("asset") or "?"
        # Gate 5+6: concurrency caps. Active = entries in
        # self._maker_tails. Per-asset and global checked together so
        # a single pass over the dict suffices.
        per_asset_active = sum(
            1 for r in self._maker_tails.values()
            if r.get("asset") == asset)
        global_active = len(self._maker_tails)
        if per_asset_active >= MAKER_TAIL_MAX_PER_ASSET:
            self._session_maker_tails_skipped_cap += 1
            logging.info(
                "MAKER_TAIL_SKIP_CAP_ASSET: %s asset=%s strategy=%s "
                "per_asset_active=%d cap=%d",
                candidate.get("ticker", "?"), asset, strategy,
                per_asset_active, MAKER_TAIL_MAX_PER_ASSET)
            return False
        if global_active >= MAKER_TAIL_MAX_GLOBAL:
            self._session_maker_tails_skipped_cap += 1
            logging.info(
                "MAKER_TAIL_SKIP_CAP_GLOBAL: %s asset=%s strategy=%s "
                "global_active=%d cap=%d",
                candidate.get("ticker", "?"), asset, strategy,
                global_active, MAKER_TAIL_MAX_GLOBAL)
            return False
        # Submit. post_only=True is critical — never let this become
        # an unintended taker (would cross our own scan-time best ask
        # if the book moved).
        ticker = candidate.get("ticker") or ""
        side = candidate.get("side", "yes")
        client_oid = str(uuid.uuid4())
        _price_kwarg = (
            {"no_price": ioc_price} if side == "no"
            else {"yes_price": ioc_price})
        # Settlement-race gate (defense-in-depth): MAKER_TAIL_MIN_STC_SECONDS
        # currently dominates this check, but if that floor is ever lowered
        # below MIN_ORDER_SUBMIT_STC_S, this prevents the regression.
        if self._should_skip_near_close(candidate):
            self._abort_near_close(candidate, path="maker_tail")
            return False
        try:
            resp = self._client.place_order(
                ticker=ticker, side=side, action="buy",
                count=remaining, client_order_id=client_oid,
                time_in_force="good_till_canceled",
                post_only=True, **_price_kwarg)
        except Exception:
            logging.warning(
                "MAKER_TAIL_PLACE_FAILED: %s asset=%s strategy=%s "
                "count=%d price=%d", ticker, asset, strategy,
                remaining, ioc_price, exc_info=True)
            return False
        if resp is None:
            logging.warning(
                "MAKER_TAIL_PLACE_NONE: %s asset=%s strategy=%s "
                "count=%d price=%d (place_order returned None)",
                ticker, asset, strategy, remaining, ioc_price)
            return False
        order_id = (resp.get("order") or {}).get(
            "order_id", client_oid)
        now_mono = time.monotonic()
        self._maker_tails[order_id] = {
            "asset": asset,
            "ticker": ticker,
            "strategy": strategy,
            "count": remaining,
            "price_cents": ioc_price,
            "client_order_id": client_oid,
            "posted_monotonic": now_mono,
            "expires_monotonic": now_mono + MAKER_TAIL_TTL_SECONDS,
        }
        self._session_maker_tails_posted += 1
        logging.info(
            "MAKER_TAIL_POSTED: %s asset=%s strategy=%s count=%d "
            "price=%d¢ ttl=%ds order_id=%s (per_asset_active=%d "
            "global_active=%d)",
            ticker, asset, strategy, remaining, ioc_price,
            MAKER_TAIL_TTL_SECONDS, order_id,
            per_asset_active + 1, global_active + 1)
        return True

    def _sweep_maker_tails(self) -> None:
        """Cancel any maker tail past its TTL. Called from tick().

        Records are dropped from _maker_tails AFTER the cancel call
        completes (success or failure) — this prevents a leaked record
        if cancel raises, and prevents a permanent lock on the per-
        asset cap if Kalshi 404s the order.
        """
        if not self._maker_tails:
            return
        now = time.monotonic()
        expired_oids = [
            oid for oid, rec in self._maker_tails.items()
            if rec.get("expires_monotonic", 0) <= now]
        for oid in expired_oids:
            rec = self._maker_tails.pop(oid, None)
            if rec is None:
                continue
            try:
                self._client.cancel_order(oid)
            except Exception:
                logging.warning(
                    "MAKER_TAIL_CANCEL_FAILED: order_id=%s ticker=%s "
                    "(record dropped from tracker regardless)",
                    oid, rec.get("ticker", "?"), exc_info=True)
            self._session_maker_tails_cancelled_ttl += 1
            logging.info(
                "MAKER_TAIL_CANCELLED_TTL: order_id=%s ticker=%s "
                "asset=%s strategy=%s age=%.1fs",
                oid, rec.get("ticker", "?"), rec.get("asset", "?"),
                rec.get("strategy", "?"),
                now - rec.get("posted_monotonic", now))

    # ── Fill detection ────────────────────────────────────────────────────

    def _check_for_fill(self, order: Dict) -> Optional[Dict]:
        """Check if order has been filled via REST fills endpoint.

        Tracks seen fill IDs on the order dict to avoid double-counting
        partial fills on consecutive polls.
        """
        min_ts = int(order["submit_time"])
        resp = self._client.get_fills(
            ticker=order["ticker"], min_ts=min_ts
        )
        if LOG_RAW_IOC_FILLS and resp:
            _append_raw_api_journal({
                "kind": "ioc_fills",
                "ticker": order["ticker"],
                "order_id": order.get("order_id"),
                "submit_count": order.get("count"),
                "min_ts": min_ts,
                "resp": resp,
            })
        if not resp or not resp.get("fills"):
            return None

        seen = order.setdefault("_seen_fill_ids", set())
        for fill in resp["fills"]:
            fill_id = fill.get("trade_id") or fill.get("id")
            if not fill_id:
                logging.warning(f"REST fill missing trade_id/id for {order['ticker']} — skipping to avoid double-count")
                continue
            if fill.get("order_id") == order["order_id"] and fill_id not in seen:
                seen.add(fill_id)
                return fill
        return None

    # ── Fill handling ─────────────────────────────────────────────────────

    def _on_fill(self, fill: Dict, order: Dict) -> int:
        """Handle fill: update SQLite, log trade, record position.

        Returns the fill_count so callers can track partial vs complete fills.
        """
        order_id = order["order_id"]
        ticker = order["ticker"]
        candidate = order["candidate"]

        # Extract fill details — prefer FP/dollar fields, fall back to legacy
        raw_fill_count = fp_str_to_int(fill.get("count_fp")) or (fill.get("count") or order["count"])
        remaining = order["count"] - order.get("filled_so_far", 0)
        if raw_fill_count > remaining > 0:
            logging.warning(
                f"Fill count {raw_fill_count} exceeds remaining {remaining} for "
                f"{order['ticker']} — capping to {remaining}")
            fill_count = remaining
        else:
            fill_count = raw_fill_count
        # For NO-side orders, Kalshi returns yes_price as 100-no_price (YES-equivalent),
        # which is NOT the cost paid. Read no_price_dollars/no_price for NO fills.
        if order.get("side") == "no":
            fill_price_d = fill.get("no_price_dollars")
            fill_price = dollars_str_to_cents(fill_price_d) if fill_price_d else (fill.get("no_price") or order["price_cents"])
        else:
            fill_price_d = fill.get("yes_price_dollars")
            fill_price = dollars_str_to_cents(fill_price_d) if fill_price_d else (fill.get("yes_price") or order["price_cents"])

        # Track cumulative fills for partial fill detection
        order["filled_so_far"] = min(
            order.get("filled_so_far", 0) + fill_count,
            order["count"]
        )
        is_complete = order["filled_so_far"] >= order["count"]

        # Update order status only when fully filled
        if is_complete:
            self._state.mark_order_status(order_id, "filled")
        else:
            logging.info(
                f"Partial fill: {ticker} {fill_count}/{order['count']} "
                f"(cumulative {order['filled_so_far']}/{order['count']})")

        # Log fill model sample for ML training
        self._log_fill_model_sample(order, "filled", fill=fill)

        # Track fill latency
        fill_latency = round(time.time() - order["submit_time"], 3)
        try:
            if self._ml:
                self._ml._recent_fill_latencies.append(fill_latency)
                self._ml._session_fill_count += 1
                if not order.get("is_taker", True):
                    self._ml._session_maker_fills += 1
        except Exception:
            logging.debug("Fill latency tracking failed", exc_info=True)
        logging.info(f"Fill latency: {fill_latency:.3f}s ({'taker' if order.get('is_taker') else 'maker'})")

        # Record position in SQLite
        self._state.record_position_from_fill(
            ticker=ticker,
            event_ticker=order["event_ticker"],
            asset=order["asset"],
            side=order.get("side", "yes"),
            count=fill_count,
            price_cents=fill_price,
            strategy=candidate.get("strategy"),
            seconds_to_close=order.get("seconds_to_close_at_submit"),
            fill_latency=fill_latency,
            vol_regime=candidate.get("vol_regime"),
            calibrated_prob=candidate.get("calibrated_prob"),
            edge=candidate.get("edge"),
            kelly_f=candidate.get("kelly_f"),
            is_taker=order.get("is_taker", False),
            fill_source=order.get("fill_source", "rest_poll"),
            execution_method=order.get("execution_method", "maker"),
            escalation_type=candidate.get("escalation_type", "none"),
            maker_price_cents=candidate.get("maker_price_cents"),
            maker_wait_seconds=candidate.get("maker_wait_seconds"),
        )

        # Invalidate scanner balance cache so next tick gets fresh balance
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                self._ml.scanner._balance_cache = (None, 0.0)
        except Exception:
            pass

        # Log trade with all required fields
        is_taker = order.get("is_taker", False)
        cost_cents = fill_count * fill_price
        fee_cents = calculate_fee(fill_count, fill_price, is_taker=is_taker)

        self._logger.log_trade({
            "ticker": ticker,
            "direction": "yes",
            "price": fill_price,
            "count": fill_count,
            "cost": cost_cents,
            "fee": fee_cents,
            "z_score": candidate.get("z_score"),
            "p_calibrated": candidate.get("calibrated_prob"),
            "balance_at_entry": order["balance_at_entry"],
            "tier": "taker" if is_taker else "maker",
            "is_taker": is_taker,
            "is_panic": order.get("is_panic", False),
            "edge": candidate.get("edge"),
            "kelly_f": candidate.get("kelly_f"),
            "asset": order["asset"],
            "event_ticker": order["event_ticker"],
            "order_id": order_id,
            "client_order_id": order["client_order_id"],
            "strategy_used": candidate.get("strategy"),
            "decision_scores": candidate.get("strategy_scores"),
            "orderbook_snapshot": candidate.get("ob_snapshot"),
        })

        logging.info(
            f"FILL: {ticker} {fill_count}x @ {fill_price}¢ "
            f"({'taker' if is_taker else 'maker'}) "
            f"cost={cost_cents}¢ fee={fee_cents}¢"
            f"{'' if is_complete else ' [PARTIAL ' + str(order['filled_so_far']) + '/' + str(order['count']) + ']'}"
        )

        # Lifecycle snapshot at fill — captures the book left behind after
        # our fill. Pairs with the submit snapshot to expose the book delta
        # and explain N→1 destruction patterns (XRP TM-96 case).
        # source = strategy (uniform vocab with submit event); execution tier
        # (taker/maker) is recoverable via order_id join with positions if needed.
        try:
            self._state.insert_order_lifecycle_snapshot(
                order_id=order_id, ticker=ticker,
                event_type=("fill" if is_complete else "partial_fill"),
                source=candidate.get("strategy"))
        except Exception:
            logging.warning("insert_order_lifecycle_snapshot (fill) failed",
                            exc_info=True)

        # ── Telegram trade alert ────────────────────────────────────────
        if _telegram_state._TELEGRAM and is_complete:
            try:
                _strategy = candidate.get("strategy") or order.get("entry_path", "core")
                _product = candidate.get("product_type", "15m")
                _edge = candidate.get("edge")
                _edge_str = f" edge={_edge:.2%}" if _edge is not None else ""
                _prob = candidate.get("calibrated_prob")
                _prob_str = f" prob={_prob:.1%}" if _prob is not None else ""
                _stc = order.get("seconds_to_close_at_submit")
                _stc_str = f" stc={_stc:.0f}s" if _stc is not None else ""
                _side = order.get("side", "yes").upper()
                _telegram_state._TELEGRAM.send(
                    f"TRADE: {order['asset']} {_side} {fill_count}ct @ {fill_price}c "
                    f"({'taker' if is_taker else 'maker'}) "
                    f"[{_product}/{_strategy}]{_prob_str}{_edge_str}{_stc_str} "
                    f"${cost_cents / 100:.2f}",
                    dedup_key=f"fill_{ticker}")
            except Exception:
                logging.debug("Trade telegram alert failed", exc_info=True)

        # ── Sub-floor fill alert ────────────────────────────────────────
        # Monitor fills below asset's MIN_ENTRY_PRICE. Position is already
        # recorded above — this is monitoring only, never blocks.
        _ASSET_FLOOR_MAP = {
            "BTC": BTC_MIN_ENTRY_PRICE, "ETH": ETH_MIN_ENTRY_PRICE,
            "SOL": SOL_MIN_ENTRY_PRICE, "XRP": XRP_MIN_ENTRY_PRICE,
        }
        _fill_asset = order["asset"]
        _fill_floor = _ASSET_FLOOR_MAP.get(_fill_asset, MIN_ENTRY_PRICE)
        if fill_price < _fill_floor:
            _floor_gap = _fill_floor - fill_price
            _nbbo_at_eval = order.get("price_cents", fill_price)
            logging.warning(
                "SUB_FLOOR_FILL: %s %s %dct @ %dc (floor %dc, gap %dc, NBBO %dc)",
                ticker, _fill_asset, fill_count, fill_price,
                _fill_floor, _floor_gap, _nbbo_at_eval)
            if _telegram_state._TELEGRAM:
                try:
                    _telegram_state._TELEGRAM.send(
                        f"\u26a0\ufe0f SUB-FLOOR FILL: {_fill_asset} {fill_price}c "
                        f"(floor {_fill_floor}c) NBBO={_nbbo_at_eval}c gap={_floor_gap}c "
                        f"{fill_count}ct ${cost_cents / 100:.2f} exposure",
                        dedup_key=f"subfloor_{ticker}")
                except Exception:
                    logging.debug("Sub-floor Telegram alert failed", exc_info=True)

        # Register for confirmation addon evaluation (only on complete fills)
        if is_complete:
            try:
                self._register_addon_eligible(order, fill_price, fill_latency)
            except Exception:
                logging.debug("addon registration failed", exc_info=True)

        return fill_count

    # ── Fill Model Logging ────────────────────────────────────────────────

    def _log_fill_model_sample(self, order: Dict, outcome: str,
                               fill: Optional[Dict] = None,
                               cancel_reason: Optional[str] = None):
        """Write one fill_model_sample to FILL_MODEL_JOURNAL for ML training."""
        try:
            candidate = order.get("candidate", {})
            now = time.time()
            elapsed = now - order["submit_time"]
            fill_latency = round(elapsed, 3) if outcome == "filled" else None
            ob_snap = candidate.get("ob_snapshot", {})

            sample = {
                "type": "fill_model_sample",
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "ticker": order["ticker"],
                "asset": order["asset"],
                "outcome": outcome,
                "fill_latency_s": fill_latency,
                "fill_source": order.get("fill_source"),
                # Submission context
                "price_cents": order["price_cents"],
                "fair_value": candidate.get("best_yes_ask"),
                "offset_cents": (candidate.get("best_yes_ask", 0) - order["price_cents"])
                    if candidate.get("best_yes_ask") else None,
                "count": order["count"],
                "post_only": not order.get("is_taker", False),
                # Market context at submission
                "seconds_to_close": order.get("seconds_to_close_at_submit"),
                "vol_regime": candidate.get("vol_regime"),
                "blended_rv": candidate.get("blended_rv"),
                "ask_depth": ob_snap.get("ask_depth"),
                "total_ob_depth": ob_snap.get("total_depth"),
                "spread_at_submit": ob_snap.get("spread"),
                "bid_depth": ob_snap.get("bid_depth"),
                "convergence_velocity": candidate.get("convergence_velocity"),
                "z_score": candidate.get("z_score"),
                "edge": candidate.get("edge"),
                "kelly_f": candidate.get("kelly_f"),
                # Queue tracking
                "queue_position_initial": order.get("queue_position_initial"),
                "queue_position_final": order.get("queue_position"),
                # Execution details
                "execution_method": order.get("execution_method", "maker"),
                "entry_path": order.get("entry_path", "maker"),
                "cancel_reason": cancel_reason,
                "elapsed_seconds": round(elapsed, 1),
                # WS state
                "ws_connected": (self._kalshi_feed.is_connected
                                 if self._kalshi_feed else False),
                # Config stamps for regime-filtered analysis
                "maker_only_threshold": MAKER_ONLY_THRESHOLD,
            }

            with open(FILL_MODEL_JOURNAL, "a") as f:
                f.write(json.dumps(sample) + "\n")
        except Exception:
            logging.debug("fill_model_sample write failed", exc_info=True)

    # ── Confirmation Addon ─────────────────────────────────────────────────

    def _register_addon_eligible(self, order: Dict,
                                actual_fill_price: int = 0,
                                fill_latency: float = 0.0):
        """After a fill, register the position for addon evaluation.

        Uses actual fill price (not maker limit price) and corrects STC
        for fill latency so addon timing is accurate.

        Skips if the fill itself is an addon (prevents recursive registration).
        """
        if not ADDON_ENABLED:
            return
        candidate = order.get("candidate", {})
        # Don't re-register addon fills
        if candidate.get("entry_path") in ("confirmation_addon", "dip_addon", "tm_taker", "bracket_no_taker"):
            return

        ticker = order["ticker"]
        # Use actual execution price, not the submitted limit price
        entry_price = actual_fill_price if actual_fill_price > 0 else order["price_cents"]
        fill_count = order.get("filled_so_far", order["count"])

        # Correct STC: subtract fill latency from submit-time STC
        stc_at_submit = order.get("seconds_to_close_at_submit")
        stc_at_fill = (stc_at_submit - fill_latency) if stc_at_submit is not None else None

        meta = {
            "ticker": ticker,
            "event_ticker": order["event_ticker"],
            "asset": order["asset"],
            "entry_price_cents": entry_price,
            "entry_count": fill_count,
            "fill_time": time.time(),
            "seconds_to_close_at_fill": stc_at_fill,
            "threshold": candidate.get("threshold"),
            "blended_rv": candidate.get("blended_rv"),
            "calibrated_prob": candidate.get("calibrated_prob"),
            "product_type": candidate.get("product_type"),
            "candidate": candidate,
        }
        self._addon_eligible[ticker] = meta
        logging.info(
            "addon_registered: %s entry=%d¢ count=%d stc=%.0f",
            ticker, entry_price, fill_count, stc_at_fill or 0)

    def _check_addon_opportunities(self):
        """Evaluate open positions for confirmation addon. Called from _tick."""
        if OBSERVATION_MODE:
            return  # Never execute addons in observation mode
        if not ADDON_ENABLED or not self._addon_eligible:
            return

        now = time.time()
        expired = []

        for ticker, meta in list(self._addon_eligible.items()):
            # Skip hourly fills — addons are for 15M maker-first price improvement only.
            # Hourly uses fixed-size taker IOC; addon would bypass hourly constraints
            # (price cap, fixed sizing, asset exclusion). (Bug fix: Mar 24 2026)
            if meta.get("product_type") == "hourly":
                expired.append(ticker)
                continue

            # Cleanup: remove entries >5min old
            if now - meta["fill_time"] > 300:
                expired.append(ticker)
                continue

            # Already addon'd this position
            if ticker in self._addon_completed:
                continue

            # Elapsed check
            elapsed = now - meta["fill_time"]
            if elapsed < ADDON_MIN_SECONDS_SINCE_FILL:
                continue

            # STC check
            stc_at_fill = meta.get("seconds_to_close_at_fill")
            if stc_at_fill is None:
                continue
            current_stc = stc_at_fill - elapsed
            if current_stc < ADDON_MIN_STC_REMAINING:
                self._session_addon_skipped += 1
                logging.info(
                    "addon_SKIP_stc: %s stc_remaining=%.0f < %.0f",
                    ticker, current_stc, ADDON_MIN_STC_REMAINING)
                expired.append(ticker)
                continue

            # Get current best ask
            current_ask = self._get_addon_best_ask(ticker)
            if current_ask is None:
                continue  # Deferred to next tick

            # Price improvement check
            improvement = current_ask - meta["entry_price_cents"]
            if improvement < ADDON_MIN_PRICE_IMPROVEMENT:
                continue  # Not enough improvement yet

            # Price cap
            if current_ask > ADDON_MAX_ENTRY_PRICE:
                self._session_addon_skipped += 1
                logging.info(
                    "addon_SKIP_price_cap: %s ask=%d¢ > %d¢",
                    ticker, current_ask, ADDON_MAX_ENTRY_PRICE)
                continue

            # Get current spot price
            asset = meta["asset"]
            spot = self._get_addon_spot(asset)
            if spot is None:
                continue

            # Recalculate probability with current spot and STC
            blended_rv = meta.get("blended_rv")
            try:
                if self._ml and hasattr(self._ml, 'vol'):
                    fresh_vol = self._ml.vol._cache.get(asset)
                    if fresh_vol and fresh_vol.get("blended_rv"):
                        blended_rv = fresh_vol["blended_rv"]
            except Exception:
                pass
            threshold = meta.get("threshold")
            if blended_rv is None or threshold is None:
                continue

            prob_result = ProbabilityEngine.compute(
                spot, threshold, current_stc, blended_rv, asset=asset,
                product_type=meta.get("candidate", {}).get("product_type"))
            cal_prob = prob_result.get("calibrated_prob")
            if cal_prob is None:
                continue

            # Taker edge check
            addon_count = max(1, int(meta["entry_count"] * ADDON_SIZE_FRACTION))
            taker_fee = calculate_taker_fee(addon_count, current_ask)
            net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (addon_count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                self._session_addon_skipped += 1
                logging.info(
                    "addon_SKIP_edge: %s net_edge=%.4f < %.4f ask=%d¢ "
                    "prob=%.4f fee=%d¢",
                    ticker, net_edge, MIN_EDGE_PCT / 100.0,
                    current_ask, cal_prob, taker_fee)
                continue

            # Balance check — addon cost capped at 50% of current balance
            balance = self._get_addon_balance()
            if balance is None:
                continue

            addon_cost = addon_count * current_ask
            max_addon_cost = int(balance * 0.50)
            if addon_cost > max_addon_cost:
                # Reduce count to fit within 50% of balance
                if current_ask > 0:
                    addon_count = max_addon_cost // current_ask
                if addon_count < 1:
                    self._session_addon_skipped += 1
                    logging.info(
                        "addon_SKIP_balance: %s cost=%d¢ > 50%% balance=%d¢",
                        ticker, addon_cost, balance)
                    continue
                addon_cost = addon_count * current_ask
                taker_fee = calculate_taker_fee(addon_count, current_ask)
                net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (addon_count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    self._session_addon_skipped += 1
                    logging.info(
                        "addon_SKIP_edge_after_resize: %s count=%d edge=%.4f",
                        ticker, addon_count, net_edge)
                    continue

            # All checks passed — execute addon
            filled = self._execute_addon(
                meta, addon_count, current_ask, current_stc,
                cal_prob, net_edge, balance, spot)
            if filled:
                self._addon_completed.add(ticker)

        # Cleanup expired entries
        for t in expired:
            self._addon_eligible.pop(t, None)
        # Also clean completed tickers no longer in eligible
        for t in list(self._addon_completed):
            if t not in self._addon_eligible:
                self._addon_completed.discard(t)

    def _execute_addon(self, meta: Dict, count: int, price: int,
                       stc: float, prob: float, edge: float,
                       balance: int, spot: float) -> bool:
        """Submit taker IOC for confirmation addon. Returns True on fill."""
        ticker = meta["ticker"]
        self._session_addon_attempts += 1

        logging.info(
            "addon_TRIGGER: %s %dx @ %d¢ (entry=%d¢ +%d¢) "
            "stc=%.0f edge=%.4f prob=%.4f balance=%d¢ spot=%.2f",
            ticker, count, price, meta["entry_price_cents"],
            price - meta["entry_price_cents"],
            stc, edge, prob, balance, spot)

        # Build addon candidate for _submit_taker
        addon_candidate = {
            "ticker": ticker,
            "event_ticker": meta["event_ticker"],
            "asset": meta["asset"],
            "best_yes_ask": price,
            "position_size": count,
            "calibrated_prob": prob,
            "edge": edge,
            "seconds_to_close": stc,
            "balance_at_scan": balance,
            "entry_path": "confirmation_addon",
            "strategy": "CONFIRMATION_ADDON",
            "blended_rv": meta.get("blended_rv"),
            "threshold": meta.get("threshold"),
            "vol_regime": meta.get("candidate", {}).get("vol_regime"),
            "z_score": meta.get("candidate", {}).get("z_score"),
            "kelly_f": meta.get("candidate", {}).get("kelly_f"),
            "ob_snapshot": {},
            "original_entry_price": meta["entry_price_cents"],
            "original_entry_count": meta["entry_count"],
            "price_improvement": price - meta["entry_price_cents"],
        }

        if OBSERVATION_MODE:
            logging.info(
                "addon_OBSERVATION: %s %dx @ %d¢ — would submit taker IOC",
                ticker, count, price)
            self._logger.log_execution({
                "action": "addon_observation",
                "ticker": ticker,
                "asset": meta["asset"],
                "price": price,
                "count": count,
                "edge": edge,
                "prob": prob,
                "stc": stc,
                "original_entry_price": meta["entry_price_cents"],
                "price_improvement": price - meta["entry_price_cents"],
            })
            return True

        # Live: submit taker IOC
        result = self._submit_taker(addon_candidate)

        if result is not None:
            self._session_addon_fills += 1
            logging.info(
                "addon_FILLED: %s %dx @ %d¢ (+%d¢ from entry)",
                ticker, count, price,
                price - meta["entry_price_cents"])
            if _telegram_state._TELEGRAM:
                try:
                    _addon_cost = count * price / 100
                    _telegram_state._TELEGRAM.send(
                        f"\u2795 Addon: {meta.get('asset', '?')} {count}ct @ {price}c "
                        f"(${_addon_cost:.2f}, +{price - meta['entry_price_cents']}c slip)")
                except Exception:
                    pass

            self._logger.log_execution({
                "action": "addon_filled",
                "ticker": ticker,
                "asset": meta["asset"],
                "price": price,
                "count": count,
                "edge": edge,
                "prob": prob,
                "stc": stc,
                "original_entry_price": meta["entry_price_cents"],
                "price_improvement": price - meta["entry_price_cents"],
                "balance_after": balance - (count * price) - calculate_taker_fee(count, price),
            })
            return True
        else:
            self._session_addon_unfilled += 1
            self._addon_completed.add(ticker)  # Don't retry — single attempt
            logging.warning(
                "addon_UNFILLED: %s %dx @ %d¢", ticker, count, price)
            self._logger.log_execution({
                "action": "addon_unfilled",
                "ticker": ticker,
                "asset": meta["asset"],
                "price": price,
                "count": count,
            })
            return False

    def _get_addon_best_ask(self, ticker: str) -> Optional[int]:
        """Get best YES ask for ticker via scanner cache, then REST fallback."""
        try:
            scanner = self._ml.scanner if self._ml else None
            if scanner:
                ob_data, _ = scanner._get_orderbook_cached(ticker)
                if ob_data:
                    return OpportunityScanner._best_yes_ask_cents(ob_data)
        except Exception:
            logging.debug("addon orderbook cache lookup failed", exc_info=True)

        # REST fallback
        try:
            ob_resp = self._client.get_orderbook(ticker, depth=5)
            if ob_resp:
                orderbook_fp = ob_resp.get("orderbook_fp")
                if orderbook_fp and self._ml and hasattr(self._ml, 'scanner'):
                    ob_data = self._ml.scanner._convert_orderbook_fp(orderbook_fp)
                else:
                    ob_data = ob_resp.get("orderbook", ob_resp)
                if ob_data:
                    return OpportunityScanner._best_yes_ask_cents(ob_data)
        except Exception:
            logging.debug("addon orderbook REST fallback failed", exc_info=True)
        return None

    def _nbbo_fallback_price(self, candidate: Dict) -> Optional[int]:
        """Return NBBO yes_ask price if candidate passes per-asset gates, else None.

        Called when _get_addon_best_ask() returns None (empty orderbook).
        Uses the NBBO price from scan() (candidate["best_yes_ask"]) which was
        sourced from the market listing's yes_ask field.
        """
        asset = candidate.get("asset", "")
        gate = NBBO_FALLBACK_GATES.get(asset)
        if gate is None:
            return None

        min_price, max_price, max_stc = gate
        nbbo_price = candidate.get("best_yes_ask")
        stc = candidate.get("seconds_to_close")

        if nbbo_price is None:
            return None

        # Price gate
        if nbbo_price < min_price or nbbo_price > max_price:
            logging.info(
                "nbbo_fallback_BLOCKED_price: %s asset=%s price=%dc gate=[%d-%d]",
                candidate.get("ticker", "?"), asset, nbbo_price, min_price, max_price)
            self._session_nbbo_fallback_blocked += 1
            return None

        # STC gate (None = no restriction)
        if max_stc is not None and stc is not None and stc >= max_stc:
            logging.info(
                "nbbo_fallback_BLOCKED_stc: %s asset=%s stc=%.0fs gate=<%.0fs",
                candidate.get("ticker", "?"), asset, stc, max_stc)
            self._session_nbbo_fallback_blocked += 1
            return None

        logging.info(
            "nbbo_fallback_USING: %s asset=%s price=%dc stc=%.0fs",
            candidate.get("ticker", "?"), asset, nbbo_price, stc or 0)
        self._session_nbbo_fallback_attempts += 1
        return nbbo_price

    def _get_addon_spot(self, asset: str) -> Optional[float]:
        """Get current spot price for asset via feed."""
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                return self._ml.scanner._feed.get_price(asset)
        except Exception:
            logging.debug("addon spot price lookup failed", exc_info=True)
        return None

    def _get_addon_balance(self) -> Optional[int]:
        """Get current balance in cents."""
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                return self._ml.scanner._get_balance_cached()
        except Exception:
            logging.debug("addon balance lookup failed", exc_info=True)
        # Direct API fallback
        try:
            resp = self._client.get_balance()
            if resp:
                return resp.get("balance") or 0
        except Exception:
            logging.debug("addon balance API fallback failed", exc_info=True)
        return None

    # ── Dip Addon ──────────────────────────────────────────────────────────

    def _check_dip_addon_opportunities(self):
        """Check filled positions for dip buy opportunities.

        Two-tier logging:
          1. Shadow tier (>=50c): Every qualifying dip -> evaluated_opportunities
          2. Live tier (>=87c): Execution path (shadow or real)
        """
        if OBSERVATION_MODE:
            return  # Never execute dip addons in observation mode
        if not DIP_ADDON_ENABLED or not self._addon_eligible:
            return

        now = time.time()

        # Cleanup expired tickers from completed set
        for t in list(self._dip_addon_completed):
            if t not in self._addon_eligible:
                self._dip_addon_completed.discard(t)

        for ticker, meta in list(self._addon_eligible.items()):
            # Already dip-addon'd or expired
            if ticker in self._dip_addon_completed:
                continue
            if now - meta["fill_time"] > 300:
                continue

            # Don't dip-addon on addon fills
            if meta.get("candidate", {}).get("entry_path") in (
                    "confirmation_addon", "dip_addon", "tm_taker", "bracket_no_taker"):
                continue

            # Time checks
            elapsed = now - meta["fill_time"]
            if elapsed < DIP_ADDON_MIN_SECONDS_SINCE_FILL:
                continue

            stc_at_fill = meta.get("seconds_to_close_at_fill")
            if stc_at_fill is None:
                continue
            current_stc = stc_at_fill - elapsed
            if current_stc < DIP_ADDON_MIN_STC_REMAINING:
                continue

            # Get current ask
            current_ask = self._get_addon_best_ask(ticker)
            if current_ask is None:
                continue

            # DIP CHECK: ask must drop >= threshold below entry
            drop = meta["entry_price_cents"] - current_ask
            if drop < DIP_ADDON_MIN_DROP_CENTS:
                continue

            # ── Shared computation (needed by both tiers) ──────────
            asset = meta["asset"]
            spot = self._get_addon_spot(asset)
            if spot is None:
                continue

            blended_rv = meta.get("blended_rv")
            try:
                if self._ml and hasattr(self._ml, 'vol'):
                    fresh_vol = self._ml.vol._cache.get(asset)
                    if fresh_vol and fresh_vol.get("blended_rv"):
                        blended_rv = fresh_vol["blended_rv"]
            except Exception:
                pass

            threshold = meta.get("threshold")
            if blended_rv is None or threshold is None:
                continue

            # Recompute probability at current spot/vol/stc
            prob_result = ProbabilityEngine.compute(
                spot, threshold, current_stc, blended_rv, asset=asset,
                product_type=meta.get("candidate", {}).get("product_type"))
            cal_prob = prob_result.get("calibrated_prob")
            if cal_prob is None:
                continue

            # Sizing (computed once, used by both tiers)
            addon_count = max(1, int(
                meta["entry_count"] * DIP_ADDON_SIZE_FRACTION))
            taker_fee = calculate_taker_fee(addon_count, current_ask)
            net_edge = (cal_prob - (current_ask / 100.0)
                        - (taker_fee / (addon_count * 100.0)))

            # ── TIER 1: Shadow observation (50c floor) ─────────────
            if current_ask >= DIP_ADDON_SHADOW_FLOOR:
                self._session_dip_addon_shadow += 1
                # OFT signals for dip addon
                _dip_oft_db = {}
                if self._kalshi_oft is not None:
                    try:
                        _dip_koft = self._kalshi_oft.get_signals(ticker)
                        if _dip_koft:
                            _dip_oft_db = {
                                "oft_prob_adjustment": _dip_koft.get("prob_adjustment"),
                                "oft_imbalance_ratio": _dip_koft.get("imbalance_ratio"),
                                "oft_n_snapshots": _dip_koft.get("n_snapshots"),
                            }
                    except Exception:
                        pass
                try:
                    self._state.insert_evaluated_opportunity(
                        ticker=ticker,
                        event_ticker=meta["event_ticker"],
                        asset=asset,
                        filter_stage="dip_addon_shadow",
                        spot_price=spot,
                        threshold=threshold,
                        volatility=blended_rv,
                        market_price=current_ask,
                        seconds_to_close=current_stc,
                        calibrated_prob=cal_prob,
                        edge=net_edge,
                        strategy="DIP_ADDON_SHADOW",
                        position_size=addon_count,
                        z_score=meta.get("candidate", {}).get("z_score"),
                        vol_regime=meta.get("candidate", {}).get(
                            "vol_regime"),
                        raw_prob=prob_result.get("raw_prob"),
                        fee_adjusted_edge=net_edge,
                        counterfactual=(
                            "entry=%dc drop=%dc orig_count=%d"
                            % (meta["entry_price_cents"], drop,
                               meta["entry_count"])),
                        product_type="dip_addon_shadow",
                        hourly_pre_temp_prob=None, hourly_applied_temp_t=None,
                        hourly_shadow_temp_2_0=None, hourly_shadow_temp_1_0=None,
                        hourly_shadow_temp_2_5=None, hourly_shadow_blend_50=None,
                        hourly_shadow_temp_1_75=None, hourly_shadow_temp_3_0=None,
                        hourly_shadow_blend_20=None, hourly_shadow_blend_30=None,
                        hourly_shadow_blend_60=None, hourly_post_temp_prob=None,
                        **_dip_oft_db,
                    )
                except Exception:
                    logging.debug("dip_addon shadow DB insert failed",
                                  exc_info=True)

                logging.info(
                    "dip_addon_SHADOW_OBS: %s ask=%dc entry=%dc drop=%dc "
                    "edge=%.4f prob=%.4f stc=%.0f count=%d",
                    ticker, current_ask, meta["entry_price_cents"], drop,
                    net_edge, cal_prob, current_stc, addon_count)

            # ── TIER 2: Live execution path (87c floor) ────────────
            # Mark completed after shadow log — one observation per ticker
            self._dip_addon_completed.add(ticker)

            if current_ask < DIP_ADDON_MIN_ENTRY_PRICE:
                logging.info("dip_addon_SKIP_floor: %s ask=%dc < %dc",
                             ticker, current_ask, DIP_ADDON_MIN_ENTRY_PRICE)
                continue

            # Edge check
            if net_edge < MIN_EDGE_PCT / 100.0:
                self._session_dip_addon_skipped += 1
                logging.info(
                    "dip_addon_SKIP_edge: %s edge=%.4f ask=%dc prob=%.4f",
                    ticker, net_edge, current_ask, cal_prob)
                continue

            # Balance + combined exposure check
            balance = self._get_addon_balance()
            if balance is None:
                continue

            original_cost = meta["entry_count"] * meta["entry_price_cents"]
            addon_cost = addon_count * current_ask
            total_exposure = original_cost + addon_cost
            max_allowed = int(
                (balance + original_cost) * DIP_ADDON_MAX_TOTAL_RISK)
            if total_exposure > max_allowed:
                addon_count = max(
                    0, (max_allowed - original_cost) // current_ask)
                if addon_count < 1:
                    self._session_dip_addon_skipped += 1
                    logging.info(
                        "dip_addon_SKIP_exposure: %s total=%dc > %dc",
                        ticker, total_exposure, max_allowed)
                    continue
                addon_cost = addon_count * current_ask
                taker_fee = calculate_taker_fee(addon_count, current_ask)
                net_edge = (cal_prob - (current_ask / 100.0)
                            - (taker_fee / (addon_count * 100.0)))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    continue

            # ── EXECUTE (or shadow-log the live tier) ──────────────
            self._session_dip_addon_attempts += 1

            if DIP_ADDON_SHADOW_MODE:
                logging.info(
                    "dip_addon_LIVE_SHADOW: %s %dx @ %dc (entry=%dc -%dc) "
                    "stc=%.0f edge=%.4f prob=%.4f bal=%dc",
                    ticker, addon_count, current_ask,
                    meta["entry_price_cents"], drop, current_stc,
                    net_edge, cal_prob, balance)
                self._logger.log_execution({
                    "action": "dip_addon_live_shadow",
                    "ticker": ticker, "asset": asset,
                    "entry_price": meta["entry_price_cents"],
                    "dip_price": current_ask, "drop_cents": drop,
                    "addon_count": addon_count, "edge": net_edge,
                    "prob": cal_prob, "stc": current_stc,
                    "balance": balance,
                })
                return  # One per tick

            # LIVE: taker IOC, single attempt
            logging.info(
                "dip_addon_TRIGGER: %s %dx @ %dc (entry=%dc -%dc) "
                "stc=%.0f edge=%.4f prob=%.4f bal=%dc",
                ticker, addon_count, current_ask,
                meta["entry_price_cents"], drop, current_stc,
                net_edge, cal_prob, balance)

            addon_candidate = {
                "ticker": ticker,
                "event_ticker": meta["event_ticker"],
                "asset": asset,
                "best_yes_ask": current_ask,
                "position_size": addon_count,
                "calibrated_prob": cal_prob,
                "edge": net_edge,
                "seconds_to_close": current_stc,
                "balance_at_scan": balance,
                "entry_path": "dip_addon",
                "strategy": "DIP_ADDON",
                "blended_rv": blended_rv,
                "threshold": threshold,
                "vol_regime": meta.get("candidate", {}).get("vol_regime"),
                "z_score": meta.get("candidate", {}).get("z_score"),
                "kelly_f": meta.get("candidate", {}).get("kelly_f"),
                "ob_snapshot": {},
                "original_entry_price": meta["entry_price_cents"],
                "original_entry_count": meta["entry_count"],
                "price_drop": drop,
            }

            result = self._submit_taker(addon_candidate)
            if result is not None:
                self._session_dip_addon_fills += 1
                logging.info("dip_addon_FILLED: %s %dx @ %dc (-%dc)",
                             ticker, addon_count, current_ask, drop)
                if _telegram_state._TELEGRAM:
                    try:
                        _cost = addon_count * current_ask / 100
                        _telegram_state._TELEGRAM.send(
                            f"Dip addon: {asset} {addon_count}ct "
                            f"@ {current_ask}c "
                            f"(${_cost:.2f}, -{drop}c from entry)")
                    except Exception:
                        pass
            else:
                logging.warning("dip_addon_UNFILLED: %s %dx @ %dc",
                                ticker, addon_count, current_ask)
            return  # One per tick max

    # ── Cancel ────────────────────────────────────────────────────────────

    # Allowed sources for _handle_cancel_404 (round-5/6/7 reviews).
    # Bad source values are logged + downgraded to "unknown" rather
    # than raising — _tick_one's broad except would swallow an
    # AssertionError and leave the asset stuck.
    # See kb/decisions/cancel-404-fix-v2-design-may04.md
    _CANCEL_404_SOURCES = ("direct", "reconciliation")

    def _handle_cancel_404(self, order: Dict, asset: str,
                           reason: str, source: str) -> bool:
        """Handle a 404 from cancel API.

        404 *should* mean Kalshi already expired/canceled the order.
        Verify defensively via get_orders to guard against
        wrong-order-id / caller bugs (the conservative cancel_pending
        branch's original purpose).

        Args:
            order: Order dict in self._active_orders[asset].
            asset: Asset key (BTC/ETH/SOL/XRP).
            reason: Free-form trigger label (close_approaching, timeout,
                escalation_*, cancel_pending_retry). Threaded into
                cancel_reason for forensics.
            source: One of _CANCEL_404_SOURCES. Unknown values logged
                and downgraded to "unknown" — must not block the pop.

        Returns True if popped (caller may submit replacement).
        Returns False if held conservatively (order still resting on
        Kalshi — something is wrong).

        Note: pop happens BEFORE audit writes (mark_order_status,
        log_order, _log_fill_model_sample, update_evaluated_opportunity_order)
        so audit failures cannot resurrect the lockout. Of those four,
        only mark_order_status is un-self-guarded (can raise
        sqlite3.OperationalError); the others catch internally
        (bot/_impl.py:2716, 23630, 5089).
        """
        if source not in self._CANCEL_404_SOURCES:
            logging.error(
                f"_handle_cancel_404 unknown source={source!r} — "
                f"falling back to 'unknown' to keep pop priority")
            source = "unknown"
        self._cancel_404_count += 1
        filled = order.get("filled_so_far", 0)

        # Defensive verify: 404 should mean the order is gone, but
        # confirm via get_orders before popping. Three failure modes:
        #   (a) get_orders raises → exception path → log + pop
        #   (b) get_orders returns None (breaker OPEN) → log + pop
        #   (c) get_orders shows order resting → HOLD cancel_pending
        try:
            orders_resp = self._client.get_orders(ticker=order["ticker"])
            if orders_resp is None:
                logging.warning(
                    f"cancel_404_verify_unavailable: {order['ticker']} "
                    f"from {source} — get_orders returned None "
                    f"(breaker open?), falling through to pop.")
            elif orders_resp:
                for o in orders_resp.get("orders", []):
                    if (o.get("order_id") == order["order_id"]
                            and o.get("status") in ("resting", "open")):
                        logging.error(
                            f"cancel_404_but_resting: {order['ticker']} "
                            f"{order['order_id']} from {source} — "
                            f"Kalshi 404'd cancel but order still in "
                            f"/orders. Holding cancel_pending for retry.")
                        order["cancel_pending"] = True
                        return False
        except Exception:
            logging.warning(
                f"cancel_404_verify_failed: {order['ticker']} from "
                f"{source} — falling through to pop based on 404 signal",
                exc_info=True)

        # Preserve partial-fill labeling (kalshi_fill_simulator.py
        # treats partial_canceled as label=1).
        if filled > 0:
            db_status = "partial_canceled"
            outcome = "partial_filled"
            fm_label = "partial_canceled"
        else:
            db_status = "expired"
            outcome = "expired"
            fm_label = "expired"

        cancel_reason_str = f"kalshi_404_{source}_{reason}"

        # POP FIRST. Audit writes must NOT be a prerequisite to the
        # pop — if any audit write raises, the asset would stay stuck
        # and re-create the May 4 lockout.
        self._active_orders.pop(asset, None)

        # Of the four audit writes, only mark_order_status is un-self-
        # guarded (can raise sqlite3.OperationalError on lock
        # contention). log_order/_log_fill_model_sample/
        # update_evaluated_opportunity_order catch internally
        # (bot/_impl.py:2716, 23630, 5089). Single outer try is
        # belt-and-suspenders defense-in-depth — primary protection
        # is the pop above.
        try:
            self._state.mark_order_status(order["order_id"], db_status)
            self._logger.log_order({
                "action": "maker_canceled",
                "ticker": order["ticker"],
                "order_id": order["order_id"],
                "reason": cancel_reason_str,
                "elapsed": round(time.time() - order["submit_time"], 1),
                "filled_so_far": filled,
            })
            self._log_fill_model_sample(
                order, fm_label, cancel_reason=cancel_reason_str)
            if asset not in self._escalating_assets:
                self._state.update_evaluated_opportunity_order(
                    order["ticker"], order_outcome=outcome)
        except Exception:
            logging.error(
                f"cancel_404_audit_failed: {order['ticker']} "
                f"{order['order_id']} — pop already complete, audit "
                f"writes incomplete.",
                exc_info=True)

        logging.warning(
            f"cancel_404_already_gone: {order['ticker']} "
            f"{order['order_id']} filled={filled}/{order['count']} "
            f"from {source} (session count: {self._cancel_404_count})")
        return True

    def _cancel_order(self, asset: str, reason: str) -> bool:
        """Cancel the active maker order for a specific asset.

        Returns True if cancel succeeded (safe to submit replacement).
        Returns False if cancel API failed (order may still be live).
        """
        order = self._active_orders.get(asset)
        if order is None:
            return True  # nothing to cancel

        filled = order.get("filled_so_far", 0)

        cancel_resp = self._client.cancel_order(order["order_id"])
        if cancel_resp is None:
            logging.error(f"Cancel API FAILED for {order['order_id']} — order may still be resting on exchange")
            # Don't mark canceled in DB — order may still be live on Kalshi
            order["cancel_pending"] = True
            # Do NOT pop — order may still be live, prevent double position
            return False

        # 404 sentinel — Kalshi has aged out the order. Route through
        # _handle_cancel_404 (defensive verify + pop-first + correct
        # labeling). See kb/failures/cancel-404-asset-lockout-may04.md
        if isinstance(cancel_resp, dict) and cancel_resp.get("_status_code") == 404:
            return self._handle_cancel_404(order, asset, reason, source="direct")

        # Normal success
        status = "partial_canceled" if filled > 0 else "canceled"
        self._state.mark_order_status(order["order_id"], status)

        # Log fill model sample for canceled order
        self._log_fill_model_sample(order, "canceled", cancel_reason=reason)

        self._logger.log_order({
            "action": "maker_canceled",
            "ticker": order["ticker"],
            "order_id": order["order_id"],
            "reason": reason,
            "elapsed": round(time.time() - order["submit_time"], 1),
            "filled_so_far": filled,
        })
        logging.info(
            f"Maker order canceled: {order['ticker']} reason={reason}"
            f"{' (partial fill: ' + str(filled) + '/' + str(order['count']) + ')' if filled > 0 else ''}"
        )
        self._active_orders.pop(asset, None)
        # Update order outcome — skip if escalating (escalation handler sets outcome)
        if asset not in self._escalating_assets:
            _outcome = "partial_filled" if filled > 0 else "canceled"
            self._state.update_evaluated_opportunity_order(
                order["ticker"], order_outcome=_outcome)
        return True

    def _cancel_active(self, reason: str):
        """Cancel all active maker orders. Used by _reprice_maker compat."""
        for asset in list(self._active_orders):
            self._cancel_order(asset, reason)


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
