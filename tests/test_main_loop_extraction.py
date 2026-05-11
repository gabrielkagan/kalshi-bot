"""Bit 9.3 — MainLoop extracted from bot/_impl.py to bot/main_loop.py.

Bit 9.3 (2026-05-10), Sprint 9 closing leaf — fourth and final Sprint 9
extraction (after 9.1 OrderExecutor → bot/executor.py, 9.2
SettlementTracker + discover_active_windows → bot/settlement.py, and
ahead of sister Bit 9.3.5 OFE+KOFT → bot/order_flow.py per USER Option-B
decision via AskUserQuestion).

Two-step atomic per master plan L2195-2216:
  - 9.3-i (this commit): bot/main_loop.py created; bot/_impl.py adds
    re-export `from bot.main_loop import MainLoop`; bot/__main__.py
    UNCHANGED (still `from bot._impl import MainLoop` via the proxy).
    Audit + caller-update sweep complete. Soak ≥7d per master plan.
  - 9.3-ii (separate later commit): swap bot/__main__.py to
    `from bot.main_loop import MainLoop`. bot/_impl.py SHRINKS to ~365
    LOC (OFE + KOFT + module-level helpers + retired alias
    breadcrumbs). NOT deleted (Option B — Bit 9.3.5 deletes it).

Path-A (METHOD-BODY late-binding for ALL bot._impl access):
  bot/main_loop.py has ZERO top-level `from bot._impl import ...` or
  `import bot._impl`. Top-level imports would cause partial-module
  ImportError because bot/_impl.py at line ~119 re-exports
  `from bot.main_loop import MainLoop` — triggering bot.main_loop's
  load BEFORE bot/_impl.py reaches line 334+ (where _HPSB_MISSING_BLEEDERS
  is bound). Neither Plan-agent path A nor Plan-agent path A++ accounted
  for this; the correct path is method-body late-binding INSIDE
  __init__ + startup, matching the bot.executor
  _get_opportunity_scanner() shape.

  Method-body late-bound names (post-Bit-9.3.5):
    Inside MainLoop.__init__:
      - _HPSB_MISSING_BLEEDERS (1 read at gate-state log line)
      - _HPSB_VALIDATOR_UNAVAILABLE_REASON (1 read at gate-state log line)
    Inside MainLoop.startup:
      - detect_orphan_db_holders (1 call site)

  Bit 9.3.5 (2026-05-10) collapsed the prior OrderFlowEngine +
  KalshiOrderFlowTracker entries to a top-level
  `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker`
  at bot/main_loop.py module scope (bot/order_flow.py is a clean leaf).

  No new .importlinter carve-out needed — bot/main_loop.py has no
  top-level bot._impl edge in the import graph. Net contracts stays at 5.

Cross-module access patterns preserved:
  - `_telegram_state._TELEGRAM` (Bit 8.1 path-A++; ~11 read sites + 1 write
    at __init__). Canonical alias form `import bot.notifier as _telegram_state`
    per L84.
  - `_cal_state._CALIBRATION_ENGINE` / `._CAL_REGISTRY` / `._resolve_cal_engine`
    (Bit 6.3 path-B; ~5 read+write sites). Alias form
    `from bot.engines import calibration as _cal_state`.

Sister cleanup atomic in same commit:
  - bot/notifier.py docstring: 5 consumers post-Bit-9.3 (grows from 4 →
    5; bot/_impl.py STAYS for the orphan-DB watchdog helpers
    `_alert_orphan_db_holder` + `detect_orphan_db_holders` lsof-not-found
    Telegram alert branch; bot/main_loop.py ADDS for MainLoop reads + the
    singleton WRITE in `__init__`).
  - bot/__init__.py docstring extended for Bit 9.3.
  - bot/CLAUDE.md "Deploy a change" step 3 catalog gains MainLoop paragraph
    + flips Bit 8.1/9.1/9.2 prior-paragraph "MainLoop reads" mentions.
  - bot/scanner/__init__.py + bot/scanner/CLAUDE.md: 4-consumer narrative
    flipped (bot/_impl.py → bot/main_loop.py for the MainLoop slot).
  - bot/_impl.py header docstring + the _telegram_state alias comment block
    flipped (bot/_impl.py is no longer a consumer).
  - bot/settlement.py + bot/executor.py docstrings: 4-consumer enum flipped.
  - agent_docs/bot_layout.md: header line count, class table loses MainLoop
    row + adds bot/main_loop.py block, Sprint 9 marker advanced.
  - tests/test_state_extraction.py quadruple-walk → quintuple-walk
    (BOT_PY + SCANNER_PY + EXECUTOR_PY + SETTLEMENT_PY + MAIN_LOOP_PY).
  - tests/test_settlement_extraction.py + tests/test_executor_extraction.py
    consumer test guard walk-set extended.
  - tests/test_orphan_db_watchdog.py AST walk retargeted from BOT_PY to
    MAIN_LOOP_PY (helpers stay in bot/_impl.py).
  - 7+ BOT_PY-defined tests retargeted from BOT_PY → MAIN_LOOP_PY for
    MainLoop content walks (per L38 — AST walk retargeting, per-test).
  - tests/test_order_outcome_vocab.py SCANNED_PATHS extended.
  - tests/test_low_price_shadow.py + tests/test_stacking.py +
    tests/test_tm_sweep_shadow.py + tests/test_regression.py
    `_read_bot()` / `_paths` helpers extended.
  - tests/test_bot_h2_step2_wiring.py concat list extended.

Mirrors tests/test_settlement_extraction.py (Bit 9.2) + tests/test_executor_extraction.py
(Bit 9.1) + tests/test_scanner_extraction.py (Bit 8.1) + tests/test_state_extraction.py
(Bit 7.1).

Related lessons reinforced:
  L32 (Plan-agent), L33 (consumer-class identity quintuple-walk),
  L38 (AST walk retargets, ~7 BOT_PY tests for MainLoop content),
  L40 (no @patch routing — constants reach via bot.constants directly),
  L41 (no hand-counted breadcrumbs), L78 (free-var scan returned 71
  names), L79 (no path-A vs path-A++ surface — method-body late-binding
  IS the path; both Plan-agent options had import-time issues), L83/L84
  (module-attribute access pattern preserved), L86/L90 (doc-drift
  contagion — pre-emptively grep ALL parallel narratives in same edit;
  the 7-site walk extends to 8 with bot/main_loop.py + bot/settlement.py +
  bot/executor.py docstrings).
"""
from __future__ import annotations

