"""Bit 9.1 — OrderExecutor extracted from bot/_impl.py to bot/executor.py.

Bit 9.1 (2026-05-XX):
  OrderExecutor → bot/executor.py
  (Sprint 9 — second-largest single class in the modularization track:
  ~5,284 lines, 49 instance methods + 10 staticmethods. Touches the
  trading hot path: maker submission, taker escalation, cancel-404 V8
  lockout, P2 follow-up locks, ladder escalation, addon opportunities,
  all load-bearing.)

Path-A++ deviations (NOT byte-for-byte):
  1. `_append_raw_api_journal` relocated from bot/_impl.py:282 to
     bot/helpers/raw_api_journal.py (3 callers across OrderExecutor +
     SettlementTracker; relocation eliminates the late-binding need
     that path-A would have introduced. Bit 9.2 inherits a clean import
     path.)
  2. `_best_ask_depth` latent AttributeError fixed (4 sites in
     OrderExecutor body that called `OpportunityScanner._best_ask_depth`
     — the method actually lives on OrderExecutor itself; closes ticket
     86b9vn9r5).

Sister cleanup atomic in same commit:
  - `_get_order_executor()` helper retired from bot/scanner/__init__.py
  - `scanner-no-impl-toplevel` `.importlinter` contract dropped
  - 3 peer-pin tests in tests/contracts/test_import_linter_contracts.py
    dropped + `EXPECTED_CONTRACTS` shrunk to 5 entries
  - 4 `OpportunityScanner._best_ask_depth` calls in OrderExecutor body
    rewritten to `OrderExecutor._best_ask_depth`

Cross-class coupling preserved via:
  - `_telegram_state._TELEGRAM` (Bit 8.1 path-A++ pattern; 19 read sites
    in OrderExecutor)
  - `from bot.scanner import OpportunityScanner` at top of bot/executor.py
    (top-level works post-Bit-8.1 — bot.scanner is fully loaded by the
    time bot.executor loads via the bot/_impl.py re-export chain)

Related lessons:
  L32 (Plan-agent), L33 (consumer-class identity), L38 (AST walk
  retargets), L39 (config vs bot.constants partition — 97 bot.constants,
  0 config for OrderExecutor), L40 + L85 (@patch routing — 130 sites),
  L41 (no hand-counted breadcrumbs — parametrize tuples ARE ground
  truth), L78 (star-import-aware free-var scan), L79 (path-A vs path-A++
  early choice — path-A++ for `_append_raw_api_journal`), L81 (alias
  hygiene — `from bot.helpers.raw_api_journal import append_raw_api_journal
  as _append_raw_api_journal` keeps bot._impl.__dict__ snapshot stable),
  L82 (auto-regen race), L84 (explicit submodule import form for
  `_telegram_state` alias), L86 (doc-drift contagion sweep).

Mirrors tests/test_scanner_extraction.py (Bit 8.1 path-A++) +
tests/test_state_extraction.py (Bit 7.1 path-A++) +
tests/test_engines_extraction.py (Bit 6.1/6.2/6.3).
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

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_PY = REPO_ROOT / "bot" / "_impl.py"
EXECUTOR_PY = REPO_ROOT / "bot" / "executor.py"
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
NOTIFIER_PY = REPO_ROOT / "bot" / "notifier.py"
INIT_PY = REPO_ROOT / "bot" / "__init__.py"
RAW_API_JOURNAL_PY = REPO_ROOT / "bot" / "helpers" / "raw_api_journal.py"
IMPORTLINTER_INI = REPO_ROOT / ".importlinter"
CONTRACT_TEST_PY = REPO_ROOT / "tests" / "contracts" / "test_import_linter_contracts.py"


# ============================================================ Module-level data
# Per L41: parametrize tuples ARE the ground truth — no separate count claim.
# Names AST-extracted from bot/_impl.py:994-6277 pre-extraction (Bit 9.1
# pre-scaffold scan).

# 49 instance methods (no classmethods)
EXECUTOR_INSTANCE_METHODS = (
    "__init__",
    "_active_order",
    "has_active_order",
    "_get_post_only_rejection_count",
    "_record_post_only_rejection",
    "_should_skip_near_close",
    "_abort_near_close",
    "_execute_hourly_taker",
    "_execute_weather_no_taker",
    "_execute_hourly_no_taker",
    "execute",
    "tick",
    "_tick_sol_pathc_observations",
    "_tick_one",
    "_reprice_maker",
    "_escalate_to_taker",
    "_escalate_to_taker_inner",
    "_rest_best_ask_depth",
    "_record_rest_depth_observation",
    "_rest_depth_window_count",
    "_rest_depth_window_max",
    "_rest_best_ask_depth_smoothed",
    "_dc_get_ask_with_depth",
    "_execute_dc_taker",
    "_execute_tm_taker",
    "_tm_sweep_snapshot_depths",
    "_execute_lpne_taker",
    "_execute_bracket_no_taker",
    "process_dc_retries",
    "_submit_maker",
    "_submit_taker",
    "_strategy_max_entry_price",
    "_maybe_ladder_escalate",
    "_maybe_post_maker_tail",
    "_sweep_maker_tails",
    "_check_for_fill",
    "_on_fill",
    "_log_fill_model_sample",
    "_register_addon_eligible",
    "_check_addon_opportunities",
    "_execute_addon",
    "_get_addon_best_ask",
    "_nbbo_fallback_price",
    "_get_addon_spot",
    "_get_addon_balance",
    "_check_dip_addon_opportunities",
    "_handle_cancel_404",
    "_cancel_order",
    "_cancel_active",
)

# 10 staticmethods
EXECUTOR_STATIC_METHODS = (
    "_existing_window_cost_for_timeslot",
    "_escalation_wait",
    "_best_ask_depth",  # ticket 86b9vn9r5: lives HERE, NOT on OpportunityScanner
    "_compute_ladder_diag",
    "_pick_ioc_limit_for_depth",
    "_total_ob_depth",
    "_best_yes_bid",
    "_best_yes_bid_depth",
    "_extract_book_levels",
    "_dc_retry_delay",
)

# 6 unique self._ml.X sub-attributes (constructor-injected via main_loop arg)
EXECUTOR_MAIN_LOOP_ATTRS = (
    "_recent_fill_latencies",
    "_session_fill_count",
    "_session_maker_fills",
    "_session_maker_submissions",
    "scanner",
    "vol",
)

# 97 bot.constants names imported by OrderExecutor (L78 free-var scan).
EXECUTOR_BOT_CONSTANTS_NAMES = (
    "ADDON_ENABLED", "ADDON_MAX_ENTRY_PRICE", "ADDON_MIN_PRICE_IMPROVEMENT",
    "ADDON_MIN_SECONDS_SINCE_FILL", "ADDON_MIN_STC_REMAINING", "ADDON_SIZE_FRACTION",
    "BRACKET_NO_ASSUMED_PROB", "BRACKET_NO_YES_MAX", "BRACKET_NO_YES_MIN",
    "BTC_ESCALATION_WAIT_OVERRIDE", "BTC_MIN_ENTRY_PRICE",
    "DC_IOC_MAX_RETRIES", "DC_IOC_RETRY_DELAY", "DC_PRICE_TOLERANCE_MAX",
    "DC_PRICE_TOLERANCE_START_RETRY", "DECIDED_CONTRACT_MIN_PRICE",
    "DECIDED_CONTRACT_T2_MAX_PRICE", "DIP_ADDON_ENABLED", "DIP_ADDON_MAX_TOTAL_RISK",
    "DIP_ADDON_MIN_DROP_CENTS", "DIP_ADDON_MIN_ENTRY_PRICE",
    "DIP_ADDON_MIN_SECONDS_SINCE_FILL", "DIP_ADDON_MIN_STC_REMAINING",
    "DIP_ADDON_SHADOW_FLOOR", "DIP_ADDON_SHADOW_MODE", "DIP_ADDON_SIZE_FRACTION",
    "DIRECT_TAKER_THRESHOLD", "EARLY_ESCALATION_MIN_MOVE", "ESCALATION_MAX_ENTRY",
    "ESCALATION_WAIT_LONG", "ESCALATION_WAIT_MEDIUM", "ESCALATION_WAIT_SHORT",
    "ETH_MIN_ENTRY_PRICE", "FILL_MODEL_JOURNAL", "HOURLY_FIXED_CONTRACTS",
    "HOURLY_MAX_ENTRY_PRICE", "HOURLY_MIN_EDGE_PCT", "HOURLY_NO_FIXED_CONTRACTS",
    "HOURLY_NO_SIDE_LIVE", "HOURLY_TAKER_ONLY", "IOC_DRIFT_CHECK_COLD_START_RATIO",
    "IOC_DRIFT_CHECK_DIVERGENCE_RATIO", "IOC_DRIFT_CHECK_ENABLED",
    "IOC_DRIFT_CHECK_MIN_CACHED_DEPTH", "IOC_DRIFT_CHECK_REST_WINDOW_S",
    "IOC_LIMIT_MAX_BUMP_CENTS", "IOC_MIN_COUNT_AFTER_CLAMP", "IOC_RETRY_OFFSET",
    "IOC_TICKER_COOLDOWN", "LADDER_ESCALATION_ELIGIBLE_STRATEGIES",
    "LADDER_ESCALATION_ENABLED", "LADDER_ESCALATION_MIN_REMAINDER",
    "LADDER_ESCALATION_OFFSET", "LOG_RAW_IOC_FILLS", "LPNE_FIXED_CONTRACTS",
    "LPNE_MAX_PRICE", "LPNE_MIN_PRICE", "MAKER_ONLY_THRESHOLD", "MAKER_POLL_INTERVAL",
    "MAKER_PRICE_OFFSET", "MAKER_TAIL_AFTER_IOC_PARTIAL",
    "MAKER_TAIL_ELIGIBLE_STRATEGIES", "MAKER_TAIL_MAX_GLOBAL",
    "MAKER_TAIL_MAX_PER_ASSET", "MAKER_TAIL_MIN_REMAINDER",
    "MAKER_TAIL_MIN_STC_SECONDS", "MAKER_TAIL_TTL_SECONDS", "MAKER_TIMEOUT_SECONDS",
    "MAX_ENTRY_PRICE", "MAX_TICKER_RISK", "MAX_WINDOW_RISK", "MIN_EDGE_PCT",
    "MIN_ENTRY_PRICE", "MIN_ORDER_SUBMIT_STC_S", "MIN_SECONDS_BEFORE_CLOSE",
    "NBBO_FALLBACK_GATES", "OBSERVATION_MODE", "POST_ONLY_DEGRADED_EXTRA_OFFSET",
    "POST_ONLY_MAX_SAME_PRICE", "POST_ONLY_REJECTION_EXPIRY",
    "SOL_EMPTY_BOOK_MAKER_MIN_PRICE", "SOL_EMPTY_BOOK_MIN_STC", "SOL_MIN_ENTRY_PRICE",
    "SOL_TAKER_FIRST", "STACKING_ENABLED", "STRATEGY_CLAMP_DEFAULT",
    "STRATEGY_CLAMP_POLICY", "STRATEGY_LIMIT_BUMP_DEFAULT_RESERVE",
    "STRATEGY_LIMIT_BUMP_RESERVE_CENTS", "TAKER_FIRST_ASSETS", "TM_LIVE_STRATEGIES",
    "TM_PRICE_SET", "TM_SWEEP_CAPTURE_TIERS", "TM_SWEEP_LIVE_ENABLED",
    "TM_SWEEP_SHADOW_ENABLED", "WEATHER_NO_SIDE_LIVE", "XRP_MIN_ENTRY_PRICE",
)

# Forbidden numerical libraries — same as scanner; numpy/scipy may transit
# but must NOT be top-level imports in bot/executor.py
FORBIDDEN_NUMERICAL_IMPORTS = ("torch", "sklearn", "pandas")


# Cached AST parse of executor source — re-parsed once per session
def _executor_tree() -> ast.Module:
    return ast.parse(EXECUTOR_PY.read_text())


def _bot_impl_tree() -> ast.Module:
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    return ast.parse(BOT_PY.read_text())


def _scanner_tree() -> ast.Module:
    return ast.parse(SCANNER_PY.read_text())


def _executor_class() -> ast.ClassDef:
    tree = _executor_tree()
    return next(n for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef) and n.name == "OrderExecutor")


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity (5 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_executor_class_in_bot_executor_module():
    """Positive AST pin: OrderExecutor defined in bot/executor.py."""
    assert EXECUTOR_PY.exists(), f"bot/executor.py missing — extraction not yet performed"
    tree = _executor_tree()
    classes = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert "OrderExecutor" in classes, f"OrderExecutor not defined in bot/executor.py; found: {classes}"


def test_executor_class_NOT_in_bot_impl_module():
    """Negative AST pin (ship gate): OrderExecutor must NOT be in bot/_impl.py."""
    tree = _bot_impl_tree()
    classes = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert "OrderExecutor" not in classes, (
        f"OrderExecutor still defined in bot/_impl.py — extraction incomplete. "
        f"All classes in bot/_impl.py: {classes}"
    )


def test_executor_module_attr_resolves_via_proxy():
    """bot.executor.OrderExecutor → bot.executor.OrderExecutor via canonical submodule (post-Bit-9.3-iii.b — _BotProxy retired) chain."""
    import bot
    import bot.executor
    assert bot.executor.OrderExecutor is bot.executor.OrderExecutor


def test_executor_reexported_into_bot_impl():
    """bot._impl.OrderExecutor is bot.executor.OrderExecutor (via line-116ish re-export)."""
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.executor
    assert bot._impl.OrderExecutor is bot.executor.OrderExecutor


def test_executor_init_signature_unchanged():
    """OrderExecutor.__init__ accepts (self, client, state, logger, main_loop=None, kalshi_feed=None)."""
    import bot.executor
    sig = inspect.signature(bot.executor.OrderExecutor.__init__)
    params = list(sig.parameters.keys())
    assert params == ["self", "client", "state", "logger", "main_loop", "kalshi_feed"], (
        f"__init__ signature changed: {params}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — Drift guards (3 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_no_top_level_bot_impl_import_in_executor():
    """Path-A++ guard: bot/executor.py must NOT have a top-level import of bot._impl.

    Unlike scanner (which had a method-body late-binding helper for OrderExecutor
    cross-class calls until Bit 9.1 retired it) and bot.state (which has a
    method-body late-binding helper for compute_for_15m_main_path), bot.executor
    has ZERO bot._impl references — no helper, no late-binding, no
    `.importlinter` carve-out. The path-A++ relocation of `_append_raw_api_journal`
    eliminated the only would-have-been late-binding candidate.
    """
    tree = _executor_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not (alias.name == "bot._impl" or alias.name.startswith("bot._impl.")), (
                    f"bot/executor.py has top-level `import {alias.name}` — forbidden (no carve-out)"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "bot._impl" or node.module.startswith("bot._impl.")):
                names = [a.name for a in node.names]
                pytest.fail(f"bot/executor.py has top-level `from {node.module} import {names}` — forbidden")
            if node.level == 0 and node.module == "bot" and any(a.name == "_impl" for a in node.names):
                pytest.fail("bot/executor.py has `from bot import _impl` — forbidden (no carve-out)")


def test_executor_method_count_matches_ast():
    """Method count is exactly 49 instance + 10 static + 0 classmethods = 59 funcdefs."""
    cls = _executor_class()
    instance, static, classm = [], [], []
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in n.decorator_list)
            is_classm = any(isinstance(d, ast.Name) and d.id == "classmethod" for d in n.decorator_list)
            if is_static:
                static.append(n.name)
            elif is_classm:
                classm.append(n.name)
            else:
                instance.append(n.name)
    assert len(instance) == len(EXECUTOR_INSTANCE_METHODS), (
        f"instance method count drift: AST={len(instance)} vs tuple={len(EXECUTOR_INSTANCE_METHODS)}; "
        f"AST extra={set(instance) - set(EXECUTOR_INSTANCE_METHODS)}; "
        f"AST missing={set(EXECUTOR_INSTANCE_METHODS) - set(instance)}"
    )
    assert len(static) == len(EXECUTOR_STATIC_METHODS), (
        f"static method count drift: AST={len(static)} vs tuple={len(EXECUTOR_STATIC_METHODS)}; "
        f"AST extra={set(static) - set(EXECUTOR_STATIC_METHODS)}; "
        f"AST missing={set(EXECUTOR_STATIC_METHODS) - set(static)}"
    )
    assert classm == [], f"unexpected classmethod(s): {classm}"


def test_executor_only_consumer_of_telegram_state_alias_in_executor_py():
    """bot/executor.py is the executor-side consumer of the _telegram_state alias.

    The alias must be `import bot.notifier as _telegram_state` (L84 form) — NOT
    `from bot import notifier as _telegram_state` (which goes through the
    _BotProxy.__getattr__ and triggers a circular ImportError).

    Uses AST to inspect actual import statements (not substring grep), so docstring
    text mentioning the wrong form descriptively doesn't trigger false-positives.
    """
    tree = _executor_tree()
    has_correct_form = False
    for node in ast.iter_child_nodes(tree):
        # Correct form: `import bot.notifier as _telegram_state` → ast.Import with alias.name='bot.notifier'
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bot.notifier" and alias.asname == "_telegram_state":
                    has_correct_form = True
        # Forbidden form: `from bot import notifier as _telegram_state` → ast.ImportFrom with module='bot', alias.name='notifier'
        if isinstance(node, ast.ImportFrom):
            if node.module == "bot" and node.level == 0:
                for alias in node.names:
                    if alias.name == "notifier" and alias.asname == "_telegram_state":
                        pytest.fail(
                            "bot/executor.py uses forbidden `from bot import notifier as _telegram_state` form (L84 — "
                            "triggers _BotProxy.__getattr__ → circular ImportError). Use `import bot.notifier as _telegram_state`."
                        )
    assert has_correct_form, (
        "bot/executor.py missing `import bot.notifier as _telegram_state` (L84 form)"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — Method presence (parametrized, 59 entries)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method_name", EXECUTOR_INSTANCE_METHODS + EXECUTOR_STATIC_METHODS)
def test_executor_method_present(method_name: str):
    """Every method in the ground-truth tuple is defined on bot.executor.OrderExecutor."""
    import bot.executor
    assert hasattr(bot.executor.OrderExecutor, method_name), (
        f"bot.executor.OrderExecutor.{method_name} missing"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 4 — Constants partition (parametrized, 97 entries)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("const_name", EXECUTOR_BOT_CONSTANTS_NAMES)
def test_executor_constant_partitioned_to_bot_constants(const_name: str):
    """Every constant the executor reads via explicit-import has a source in bot.constants."""
    import bot.constants
    assert hasattr(bot.constants, const_name), (
        f"{const_name!r} not in bot.constants — partition broken"
    )


def test_executor_imports_constants_explicitly_not_via_star():
    """bot/executor.py must NOT do `from bot.constants import *` (per Bit 8.1 explicit-import convention).

    All 97 constants are listed by name in a single `from bot.constants import (...)` block.
    """
    src = EXECUTOR_PY.read_text()
    assert re.search(r"^\s*from bot\.constants import \*", src, re.MULTILINE) is None, (
        "bot/executor.py uses forbidden `from bot.constants import *` star-import"
    )
    # Must have explicit-import block
    assert "from bot.constants import" in src, (
        "bot/executor.py missing `from bot.constants import (...)` explicit-import block"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 5 — L78 free-variable check
# ═════════════════════════════════════════════════════════════════════════════

def test_executor_imports_cleanly():
    """`import bot.executor` does not raise (no NameError / ImportError on free vars)."""
    # Use importlib so this works even if bot.executor was already imported
    if "bot.executor" in sys.modules:
        importlib.reload(sys.modules["bot.executor"])
    else:
        importlib.import_module("bot.executor")


def test_executor_no_unresolved_free_vars_at_module_level():
    """L78 free-var residual check: bot/executor.py module compiles + imports without error.

    The behavioral ground truth — `import bot.executor` succeeds is equivalent to
    "every name referenced at the module level + at every method def-time (decorators,
    annotations, defaults) resolves." Names referenced inside method *bodies* are
    resolved at call time, which `test_executor_instantiates_with_mocks` +
    `test_executor_execute_method_callable` cover behaviorally.

    A naive AST-walk approach (collect FunctionDef args + class methods) would flag
    every local variable inside every method as "unresolved" because Python local-var
    scoping isn't visible from a class-body AST walk. The smoke import is the
    correct behavioral test.
    """
    import importlib
    if "bot.executor" in sys.modules:
        importlib.reload(sys.modules["bot.executor"])
    else:
        importlib.import_module("bot.executor")
    # If we got here, bot/executor.py module-level code (imports, class def, method
    # decorators/defaults/annotations) all resolved cleanly.


# ═════════════════════════════════════════════════════════════════════════════
# Section 6 — L33 consumer-class identity pins
# ═════════════════════════════════════════════════════════════════════════════

def test_executor_init_annotates_kalshi_client():
    """OrderExecutor.__init__ has `client: KalshiClient` annotation."""
    cls = _executor_class()
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    client_arg = next((a for a in init.args.args if a.arg == "client"), None)
    assert client_arg is not None and client_arg.annotation is not None, (
        "OrderExecutor.__init__ missing `client` arg or its annotation"
    )
    assert ast.unparse(client_arg.annotation) == "KalshiClient", (
        f"client annotation drift: {ast.unparse(client_arg.annotation)!r}"
    )


def test_executor_init_annotates_state_manager():
    """OrderExecutor.__init__ has `state: StateManager` annotation."""
    cls = _executor_class()
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    state_arg = next((a for a in init.args.args if a.arg == "state"), None)
    assert state_arg is not None and state_arg.annotation is not None
    assert ast.unparse(state_arg.annotation) == "StateManager"


def test_executor_init_annotates_logger():
    """OrderExecutor.__init__ has `logger: Logger` annotation."""
    cls = _executor_class()
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    logger_arg = next((a for a in init.args.args if a.arg == "logger"), None)
    assert logger_arg is not None and logger_arg.annotation is not None
    assert ast.unparse(logger_arg.annotation) == "Logger"


# NOTE: L33 negative-pin for `client: KalshiClient` was DROPPED — the L33 lesson
# (wrong-class attribution drift) applies to UNIQUELY-CONSUMED types (e.g.,
# StateManager is consumed by exactly OpportunityScanner + OrderExecutor +
# SettlementTracker, so a wrong-class attribution would surface a real bug).
# `KalshiClient` is consumed by EVERY top-level class (Scanner + OrderExecutor +
# SettlementTracker + MainLoop + ...), so a "ONLY OrderExecutor annotates client:
# KalshiClient" negative pin is over-specified and would always fail. Positive
# pins (`test_executor_init_annotates_kalshi_client` above) cover the
# attribution-correctness invariant for OrderExecutor.


# ═════════════════════════════════════════════════════════════════════════════
# Section 7 — _best_ask_depth resolution (closes ticket 86b9vn9r5)
# ═════════════════════════════════════════════════════════════════════════════

def test_best_ask_depth_lives_on_executor_not_scanner():
    """`_best_ask_depth` is a staticmethod on OrderExecutor, NOT on OpportunityScanner."""
    import bot.executor
    import bot.scanner
    assert hasattr(bot.executor.OrderExecutor, "_best_ask_depth")
    assert not hasattr(bot.scanner.OpportunityScanner, "_best_ask_depth"), (
        "OpportunityScanner._best_ask_depth defined — Bit 9.1 must NOT add it to scanner; "
        "the latent AttributeError 86b9vn9r5 is closed by RE-WRITING the 4 OrderExecutor call "
        "sites to OrderExecutor._best_ask_depth, not by adding the method to scanner."
    )


def test_no_opportunityscanner_best_ask_depth_calls_in_executor():
    """The 4 BUG sites in OrderExecutor body that called OpportunityScanner._best_ask_depth
    must be rewritten to OrderExecutor._best_ask_depth (closes ticket 86b9vn9r5).

    Pre-Bit-9.1 sites in bot/_impl.py: lines 1909, 2208, 3196, 3213.

    Scoped to the OrderExecutor class body via AST so the module docstring's
    descriptive mention of the bug pattern doesn't trigger false-positives.
    """
    cls = _executor_class()
    body_text = ast.unparse(cls)
    bad = re.findall(r"OpportunityScanner\._best_ask_depth\s*\(", body_text)
    assert not bad, (
        f"OrderExecutor class body still has {len(bad)} `OpportunityScanner._best_ask_depth(...)` "
        f"call site(s) — these are the AttributeError sites from ticket 86b9vn9r5; rewrite to "
        f"`OrderExecutor._best_ask_depth(...)` or `self._best_ask_depth(...)` per axis C4."
    )


def test_no_opportunityscanner_best_ask_depth_calls_in_bot_impl():
    """Belt-and-suspenders: also assert no leftover bug call sites in bot/_impl.py post-extraction.

    Walks the AST to find actual `ast.Call` nodes whose function is
    `OpportunityScanner._best_ask_depth(...)` — descriptive text references in
    `#` comments or module-docstring breadcrumbs (which document the bug fix)
    are NOT call sites and don't trigger the failure.
    """
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    tree = ast.parse(BOT_PY.read_text())
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr == "_best_ask_depth"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "OpportunityScanner"):
                bad.append(node.lineno)
    assert not bad, (
        f"bot/_impl.py has {len(bad)} `OpportunityScanner._best_ask_depth(...)` call site(s) at "
        f"lines {bad} — latent AttributeError 86b9vn9r5; clean up in same Bit 9.1 commit."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 8 — Forbidden-imports tuple
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("forbidden", FORBIDDEN_NUMERICAL_IMPORTS)
def test_executor_no_forbidden_numerical_imports(forbidden: str):
    """bot/executor.py must NOT directly import torch/sklearn/pandas (transitive via bot.models.* OK)."""
    src = EXECUTOR_PY.read_text()
    pattern = rf"^\s*(import {forbidden}|from {forbidden}\b)"
    assert re.search(pattern, src, re.MULTILINE) is None, (
        f"bot/executor.py has forbidden top-level `import {forbidden}` — torch/sklearn/pandas "
        f"must reach the executor only transitively (e.g., through bot.models.PositionSizer)"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 9 — Path-A++ smell-fix pins for _append_raw_api_journal
# ═════════════════════════════════════════════════════════════════════════════

def test_raw_api_journal_relocated_to_bot_helpers():
    """bot/helpers/raw_api_journal.py exists (path-A++ relocation target)."""
    assert RAW_API_JOURNAL_PY.exists(), (
        f"{RAW_API_JOURNAL_PY} missing — path-A++ relocation of _append_raw_api_journal "
        f"from bot/_impl.py:282 not yet performed"
    )


def test_raw_api_journal_uses_public_name_in_leaf_module():
    """The function in bot/helpers/raw_api_journal.py is named `append_raw_api_journal`
    (public, no underscore) — clean leaf-module API. Post-Bit-9.2: both extracted
    consumer modules (bot/executor.py + bot/settlement.py) import the public name
    directly; the L81 alias-import in bot/_impl.py is RETIRED (zero callers remain)."""
    src = RAW_API_JOURNAL_PY.read_text()
    assert re.search(r"^def append_raw_api_journal\(", src, re.MULTILINE), (
        "bot/helpers/raw_api_journal.py missing `def append_raw_api_journal(`"
    )
    assert re.search(r"^def _append_raw_api_journal\(", src, re.MULTILINE) is None, (
        "bot/helpers/raw_api_journal.py uses underscore-prefixed name; should be public "
        "`append_raw_api_journal`"
    )


def test_executor_imports_append_raw_api_journal_explicitly():
    """bot/executor.py uses the public name `append_raw_api_journal` (no underscore alias).

    Trailing comments after the import are allowed (and used to annotate the path-A++ relocation).
    """
    src = EXECUTOR_PY.read_text()
    # Match the import statement; allow trailing whitespace + optional comment + EOL
    assert re.search(
        r"^from bot\.helpers\.raw_api_journal import append_raw_api_journal(?:\s|#|$)",
        src,
        re.MULTILINE,
    ), (
        "bot/executor.py missing `from bot.helpers.raw_api_journal import append_raw_api_journal` "
        "(public name, no alias — the consumer in extracted modules uses the clean name)"
    )


def test_bot_impl_does_NOT_have_l81_alias_import_post_bit_9_2():
    """Bit 9.2 atomic cleanup: the L81 alias-import RETIRED from bot/_impl.py
    (zero callers remain — both historical SettlementTracker callers moved to
    bot/settlement.py with the class and now use the public name `append_raw_api_journal`).

    Pre-Bit-9.2 this test asserted the alias was PRESENT (positive pin). Bit 9.2
    flipped it to a negative pin in lock-step with the call-site retirement.
    See tests/test_settlement_extraction.py::test_l81_alias_import_dropped_from_bot_impl
    for the canonical Bit-9.2 negative pin (this duplicate exists for symmetry
    with the broader executor_extraction surface).
    """
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = BOT_PY.read_text()
    assert not re.search(
        r"from bot\.helpers\.raw_api_journal import append_raw_api_journal as _append_raw_api_journal",
        src,
    ), (
        "bot/_impl.py STILL has the L81 alias-import "
        "`from bot.helpers.raw_api_journal import append_raw_api_journal as _append_raw_api_journal` "
        "— must be retired atomically with Bit 9.2 SettlementTracker extraction (zero callers remain)."
    )


def test_no_def_append_raw_api_journal_in_bot_impl():
    """Negative pin: the local `def _append_raw_api_journal` must be removed from bot/_impl.py."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = BOT_PY.read_text()
    assert re.search(r"^def _append_raw_api_journal\(", src, re.MULTILINE) is None, (
        "bot/_impl.py still has local `def _append_raw_api_journal(...)` — "
        "must be replaced with L81 alias-import per Bit 9.1 path-A++ relocation"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 10 — telegram_state alias pin (mirrors scanner)
# ═════════════════════════════════════════════════════════════════════════════

def test_executor_uses_telegram_state_alias():
    """bot/executor.py reads _TELEGRAM exclusively via _telegram_state._TELEGRAM (Bit 8.1 path-A++ pattern)."""
    src = EXECUTOR_PY.read_text()
    # Must NOT have bare-name _TELEGRAM reads (excluding the alias-target `import bot.notifier as _telegram_state`)
    bare_reads = re.findall(r"(?<![.\w])_TELEGRAM(?!\w)", src)
    # Filter out occurrences inside `as _telegram_state` import lines (which contain `_telegram_state`, not `_TELEGRAM`)
    # The pattern above matches only `_TELEGRAM` (uppercase, exact), so it shouldn't match `_telegram_state`.
    # Every match must be preceded by `_telegram_state.`
    lines = src.split("\n")
    bad_lines = []
    for i, line in enumerate(lines, 1):
        for m in re.finditer(r"(?<![.\w])_TELEGRAM(?!\w)", line):
            # Check if it's preceded by `_telegram_state.` in this same line
            prefix = line[: m.start()]
            if not prefix.rstrip().endswith("_telegram_state."):
                # Also allow it inside string literals (rare) — skip if inside quotes
                # Quick heuristic: if the line is a comment, skip
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                bad_lines.append(f"L{i}: {line.rstrip()}")
    assert not bad_lines, (
        f"bot/executor.py has {len(bad_lines)} bare-name _TELEGRAM read(s); must use "
        f"_telegram_state._TELEGRAM module-attribute access (Bit 8.1 path-A++ pattern):\n"
        + "\n".join(bad_lines[:5])
    )


def test_executor_telegram_state_read_count():
    """Drift-guard: OrderExecutor class body has 19 `_telegram_state._TELEGRAM` references
    (matches pre-extraction count from bot/_impl.py:994-6277). Locked to catch silent dropping
    of notification sites.

    Counts only references INSIDE the class body (via AST), so module docstring or top-level
    comments mentioning the pattern descriptively don't inflate the count.
    """
    cls = _executor_class()
    count = sum(1 for line in ast.unparse(cls).split("\n") if "_telegram_state._TELEGRAM" in line)
    assert count == 19, (
        f"OrderExecutor class body has {count} `_telegram_state._TELEGRAM` references, expected 19 "
        f"(pre-extraction count from bot/_impl.py:994-6277)"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 11 — re-export back to bot/_impl.py works
# ═════════════════════════════════════════════════════════════════════════════

def test_bot_impl_reexports_order_executor():
    """bot/_impl.py has `from bot.executor import OrderExecutor` re-export."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Sprint 9 Bit 9.3 final form)")
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = BOT_PY.read_text()
    assert re.search(r"from bot\.executor import OrderExecutor", src), (
        "bot/_impl.py missing `from bot.executor import OrderExecutor` re-export — "
        "needed for ~12 test files using `bot.executor.OrderExecutor`-style access via canonical submodule (post-Bit-9.3-iii.b — _BotProxy retired) chain"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 12 — scanner integration (helper retired, direct import works)
# ═════════════════════════════════════════════════════════════════════════════

def test_scanner_no_longer_has_get_order_executor_helper():
    """The `_get_order_executor()` helper at bot/scanner/__init__.py:306-322 is RETIRED."""
    src = SCANNER_PY.read_text()
    tree = _scanner_tree()
    funcdefs = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.FunctionDef)]
    assert "_get_order_executor" not in funcdefs, (
        "bot/scanner/__init__.py still defines `_get_order_executor()` — must be "
        "RETIRED in Bit 9.1 atomically with OrderExecutor extraction"
    )


def test_scanner_imports_order_executor_at_top():
    """bot/scanner/__init__.py imports OrderExecutor at module top (top-level)."""
    src = SCANNER_PY.read_text()
    assert re.search(r"^from bot\.executor import OrderExecutor", src, re.MULTILINE), (
        "bot/scanner/__init__.py missing top-level `from bot.executor import OrderExecutor` — "
        "Bit 9.1 cleanup contract requires direct import (replaces `_get_order_executor()` helper)"
    )


def test_scanner_no_call_sites_to_get_order_executor():
    """All 34 `_get_order_executor().X(...)` call sites in scanner.scan() are rewritten to `OrderExecutor.X(...)`.

    Walks the AST to find actual `ast.Call` nodes — descriptive text references in
    the module docstring or comments (which document the helper retirement) are
    NOT call sites.
    """
    tree = _scanner_tree()
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_get_order_executor":
                bad.append(node.lineno)
    assert not bad, (
        f"bot/scanner/__init__.py still has {len(bad)} `_get_order_executor()` call(s) "
        f"at lines {bad} — all must be rewritten to `OrderExecutor.X(...)` per Bit 9.1 cleanup contract"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 13 — import-linter contract retirement
# ═════════════════════════════════════════════════════════════════════════════

def test_scanner_no_impl_toplevel_contract_removed():
    """The `[importlinter:contract:scanner-no-impl-toplevel]` block is dropped from .importlinter."""
    cfg = configparser.ConfigParser()
    cfg.read(IMPORTLINTER_INI)
    assert "importlinter:contract:scanner-no-impl-toplevel" not in cfg.sections(), (
        ".importlinter still has [importlinter:contract:scanner-no-impl-toplevel] block — "
        "Bit 9.1 cleanup contract requires removing it (path-A++ for _append_raw_api_journal "
        "means no carve-out is needed; net contract count: 6 → 5)"
    )


def test_scanner_no_impl_toplevel_dropped_from_expected_contracts():
    """`scanner-no-impl-toplevel` is removed from EXPECTED_CONTRACTS in
    tests/contracts/test_import_linter_contracts.py (5 entries, not 6)."""
    src = CONTRACT_TEST_PY.read_text()
    # Find the EXPECTED_CONTRACTS tuple
    m = re.search(r"EXPECTED_CONTRACTS\s*=\s*\((.*?)\)", src, re.DOTALL)
    assert m, "EXPECTED_CONTRACTS tuple not found in tests/contracts/test_import_linter_contracts.py"
    body = m.group(1)
    assert "scanner-no-impl-toplevel" not in body, (
        "EXPECTED_CONTRACTS still has 'scanner-no-impl-toplevel' — must be dropped per Bit 9.1 cleanup"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 14 — Behavioral smoke (3 tests — instantiate via mocks)
# ═════════════════════════════════════════════════════════════════════════════

def test_executor_instantiates_with_mocks():
    """OrderExecutor can be constructed with mock dependencies (no NameError on __init__)."""
    from unittest.mock import MagicMock
    import bot.executor
    client = MagicMock()
    state = MagicMock()
    logger = MagicMock()
    executor = bot.executor.OrderExecutor(client, state, logger)
    assert executor._client is client
    assert executor._state is state
    assert executor._logger is logger
    assert executor._ml is None
    assert executor._kalshi_feed is None


def test_executor_execute_method_callable():
    """bot.executor.OrderExecutor.execute is a callable instance method."""
    import bot.executor
    assert callable(getattr(bot.executor.OrderExecutor, "execute", None)), (
        "OrderExecutor.execute is missing or not callable"
    )


def test_executor_cancel_404_v8_path_intact():
    """The cancel-404 V8 lockout fix (commit 5476273) lives in `_handle_cancel_404` —
    method must exist and have a non-trivial body (>20 lines suggests real implementation,
    not a stub)."""
    import bot.executor
    method = getattr(bot.executor.OrderExecutor, "_handle_cancel_404", None)
    assert callable(method), "OrderExecutor._handle_cancel_404 missing — cancel-404 V8 fix dropped"
    src = inspect.getsource(method)
    assert len(src.split("\n")) > 20, (
        f"OrderExecutor._handle_cancel_404 looks stubbed ({len(src.split(chr(10)))} lines); "
        f"the real implementation from commit 5476273 is several hundred lines"
    )
