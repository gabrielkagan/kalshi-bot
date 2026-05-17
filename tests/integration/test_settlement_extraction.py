"""Bit 9.2 — SettlementTracker (+ discover_active_windows) extracted from bot/_impl.py to bot/settlement.py.

Bit 9.2 (2026-05-10):
  SettlementTracker → bot/settlement.py
  discover_active_windows() → bot/settlement.py (USER chose to bundle per
  AskUserQuestion; deviates from master plan Phase AA — settlement-adjacent
  in source layout; only true caller is MainLoop.)

  (Sprint 9 — third-largest extraction; ~1,088 LOC SettlementTracker class
  body + ~80 LOC discover_active_windows + ~107 LOC header/imports = 1,275 LOC
  total bot/settlement.py file. 13 instance methods + 2 staticmethods (15
  def-lines total per AST). Touches the settlement hot path: API polling,
  settled trade recording, fee/PnL computation, evaluated_opportunities
  counterfactual tracking, weather backfill.)

Path-A++ deviations (NOT byte-for-byte):
  1. bot/settlement.py imports the public name `append_raw_api_journal`
     directly from bot/helpers/raw_api_journal.py (no underscore alias).
     The 2 SettlementTracker call sites that previously used the
     L81-aliased `_append_raw_api_journal({...})` are rewritten to
     `append_raw_api_journal({...})` — mirrors the bot/executor.py:98
     convention from Bit 9.1.
  2. The L81 alias-import line at bot/_impl.py:285 is RETIRED atomically
     in this Bit (zero callers remain — both ST callers move to bot/settlement.py).

Sister cleanup atomic in same commit:
  - tests/integration/test_executor_extraction.py::test_bot_impl_uses_l81_alias_import
    DELETED (positive pin — was asserting alias is STILL in bot/_impl.py).
  - bot/notifier.py docstring 3 → 4 consumers (adds bot/settlement.py).
  - bot/__init__.py docstring extended for Bit 9.2.
  - bot/CLAUDE.md Deploy step 3 catalog gains SettlementTracker paragraph.
  - bot/scanner/CLAUDE.md _TELEGRAM 3-consumers narrative → 4.
  - tests/integration/test_state_extraction.py triple-walk → quadruple-walk
    (BOT_PY + SCANNER_PY + EXECUTOR_PY + SETTLEMENT_PY).
  - tests/contracts/test_order_outcome_vocab.py SCANNED_PATHS extended with bot/settlement.py.
  - tests/integration/test_low_price_shadow.py + tests/integration/test_stacking.py +
    tests/integration/test_tm_sweep_shadow.py + tests/integration/test_regression.py
    `_read_bot()` / `_paths` helpers extended to concat bot/settlement.py.
  - tests/integration/test_tracker_tick_threaded.py BOT_PY → SETTLEMENT_PY retarget.
  - tests/contracts/test_logger_extraction.py + tests/contracts/test_kalshi_client_extraction.py
    NOT touched — they test Logger/KalshiClient classes themselves (neither
    moved); their docstring references to SettlementTracker remain accurate
    via the proxy chain.
  - agent_docs/bot_layout.md: header line count, Layer-2 enum, class table,
    bot/settlement.py block, Sprint 9 SHIPPED marker, discover_active_windows
    location flip (was Bit 9.3).

Bundled bug fix (USER-CONFIRMED ride-along, ticket 86b9vppn3):
  Pre-existing UnboundLocalError `'best_ask'` in OpportunityScanner.scan()
  low_probability_15m insert_rejection branch (predates Bit 8.1 per git
  blame). Fix: initialize `best_ask = None` at iteration start.

No new .importlinter carve-outs needed — SettlementTracker has zero
references to names defined below the line-116 re-export point. Net
contracts stays at 5.

Cross-class coupling preserved via:
  - `_telegram_state._TELEGRAM` (Bit 8.1 path-A++ pattern; 4 read sites
    in SettlementTracker)
  - `_cal_state._CALIBRATION_ENGINE` / `_cal_state._resolve_cal_engine`
    (Bit 6.3 path-B pattern; 4 read sites in SettlementTracker)
  - `from bot.helpers.raw_api_journal import append_raw_api_journal`
    (public name; mirrors bot/executor.py)

Related lessons:
  L32 (Plan-agent), L33 (consumer-class identity — quadruple-walk
  extension this Bit), L38 (AST walk retargets), L40 (no @patch routing
  needed; constants reach via bot.constants directly), L41 (no
  hand-counted breadcrumbs), L78 (star-import-aware free-var scan), L79
  (no path-A vs path-A++ surface — clean leaf, both paths converge), L81
  (alias-drop atomic; bot/_impl.py:285 retired this Bit), L83/L84
  (inherited verbatim from Bit 8.1 — module-attribute access pattern),
  L86/L90 (doc-drift contagion sweep — bot 9.1 wasted 4 rounds; we
  pre-emptively grep ALL parallel narratives in same edit).

Mirrors tests/integration/test_executor_extraction.py (Bit 9.1 path-A++) +
tests/integration/test_scanner_extraction.py (Bit 8.1) + tests/integration/test_state_extraction.py
(Bit 7.1 path-A++).
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


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOT_PY = REPO_ROOT / "bot" / "_impl.py"
SETTLEMENT_PY = REPO_ROOT / "bot" / "settlement.py"
EXECUTOR_PY = REPO_ROOT / "bot" / "executor.py"
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
NOTIFIER_PY = REPO_ROOT / "bot" / "notifier.py"
INIT_PY = REPO_ROOT / "bot" / "__init__.py"
RAW_API_JOURNAL_PY = REPO_ROOT / "bot" / "helpers" / "raw_api_journal.py"
IMPORTLINTER_INI = REPO_ROOT / ".importlinter"


# ============================================================ Module-level data
# Per L41: parametrize tuples ARE the ground truth — no separate count claim.
# Names AST-extracted from bot/_impl.py:1003-2090 + 2097-2181 pre-extraction
# (Bit 9.2 pre-flight scan).

# 13 instance methods (no classmethods)
SETTLEMENT_INSTANCE_METHODS = (
    "__init__",
    "startup",
    "_load_pending_rejections",
    "_load_processed_tickers",
    "tick",
    "_poll",
    "_sweep_stuck_positions",
    "_process_settlement",
    "register_rejection_ticker",
    "_poll_rejections",
    "_process_rejection_settlement",
    "_poll_evaluated_opportunities",
    "_backfill_weather_actual_temps",
)

# 2 staticmethods
SETTLEMENT_STATIC_METHODS = (
    "_parse_weather_market_date",
    "_estimate_actual_temp_from_bracket",
)

# 8 bot.constants names imported by SettlementTracker + discover_active_windows
# (L78 free-var scan output).
SETTLEMENT_BOT_CONSTANTS_NAMES = (
    "HOURLY_OBSERVATION_ENABLED",
    "HOURLY_SERIES_TICKERS",
    "LOG_RAW_IOC_FILLS",
    "LOG_RAW_SETTLEMENTS",
    "SERIES_TICKERS",
    "SETTLEMENT_CHECK_SECONDS",
    "TM_SWEEP_SHADOW_ENABLED",
    "WEATHER_MIN_ENTRY_PRICE",
)

# 2 bot.helpers.strings names
SETTLEMENT_BOT_HELPERS_STRINGS_NAMES = (
    "dollars_str_to_cents",
    "fp_str_to_int",
)

# 3 models names
SETTLEMENT_MODELS_NAMES = (
    "calculate_fee",
    "calculate_maker_fee",
    "calculate_taker_fee",
)

# Forbidden numerical libraries — same as scanner/executor; SettlementTracker
# is a clean leaf (settlement is pure stdlib + sqlite3 + requests through KalshiClient).
FORBIDDEN_NUMERICAL_IMPORTS = ("numpy", "scipy", "torch", "sklearn", "pandas")


# ─── Cached AST parse helpers ──────────────────────────────────────────────

def _settlement_tree() -> ast.Module:
    return ast.parse(SETTLEMENT_PY.read_text())


def _bot_impl_tree() -> ast.Module:
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    return ast.parse(BOT_PY.read_text())


def _settlement_class() -> ast.ClassDef:
    tree = _settlement_tree()
    return next(
        n for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.ClassDef) and n.name == "SettlementTracker"
    )


def _settlement_func(name: str) -> ast.FunctionDef:
    tree = _settlement_tree()
    return next(
        n for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.FunctionDef) and n.name == name
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity (5 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_settlement_class_in_bot_settlement_module():
    """Positive AST pin: SettlementTracker defined in bot/settlement.py."""
    assert SETTLEMENT_PY.exists(), "bot/settlement.py missing — extraction not yet performed"
    tree = _settlement_tree()
    classes = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert "SettlementTracker" in classes, (
        f"SettlementTracker classdef not found in bot/settlement.py; classes present: {classes}"
    )


def test_settlement_class_NOT_in_bot_impl_module():
    """Negative AST pin: SettlementTracker classdef is NOT in bot/_impl.py post-extraction."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    tree = _bot_impl_tree()
    classes = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert "SettlementTracker" not in classes, (
        "bot/_impl.py still contains class SettlementTracker — extraction incomplete; "
        "the class body must move to bot/settlement.py atomically with the re-export."
    )