import ast
import configparser
import importlib
import inspect
import re
import sys
from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_PY = REPO_ROOT / "bot" / "_impl.py"
MAIN_LOOP_PY = REPO_ROOT / "bot" / "main_loop.py"
SETTLEMENT_PY = REPO_ROOT / "bot" / "settlement.py"
EXECUTOR_PY = REPO_ROOT / "bot" / "executor.py"
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
NOTIFIER_PY = REPO_ROOT / "bot" / "notifier.py"
INIT_PY = REPO_ROOT / "bot" / "__init__.py"
MAIN_PY = REPO_ROOT / "bot" / "__main__.py"
IMPORTLINTER_INI = REPO_ROOT / ".importlinter"


# ============================================================ Module-level data
# Per L41: parametrize tuples ARE the ground truth — no separate count claim.
# Names AST-extracted from bot/_impl.py:1019-3017 pre-extraction (Bit 9.3 pre-flight scan).

# 15 instance methods, 0 staticmethods (verified via AST)
MAIN_LOOP_INSTANCE_METHODS = (
    "__init__",
    "_check_db_health",
    "_setup_signals",
    "_handle_signal",
    "startup",
    "_refresh_active_windows",
    "_subscribe_discovery_orderbooks",
    "_backfill_calibration_data",
    "_log_daily_summary",
    "_active_windows_is_stale",
    "_maybe_log_cache_stale",
    "_maybe_log_cache_fresh_recovery",
    "_tick",
    "run",
    "_cleanup",
)

MAIN_LOOP_STATIC_METHODS: Tuple[str, ...] = ()  # MainLoop has no @staticmethod / @classmethod

# 23 bot.constants names imported by MainLoop (L78 free-var scan output)
MAIN_LOOP_BOT_CONSTANTS_NAMES = (
    "ACTIVE_WINDOWS_STALENESS_BUDGET_S",
    "CALIBRATION_STATE_PATH",
    "CROSS_EXCHANGE_ENABLED",
    "DB_PATH",
    "HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES",
    "HIGH_PRICE_STC_BLOCK_ENABLED",
    "HIGH_PRICE_STC_BLOCK_FILTER_STAGE",
    "HOURLY_OBSERVATION_ENABLED",
    "KALSHI_OFT_ENABLED",
    "MARKET_REFRESH_SECONDS",
    "MAX_SECONDS_BEFORE_CLOSE",
    "MIN_SECONDS_BEFORE_CLOSE",
    "OBSERVATION_MODE",
    "POSITION_PRICE_MONITOR_ENABLED",
    "POSITION_PRICE_MONITOR_WS_STALE_SEC",
    "REJECTION_JOURNAL",
    "SCAN_INTERVAL_SECONDS",
    "SOL_BLEED_V2_BLOCK_FILTER_STAGE",
    "SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE",
    "SPORTS_ENABLED",
    "SPX_HOURLY_ENABLED",
    "TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE",
    "WEATHER_ENABLED",
    "WS_PERIODIC_RESNAPSHOT_INTERVAL_S",
)

# 1 from config (Bit 3.1 left in config.py)
MAIN_LOOP_CONFIG_NAMES = ("ASSETS",)

# 1 from market_config
MAIN_LOOP_MARKET_CONFIG_NAMES = ("MARKET_CONFIGS",)

# 4 from models (the math/sizing classes constructed in __init__)
MAIN_LOOP_MODELS_NAMES = (
    "EGARCHEstimator",
    "MincerZarnowitzTracker",
    "PositionSizer",
    "calculate_taker_fee",
)

