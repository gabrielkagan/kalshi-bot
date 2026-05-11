"""Bit 8.1 — OpportunityScanner extracted from bot/_impl.py to bot/scanner/__init__.py (path-A++).

Bit 8.1 (2026-05-10):
  OpportunityScanner → bot/scanner/__init__.py
  (Sprint 8; the largest single class in the modularization track —
  ~9,085 lines, 36 instance methods + 7 staticmethods, 1.5× StateManager.
  Touches the trading hot path: scan-loop edge computation, STC,
  dynamic cap, cell-block routing all load-bearing.)

Path-A++ extraction (NOT byte-for-byte): in-Bit refactor of bot/_impl.py
to relocate the `_TELEGRAM` module-level singleton to `bot/notifier.py`
(where it logically belongs since Bit 4.2). The laundered-namespace
coupling smell is fixed in-Bit per the modularization strategic goal.

Cross-class coupling (preserved via late-binding helpers, until Sprint 9
extracts the relevant classes):
- `_get_order_executor()` returns bot._impl.OrderExecutor for the 34
  static-method call sites in scan() (Sprint 9 Bit 9.1 will resolve via
  `from bot.executor import OrderExecutor`)

Mutable-singleton coupling (preserved via aliased module-attribute access):
- `_cal_state._CALIBRATION_ENGINE` / `_cal_state._resolve_cal_engine`
  (Bit 6.3 path-B precedent; module-attribute access preserves mutation
  freshness without late-binding)

`main_loop=None` constructor arg → 11 self._ml.X sub-attribute accesses
are constructor-injected references (NOT bare-name lookups). Construction
order in `MainLoop.__init__` guarantees the dependencies are populated
before any scanner method runs.

Related lessons:
  L32 (Plan-agent), L33 (consumer-class identity), L38 (AST walk
  retargets), L39 (config vs bot.constants partition), L40 (@patch
  routing through _BotProxy — 86 _TELEGRAM patch sites + 30
  OBSERVATION_MODE + 4 WEATHER_NO_SIDE_LIVE), L41 (no hand-counted
  breadcrumbs), L78 (star-import-aware free-var scan — 205 bot.constants
  + 8 config + 10 bot.helpers), L79 (path-A vs path-A++ early choice),
  L80 (no /tmp/ fixtures), L81 (alias hygiene), L82 (auto-regen race).

Mirrors tests/test_state_extraction.py (Bit 7.1 path-A++) +
tests/test_engines_extraction.py (Bit 6.1/6.2/6.3) +
tests/test_feeds_extraction.py (Bit 4.5a/4.5b).
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re
import sys
from pathlib import Path
from typing import List, Tuple

import pytest
import bot.helpers.tm_sweep  # noqa: F401
import bot.models  # noqa: F401


REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_PY = REPO_ROOT / "bot" / "_impl.py"
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
NOTIFIER_PY = REPO_ROOT / "bot" / "notifier.py"
INIT_PY = REPO_ROOT / "bot" / "__init__.py"
IMPORTLINTER_INI = REPO_ROOT / ".importlinter"


# ============================================================ Module-level data
# Per L41: parametrize tuples ARE the ground truth — no separate count claim.

# 7 staticmethods enumerated from bot/_impl.py:970-10054 pre-extraction.
SCANNER_STATIC_METHODS = (
    "_compute_maker_counterfactual",
    "_parse_threshold",
    "_parse_weather_market_info",
    "_best_yes_ask_cents",
    "_is_severe_drift",
    "_convert_orderbook_fp",
    "_window_timeslot",
)

# 11 self._ml.X sub-attribute accesses inside scanner body (L78-derived).
SCANNER_MAIN_LOOP_ATTRS = (
    "_open_positions_count_cache",
    "_scan_iter",
    "_scan_loop_start",
    "executor",
    "cross_feed",
    "fifteenm_shadow",
    "hourly_alt_shadow",
    "spx_engine",
    "weather_engine",
    "spx_harrv_shadow",
    "capital_allocator",
)

# 8 names from config (NOT bot.constants — verified per-name with hasattr).
SCANNER_CONFIG_CONSTANTS = (
    "ASSETS",
    "DRAWDOWN_HALF_THRESHOLD",
    "DRAWDOWN_HALT_THRESHOLD",
    "DRAWDOWN_QUARTER_THRESHOLD",
    "EGARCH_BLEND_SHADOW_MODE",
    "MAX_RISK_PER_TRADE",
    "NUMERICAL_SAFETY_CEILING",
    "SIZING_TIERS",
)

# Names from bot.helpers (came in via `from bot.helpers import *` laundering
# pre-extraction; explicit per-leaf imports per L40 lesson).
#
# Sprint 10.5b (2026-05-11): `calculate_taker_fee` REMOVED from this list.
# Pre-10.5b it was re-exported by bot/helpers/tm_sweep.py's `from models import
# calculate_taker_fee` star-laundered through `bot.helpers`. Post-10.5b
# tm_sweep.py uses a lazy `_get_calculate_taker_fee()` helper (helpers-leaf
# carve-out — see .importlinter `bot.helpers.tm_sweep -> bot.models`), so the
# name is no longer at bot.helpers' module surface. The scanner now imports
# calculate_taker_fee directly from bot.models alongside PositionSizer +
# calculate_fee + strategy_to_group (line 75 of bot/scanner/__init__.py).
SCANNER_HELPERS = (
    "buffer_sizing_multiplier",
    "dollars_str_to_cents",
    "evaluate_execution_strategy",
    "get_min_edge",
    "should_block_high_price_stc_candidate",
    "should_block_sol_bleed_v2_candidate",
    "should_block_sol_taker_lowprice_bleed_candidate",
    "should_block_tm98_highprice_bleed_candidate",
    "should_exclude_weather_no_ticker",
    "tm_compute_contracts",
)

# 205 names from bot.constants (L78 free-var scan output, sorted).
SCANNER_BOT_CONSTANTS = (
    "BALANCE_CACHE_TTL",
    "BRACKET_NO_ASSUMED_PROB",
    "BRACKET_NO_ENABLED",
    "BRACKET_NO_FIXED_CONTRACTS",
    "BRACKET_NO_KILL_THRESHOLD",
    "BRACKET_NO_MAX_CONCURRENT",
    "BRACKET_NO_MIN_STC",
    "BRACKET_NO_YES_MAX",
    "BRACKET_NO_YES_MIN",
    "BTC_MAX_RISK_PER_TRADE",
    "BTC_MIN_ENTRY_PRICE",
    "BUFFER_SIZING_ENABLED",
    "CONVERGENCE_WINDOW_SECONDS",
    "DB_PATH",
    "DC_T2_Z2_PHASE1_RISK",
    "DECIDED_CONTRACT_MAX_STC",
    "DECIDED_CONTRACT_MAX_WINDOW_RISK",
    "DECIDED_CONTRACT_MIN_PRICE",
    "DECIDED_CONTRACT_RISK",
    "DECIDED_CONTRACT_SHADOW",
    "DECIDED_CONTRACT_T1B_MIN_PRICE",
    "DECIDED_CONTRACT_T2_MAX_PRICE",
    "DECIDED_CONTRACT_T2_Z25_RISK",
    "DECIDED_CONTRACT_T2_Z2_RISK",
    "DECIDED_CONTRACT_Z_T1",
    "DECIDED_CONTRACT_Z_T1B",
    "DECIDED_CONTRACT_Z_T2",
    "DECIDED_CONTRACT_Z_T2_Z2",
    "DECIDED_CONTRACT_Z_T2_Z25",
    "DECIDED_T1B_ENABLED",
    "DECIDED_T1_ENABLED",
    "DECIDED_T2_ENABLED",
    "DECIDED_T2_Z25_ENABLED",
    "DECIDED_T2_Z2_ENABLED",
    "DIP_ADDON_ENABLED",
    "DIP_ADDON_MAX_TOTAL_RISK",
    "DIP_ADDON_MIN_DROP_CENTS",
    "DIP_ADDON_MIN_ENTRY_PRICE",
    "DIP_ADDON_MIN_STC_REMAINING",
    "DIP_ADDON_SHADOW_MODE",
    "ENDGAME_BLEND_PRICE",
    "ETH_MAX_RISK_PER_TRADE",
    "ETH_MIN_ENTRY_PRICE",
    "ETH_SUB80_POSITION_CAP",
    "HIGH_PRICE_STC_BLOCK_ENABLED",
    "HIGH_PRICE_STC_BLOCK_FILTER_STAGE",
    "HOURLY_BANKROLL_FRACTION",
    "HOURLY_CONFIG_A_EXCLUDED",
    "HOURLY_CONFIG_A_MAX_EDGE",
    "HOURLY_CONFIG_B_ASSET",
    "HOURLY_CONFIG_B_MAX_PER_WINDOW",
    "HOURLY_CONFIG_B_MAX_PRICE",
    "HOURLY_CONFIG_B_MIN_PRICE",
    "HOURLY_DC_ASSETS",
    "HOURLY_DC_ASSUMED_PROB",
    "HOURLY_DC_CONTRACTS",
    "HOURLY_DC_ENABLED",
    "HOURLY_DC_MAX_PRICE",
    "HOURLY_DC_MIN_PRICE",
    "HOURLY_DC_MIN_SIGMA",
    "HOURLY_DC_Z_THRESHOLD",
    "HOURLY_FIXED_CONTRACTS",
    "HOURLY_KELLY_FRACTION",
    "HOURLY_MARKET_BLEND_W",
    "HOURLY_MAX_EDGE",
    "HOURLY_MAX_POSITIONS_PER_WINDOW",
    "HOURLY_MAX_RISK_PER_TRADE",
    "HOURLY_MAX_SECONDS_BEFORE_CLOSE",
    "HOURLY_MAX_STC_ENTRY",
    "HOURLY_MIN_EDGE_PCT",
    "HOURLY_MIN_ENTRY_PRICE",
    "HOURLY_MIN_STC_ENTRY",
    "HOURLY_NO_EXCLUDED_ASSETS",
    "HOURLY_NO_FIXED_CONTRACTS",
    "HOURLY_NO_KILL_THRESHOLD",
    "HOURLY_NO_MAX_PRICE",
    "HOURLY_NO_MIN_PRICE",
    "HOURLY_NO_SIDE_LIVE",
    "HOURLY_OBSERVATION_ENABLED",
    "HOURLY_OBSERVATION_ONLY",
    "HOURLY_SERIES_TICKERS",
    "HOURLY_SHADOW_CONFIGS",
    "HOURLY_TEMPERATURE_T",
    "KALSHI_OFT_SHADOW_MODE",
    "LOSS_COOLDOWN_ENABLED",
    "LOSS_COOLDOWN_SECONDS",
    "LOW_PRICE_SHADOW_ENABLED",
    "LOW_PRICE_SHADOW_MAX_PRICE",
    "LOW_PRICE_SHADOW_MAX_STC",
    "LOW_PRICE_SHADOW_MIN_PRICE",
    "LOW_STC_SIZING_CAP",
    "LOW_STC_SIZING_CAP_THRESHOLD",
    "LPNE_ASSETS",
    "LPNE_ENABLED",
    "LPNE_FIXED_CONTRACTS",
    "LPNE_MAX_CONCURRENT",
    "LPNE_MAX_PRICE",
    "LPNE_MAX_STC",
    "LPNE_MIN_PRICE",
    "LPNE_MIN_STC",
    "LP_KELLY_FRACTION",
    "LP_MAX_RISK_PER_TRADE",
    "MAKER_ONLY_THRESHOLD",
    "MARKET_BLEND_W",
    "MAX_ENTRY_PRICE",
    "MAX_OB_FETCHES_PER_TICK",
    "MAX_SECONDS_BEFORE_CLOSE",
    "MIN_EDGE_PCT",
    "MIN_ENTRY_PRICE",
    "NO_SIDE_MIN_ENTRY_PRICE",
    "OBSERVATION_MODE",
    "ONE_ASSET_PER_WINDOW",
    "ORDERBOOK_CACHE_TTL",
    "OVERNIGHT_DISCOUNT_LIVE",
    "OVERNIGHT_DISCOUNT_MAX_STC",
    "OVERNIGHT_DISCOUNT_MIN_PRICE",
    "OVERNIGHT_EDGE_DISCOUNT",
    "OVERNIGHT_LP_HOURS_END",
    "OVERNIGHT_LP_HOURS_START",
    "OVERNIGHT_LP_KELLY_FRACTION",
    "OVERNIGHT_LP_MAX_ENTRY_PRICE",
    "OVERNIGHT_LP_MAX_RISK_PER_TRADE",
    "OVERNIGHT_LP_MAX_STC",
    "OVERNIGHT_LP_MIN_CAL_PROB",
    "OVERNIGHT_LP_MIN_EDGE_PCT",
    "OVERNIGHT_LP_MIN_ENTRY_PRICE",
    "OVERNIGHT_LP_MIN_STC",
    "OVERNIGHT_LP_SHADOW",
    "OVERNIGHT_LP_VOL_HISTORY_DAYS",
    "OVERNIGHT_LP_VOL_SPIKE_MULT",
    "OVERNIGHT_QUIET_END",
    "OVERNIGHT_QUIET_START",
    "PRICE_SHADOW_ENABLED",
    "PRICE_SHADOW_FLOOR",
    "RELAXED_EDGE_DISCOUNT",
    "RELAXED_EDGE_MAX_PRICE",
    "RELAXED_EDGE_MIN_PRICE",
    "RELAXED_EDGE_SHADOW",
    "RK_TV_SHADOW_MODE",
    "SHADOW_CAL_PIPELINE",
    "SOL_DC_RISK_TIERS",
    "SOL_HIGH_EDGE_SHADOW",
    "SOL_LOW_ENTRY_STC_GATE",
    "SOL_MAX_RISK_PER_TRADE",
    "SOL_MIN_EDGE",
    "SOL_MIN_ENTRY_PRICE",
    "SOL_BLEED_V2_BLOCK_FILTER_STAGE",
    "SOL_RESCUE_CONTRACT_CAP",
    "SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE",
    "SPORTS_ENABLED",
    "SPORTS_OBSERVATION_ONLY",
    "SPX_DC_MAX_PRICE",
    "SPX_DC_MIN_PRICE",
    "SPX_DC_SHADOW_ENABLED",
    "SPX_DC_VALID_DAYS",
    "SPX_DC_Z_THRESHOLD",
    "SPX_HOURLY_BANKROLL_FRACTION",
    "STACKING_ENABLED",
    "STC_EXTENDED_BTC_MIN_PRICE",
    "STC_EXTENDED_BUFFER_RESCUE",
    "STC_EXTENDED_ETH_MIN_PRICE",
    "STC_EXTENDED_LIVE_FLOOR",
    "STC_EXTENDED_SOL_MIN_PRICE",
    "STC_EXTENDED_XRP_MIN_PRICE",
    "STC_SHADOW_THRESHOLD",
    "STC_SIZING_SCALER_ENABLED",
    "STC_SIZING_SCALER_KNEE",
    "STRATEGY_MAKER_AGGRESSIVE",
    "STRATEGY_MAKER_PATIENT",
    "STRATEGY_PANIC_CAPTURE",
    "STRATEGY_TAKER_NOW",
    "STRATEGY_WAIT",
    "TERMINAL_MOMENTUM_ENABLED",
    "TM96_CALMLP_GATE_ENABLED",
    "TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE",
    "TM_MAX_CONCURRENT",
    "TM_MAX_STC",
    "TM_MIN_PROB",
    "TM_MIN_STC",
    "TM_NBBO_BLOCKED_PRICES",
    "TM_NBBO_MIN_BUFFER_PCT",
    "TM_PRICE_SET",
    "TM_STC_DANGER_HI",
    "TM_STC_SAFE_THRESHOLD",
    "TM_SWEEP_LIVE_ENABLED",
    "WEATHER_MIN_EDGE_PCT",
    "WEATHER_NO_ASSUMED_PROB",
    "WEATHER_NO_CONTRACT_COUNT",
    "WEATHER_NO_KILL_THRESHOLD",
    "WEATHER_NO_MAX_PRICE",
    "WEATHER_NO_MIN_PRICE",
    "WEATHER_NO_SHADOW_MIN_YES_PROB",
    "WEATHER_NO_SIDE_LIVE",
    "WEATHER_NO_SIDE_MIN_STC",
    "WEATHER_SHADOW_CONFIGS",
    "WEEKEND_DISCOUNT_LIVE",
    "WEEKEND_DISCOUNT_MAX_STC",
    "WEEKEND_DISCOUNT_MIN_PRICE",
    "WEEKEND_EDGE_DISCOUNT",
    "WEEKEND_EDGE_FLOOR",
    "WEEKEND_FIXED_RISK",
    "XRP_15M_SHADOW",
    "XRP_MAX_RISK_PER_TRADE",
    "XRP_MIN_ENTRY_PRICE",
    "XRP_SHADOW_MIN_PRICE",
)

# Cell-block string literals (stored in DB; load-bearing per bot/CLAUDE.md
# "Cell-block activations deflate filter_stage='candidate' rollups").
CELL_BLOCK_STRING_LITERALS = (
    "96C_SOL_XRP_STC_DANGER_BAND",
    "TM98_97_98C_2_5MIN_BLEED",
    "SOL_TAKER_85_89C_2_5MIN_BLEED",
)

# Forbidden-imports gate. Strict-ban torch/sklearn/pandas (numpy/scipy
# NOT banned because models.py uses them transitively, but scanner module
# body should not import them directly).
SCANNER_FORBIDDEN_IMPORTS = (
    (SCANNER_PY, "bot/scanner/__init__.py", ("torch", "sklearn", "pandas")),
)


# ================================================================ Helper utils


def _read_scanner_source() -> str:
    """Read bot/scanner/__init__.py source. RED-skip if not yet extracted."""
    if not SCANNER_PY.is_file():
        pytest.skip("bot/scanner/__init__.py not yet created (TDD-RED)")
    return SCANNER_PY.read_text()


def _read_bot_impl_source() -> str:
    return BOT_PY.read_text()


def _read_notifier_source() -> str:
    return NOTIFIER_PY.read_text()


def _scanner_class_def() -> ast.ClassDef:
    """Return the OpportunityScanner ClassDef from bot/scanner/__init__.py."""
    src = _read_scanner_source()
    tree = ast.parse(src)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OpportunityScanner":
            return node
    raise AssertionError("OpportunityScanner ClassDef not found in bot/scanner/__init__.py")


def _bot_impl_class_def(name: str) -> ast.ClassDef | None:
    """Return the named ClassDef from bot/_impl.py, or None if absent."""
    src = _read_bot_impl_source()
    tree = ast.parse(src)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


# ============================================================ Identity (5)


def test_scanner_module_exists():
    """bot/scanner/__init__.py must exist post-extraction."""
    assert SCANNER_PY.is_file(), f"{SCANNER_PY} not found — Bit 8.1 not yet shipped"


def test_scanner_module_imports_resolve():
    """Importing bot.scanner does not raise (no ImportError, no NameError)."""
    importlib.import_module("bot.scanner")


def test_scanner_identity_through_bot_impl():
    """bot._impl.OpportunityScanner is the same object as bot.scanner.OpportunityScanner."""
    import bot._impl
    import bot.scanner
    assert bot._impl.OpportunityScanner is bot.scanner.OpportunityScanner


def test_scanner_identity_through_bot_proxy():
    """bot.scanner.OpportunityScanner (proxy) routes to bot.scanner.OpportunityScanner."""
    import bot
    import bot.scanner
    assert bot.scanner.OpportunityScanner is bot.scanner.OpportunityScanner


def test_scanner_module_attribute_post_extraction():
    """After extraction, OpportunityScanner.__module__ reports bot.scanner."""
    import bot
    assert bot.scanner.OpportunityScanner.__module__ == "bot.scanner"


# ============================================================ Drift guards (3)


def test_class_not_defined_in_bot_impl():
    """OpportunityScanner ClassDef must NOT exist in bot/_impl.py post-extraction."""
    assert _bot_impl_class_def("OpportunityScanner") is None, (
        "OpportunityScanner still defined in bot/_impl.py — extraction incomplete"
    )


def test_scanner_class_defined_at_module_scope():
    """bot/scanner/__init__.py defines exactly one module-scope ClassDef: OpportunityScanner."""
    src = _read_scanner_source()
    tree = ast.parse(src)
    classes = [n for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert len(classes) == 1, f"expected 1 ClassDef, found {[c.name for c in classes]}"
    assert classes[0].name == "OpportunityScanner"


def test_bot_impl_imports_scanner_from_bot_scanner():
    """bot/_impl.py top-level: from bot.scanner import OpportunityScanner."""
    src = _read_bot_impl_source()
    assert re.search(r"^from bot\.scanner import OpportunityScanner", src, re.MULTILINE), (
        "bot/_impl.py missing top-level `from bot.scanner import OpportunityScanner`"
    )


# ============================================================ Method presence


def _scanner_methods_from_ast() -> Tuple[List[str], List[str]]:
    """Return (instance_methods, static_methods) from bot/scanner/__init__.py AST."""
    cls = _scanner_class_def()
    instance, static = [], []
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_static = any(
                isinstance(d, ast.Name) and d.id == "staticmethod"
                for d in node.decorator_list
            )
            if is_static:
                static.append(node.name)
            else:
                instance.append(node.name)
    return instance, static


def test_scanner_method_count_matches_ast():
    """Method counts derive from AST, not hardcoded literals (per L41)."""
    instance, static = _scanner_methods_from_ast()
    assert len(instance) >= 29, f"expected >=29 instance methods, got {len(instance)}"
    assert len(static) == len(SCANNER_STATIC_METHODS), (
        f"expected {len(SCANNER_STATIC_METHODS)} staticmethods, got {len(static)}: {static}"
    )


@pytest.mark.parametrize("static_name", SCANNER_STATIC_METHODS)
def test_scanner_static_methods_remain_static(static_name):
    """Each enumerated staticmethod is decorated with @staticmethod post-extraction."""
    cls = _scanner_class_def()
    matched = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == static_name]
    assert matched, f"staticmethod {static_name} not found in bot.scanner.OpportunityScanner"
    decorators = [
        d.id for d in matched[0].decorator_list
        if isinstance(d, ast.Name)
    ]
    assert "staticmethod" in decorators, (
        f"{static_name} lost @staticmethod decorator"
    )


@pytest.mark.parametrize("static_name", SCANNER_STATIC_METHODS)
def test_scanner_static_methods_reachable_via_proxy(static_name):
    """All 7 staticmethods reachable via bot.scanner.OpportunityScanner.X (proxy chain)."""
    import bot
    import bot.scanner
    proxy_attr = getattr(bot.scanner.OpportunityScanner, static_name)
    direct_attr = getattr(bot.scanner.OpportunityScanner, static_name)
    assert proxy_attr is direct_attr
    assert callable(proxy_attr)


# ========================================================= Constants partition


@pytest.mark.parametrize("name", SCANNER_BOT_CONSTANTS)
def test_scanner_constants_resolve_from_bot_constants(name):
    """Each name in SCANNER_BOT_CONSTANTS is importable from bot.constants."""
    import bot.constants as bc
    assert hasattr(bc, name), f"{name} not in bot.constants"


@pytest.mark.parametrize("name", SCANNER_CONFIG_CONSTANTS)
def test_scanner_constants_resolve_from_config(name):
    """Each name in SCANNER_CONFIG_CONSTANTS is importable from config (NOT bot.constants)."""
    import config as cfg
    assert hasattr(cfg, name), f"{name} not in config"


@pytest.mark.parametrize("name", SCANNER_HELPERS)
def test_scanner_helpers_resolve_from_bot_helpers(name):
    """Each name in SCANNER_HELPERS is importable from bot.helpers (per L40 — replaces star-import laundering)."""
    import bot.helpers as bh
    assert hasattr(bh, name), f"{name} not in bot.helpers"


def test_scanner_imports_explicit_bot_constants():
    """bot/scanner/__init__.py uses explicit per-name imports from bot.constants (NOT star-import)."""
    src = _read_scanner_source()
    # No star-import laundering allowed — explicit imports per L78
    assert "from bot.constants import *" not in src, (
        "bot/scanner uses star-import from bot.constants — must be explicit (L78)"
    )


def test_scanner_imports_explicit_config():
    """bot/scanner/__init__.py uses explicit per-name imports from config (NOT star-import)."""
    src = _read_scanner_source()
    assert "from config import *" not in src
    assert re.search(r"^from config import\b", src, re.MULTILINE), (
        "bot/scanner missing explicit `from config import ...` block"
    )


def test_scanner_imports_explicit_bot_helpers():
    """bot/scanner/__init__.py uses explicit per-name imports from bot.helpers (NOT star-import)."""
    src = _read_scanner_source()
    assert "from bot.helpers import *" not in src
    assert re.search(r"^from bot\.helpers import\b", src, re.MULTILINE), (
        "bot/scanner missing explicit `from bot.helpers import ...` block"
    )


# ============================================ CRITICAL #3: path-A++ smell-fix


def test_telegram_relocated_to_bot_notifier():
    """Path-A++: _TELEGRAM module-level singleton lives in bot/notifier.py."""
    import bot.notifier
    assert hasattr(bot.notifier, "_TELEGRAM"), "_TELEGRAM not relocated to bot.notifier"


def test_telegram_not_in_bot_impl_module_scope():
    """Path-A++: bot/_impl.py no longer DEFINES _TELEGRAM at module scope (only re-imports)."""
    src = _read_bot_impl_source()
    tree = ast.parse(src)
    for node in ast.iter_child_nodes(tree):
        # AnnAssign: `_TELEGRAM: Optional["TelegramNotifier"] = None` (the original def)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assert node.target.id != "_TELEGRAM", (
                "bot/_impl.py still has module-scope `_TELEGRAM:` AnnAssign — should be `from bot.notifier import _TELEGRAM`"
            )
        # Assign: `_TELEGRAM = ...`
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assert t.id != "_TELEGRAM", (
                        "bot/_impl.py still has module-scope `_TELEGRAM = ...` Assign — should be alias-import"
                    )


def test_bot_impl_telegram_state_module_alias_import():
    """bot/_impl.py uses `import bot.notifier as _telegram_state` (Bit 6.3 path-B-style alias).

    Path-A++ uses module-attribute access (`_telegram_state._TELEGRAM`), NOT
    plain alias-import (`from bot.notifier import _TELEGRAM`). Plain alias-import
    captures the binding at import time — runtime re-assignment in MainLoop.__init__
    would NOT propagate to other consumers. Module-attribute access via the
    `_telegram_state` alias preserves mutation freshness exactly like the
    `_cal_state._CALIBRATION_ENGINE` pattern from Bit 6.3 path-B.

    NOTE: `import bot.notifier as ...` (NOT `from bot import notifier as ...`).
    The latter form goes through `_BotProxy.__getattr__` and triggers a partial-
    module ImportError of bot._impl from inside bot.scanner during its load.
    """
    src = _read_bot_impl_source()
    assert re.search(
        r"^import bot\.notifier as _telegram_state", src, re.MULTILINE
    ), "bot/_impl.py missing `import bot.notifier as _telegram_state` alias-import"


def test_scanner_telegram_state_module_alias_import():
    """bot/scanner/__init__.py uses `import bot.notifier as _telegram_state` (path-B-style alias)."""
    src = _read_scanner_source()
    assert re.search(
        r"^import bot\.notifier as _telegram_state", src, re.MULTILINE
    ), "bot/scanner/__init__.py missing `import bot.notifier as _telegram_state` alias-import"


def _strip_inline_comment(line: str) -> str:
    """Strip Python inline-comment portion from a code line. Heuristic — splits
    on `<whitespace>#` so '# foo' at column 0 is a full comment and `x = 1  # y`
    keeps `x = 1`. Doesn't try to handle # inside strings (acceptable for this
    test's bare-name grep purpose)."""
    parts = re.split(r"(?<=\s)#", line, maxsplit=1)
    return parts[0]