def test_discover_active_windows_in_bot_settlement_module():
    """Positive AST pin: discover_active_windows defined in bot/settlement.py."""
    tree = _settlement_tree()
    funcs = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.FunctionDef)]
    assert "discover_active_windows" in funcs, (
        f"discover_active_windows function not found in bot/settlement.py; funcs present: {funcs}"
    )


def test_discover_active_windows_NOT_in_bot_impl_module():
    """Negative AST pin: discover_active_windows is NOT a top-level def in bot/_impl.py."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    tree = _bot_impl_tree()
    funcs = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.FunctionDef)]
    assert "discover_active_windows" not in funcs, (
        "bot/_impl.py still contains def discover_active_windows — extraction incomplete; "
        "the function must move to bot/settlement.py atomically with the SettlementTracker class."
    )


def test_settlement_module_attr_resolves_via_proxy():
    """Bit 9.2 re-export: bot.settlement.SettlementTracker resolves through the proxy chain."""
    import bot
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.settlement
    assert bot.settlement.SettlementTracker is bot._impl.SettlementTracker, (
        "bot.settlement.SettlementTracker not resolving to bot._impl.SettlementTracker via proxy"
    )
    assert bot._impl.SettlementTracker is bot.settlement.SettlementTracker, (
        "bot._impl.SettlementTracker not the SAME class as bot.settlement.SettlementTracker — "
        "the line-117 re-export `from bot.settlement import SettlementTracker` must bind the "
        "same class object (not re-define)."
    )
    assert bot.settlement.SettlementTracker.__module__ == "bot.settlement", (
        f"bot.settlement.SettlementTracker.__module__ = {bot.settlement.SettlementTracker.__module__!r}; "
        f"expected 'bot.settlement' post-extraction."
    )


def test_discover_active_windows_module_attr_resolves_via_proxy():
    """Bit 9.2 re-export: bot.settlement.discover_active_windows resolves through the proxy chain."""
    import bot
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.settlement
    assert bot.settlement.discover_active_windows is bot._impl.discover_active_windows, (
        "bot.settlement.discover_active_windows not resolving via proxy"
    )
    assert bot._impl.discover_active_windows is bot.settlement.discover_active_windows, (
        "bot._impl.discover_active_windows not the SAME function as bot.settlement.discover_active_windows"
    )


def test_settlement_init_signature_unchanged():
    """SettlementTracker.__init__ signature must be byte-identical (extraction is structural, not behavioral)."""
    import bot.settlement
    sig = inspect.signature(bot.settlement.SettlementTracker.__init__)
    params = list(sig.parameters.keys())
    assert params == ["self", "client", "state", "logger", "main_loop"], (
        f"SettlementTracker.__init__ signature drift: {params}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — Drift guards (3 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_no_top_level_bot_impl_import_in_settlement():
    """L78/L83 — bot/settlement.py must NOT have a top-level `import bot._impl` or
    `from bot._impl import ...` (clean leaf; no late-binding helper needed)."""
    tree = _settlement_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/settlement.py has top-level `import bot._impl` (line {node.lineno}). "
                    f"This Bit requires NO late-binding — SettlementTracker is a clean leaf."
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/settlement.py has top-level `from bot._impl import ...` (line {node.lineno}). "
                f"This Bit requires NO late-binding — SettlementTracker is a clean leaf."
            )


def test_settlement_method_count_matches_ast():
    """L41 — the parametrize tuples ARE the ground truth; assert the AST agrees."""
    cls = _settlement_class()
    actual = sorted(
        m.name for m in ast.iter_child_nodes(cls) if isinstance(m, ast.FunctionDef)
    )
    expected = sorted(SETTLEMENT_INSTANCE_METHODS + SETTLEMENT_STATIC_METHODS)
    assert actual == expected, (
        f"SettlementTracker method drift:\n"
        f"  Missing from AST: {set(expected) - set(actual)}\n"
        f"  Extra in AST: {set(actual) - set(expected)}"
    )


def test_settlement_static_methods_decorated():
    """L33 — both staticmethods retain @staticmethod decorator post-move."""
    cls = _settlement_class()
    for m in ast.iter_child_nodes(cls):
        if isinstance(m, ast.FunctionDef) and m.name in SETTLEMENT_STATIC_METHODS:
            decos = [
                d.id for d in m.decorator_list
                if isinstance(d, ast.Name)
            ]
            assert "staticmethod" in decos, (
                f"SettlementTracker.{m.name} lost @staticmethod decorator post-extraction; "
                f"decorators present: {decos}"
            )


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — Method presence (parametrized)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method_name", SETTLEMENT_INSTANCE_METHODS + SETTLEMENT_STATIC_METHODS)
def test_settlement_method_present(method_name: str):
    """Every named method survives extraction."""
    import bot.settlement
    assert hasattr(bot.settlement.SettlementTracker, method_name), (
        f"SettlementTracker.{method_name} missing post-extraction"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 4 — Constants partition (parametrized, 8 entries)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("const_name", SETTLEMENT_BOT_CONSTANTS_NAMES)
def test_settlement_constant_partitioned_to_bot_constants(const_name: str):
    """L39 — every named constant lives in bot.constants (not config / bot.helpers / models)."""
    import bot.constants as bc
    assert hasattr(bc, const_name), (
        f"{const_name} not in bot.constants — partition drift; "
        f"check whether it actually lives in config / models / bot.helpers / market_config."
    )


def test_settlement_imports_constants_explicitly_not_via_star():
    """L40 — bot/settlement.py uses explicit `from bot.constants import (...)`, NOT star-import.
    Star-import laundering is the L40 smell that path-A++ extractions fix."""
    src = SETTLEMENT_PY.read_text()
    assert "from bot.constants import *" not in src, (
        "bot/settlement.py uses `from bot.constants import *` — replace with explicit imports "
        "per L40 (star-import laundering creates @patch-target drift)."
    )
    # Positive pin: the explicit import block exists
    assert re.search(
        r"from bot\.constants import \(", src
    ) or re.search(
        r"from bot\.constants import \w+", src
    ), (
        "bot/settlement.py missing `from bot.constants import (...)` block"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 5 — bot.helpers + models + market_config partition
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("helper_name", SETTLEMENT_BOT_HELPERS_STRINGS_NAMES)
def test_settlement_helper_partitioned_to_bot_helpers_strings(helper_name: str):
    """L78 — dollars_str_to_cents + fp_str_to_int live in bot.helpers.strings."""
    import bot.helpers.strings as bhs
    assert hasattr(bhs, helper_name), (
        f"{helper_name} not in bot.helpers.strings — partition drift"
    )


@pytest.mark.parametrize("model_name", SETTLEMENT_MODELS_NAMES)
def test_settlement_model_present(model_name: str):
    """L78 — calculate_fee/calculate_maker_fee/calculate_taker_fee live in models."""
    import bot.models as models
    assert hasattr(models, model_name), (
        f"{model_name} not in models — partition drift"
    )


def test_settlement_imports_market_config():
    """L78 — get_market_config (from market_config) used by SettlementTracker."""
    src = SETTLEMENT_PY.read_text()
    assert "get_market_config" in src, (
        "bot/settlement.py missing get_market_config reference"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 6 — L33 consumer-class identity pins
# ═════════════════════════════════════════════════════════════════════════════

def test_settlement_init_annotates_kalshi_client():
    """L33 — `client: KalshiClient` annotation survives extraction; bot.kalshi_client import."""
    src = SETTLEMENT_PY.read_text()
    assert "from bot.kalshi_client import KalshiClient" in src, (
        "bot/settlement.py missing `from bot.kalshi_client import KalshiClient` — "
        "needed for both __init__ annotation AND discover_active_windows() param annotation"
    )
    cls = _settlement_class()
    init = next(m for m in ast.iter_child_nodes(cls) if isinstance(m, ast.FunctionDef) and m.name == "__init__")
    client_arg = next((a for a in init.args.args if a.arg == "client"), None)
    assert client_arg is not None and client_arg.annotation is not None
    assert ast.unparse(client_arg.annotation) == "KalshiClient", (
        f"SettlementTracker.__init__ client annotation drift: {ast.unparse(client_arg.annotation)}"
    )


def test_settlement_init_annotates_state_manager():
    """L33 — `state: StateManager` annotation survives extraction; bot.state import."""
    src = SETTLEMENT_PY.read_text()
    assert "from bot.state import StateManager" in src
    cls = _settlement_class()
    init = next(m for m in ast.iter_child_nodes(cls) if isinstance(m, ast.FunctionDef) and m.name == "__init__")
    state_arg = next((a for a in init.args.args if a.arg == "state"), None)
    assert state_arg is not None and state_arg.annotation is not None
    assert ast.unparse(state_arg.annotation) == "StateManager"


def test_settlement_init_annotates_logger():
    """L33 — `logger: Logger` annotation survives extraction; bot.logger import."""
    src = SETTLEMENT_PY.read_text()
    assert "from bot.logger import Logger" in src
    cls = _settlement_class()
    init = next(m for m in ast.iter_child_nodes(cls) if isinstance(m, ast.FunctionDef) and m.name == "__init__")
    logger_arg = next((a for a in init.args.args if a.arg == "logger"), None)
    assert logger_arg is not None and logger_arg.annotation is not None
    assert ast.unparse(logger_arg.annotation) == "Logger"


def test_discover_active_windows_annotates_kalshi_client():
    """L33 — discover_active_windows(client: KalshiClient) annotation survives."""
    func = _settlement_func("discover_active_windows")
    client_arg = next((a for a in func.args.args if a.arg == "client"), None)
    assert client_arg is not None and client_arg.annotation is not None
    assert ast.unparse(client_arg.annotation) == "KalshiClient"


# ═════════════════════════════════════════════════════════════════════════════
# Section 7 — Forbidden-imports tuple
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("forbidden", FORBIDDEN_NUMERICAL_IMPORTS)
def test_settlement_no_forbidden_numerical_imports(forbidden: str):
    """numpy/scipy/torch/sklearn/pandas must NOT be top-level imports in bot/settlement.py
    (clean leaf; settlement is pure stdlib + sqlite3 + requests through KalshiClient)."""
    tree = _settlement_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden), (
                    f"bot/settlement.py has top-level `import {alias.name}` — "
                    f"{forbidden} is forbidden (per kb/failures/cal-mlp-torch-thread-contention-apr29.md)"
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith(forbidden), (
                f"bot/settlement.py has top-level `from {node.module} import ...` — "
                f"{forbidden} is forbidden"
            )


# ═════════════════════════════════════════════════════════════════════════════
# Section 8 — Path-A++ smell-fix pins for _append_raw_api_journal (L81 alias retired)
# ═════════════════════════════════════════════════════════════════════════════

def test_settlement_imports_append_raw_api_journal_explicitly():
    """bot/settlement.py uses the public name `append_raw_api_journal` (no underscore alias).

    Mirrors bot/executor.py:98 convention. Trailing comments allowed.
    """
    src = SETTLEMENT_PY.read_text()
    assert re.search(
        r"^from bot\.helpers\.raw_api_journal import append_raw_api_journal(?:\s|#|$)",
        src,
        re.MULTILINE,
    ), (
        "bot/settlement.py missing `from bot.helpers.raw_api_journal import append_raw_api_journal` "
        "(public name, no alias)"
    )
    # Negative: NO underscore-prefixed alias
    assert not re.search(
        r"from bot\.helpers\.raw_api_journal import append_raw_api_journal as _append_raw_api_journal",
        src,
    ), (
        "bot/settlement.py uses the L81 underscore alias — should use the public name directly "
        "(the alias was a Bit 9.1 transition device for bot/_impl.py only, retired in Bit 9.2)"
    )


def test_settlement_call_sites_use_public_name():
    """The 2 _append_raw_api_journal({...}) callers in SettlementTracker rewrite to public name."""
    src = SETTLEMENT_PY.read_text()
    # Negative: no underscore-prefixed call sites
    underscore_calls = re.findall(r"\b_append_raw_api_journal\s*\(", src)
    assert not underscore_calls, (
        f"bot/settlement.py still has {len(underscore_calls)} `_append_raw_api_journal(` call sites — "
        "rewrite to public name `append_raw_api_journal(` (Bit 9.2 atomic cleanup)."
    )
    # Positive: at least 2 public call sites (the 2 callers from bot/_impl.py:1132 + 1374)
    public_calls = re.findall(r"\bappend_raw_api_journal\s*\(", src)
    # subtract import-line occurrence
    assert len(public_calls) >= 2, (
        f"bot/settlement.py has only {len(public_calls)} `append_raw_api_journal(` call sites — "
        "expected ≥2 (the 2 SettlementTracker callers that moved with the class)"
    )


def test_l81_alias_import_dropped_from_bot_impl():
    """Bit 9.2 atomic cleanup: the L81 alias-import line at bot/_impl.py:285 is GONE."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = BOT_PY.read_text()
    assert not re.search(
        r"from bot\.helpers\.raw_api_journal import append_raw_api_journal as _append_raw_api_journal",
        src,
    ), (
        "bot/_impl.py still has the L81 alias-import "
        "`from bot.helpers.raw_api_journal import append_raw_api_journal as _append_raw_api_journal` — "
        "must be dropped atomically with Bit 9.2 (zero callers remain post-extraction)."
    )