# Method-body late-binding names from bot._impl (Path-A — see module docstring).
# These names exist as module-level state/functions in bot/_impl.py and MUST
# be late-bound INSIDE method bodies (NOT top-level import) to avoid the
# partial-module ImportError chain documented in test_no_top_level_bot_impl_import_in_main_loop.
# Bit 9.3.5 (2026-05-10) collapsed the OFE+KOFT entries to a top-level
# `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker` —
# bot/order_flow.py is a clean leaf (stdlib + bot.constants only) so the
# top-level edge is safe. Only the HPSB pair remains late-bound (both are
# module-level state bound BELOW the line-119 re-export point in bot/_impl.py).
MAIN_LOOP_BOT_IMPL_INIT_LATE_BOUND = (
    "_HPSB_MISSING_BLEEDERS",
    "_HPSB_VALIDATOR_UNAVAILABLE_REASON",
)

# Bit 9.3-ii (2026-05-10) retargeted MainLoop.startup's late-binding for
# `detect_orphan_db_holders` from `bot._impl` to `bot.orphan_db_watchdog` (the
# function was relocated to the clean-leaf module in the same atomic commit).
# The tuple is empty post-9.3-ii — startup() has zero remaining bot._impl
# late-bindings. The retargeted import is pinned by
# test_main_loop_startup_late_binds_orphan_db_watchdog_name below.
MAIN_LOOP_BOT_IMPL_STARTUP_LATE_BOUND: tuple[str, ...] = ()

# Bit 9.3-ii: startup() now late-binds detect_orphan_db_holders from
# bot.orphan_db_watchdog (post-extraction location).
MAIN_LOOP_ORPHAN_DB_WATCHDOG_STARTUP_LATE_BOUND = (
    "detect_orphan_db_holders",
)

# 3 from integration (cal_mlp processor + cache); 2 are aliased in bot/_impl.py
MAIN_LOOP_INTEGRATION_NAMES = (
    "_calmlp_predictors",
    "_calmlp_drain_pool",          # alias of stop_post_hoc_processor
    "_calmlp_start_posthoc",       # alias of start_post_hoc_processor
)

# Forbidden numerical libraries — same as scanner/executor/settlement; MainLoop
# orchestrates engines (which carry numpy/scipy transitively through models)
# but MUST NOT import them directly.
FORBIDDEN_NUMERICAL_IMPORTS = ("numpy", "scipy", "torch", "sklearn", "pandas")


# ─── Cached AST parse helpers ──────────────────────────────────────────────

def _main_loop_tree() -> ast.Module:
    return ast.parse(MAIN_LOOP_PY.read_text())


def _bot_impl_tree() -> ast.Module:
    return ast.parse(BOT_PY.read_text())


def _main_loop_class() -> ast.ClassDef:
    tree = _main_loop_tree()
    return next(
        n for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.ClassDef) and n.name == "MainLoop"
    )