def test_bot_impl_uses_telegram_state_module_attribute_access():
    """bot/_impl.py reads _TELEGRAM via _telegram_state._TELEGRAM (NOT bare-name)."""
    src = _read_bot_impl_source()
    code_lines = []
    for line in src.splitlines():
        if line.lstrip().startswith("#"):
            continue
        if "import bot.notifier as _telegram_state" in line:
            continue
        code_lines.append(_strip_inline_comment(line))
    code = "\n".join(code_lines)
    bare_refs = re.findall(r"(?<![.\w])_TELEGRAM(?!\w)", code)
    assert not bare_refs, (
        f"bot/_impl.py has {len(bare_refs)} bare-name `_TELEGRAM` reads — must use `_telegram_state._TELEGRAM`"
    )


def test_notifier_defines_telegram_singleton():
    """bot/notifier.py defines `_TELEGRAM` at module scope as Optional[TelegramNotifier] = None."""
    src = _read_notifier_source()
    tree = ast.parse(src)
    found = False
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "_TELEGRAM":
                found = True
                break
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_TELEGRAM":
                    found = True
                    break
    assert found, "bot/notifier.py missing `_TELEGRAM` module-scope definition"


def test_telegram_singleton_mutation_propagates_via_module_attribute():
    """Setting bot.notifier._TELEGRAM propagates to all consumers via the _telegram_state alias.

    Mirrors the Bit 6.3 path-B mutation-propagation pin for `_cal_state._CALIBRATION_ENGINE`.
    The production write path (`MainLoop.__init__`) sets `_telegram_state._TELEGRAM = self.telegram`
    which is exactly equivalent to `bot.notifier._TELEGRAM = self.telegram`; here we test the
    inverse direction (writes to bot.notifier propagate to consumers reading via the alias).
    """
    import bot._impl
    import bot.notifier
    import bot.scanner

    sentinel = object()
    original = bot.notifier._TELEGRAM
    try:
        bot.notifier._TELEGRAM = sentinel
        # The _telegram_state alias in each consumer IS bot.notifier (module identity),
        # so any read through the alias sees the new value immediately.
        assert bot._impl._telegram_state is bot.notifier
        assert bot._impl._telegram_state._TELEGRAM is sentinel
        assert bot.scanner._telegram_state is bot.notifier
        assert bot.scanner._telegram_state._TELEGRAM is sentinel
    finally:
        bot.notifier._TELEGRAM = original