def test_no_def_append_raw_api_journal_in_bot_impl():
    """Negative pin: the local `def _append_raw_api_journal` must remain absent from bot/_impl.py
    (was already the case post-Bit-9.1; this Bit ensures no regression)."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = BOT_PY.read_text()
    assert re.search(r"^def _append_raw_api_journal\(", src, re.MULTILINE) is None, (
        "bot/_impl.py has local `def _append_raw_api_journal(...)` — should not exist post-Bit-9.1"
    )


def test_no_underscore_call_sites_in_bot_impl():
    """Bit 9.2 atomic: the 2 `_append_raw_api_journal({...})` call sites in bot/_impl.py
    (formerly inside SettlementTracker) MUST be gone post-extraction."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = BOT_PY.read_text()
    underscore_calls = re.findall(r"\b_append_raw_api_journal\s*\(", src)
    assert not underscore_calls, (
        f"bot/_impl.py still has {len(underscore_calls)} `_append_raw_api_journal(` call site(s) — "
        "all should have moved to bot/settlement.py (with the underscore prefix dropped to "
        "match the public-name convention)."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 9 — telegram_state alias pin (mirrors scanner/executor)
# ═════════════════════════════════════════════════════════════════════════════

def test_settlement_uses_telegram_state_alias():
    """bot/settlement.py reads _TELEGRAM exclusively via _telegram_state._TELEGRAM
    (Bit 8.1 path-A++ pattern). Plain `from bot.notifier import _TELEGRAM` would
    capture by value and freeze at None when MainLoop.__init__ later mutates."""
    src = SETTLEMENT_PY.read_text()
    # Must NOT have bare-name _TELEGRAM reads
    bare_reads = re.findall(r"(?<![.\w])_TELEGRAM(?!\w)", src)
    assert not bare_reads, (
        f"bot/settlement.py has {len(bare_reads)} bare-name _TELEGRAM read(s) — "
        f"all reads must go through `_telegram_state._TELEGRAM` (per L83 module-attribute access)"
    )
    # MUST have `import bot.notifier as _telegram_state`
    assert re.search(
        r"^import bot\.notifier as _telegram_state(?:\s|#|$)",
        src,
        re.MULTILINE,
    ), (
        "bot/settlement.py missing `import bot.notifier as _telegram_state` — "
        "required for module-attribute access pattern (per L84: explicit `import bot.X as ...` "
        "form, NOT `from bot import notifier as ...` which goes through _BotProxy.__getattr__)"
    )


def test_settlement_uses_cal_state_alias():
    """bot/settlement.py uses _cal_state for _CALIBRATION_ENGINE / _resolve_cal_engine
    (Bit 6.3 path-B pattern)."""
    src = SETTLEMENT_PY.read_text()
    assert re.search(
        r"^from bot\.engines import calibration as _cal_state(?:\s|#|$)",
        src,
        re.MULTILINE,
    ), (
        "bot/settlement.py missing `from bot.engines import calibration as _cal_state` — "
        "required for module-attribute access to _CALIBRATION_ENGINE / _resolve_cal_engine "
        "(Bit 6.3 path-B pattern)"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 10 — bot/notifier.py 4-consumer enumeration (Bit 9.2 atomic update)
# ═════════════════════════════════════════════════════════════════════════════

def test_notifier_docstring_enumerates_four_consumers():
    """Bit 9.2 atomic: every consumer-aliasing surface must enumerate FOUR
    consumers (adds bot/settlement.py). L86 contagion seal — R1 caught the
    Bit 8.1 paragraph in bot/CLAUDE.md; R2 caught two more in
    bot/scanner/__init__.py + bot/__init__.py. This guard walks ALL
    consumer-aliasing files and rejects the broader negative pattern set."""
    BOT_CLAUDE = REPO_ROOT / "bot" / "CLAUDE.md"
    BOT_LAYOUT = REPO_ROOT / "agent_docs" / "bot_layout.md"
    SCANNER_INIT = REPO_ROOT / "bot" / "scanner" / "__init__.py"
    SCANNER_CLAUDE = REPO_ROOT / "bot" / "scanner" / "CLAUDE.md"
    BOT_INIT = REPO_ROOT / "bot" / "__init__.py"
    sites = [NOTIFIER_PY, BOT_CLAUDE, BOT_LAYOUT, SCANNER_INIT, SCANNER_CLAUDE, BOT_INIT]
    # Sites that should explicitly enumerate bot/settlement.py as a consumer.
    # (Some files mention bot/settlement.py only via reference; require it
    # everywhere to keep the seal tight.)
    positive_required = {NOTIFIER_PY, BOT_CLAUDE, BOT_LAYOUT, SCANNER_INIT, BOT_INIT}
    # Negative patterns that lock the consumer-list narrative against drift.
    stale_patterns = (
        "THREE consumer", "all three of", "all three consumers",
        "across all three", "3 consumer modules",
        "Both bot._impl and bot.scanner",
        "Both `bot._impl` and `bot.scanner`",
        "all three reach", "all three of bot",
    )
    for site in sites:
        src = site.read_text()
        if site in positive_required:
            assert "bot/settlement.py" in src or "bot.settlement" in src, (
                f"{site} missing bot/settlement.py / bot.settlement consumer "
                "enumeration (Bit 9.2 path-A++ relocation; SettlementTracker "
                "has 4 read sites)"
            )
        for stale in stale_patterns:
            assert stale not in src, (
                f"{site} still contains stale '{stale}' enumeration — Bit 9.2 "
                "narratives must enumerate FOUR consumers (adds bot/settlement.py)"
            )


def test_bot_init_docstring_mentions_settlement_consumer():
    """Bit 9.2 atomic: bot/__init__.py docstring extends consumer list to include bot.settlement."""
    src = INIT_PY.read_text()
    assert "bot.settlement" in src or "bot/settlement.py" in src, (
        "bot/__init__.py docstring missing bot.settlement reference (Bit 9.2 path-A++ extension)"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 11 — re-export back to bot/_impl.py works
# ═════════════════════════════════════════════════════════════════════════════

def test_bot_impl_reexports_settlement_tracker():
    """Bit 9.2 re-export pattern: `from bot.settlement import SettlementTracker` in bot/_impl.py."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = BOT_PY.read_text()
    assert re.search(
        r"from bot\.settlement import .*SettlementTracker", src
    ), "bot/_impl.py missing `from bot.settlement import SettlementTracker, ...` re-export"


def test_bot_impl_reexports_discover_active_windows():
    """Bit 9.2 re-export: discover_active_windows accessible via bot._impl re-export."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = BOT_PY.read_text()
    assert re.search(
        r"from bot\.settlement import .*discover_active_windows", src
    ), "bot/_impl.py missing `from bot.settlement import ..., discover_active_windows` re-export"


# ═════════════════════════════════════════════════════════════════════════════
# Section 12 — .importlinter contract count unchanged (no new carve-out needed)
# ═════════════════════════════════════════════════════════════════════════════

def test_no_settlement_no_impl_toplevel_contract_added():
    """SettlementTracker is a clean leaf — no `settlement-no-impl-toplevel` carve-out needed.
    Net contracts stays at 5 (bot-side) post-Bit-9.2; D1.1 (2026-05-16) added
    the unrelated `collector-no-bot` 6th contract for the Data Corpus
    initiative, so the live count assertion below is now 6."""
    config = configparser.ConfigParser()
    config.read(IMPORTLINTER_INI)
    contracts = [s for s in config.sections() if s.startswith("importlinter:contract:")]
    contract_names = {s.split(":")[-1] for s in contracts}
    assert "settlement-no-impl-toplevel" not in contract_names, (
        "Unexpected `settlement-no-impl-toplevel` contract added — SettlementTracker is a "
        "clean leaf (no late-binding required); the contract should NOT exist."
    )
    # Sanity: 10 contracts total post-D2.1 (2026-05-17). The 5 bot-side
    # contracts post-Bit-9.3-iii.c (fetchers-no-engines, feeds-no-engines,
    # helpers-leaf, bot-no-torch, bot-no-pandas — engines-no-impl was
    # retired when bot/_impl.py was DELETED) plus D1.1's `collector-no-bot`
    # (Data Corpus initiative, ticket 86b9ypn49) plus D1.1.5's two
    # contracts (ticket 86b9zdhz2): `kalshi_wire-no-bot` +
    # `kalshi_wire-no-collector` (shared transport library, 2026-05-16
    # AMENDMENT to D0.3 §5) plus D2.1's two new contracts (ticket
    # 86b9zkpc6): `coinbase_wire-no-bot` + `coinbase_wire-no-collector`
    # (Coinbase wire scaffolding, sub-Bit of the 86b9zkkv4 D2.x umbrella).
    # This test's intent — "no settlement-no-impl-toplevel carve-out was
    # added" — is unchanged; only the unrelated 9th + 10th contracts bump
    # the count.
    assert len(contracts) == 10, (
        f".importlinter has {len(contracts)} contracts; expected 10 "
        f"post-D2.1 (5 bot-side + collector-no-bot + kalshi_wire-no-bot + "
        f"kalshi_wire-no-collector + coinbase_wire-no-bot + "
        f"coinbase_wire-no-collector). Contracts present: "
        f"{sorted(contract_names)}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 13 — Behavioral smoke (instantiate via mocks)
# ═════════════════════════════════════════════════════════════════════════════

def test_settlement_instantiates_via_mocks():
    """Behavioral smoke: SettlementTracker can be constructed with mock dependencies.
    Catches L78 free-var residuals at construction time."""
    import bot
    import bot.settlement  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.settlement.X access)
    client_mock = MagicMock()
    state_mock = MagicMock()
    logger_mock = MagicMock()
    tracker = bot.settlement.SettlementTracker(client_mock, state_mock, logger_mock)
    # __init__ sets these
    assert tracker._client is client_mock
    assert tracker._state is state_mock
    assert tracker._logger is logger_mock
    assert tracker._ml is None  # main_loop default
    assert tracker._last_check_ts == 0
    assert tracker._processed_tickers == set()
    assert tracker._pending_rejection_tickers == set()
    assert tracker._worker_running is False


def test_discover_active_windows_callable_via_proxy():
    """Behavioral smoke: discover_active_windows is callable through the bot proxy."""
    import bot
    import bot.settlement  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.settlement.X access)
    # Just verify it's a callable function with the right signature
    assert callable(bot.settlement.discover_active_windows)
    sig = inspect.signature(bot.settlement.discover_active_windows)
    params = list(sig.parameters.keys())
    assert params == ["client"], f"discover_active_windows signature drift: {params}"


# ═════════════════════════════════════════════════════════════════════════════
# Section 14 — Bundled best_ask UnboundLocalError fix (closes ticket 86b9vppn3)
# ═════════════════════════════════════════════════════════════════════════════

def test_scanner_initializes_best_ask_in_iteration():
    """Ride-along ticket 86b9vppn3: pre-existing UnboundLocalError 'best_ask' in
    OpportunityScanner.scan() low_probability_15m insert_rejection branch.

    Predates Bit 8.1 per git blame. Fix: initialize `best_ask = None` at iteration start.
    Test pattern: assert the per-window iteration block contains a `best_ask = None`
    initializer BEFORE any reference to `best_ask` (which would otherwise raise
    UnboundLocalError when the cal_prob < min_prob_needed early-return branch fires
    before the orderbook fetch sets best_ask)."""
    src = SCANNER_PY.read_text()
    # Find the per-window iteration loop in scan() — search anchor: the early-iteration setup block
    # The fix should appear as an explicit `best_ask = None` (and likely `best_ask_source = None`)
    # near the top of each window iteration.
    assert re.search(r"\bbest_ask\s*=\s*None\b", src), (
        "bot/scanner/__init__.py missing `best_ask = None` initializer (ticket 86b9vppn3) — "
        "the low_probability_15m insert_rejection branch references best_ask before assignment, "
        "raising UnboundLocalError when cal_prob < min_prob_needed early-return fires."
    )