def _main_loop_method(name: str) -> ast.FunctionDef:
    cls = _main_loop_class()
    return next(
        m for m in ast.iter_child_nodes(cls)
        if isinstance(m, ast.FunctionDef) and m.name == name
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity (5 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_main_loop_class_in_bot_main_loop_module():
    """Positive AST pin: MainLoop defined in bot/main_loop.py."""
    assert MAIN_LOOP_PY.exists(), "bot/main_loop.py missing — extraction not yet performed"
    tree = _main_loop_tree()
    classes = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert "MainLoop" in classes, (
        f"MainLoop classdef not found in bot/main_loop.py; classes present: {classes}"
    )


def test_main_loop_class_NOT_in_bot_impl_module():
    """Negative AST pin: MainLoop classdef is NOT in bot/_impl.py post-extraction."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3.5 final form)")
    tree = _bot_impl_tree()
    classes = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert "MainLoop" not in classes, (
        "bot/_impl.py still contains class MainLoop — extraction incomplete; "
        "the class body must move to bot/main_loop.py atomically with the re-export."
    )


def test_main_loop_module_attr_resolves_via_proxy():
    """Bit 9.3 re-export: bot.MainLoop resolves through the proxy chain."""
    import bot
    import bot._impl
    import bot.main_loop
    assert bot.MainLoop is bot._impl.MainLoop, (
        "bot.MainLoop not resolving to bot._impl.MainLoop via proxy"
    )
    assert bot._impl.MainLoop is bot.main_loop.MainLoop, (
        "bot._impl.MainLoop not the SAME class as bot.main_loop.MainLoop — "
        "the re-export `from bot.main_loop import MainLoop` must bind the same "
        "class object (not re-define)."
    )
    assert bot.MainLoop.__module__ == "bot.main_loop", (
        f"bot.MainLoop.__module__ = {bot.MainLoop.__module__!r}; "
        f"expected 'bot.main_loop' post-extraction."
    )


def test_main_loop_init_signature_unchanged():
    """MainLoop.__init__ signature must be byte-identical (extraction is structural)."""
    import bot
    sig = inspect.signature(bot.MainLoop.__init__)
    params = list(sig.parameters.keys())
    assert params == ["self"], (
        f"MainLoop.__init__ signature drift: {params} (expected just ['self'])"
    )


def test_main_loop_run_signature_unchanged():
    """MainLoop.run() must remain the entrypoint method called by bot/__main__.py."""
    import bot
    assert hasattr(bot.MainLoop, "run"), "MainLoop.run missing post-extraction"
    sig = inspect.signature(bot.MainLoop.run)
    assert list(sig.parameters.keys()) == ["self"], (
        f"MainLoop.run signature drift: {sig.parameters}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — Drift guards (3 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_no_top_level_bot_impl_import_in_main_loop():
    """Path-A (method-body late-binding): bot/main_loop.py must NOT have a
    top-level `import bot._impl` or `from bot._impl import ...`.

    Top-level would partial-module ImportError because bot/_impl.py at line ~119
    re-exports `from bot.main_loop import MainLoop` BEFORE bot/_impl.py finishes
    binding the residual names at lines 334+ (_HPSB_MISSING_BLEEDERS / _HPSB_VALIDATOR_UNAVAILABLE_REASON
    boot-time validator state). All bot._impl access must be method-body
    late-binding inside __init__ + startup. No `.importlinter` carve-out is
    needed because there's no top-level edge — net contracts stays at 5
    (mirrors Bit 9.2's clean leaf). Bit 9.3.5 (2026-05-10) collapsed the prior
    OrderFlowEngine + KalshiOrderFlowTracker late-binding entries to top-level
    `from bot.order_flow import` (clean leaf — no partial-module risk).
    """
    tree = _main_loop_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/main_loop.py has top-level `import bot._impl` (line {node.lineno}). "
                    f"Use method-body late-binding inside __init__/startup instead."
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/main_loop.py has top-level `from bot._impl import ...` (line {node.lineno}). "
                f"Use method-body late-binding inside __init__/startup instead."
            )


def test_main_loop_method_count_matches_ast():
    """L41 — the parametrize tuples ARE the ground truth; assert the AST agrees."""
    cls = _main_loop_class()
    actual = sorted(
        m.name for m in ast.iter_child_nodes(cls) if isinstance(m, ast.FunctionDef)
    )
    expected = sorted(MAIN_LOOP_INSTANCE_METHODS + MAIN_LOOP_STATIC_METHODS)
    assert actual == expected, (
        f"MainLoop method drift:\n"
        f"  Missing from AST: {set(expected) - set(actual)}\n"
        f"  Extra in AST: {set(actual) - set(expected)}"
    )


def test_main_loop_no_static_methods():
    """MainLoop has zero @staticmethod / @classmethod (regression seal)."""
    cls = _main_loop_class()
    for m in ast.iter_child_nodes(cls):
        if isinstance(m, ast.FunctionDef):
            decos = [d.id for d in m.decorator_list if isinstance(d, ast.Name)]
            assert "staticmethod" not in decos and "classmethod" not in decos, (
                f"MainLoop.{m.name} unexpectedly decorated with @{decos} — "
                f"MainLoop is intended to be 100% instance methods."
            )


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — Method presence (parametrized)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method_name", MAIN_LOOP_INSTANCE_METHODS + MAIN_LOOP_STATIC_METHODS)
def test_main_loop_method_present(method_name: str):
    """Every named method survives extraction."""
    import bot
    assert hasattr(bot.MainLoop, method_name), (
        f"MainLoop.{method_name} missing post-extraction"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 4 — Constants partition (parametrized)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("const_name", MAIN_LOOP_BOT_CONSTANTS_NAMES)
def test_main_loop_constant_partitioned_to_bot_constants(const_name: str):
    """L39 — every named constant lives in bot.constants (not config / models / market_config)."""
    import bot.constants as bc
    assert hasattr(bc, const_name), (
        f"{const_name} not in bot.constants — partition drift"
    )


@pytest.mark.parametrize("const_name", MAIN_LOOP_CONFIG_NAMES)
def test_main_loop_constant_partitioned_to_config(const_name: str):
    """L39 — ASSETS lives in config.py (Bit 3.1 left-in-config)."""
    import config
    assert hasattr(config, const_name), f"{const_name} not in config"


@pytest.mark.parametrize("const_name", MAIN_LOOP_MARKET_CONFIG_NAMES)
def test_main_loop_constant_partitioned_to_market_config(const_name: str):
    """L39 — MARKET_CONFIGS lives in market_config.py."""
    import market_config
    assert hasattr(market_config, const_name), f"{const_name} not in market_config"


def test_main_loop_imports_constants_explicitly_not_via_star():
    """L40 — bot/main_loop.py uses explicit `from bot.constants import (...)`, NOT star-import."""
    src = MAIN_LOOP_PY.read_text()
    assert "from bot.constants import *" not in src, (
        "bot/main_loop.py uses `from bot.constants import *` — replace with explicit imports per L40"
    )
    assert "from config import *" not in src, (
        "bot/main_loop.py uses `from config import *` — explicit names only per L40"
    )
    assert "from bot.helpers import *" not in src, (
        "bot/main_loop.py uses `from bot.helpers import *` — explicit names only per L40"
    )
    # Positive: explicit constant block exists
    assert re.search(r"from bot\.constants import \(", src) or re.search(
        r"from bot\.constants import \w+", src
    ), "bot/main_loop.py missing `from bot.constants import (...)` block"


@pytest.mark.parametrize("model_name", MAIN_LOOP_MODELS_NAMES)
def test_main_loop_model_present(model_name: str):
    """L78 — math/sizing classes from models.py are present + imported."""
    import bot.models as models
    assert hasattr(models, model_name), f"{model_name} not in models"
    src = MAIN_LOOP_PY.read_text()
    assert model_name in src, f"bot/main_loop.py missing {model_name} reference"


# ═════════════════════════════════════════════════════════════════════════════
# Section 5 — L33 consumer-class identity pins (KalshiClient / StateManager / Logger / engines)
# ═════════════════════════════════════════════════════════════════════════════

def test_main_loop_imports_kalshi_client():
    """L33 — KalshiClient construction site (`self.client = KalshiClient(...)`)."""
    src = MAIN_LOOP_PY.read_text()
    assert "from bot.kalshi_client import KalshiClient" in src, (
        "bot/main_loop.py missing `from bot.kalshi_client import KalshiClient`"
    )


def test_main_loop_imports_state_manager():
    """L33 — StateManager construction site (`self.state = StateManager()`)."""
    src = MAIN_LOOP_PY.read_text()
    assert "from bot.state import StateManager" in src, (
        "bot/main_loop.py missing `from bot.state import StateManager`"
    )


def test_main_loop_imports_logger():
    """L33 — Logger construction site (`self.logger = Logger()`)."""
    src = MAIN_LOOP_PY.read_text()
    assert "from bot.logger import Logger" in src, (
        "bot/main_loop.py missing `from bot.logger import Logger`"
    )


def test_main_loop_imports_telegram_notifier():
    """L33 — TelegramNotifier construction site (`self.telegram = TelegramNotifier(...)`)."""
    src = MAIN_LOOP_PY.read_text()
    assert "from bot.notifier import TelegramNotifier" in src, (
        "bot/main_loop.py missing `from bot.notifier import TelegramNotifier`"
    )


def test_main_loop_imports_executor():
    """L33 — OrderExecutor construction site (`self.executor = OrderExecutor(...)`)."""
    src = MAIN_LOOP_PY.read_text()
    assert "from bot.executor import OrderExecutor" in src, (
        "bot/main_loop.py missing `from bot.executor import OrderExecutor`"
    )


def test_main_loop_imports_scanner():
    """L33 — OpportunityScanner construction site (`self.scanner = OpportunityScanner(..., main_loop=self)`)."""
    src = MAIN_LOOP_PY.read_text()
    assert "from bot.scanner import OpportunityScanner" in src, (
        "bot/main_loop.py missing `from bot.scanner import OpportunityScanner`"
    )


def test_main_loop_imports_settlement_tracker_and_discover():
    """L33 — SettlementTracker construction + discover_active_windows call."""
    src = MAIN_LOOP_PY.read_text()
    assert "from bot.settlement import" in src and "SettlementTracker" in src, (
        "bot/main_loop.py missing `from bot.settlement import SettlementTracker, ...`"
    )
    assert "discover_active_windows" in src, (
        "bot/main_loop.py missing discover_active_windows reference"
    )


def test_main_loop_imports_engines():
    """L33 — VolatilityEngine + ProbabilityEngine + CalibrationEngine all imported."""
    src = MAIN_LOOP_PY.read_text()
    for cls_name in ("VolatilityEngine", "ProbabilityEngine", "CalibrationEngine"):
        assert cls_name in src, f"bot/main_loop.py missing {cls_name} reference"


def test_main_loop_imports_feeds_and_fetchers():
    """L33 — CoinbaseFeed, CrossExchangeFeed, KalshiFeed, DeribitDVOLFetcher, CoinGlassFetcher."""
    src = MAIN_LOOP_PY.read_text()
    for cls_name in (
        "CoinbaseFeed", "CrossExchangeFeed", "KalshiFeed",
        "DeribitDVOLFetcher", "CoinGlassFetcher",
    ):
        assert cls_name in src, f"bot/main_loop.py missing {cls_name} reference"


# ═════════════════════════════════════════════════════════════════════════════
# Section 6 — Forbidden-imports tuple (5 parametrized)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("forbidden", FORBIDDEN_NUMERICAL_IMPORTS)
def test_main_loop_no_forbidden_numerical_imports(forbidden: str):
    """numpy/scipy/torch/sklearn/pandas must NOT be top-level imports in bot/main_loop.py
    (orchestration only — engines carry the numerical libs transitively via models).

    Per kb/failures/cal-mlp-torch-thread-contention-apr29.md: importing torch outside
    the bot/_thread_env-pinned chain caused a production scan-loop ballooning.
    """
    tree = _main_loop_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden), (
                    f"bot/main_loop.py has top-level `import {alias.name}` — {forbidden} forbidden"
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith(forbidden), (
                f"bot/main_loop.py has top-level `from {node.module} import ...` — {forbidden} forbidden"
            )


# ═════════════════════════════════════════════════════════════════════════════
# Section 7 — Path-A method-body late-binding pins (Bit 9.3 unique surface)
# ═════════════════════════════════════════════════════════════════════════════

def _imports_inside_method(method_name: str) -> List[str]:
    """Extract all `from bot._impl import ...` names inside the named method body."""
    method = _main_loop_method(method_name)
    names: List[str] = []
    for node in ast.walk(method):
        if isinstance(node, ast.ImportFrom) and node.module == "bot._impl":
            for alias in node.names:
                names.append(alias.name)
    return names


@pytest.mark.parametrize("late_bound_name", MAIN_LOOP_BOT_IMPL_INIT_LATE_BOUND)
def test_main_loop_init_late_binds_bot_impl_name(late_bound_name: str):
    """Path-A: __init__ uses method-body `from bot._impl import (...)` for the names
    that bot/_impl.py binds at module-level AFTER the line-119 re-export point."""
    init_imports = _imports_inside_method("__init__")
    assert late_bound_name in init_imports, (
        f"MainLoop.__init__ missing method-body `from bot._impl import {late_bound_name}` — "
        f"top-level import would partial-module ImportError. Init body imports observed: {init_imports}"
    )


@pytest.mark.parametrize("late_bound_name", MAIN_LOOP_BOT_IMPL_STARTUP_LATE_BOUND)
def test_main_loop_startup_late_binds_bot_impl_name(late_bound_name: str):
    """Path-A: startup uses method-body `from bot._impl import (...)`.

    Bit 9.3-ii (2026-05-10): the tuple is empty post-extraction — startup() has
    zero bot._impl late-bindings after detect_orphan_db_holders was retargeted
    to bot.orphan_db_watchdog. The parametrize will skip when tuple is empty."""
    startup_imports = _imports_inside_method("startup")
    assert late_bound_name in startup_imports, (
        f"MainLoop.startup missing method-body `from bot._impl import {late_bound_name}` — "
        f"top-level import would partial-module ImportError. Startup body imports observed: {startup_imports}"
    )


def _orphan_db_imports_inside_method(method_name: str) -> List[str]:
    """Extract all `from bot.orphan_db_watchdog import ...` names inside the named method body."""
    method = _main_loop_method(method_name)
    names: List[str] = []
    for node in ast.walk(method):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.orphan_db_watchdog":
            for alias in node.names:
                names.append(alias.name)
    return names


@pytest.mark.parametrize("late_bound_name", MAIN_LOOP_ORPHAN_DB_WATCHDOG_STARTUP_LATE_BOUND)
def test_main_loop_startup_late_binds_orphan_db_watchdog_name(late_bound_name: str):
    """Bit 9.3-ii: startup() retargets `detect_orphan_db_holders` late-binding
    from `bot._impl` to the new clean-leaf location `bot.orphan_db_watchdog`."""
    startup_imports = _orphan_db_imports_inside_method("startup")
    assert late_bound_name in startup_imports, (
        f"MainLoop.startup missing method-body `from bot.orphan_db_watchdog "
        f"import {late_bound_name}` (Bit 9.3-ii retarget). Observed imports: {startup_imports}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 8 — _telegram_state + _cal_state alias pins (mirrors scanner/executor/settlement)
# ═════════════════════════════════════════════════════════════════════════════

def test_main_loop_uses_telegram_state_alias():
    """bot/main_loop.py reads/writes _TELEGRAM exclusively via _telegram_state._TELEGRAM
    (Bit 8.1 path-A++ pattern). Plain `from bot.notifier import _TELEGRAM` would
    capture by value at import time and freeze at None when MainLoop.__init__ later mutates."""
    src = MAIN_LOOP_PY.read_text()
    bare_reads = re.findall(r"(?<![.\w])_TELEGRAM(?!\w)", src)
    assert not bare_reads, (
        f"bot/main_loop.py has {len(bare_reads)} bare-name _TELEGRAM read(s) — "
        f"all reads must go through `_telegram_state._TELEGRAM` (per L83)"
    )
    assert re.search(
        r"^import bot\.notifier as _telegram_state(?:\s|#|$)",
        src, re.MULTILINE,
    ), (
        "bot/main_loop.py missing `import bot.notifier as _telegram_state` (per L84)"
    )


def test_main_loop_uses_cal_state_alias():
    """bot/main_loop.py uses _cal_state for _CALIBRATION_ENGINE / _CAL_REGISTRY / _resolve_cal_engine
    (Bit 6.3 path-B pattern; mutated at __init__:1053 + 1056 + 1085 + 1101 + read at 1109)."""
    src = MAIN_LOOP_PY.read_text()
    assert re.search(
        r"^from bot\.engines import calibration as _cal_state(?:\s|#|$)",
        src, re.MULTILINE,
    ), (
        "bot/main_loop.py missing `from bot.engines import calibration as _cal_state`"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 9 — bot/notifier.py 4-consumer enumeration (Bit 9.3 atomic flip)
# ═════════════════════════════════════════════════════════════════════════════

def test_notifier_docstring_enumerates_five_consumers_post_bit_9_3():
    """Bit 9.3 atomic: the consumer enumeration goes 4 → 5. bot/_impl.py STAYS
    in the list (for the orphan-DB Layer-3 helper `_alert_orphan_db_holder`
    at bot/_impl.py:431 which reads `_telegram_state._TELEGRAM`); bot/main_loop.py
    is ADDED as the new consumer for MainLoop reads + the singleton WRITE.

    Five consumers post-Bit-9.3:
      1. bot/_impl.py — for `_alert_orphan_db_holder` (orphan-DB watchdog)
      2. bot/main_loop.py — for MainLoop reads + WRITE in __init__
      3. bot/scanner/__init__.py — for OpportunityScanner reads
      4. bot/executor.py — for OrderExecutor reads
      5. bot/settlement.py — for SettlementTracker reads

    L86 contagion seal — extends the Bit 9.2 6-site walk to 8 sites, plus
    extends the negative pattern set to catch stale "FOUR consumers" / "4
    consumers" / "MainLoop reads only" prose that no longer matches reality."""
    BOT_CLAUDE = REPO_ROOT / "bot" / "CLAUDE.md"
    BOT_LAYOUT = REPO_ROOT / "agent_docs" / "bot_layout.md"
    SCANNER_INIT = REPO_ROOT / "bot" / "scanner" / "__init__.py"
    SCANNER_CLAUDE = REPO_ROOT / "bot" / "scanner" / "CLAUDE.md"
    BOT_INIT = REPO_ROOT / "bot" / "__init__.py"
    sites = [
        NOTIFIER_PY, BOT_CLAUDE, BOT_LAYOUT,
        SCANNER_INIT, SCANNER_CLAUDE, BOT_INIT,
        SETTLEMENT_PY, EXECUTOR_PY,                     # extended this Bit
    ]
    # Sites that should explicitly enumerate bot/main_loop.py as a consumer.
    positive_required = {NOTIFIER_PY, BOT_CLAUDE, BOT_LAYOUT, SCANNER_INIT, BOT_INIT}
    # Stale narrative that must FLIP post-Bit-9.3.
    stale_patterns = (
        "THREE consumer", "all three of", "all three consumers",
        "across all three", "3 consumer modules",
        "Both bot._impl and bot.scanner",
        "Both `bot._impl` and `bot.scanner`",
        "all three reach", "all three of bot",
        # Bit 9.3 NEW negatives — bot/_impl.py is no longer the MainLoop-reads
        # consumer (MainLoop moved out); the bot/_impl.py slot now reflects the
        # orphan-DB watchdog helpers, NOT MainLoop:
        "bot/_impl.py for MainLoop reads",
        "bot/_impl.py (MainLoop reads only)",
        "bot/_impl.py for MainLoop reads only",
        "this module for MainLoop reads",
        # R1 MINOR-1 + R1 retune: stale "all four" / "FOUR consumer" PRESENT-TENSE
        # claims from Bit-9.2 narratives that must update to FIVE post-Bit-9.3.
        # Carefully scoped to PRESENT-TENSE enumeration claims — historical
        # descriptions like "3→4 consumers" (Bit 9.2's atomic change) and
        # "the '4 consumers' Bit-9.2 narrative — which..." (post-Bit-9.3
        # retrospective) are NOT caught. The test enforces the LIVE state
        # description, not the change-history prose.
        "all four consumer", "all four of bot", "across all four consumer",
        "4 consumers post-Bit-9.2)",  # the parenthetical state-claim form
        "FOUR consumer", "is 4 consumers",
    )
    for site in sites:
        src = site.read_text()
        if site in positive_required:
            assert "bot/main_loop.py" in src or "bot.main_loop" in src, (
                f"{site} missing bot/main_loop.py / bot.main_loop consumer enumeration "
                "(Bit 9.3 atomic flip; MainLoop reads moved out of bot/_impl.py)"
            )
        for stale in stale_patterns:
            assert stale not in src, (
                f"{site} still contains stale '{stale}' enumeration — Bit 9.3 atomic "
                "flip moved MainLoop out of bot/_impl.py; the bot/_impl.py slot in the "
                "_telegram_state consumer list is now `_alert_orphan_db_holder` (line 431)"
            )


def test_bot_init_docstring_mentions_main_loop_consumer():
    """Bit 9.3 atomic: bot/__init__.py docstring extends consumer list to include bot.main_loop."""
    src = INIT_PY.read_text()
    assert "bot.main_loop" in src or "bot/main_loop.py" in src, (
        "bot/__init__.py docstring missing bot.main_loop reference (Bit 9.3 atomic flip)"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 10 — re-export back to bot/_impl.py works (9.3-i precondition)
# ═════════════════════════════════════════════════════════════════════════════

def test_bot_impl_reexports_main_loop():
    """Bit 9.3-i re-export pattern: `from bot.main_loop import MainLoop` in bot/_impl.py."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3.5 final form)")
    src = BOT_PY.read_text()
    assert re.search(
        r"from bot\.main_loop import .*MainLoop", src
    ), "bot/_impl.py missing `from bot.main_loop import MainLoop` re-export (Bit 9.3-i)"


# ═════════════════════════════════════════════════════════════════════════════
# Section 11 — .importlinter contract count unchanged (no new carve-out, Path-A method-body)
# ═════════════════════════════════════════════════════════════════════════════

def test_no_main_loop_no_impl_toplevel_contract_added():
    """MainLoop uses METHOD-BODY late-binding for bot._impl access (Path-A) — no
    `main_loop-no-impl-toplevel` `.importlinter` carve-out is needed because
    bot/main_loop.py has zero top-level bot._impl edge in the import graph.
    Net contracts stays at 5 post-Bit-9.3 (mirrors Bit 9.2's clean leaf shape)."""
    config = configparser.ConfigParser()
    config.read(IMPORTLINTER_INI)
    contracts = [s for s in config.sections() if s.startswith("importlinter:contract:")]
    contract_names = {s.split(":")[-1] for s in contracts}
    assert "main-loop-no-impl-toplevel" not in contract_names, (
        "Unexpected `main-loop-no-impl-toplevel` contract added — MainLoop uses "
        "METHOD-BODY late-binding (Path-A); no carve-out needed."
    )
    assert "main_loop-no-impl-toplevel" not in contract_names, (
        "Unexpected `main_loop-no-impl-toplevel` contract added"
    )
    # Sanity: 7 contracts post-Bit-12.3 (Sprint 12, 2026-05-11): 5 pre-existing
    # (engines-no-impl, fetchers-no-engines, feeds-no-engines, helpers-leaf,
    # state-no-impl-toplevel) + 2 new (bot-no-torch, bot-no-pandas).
    assert len(contracts) == 7, (
        f"Expected 7 .importlinter contracts post-Bit-12.3; found {len(contracts)}: {contract_names}"
    )


def test_helpers_leaf_includes_main_loop_in_forbidden_modules():
    """Bit 9.3 extends `helpers-leaf.forbidden_modules` to include bot.main_loop
    (parallel to the other top-level `bot.X` modules already enumerated:
    bot.scanner, bot.executor, bot.settlement, bot.state, etc.). The
    helpers-leaf contract bans helpers from importing siblings."""
    config = configparser.ConfigParser()
    config.read(IMPORTLINTER_INI)
    section = "importlinter:contract:helpers-leaf"
    assert section in config.sections(), (
        f"helpers-leaf contract missing from {IMPORTLINTER_INI}"
    )
    forbidden = config[section]["forbidden_modules"].split()
    assert "bot.main_loop" in forbidden, (
        f"helpers-leaf forbidden_modules missing bot.main_loop — must be added "
        f"alongside the other top-level bot.X siblings (bot.scanner, bot.executor, "
        f"bot.settlement, etc.). Current list: {forbidden}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 12 — bot/__main__.py SWAPPED at 9.3-ii (direct bot.main_loop import)
# ═════════════════════════════════════════════════════════════════════════════

def test_bot_main_imports_main_loop_from_bot_main_loop_at_9_3_ii():
    """Bit 9.3-ii (2026-05-10): bot/__main__.py SWAPPED to direct `from bot.main_loop
    import MainLoop` — per master plan L2197.

    INVERSION of the prior `test_bot_main_still_imports_main_loop_from_bot_impl_at_9_3_i`
    pin (the prior test scheduled its own retirement in its docstring). The proxy chain
    is no longer the entrypoint resolution path; the direct import is. Retirement of
    the _BotProxy entirely is deferred to Bit 9.3-iii.

    Defense-in-depth `import bot._thread_env` first-import is pinned separately by
    test_bot_main_imports_thread_env_as_first_import in
    test_orphan_db_watchdog_extraction.py."""
    if not MAIN_PY.exists():
        pytest.skip("bot/__main__.py missing")
    src = MAIN_PY.read_text()
    assert "from bot.main_loop import MainLoop" in src, (
        "bot/__main__.py must have `from bot.main_loop import MainLoop` post-Bit-9.3-ii "
        "(direct import, no proxy chain) per master plan L2197."
    )
    assert "from bot._impl import MainLoop" not in src, (
        "bot/__main__.py still has the pre-9.3-ii `from bot._impl import MainLoop` form. "
        "Bit 9.3-ii swap incomplete — see master plan L2197."
    )


def test_bot_main_logging_basicconfig_preserved():
    """R7 #1 — bot/__main__.py's logging.basicConfig block is load-bearing (production
    journalctl loses structured INFO logging if dropped). Verify it's intact at 9.3-i;
    9.3-ii MUST also preserve it when swapping the import target."""
    if not MAIN_PY.exists():
        pytest.skip("bot/__main__.py missing")
    src = MAIN_PY.read_text()
    assert "logging.basicConfig(" in src, "bot/__main__.py missing logging.basicConfig — R7 #1 violation"
    assert "force=True" in src, "bot/__main__.py basicConfig missing force=True — R7 #1 violation"


# ═════════════════════════════════════════════════════════════════════════════
# Section 13 — Behavioral smoke (mocked __init__-free construction)
# ═════════════════════════════════════════════════════════════════════════════

def test_main_loop_instantiable_via_new_without_init():
    """Sanity smoke: MainLoop class object is constructible via __new__ (skipping __init__
    which requires KALSHI_API_KEY / private key). Mirrors the pattern used by
    tests/test_stale_ticker_cleanup.py + tests/test_cache_staleness_watchdog.py."""
    import bot
    instance = bot.MainLoop.__new__(bot.MainLoop)
    assert isinstance(instance, bot.MainLoop)
    # Method bindings work via the class
    assert hasattr(instance, "run")
    assert callable(instance.run)