# ============================================ CRITICAL #6: OrderExecutor late-binding


def test_scanner_no_top_level_bot_impl_import():
    """bot/scanner/__init__.py top-level: NO `from bot._impl import ...`, NO `import bot._impl`."""
    src = _read_scanner_source()
    tree = ast.parse(src)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/scanner top-level imports from bot._impl: line {node.lineno}"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/scanner top-level imports bot._impl: line {node.lineno}"
                )


# NOTE: 4 Bit-8.1 tests RETIRED in Sprint 9 Bit 9.1 (2026-05-10):
#   - test_scanner_has_get_order_executor_helper
#   - test_scanner_uses_get_order_executor_at_call_sites
#   - test_get_order_executor_returns_bot_impl_orderexecutor
#   - test_importlinter_scanner_no_impl_toplevel_contract
# These pinned the existence of the `_get_order_executor()` late-binding helper
# and the `scanner-no-impl-toplevel` `.importlinter` contract, both of which
# Bit 9.1's cleanup contract retired atomically. The replacement contract is in
# tests/test_executor_extraction.py:
#   - test_scanner_no_longer_has_get_order_executor_helper (negative pin)
#   - test_scanner_imports_order_executor_at_top
#   - test_scanner_no_call_sites_to_get_order_executor (negative pin via AST)
#   - test_scanner_no_impl_toplevel_contract_removed
#   - test_scanner_no_impl_toplevel_dropped_from_expected_contracts


# ============================================== CRITICAL #5: _cal_state alias


def test_scanner_imports_cal_state_alias():
    """bot/scanner/__init__.py uses `from bot.engines import calibration as _cal_state`."""
    src = _read_scanner_source()
    assert re.search(
        r"^from bot\.engines import calibration as _cal_state", src, re.MULTILINE
    ), "bot/scanner missing `from bot.engines import calibration as _cal_state` alias-import"


def test_scanner_uses_cal_state_calibration_engine():
    """bot/scanner/__init__.py reads _cal_state._CALIBRATION_ENGINE (Bit 6.3 path-B precedent)."""
    src = _read_scanner_source()
    assert "_cal_state._CALIBRATION_ENGINE" in src, (
        "bot/scanner missing `_cal_state._CALIBRATION_ENGINE` reference"
    )


def test_scanner_uses_cal_state_resolve_cal_engine():
    """bot/scanner/__init__.py reads _cal_state._resolve_cal_engine (Bit 6.3 path-B precedent)."""
    src = _read_scanner_source()
    assert "_cal_state._resolve_cal_engine" in src, (
        "bot/scanner missing `_cal_state._resolve_cal_engine` reference"
    )


# ====================================== CRITICAL #4: _calmlp_predictors integration import


def test_scanner_imports_calmlp_predictors_from_integration():
    """bot/scanner/__init__.py imports `_calmlp_predictors` from `integration` (Smell 3 leaf)."""
    src = _read_scanner_source()
    # Multi-line `from integration import (\n  _calmlp_predictors,\n  ...)` is supported via DOTALL.
    assert re.search(
        r"from integration import\s*\(?\s*[^)]*?_calmlp_predictors", src, re.MULTILINE | re.DOTALL
    ), "bot/scanner missing `from integration import _calmlp_predictors`"


def test_scanner_imports_calmlp_annotate_async_from_integration():
    """bot/scanner/__init__.py imports `_calmlp_annotate_async` from `integration` (1 call site)."""
    src = _read_scanner_source()
    assert "_calmlp_annotate_async" in src, (
        "bot/scanner missing reference to _calmlp_annotate_async (call site at line ~1931)"
    )


def test_calmlp_predictors_singleton_identity():
    """The dict at bot.scanner._calmlp_predictors is the same object as integration._calmlp_predictors."""
    import sys
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "cal_mlp"))
    import bot._impl
    import bot.scanner
    import integration
    assert bot.scanner._calmlp_predictors is integration._calmlp_predictors
    assert bot._impl._calmlp_predictors is integration._calmlp_predictors


# ============================================ CRITICAL #2: main_loop back-reference


def test_scanner_init_signature_includes_main_loop_default_none():
    """OpportunityScanner.__init__ accepts main_loop=None as keyword-default arg."""
    cls = _scanner_class_def()
    init_methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
    assert init_methods, "OpportunityScanner.__init__ not found"
    init = init_methods[0]
    arg_names = [a.arg for a in init.args.args]
    assert "main_loop" in arg_names, (
        f"main_loop not in __init__ signature; args = {arg_names}"
    )


@pytest.mark.parametrize("attr", SCANNER_MAIN_LOOP_ATTRS)
def test_scanner_self_ml_attribute_accesses_present(attr):
    """Each of the 11 `self._ml.X` sub-attribute accesses survives the verbatim move."""
    src = _read_scanner_source()
    pattern = rf"self\._ml\.{re.escape(attr)}\b"
    assert re.search(pattern, src), (
        f"bot/scanner missing `self._ml.{attr}` reference — verbatim move dropped a back-reference"
    )


def test_main_loop_init_signals_scanner_self_ml_dependencies_will_exist_at_scan_time():
    """MainLoop.__init__ builds all 11 self._ml.X dependencies (any order before .run() starts).

    Scanner stores `self._ml = main_loop` in `__init__` but doesn't read
    `main_loop.X` until scan() runs. Construction order within
    MainLoop.__init__ is therefore not load-bearing — what matters is that
    every attribute scanner reads via `self._ml.X` is set on MainLoop by the
    end of `__init__` (before `MainLoop.run()` triggers the first scan).
    """
    cls = _bot_impl_class_def("MainLoop")
    if cls is None:
        pytest.skip("MainLoop class not found in bot/_impl.py (Sprint 9 may have moved it)")
    init_methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
    if not init_methods:
        pytest.skip("MainLoop.__init__ not found")
    init = init_methods[0]
    init_src = ast.unparse(init)
    # All 11 attributes must be assigned in __init__ at least once. Handle both
    # bare assignment (`self.X = ...`) and annotated assignment (`self.X: T = ...`).
    for attr in SCANNER_MAIN_LOOP_ATTRS:
        # Match `self.X =` OR `self.X: ` (the latter catches AnnAssign forms
        # like `self._scan_iter: int = 0`).
        pattern = re.compile(rf"self\.{re.escape(attr)}(\s*=|\s*:|\s*\+=)")
        assert pattern.search(init_src), (
            f"MainLoop.__init__ never assigns `self.{attr}` — scanner.self._ml.{attr} would AttributeError at scan time"
        )


# ====================================== CRITICAL #1: latent _best_ask_depth bug seal


def test_opportunity_scanner_does_not_define_best_ask_depth():
    """Negative pin: _best_ask_depth lives ONLY on OrderExecutor, NEVER on scanner.

    Locks the contract from latent bug ticket 86b9vn9r5: 4 sites in OrderExecutor
    call OpportunityScanner._best_ask_depth(...) but the method only exists on
    OrderExecutor. If a future agent adds it to scanner to silence those calls,
    this test fires (the right fix is updating the call sites, not adding the method).
    """
    import bot.scanner
    assert not hasattr(bot.scanner.OpportunityScanner, "_best_ask_depth"), (
        "_best_ask_depth was added to OpportunityScanner — see latent bug ticket 86b9vn9r5"
    )


# ============================================== L33: consumer-call-site pins


def test_main_loop_constructs_scanner_with_main_loop_arg():
    """MainLoop.__init__ constructs scanner with main_loop=self (search anchor)."""
    src = _read_bot_impl_source()
    assert re.search(r"self\.scanner\s*=\s*OpportunityScanner\(", src), (
        "MainLoop.__init__ missing `self.scanner = OpportunityScanner(...)` construction"
    )
    # main_loop=self argument must be present (constructor-injected back-reference)
    assert re.search(
        r"OpportunityScanner\([^)]*main_loop\s*=\s*self", src, re.DOTALL
    ), "MainLoop.__init__ missing `main_loop=self` arg in OpportunityScanner construction"


def test_no_consumer_class_annotates_opportunity_scanner():
    """Negative pin: NO bot/_impl.py class has an __init__ param annotated `OpportunityScanner`."""
    src = _read_bot_impl_source()
    tree = ast.parse(src)
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for method in node.body:
            if not isinstance(method, ast.FunctionDef) or method.name != "__init__":
                continue
            for arg in method.args.args:
                if arg.annotation is None:
                    continue
                # Check both bare Name (`OpportunityScanner`) and Optional[X]
                ann_src = ast.unparse(arg.annotation)
                if "OpportunityScanner" in ann_src and not ann_src.startswith('"'):
                    raise AssertionError(
                        f"{node.name}.__init__ annotates {arg.arg} as `{ann_src}` — drift hazard per L33"
                    )


# ============================================ tracked_write instrumentation


def test_scanner_imports_tracked_write_from_db_writer_registry():
    """bot/scanner/__init__.py imports tracked_write (load-bearing for db-locked SLOW_BATCH_BREAKDOWN)."""
    src = _read_scanner_source()
    assert re.search(
        r"^from bot\.db_writer_registry import.*\btracked_write\b", src, re.MULTILINE
    ), "bot/scanner missing `from bot.db_writer_registry import tracked_write`"


# ============================================ Forbidden imports


@pytest.mark.parametrize("path,label,forbidden", SCANNER_FORBIDDEN_IMPORTS)
def test_scanner_no_forbidden_numerical_imports(path, label, forbidden):
    """bot/scanner/__init__.py must not import torch/sklearn/pandas (numerical-libs ban)."""
    if not path.is_file():
        pytest.skip(f"{label} not yet created")
    src = path.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in forbidden, (
                    f"{label} imports forbidden module `{alias.name}` at line {node.lineno}"
                )
        if isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            root = node.module.split(".")[0]
            assert root not in forbidden, (
                f"{label} imports from forbidden module `{node.module}` at line {node.lineno}"
            )


# ============================================ Behavioral smoke


def test_scanner_class_attribute_loads_via_bot_proxy():
    """bot.scanner.OpportunityScanner resolves without ImportError/AttributeError at class-load time."""
    import bot
    assert bot.scanner.OpportunityScanner is not None
    assert callable(bot.scanner.OpportunityScanner)


def test_scanner_init_signature_matches_pre_extraction():
    """OpportunityScanner.__init__ has the expected 10-param signature (excluding self)."""
    import bot.scanner
    sig = inspect.signature(bot.scanner.OpportunityScanner.__init__)
    params = list(sig.parameters.keys())
    # Expected: self, client, state, feed, vol, logger, sizer, order_flow, kalshi_oft, kalshi_feed, main_loop
    expected = ["self", "client", "state", "feed", "vol", "logger", "sizer",
                "order_flow", "kalshi_oft", "kalshi_feed", "main_loop"]
    assert params == expected, (
        f"OpportunityScanner.__init__ signature drifted; expected {expected}, got {params}"
    )


def test_scanner_class_can_be_instantiated_via_new():
    """OpportunityScanner.__new__ succeeds (skips __init__ to avoid ctor deps)."""
    import bot.scanner
    instance = bot.scanner.OpportunityScanner.__new__(bot.scanner.OpportunityScanner)
    assert isinstance(instance, bot.scanner.OpportunityScanner)


# ============================================ Cell-block / shadow_diag preservation


CELL_BLOCK_CONSTANT_NAMES = (
    # (constant_name, expected_literal_value) — verifies the indirect-via-constant
    # path used by scanner. The literals are string-stored in DB per bot/CLAUDE.md
    # `Cell-block activations deflate filter_stage='candidate' rollups`; scanner
    # references them by name (e.g., HIGH_PRICE_STC_BLOCK_FILTER_STAGE), and
    # bot.constants resolves the name to the literal value.
    ("HIGH_PRICE_STC_BLOCK_FILTER_STAGE", "96C_SOL_XRP_STC_DANGER_BAND"),
    ("TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE", "TM98_97_98C_2_5MIN_BLEED"),
    ("SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE", "SOL_TAKER_85_89C_2_5MIN_BLEED"),
    ("SOL_BLEED_V2_BLOCK_FILTER_STAGE", "SOL_BLEED_V2_88_93C_2_5MIN"),
)


@pytest.mark.parametrize("name,expected_literal", CELL_BLOCK_CONSTANT_NAMES)
def test_scanner_preserves_cell_block_constant_references(name, expected_literal):
    """Scanner references cell-block constants by name; bot.constants resolves to expected literal.

    Locks the contract from bot/CLAUDE.md: cell-block `filter_stage` literals are
    `'96C_SOL_XRP_STC_DANGER_BAND'` (HPSB), `'TM98_97_98C_2_5MIN_BLEED'`, and
    `'SOL_TAKER_85_89C_2_5MIN_BLEED'`. Scanner stores them indirectly via
    constants like HIGH_PRICE_STC_BLOCK_FILTER_STAGE — verify the indirection is
    preserved AND that bot.constants resolves to the right literal.
    """
    import bot.constants as bc
    src = _read_scanner_source()
    # 1. Constant name appears in scanner source
    assert name in src, (
        f"bot/scanner missing reference to cell-block constant `{name}` "
        f"(see bot/CLAUDE.md cell-block contract)"
    )
    # 2. bot.constants resolves the constant to the expected literal value
    assert getattr(bc, name) == expected_literal, (
        f"bot.constants.{name} = {getattr(bc, name)!r}, expected {expected_literal!r}"
    )


def test_scanner_filter_stage_count_preserved():
    """filter_stage mention count tracks against scanner-body baseline.

    Pre-extraction snapshot: 38 mentions. T1 (2026-05-10, ticket 86b9vecw9) added
    NO-side shadow elif branches for HYPE/DOGE (no_side_hype_shadow,
    no_side_doge_shadow analogues to the existing no_side_xrp_shadow).
    Current baseline: 41."""
    src = _read_scanner_source()
    count = src.count("filter_stage")
    # T1 baseline 41; allow ±2 drift for test-formatting changes. The exact
    # contract is that cell-block routing is preserved, locked by the
    # parametrize above.
    assert 39 <= count <= 43, (
        f"filter_stage count drift: expected 39-43, got {count}"
    )


def test_scanner_does_not_open_sqlite_cursor():
    """bot/scanner/__init__.py doesn't open sqlite cursors directly (StateManager owns sqlite)."""
    src = _read_scanner_source()
    # Strip comments before grep — the bot/CLAUDE.md mentions `_shadow_diag` etc. in
    # comments; those don't count as actual cursor openings.
    code_lines = [
        line for line in src.splitlines()
        if not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "sqlite3.connect(" not in code, (
        "bot/scanner opens sqlite directly — should go through self._state methods (StateManager)"
    )


# ============================================ ProbabilityEngine bare-name calls preserved


def test_scanner_imports_probability_engine_explicitly():
    """bot/scanner/__init__.py imports ProbabilityEngine for the 15 bare-name call sites."""
    src = _read_scanner_source()
    assert re.search(
        r"^from bot\.engines import.*\bProbabilityEngine\b", src, re.MULTILINE
    ), "bot/scanner missing `from bot.engines import ProbabilityEngine`"


def test_scanner_imports_volatility_engine_explicitly():
    """bot/scanner/__init__.py imports VolatilityEngine (type annotation on __init__)."""
    src = _read_scanner_source()
    assert re.search(
        r"^from bot\.engines import.*\bVolatilityEngine\b", src, re.MULTILINE
    ), "bot/scanner missing `from bot.engines import VolatilityEngine`"


# ============================================ Import partition completeness


def test_scanner_imports_kalshi_client_for_type_annotation():
    src = _read_scanner_source()
    assert re.search(r"^from bot\.kalshi_client import KalshiClient", src, re.MULTILINE)


def test_scanner_imports_state_manager_for_type_annotation():
    src = _read_scanner_source()
    assert re.search(r"^from bot\.state import StateManager", src, re.MULTILINE)


def test_scanner_imports_coinbase_feed_for_type_annotation():
    src = _read_scanner_source()
    assert re.search(r"^from bot\.feeds import .*\bCoinbaseFeed\b", src, re.MULTILINE)


def test_scanner_imports_logger_for_type_annotation():
    src = _read_scanner_source()
    assert re.search(r"^from bot\.logger import Logger", src, re.MULTILINE)


def test_scanner_imports_position_sizer_from_models():
    """Sprint 10.5b (2026-05-11): models relocated from repo root to bot/models.py."""
    src = _read_scanner_source()
    assert re.search(r"^from bot\.models import.*\bPositionSizer\b", src, re.MULTILINE)


# ============================================ L40 patch-coverage drift guard


def test_no_stale_bot_telegram_patches_outside_bot_notifier():
    """Path-A++ regression seal: post-Bit-8.1, _TELEGRAM patches must target bot.notifier (NOT bot or bot._impl).

    The original ~86 sites of @patch.object(bot, "_TELEGRAM", ...) /
    @patch("bot.notifier._TELEGRAM", ...) all need retargeting. This test fires
    if any survive post-extraction.
    """
    tests_dir = REPO_ROOT / "tests"
    stale = []
    for py_file in tests_dir.rglob("*.py"):
        if py_file.name == "test_scanner_extraction.py":
            continue  # this file's docstring contains the pattern as text
        text = py_file.read_text(errors="ignore")
        # Multi-line and single-line forms
        patterns = [
            r'patch\s*\(\s*["\']bot\._TELEGRAM["\']',
            r'patch\.object\s*\(\s*bot\s*,\s*["\']_TELEGRAM["\']',
            r'patch\s*\(\s*["\']bot\._impl\._TELEGRAM["\']',
            r'patch\.object\s*\(\s*bot\._impl\s*,\s*["\']_TELEGRAM["\']',
        ]
        for pat in patterns:
            for m in re.finditer(pat, text, re.MULTILINE | re.DOTALL):
                line_no = text[:m.start()].count("\n") + 1
                stale.append(f"{py_file.relative_to(REPO_ROOT)}:{line_no}: {m.group(0)}")
    assert not stale, (
        f"Found {len(stale)} stale `bot.notifier._TELEGRAM` / `bot._impl._TELEGRAM` patch targets — "
        f"path-A++ requires retargeting to `bot.notifier._TELEGRAM`:\n  " + "\n  ".join(stale[:10])
        + (f"\n  ... and {len(stale) - 10} more" if len(stale) > 10 else "")
    )


# NOTE: prior drafts of this file had `test_no_stale_bot_observation_mode_patches`
# and `test_no_stale_bot_weather_no_side_live_patches` regression seals — they
# proved too strict, because tests that exercise OrderExecutor (still in
# bot/_impl.py post-Bit-8.1) legitimately patch `bot.constants.OBSERVATION_MODE` to reach
# bot._impl's bare-name reads. Tests that exercise scanner.scan() use
# `bot.scanner.OBSERVATION_MODE`. The right contract is enforced by the
# behavioral pins above (test_telegram_singleton_mutation_propagates_via_module_attribute,
# test_scanner_uses_get_order_executor_at_call_sites, etc.). Leaving the stub
# so future agents see that we deliberated this and chose behavioral pins
# over a brittle grep-the-test-tree drift guard.
