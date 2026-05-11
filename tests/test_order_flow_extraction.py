"""Bit 9.3.5 — OrderFlowEngine + KalshiOrderFlowTracker extracted from bot/_impl.py to bot/order_flow.py.

Bit 9.3.5 (Sprint 9 closing sister leaf, 2026-05-10):
  OrderFlowEngine → bot/order_flow.py
  KalshiOrderFlowTracker → bot/order_flow.py

  (122 LOC + 240 LOC class bodies + header = 378 LOC bot/order_flow.py.
  Clean leaf — mirrors Bit 9.2 SettlementTracker shape but smaller surface:
  zero _telegram_state consumers, zero _cal_state consumers, zero
  bot._impl-below-line-119 names. Constants from bot.constants only.)

  The 2 # REMOVE BIT 9.3.5 markers in MainLoop.__init__'s late-binding
  block (bot/main_loop.py) collapse to a top-level
  `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker`.

  Sister cleanup atomic in same commit:
  - bot/scanner/__init__.py: Optional["OrderFlowEngine"] and
    Optional["KalshiOrderFlowTracker"] forward-refs UNQUOTED post-9.3.5;
    new top-level `from bot.order_flow import ...` (no cycle — bot.order_flow
    has zero bot.scanner edges).
  - bot/_impl.py: ~1,035 → ~767 LOC. Class bodies deleted; re-export
    `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker`
    added immediately after the line-119 MainLoop re-export.
  - bot/main_loop.py late-binding block: 4 names → 2 names (HPSB only).
  - .importlinter: helpers-leaf forbidden_modules extended with bot.order_flow;
    net contracts unchanged at 5 (clean-leaf shape).
  - 5-consumer _telegram_state enumeration UNCHANGED — bot/order_flow.py
    has 0 hits on `_telegram_state._TELEGRAM`.

Path-A++ deviation: NONE — this is the cleanest possible leaf. No
late-binding helpers, no alias retirement, no carve-outs. Just two
self-contained classes that import only stdlib + bot.constants.

Related lessons:
  L32 (Plan-agent), L33 (consumer-class identity — bot/scanner forward-refs
  unquoted in same Bit), L38 (AST walk retargets — test_fetchers_extraction.py
  walks OrderFlowEngine and must retarget BOT_PY → ORDER_FLOW_PY), L40 (no
  @patch routing needed), L41 (no hand-counted breadcrumbs — search anchors
  only), L78 (star-import-aware free-var scan), L86/L90 (doc-drift contagion
  sweep), L93 (iCloud-conflict pre-commit sweep).

Mirrors tests/test_settlement_extraction.py (Bit 9.2 clean leaf) +
tests/test_main_loop_extraction.py (Bit 9.3 marker-collapse pattern).
"""
from __future__ import annotations

import ast
import configparser
import inspect
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import bot.engines  # noqa: F401
import bot.notifier  # noqa: F401
import bot.scanner  # noqa: F401


REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_PY = REPO_ROOT / "bot" / "_impl.py"
ORDER_FLOW_PY = REPO_ROOT / "bot" / "order_flow.py"
MAIN_LOOP_PY = REPO_ROOT / "bot" / "main_loop.py"
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
EXECUTOR_PY = REPO_ROOT / "bot" / "executor.py"
SETTLEMENT_PY = REPO_ROOT / "bot" / "settlement.py"
NOTIFIER_PY = REPO_ROOT / "bot" / "notifier.py"
INIT_PY = REPO_ROOT / "bot" / "__init__.py"
IMPORTLINTER_INI = REPO_ROOT / ".importlinter"


# ============================================================ Module-level data
# Per L41: parametrize tuples ARE the ground truth — no separate count claim.
# Names AST-extracted from bot/_impl.py:658-777 (OrderFlowEngine) +
# bot/_impl.py:780-934 (KalshiOrderFlowTracker) pre-extraction.

# 2 OrderFlowEngine instance methods
OFE_INSTANCE_METHODS = ("__init__", "get_signals")

# 5 KalshiOrderFlowTracker instance methods
KOFT_INSTANCE_METHODS = (
    "__init__", "record_snapshot", "get_signals", "cleanup_stale", "get_tracked_count",
)

# 21 bot.constants names used by OFE + KOFT (union; L78 free-var scan output).
ORDER_FLOW_BOT_CONSTANTS_NAMES = (
    # OFE only (9)
    "CROSS_EXCHANGE_CONSENSUS_MIN",
    "FUNDING_RATE_ELEVATED",
    "FUNDING_RATE_EXTREME",
    "OFA_CONSENSUS_BOOST",
    "OFA_CONSENSUS_REDUCE",
    "OFA_ELEVATED_FUNDING_REDUCE",
    "OFA_EXTREME_FUNDING_REDUCE",
    "OFA_LEAD_BOOST",
    "OFA_MAX_ADJUSTMENT",
    # KOFT only (11)
    "KALSHI_OFT_BUFFER_SIZE",
    "KALSHI_OFT_DEPTH_DRAIN_PCT",
    "KALSHI_OFT_IMBALANCE_STRONG",
    "KALSHI_OFT_IMBALANCE_WEAK",
    "KALSHI_OFT_LOG_INTERVAL",
    "KALSHI_OFT_MIN_SNAPSHOTS",
    "KALSHI_OFT_STALE_SECONDS",
    "OFA_KALSHI_CONVERGENCE_BOOST",
    "OFA_KALSHI_DEPTH_DRAIN_BOOST",
    "OFA_KALSHI_IMBALANCE_BOOST",
    "OFA_KALSHI_IMBALANCE_REDUCE",
    # Shared (1)
    "KALSHI_OFT_SHADOW_MODE",
)

# Forbidden numerical libraries — same as scanner/executor/settlement; OFE+KOFT
# are pure order-flow signals (stdlib + bot.constants only).
FORBIDDEN_NUMERICAL_IMPORTS = ("numpy", "scipy", "torch", "sklearn", "pandas")


# ─── Cached AST parse helpers ──────────────────────────────────────────────

def _order_flow_tree() -> ast.Module:
    return ast.parse(ORDER_FLOW_PY.read_text())


def _bot_impl_tree() -> ast.Module:
    return ast.parse(BOT_PY.read_text())


def _main_loop_tree() -> ast.Module:
    return ast.parse(MAIN_LOOP_PY.read_text())


def _scanner_tree() -> ast.Module:
    return ast.parse(SCANNER_PY.read_text())


def _order_flow_class(name: str) -> ast.ClassDef:
    tree = _order_flow_tree()
    return next(
        n for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.ClassDef) and n.name == name
    )


def _method_in(cls: ast.ClassDef, name: str) -> ast.FunctionDef:
    return next(
        m for m in ast.iter_child_nodes(cls)
        if isinstance(m, ast.FunctionDef) and m.name == name
    )


def _main_loop_method(name: str) -> ast.FunctionDef:
    tree = _main_loop_tree()
    cls = next(n for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef) and n.name == "MainLoop")
    return _method_in(cls, name)


def _imports_inside_method(method: ast.FunctionDef) -> set[str]:
    """Return set of bare names imported (via `from ... import ...`) inside a method body."""
    names: set[str] = set()
    for node in ast.walk(method):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity (8 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_order_flow_module_file_exists():
    """bot/order_flow.py exists post-extraction."""
    assert ORDER_FLOW_PY.exists(), "bot/order_flow.py missing — Bit 9.3.5 extraction not yet performed"


def test_orderflowengine_class_in_bot_order_flow_module():
    """Positive AST pin: OrderFlowEngine defined in bot/order_flow.py."""
    tree = _order_flow_tree()
    classes = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert "OrderFlowEngine" in classes, (
        f"OrderFlowEngine classdef not found in bot/order_flow.py; classes present: {classes}"
    )


def test_orderflowengine_class_NOT_in_bot_impl_module():
    """Negative AST pin: OrderFlowEngine classdef is NOT in bot/_impl.py post-extraction."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-ii final form)")
    tree = _bot_impl_tree()
    classes = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert "OrderFlowEngine" not in classes, (
        "bot/_impl.py still contains class OrderFlowEngine — Bit 9.3.5 extraction incomplete; "
        "the class body must move to bot/order_flow.py atomically with the re-export."
    )


def test_kalshioft_class_in_bot_order_flow_module():
    """Positive AST pin: KalshiOrderFlowTracker defined in bot/order_flow.py."""
    tree = _order_flow_tree()
    classes = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert "KalshiOrderFlowTracker" in classes, (
        f"KalshiOrderFlowTracker classdef not found in bot/order_flow.py; classes present: {classes}"
    )


def test_kalshioft_class_NOT_in_bot_impl_module():
    """Negative AST pin: KalshiOrderFlowTracker classdef is NOT in bot/_impl.py post-extraction."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-ii final form)")
    tree = _bot_impl_tree()
    classes = [n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)]
    assert "KalshiOrderFlowTracker" not in classes, (
        "bot/_impl.py still contains class KalshiOrderFlowTracker — Bit 9.3.5 extraction incomplete."
    )


def test_orderflowengine_module_attr_resolves_via_proxy():
    """Bit 9.3.5 re-export: bot.order_flow.OrderFlowEngine resolves through the proxy chain."""
    import bot
    import bot._impl
    import bot.order_flow
    assert bot.order_flow.OrderFlowEngine is bot._impl.OrderFlowEngine, (
        "bot.order_flow.OrderFlowEngine not resolving to bot._impl.OrderFlowEngine via proxy"
    )
    assert bot._impl.OrderFlowEngine is bot.order_flow.OrderFlowEngine, (
        "bot._impl.OrderFlowEngine not the SAME class as bot.order_flow.OrderFlowEngine — "
        "the re-export `from bot.order_flow import OrderFlowEngine` must bind the same "
        "class object (not re-define)."
    )
    assert bot.order_flow.OrderFlowEngine.__module__ == "bot.order_flow", (
        f"bot.order_flow.OrderFlowEngine.__module__ = {bot.order_flow.OrderFlowEngine.__module__!r}; "
        f"expected 'bot.order_flow' post-extraction."
    )


def test_kalshioft_module_attr_resolves_via_proxy():
    """Bit 9.3.5 re-export: bot.order_flow.KalshiOrderFlowTracker resolves through the proxy chain."""
    import bot
    import bot._impl
    import bot.order_flow
    assert bot.order_flow.KalshiOrderFlowTracker is bot._impl.KalshiOrderFlowTracker
    assert bot._impl.KalshiOrderFlowTracker is bot.order_flow.KalshiOrderFlowTracker
    assert bot.order_flow.KalshiOrderFlowTracker.__module__ == "bot.order_flow"


def test_orderflowengine_init_signature_unchanged():
    """OFE.__init__ signature must be byte-identical (extraction is structural)."""
    import bot
    sig = inspect.signature(bot.order_flow.OrderFlowEngine.__init__)
    params = list(sig.parameters.keys())
    assert params == ["self", "cross_feed", "coinglass", "kalshi_oft"], (
        f"OrderFlowEngine.__init__ signature drift: {params}"
    )
    # All three injected dependencies default to None
    for p in ("cross_feed", "coinglass", "kalshi_oft"):
        assert sig.parameters[p].default is None, (
            f"OrderFlowEngine.__init__ {p} default drift: {sig.parameters[p].default!r}"
        )


def test_kalshioft_init_signature_unchanged():
    """KOFT.__init__ signature must be byte-identical (no params)."""
    import bot
    sig = inspect.signature(bot.order_flow.KalshiOrderFlowTracker.__init__)
    params = list(sig.parameters.keys())
    assert params == ["self"], f"KalshiOrderFlowTracker.__init__ signature drift: {params}"


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — Drift guards (3 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_no_top_level_bot_impl_import_in_order_flow():
    """Clean leaf: bot/order_flow.py must NOT have a top-level `import bot._impl`
    or `from bot._impl import ...`. OFE+KOFT have zero references to names
    bound below the line-119 re-export point in bot/_impl.py — they only need
    bot.constants + stdlib. This pins the clean-leaf shape (mirrors Bit 9.2)."""
    tree = _order_flow_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/order_flow.py has top-level `import bot._impl` (line {node.lineno}). "
                    f"Bit 9.3.5 is a clean leaf — no late-binding needed."
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/order_flow.py has top-level `from bot._impl import ...` (line {node.lineno}). "
                f"Bit 9.3.5 is a clean leaf — no late-binding needed."
            )


def _executable_source(tree: ast.Module) -> str:
    """Return the source text of all top-level nodes that are NOT module docstrings.
    Used to test for code-level references without false positives from
    historical narrative in the module docstring."""
    src = ORDER_FLOW_PY.read_text()
    pieces: list[str] = []
    for node in ast.iter_child_nodes(tree):
        # Skip the module-level docstring (first Expr -> Constant str)
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            continue
        seg = ast.get_source_segment(src, node)
        if seg:
            pieces.append(seg)
    return "\n".join(pieces)


def test_no_telegram_state_in_order_flow():
    """L83 — bot/order_flow.py must NOT reach the _TELEGRAM singleton. OFE+KOFT
    do not emit alerts. AST + non-docstring-source check; historical narrative
    in the module docstring is allowed."""
    tree = _order_flow_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot.notifier", (
                    "bot/order_flow.py imports bot.notifier at the top level — "
                    "OFE+KOFT do not need it."
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module != "bot.notifier", (
                "bot/order_flow.py imports from bot.notifier at the top level — "
                "OFE+KOFT do not need it."
            )
    code = _executable_source(tree)
    assert "_telegram_state._TELEGRAM" not in code, (
        "bot/order_flow.py references _telegram_state._TELEGRAM in executable code — "
        "OFE+KOFT do not emit alerts; this is a regression."
    )


def test_no_cal_state_in_order_flow():
    """Parallel to test_no_telegram_state — OFE+KOFT do not consume the
    calibration singleton. AST + non-docstring-source check."""
    tree = _order_flow_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "bot.engines" or (node.module or "").startswith("bot.engines."):
                imported = {alias.asname or alias.name for alias in node.names}
                assert "_cal_state" not in imported and "calibration" not in imported, (
                    "bot/order_flow.py imports calibration (as _cal_state or otherwise) — "
                    "OFE+KOFT do not consume calibration; this is a regression."
                )
    code = _executable_source(tree)
    assert "_cal_state." not in code, (
        "bot/order_flow.py references _cal_state.X in executable code — "
        "OFE+KOFT do not consume calibration; this is a regression."
    )


def test_order_flow_method_count_matches_ast():
    """L41 — parametrize tuples ARE the ground truth; assert the AST agrees."""
    ofe_cls = _order_flow_class("OrderFlowEngine")
    koft_cls = _order_flow_class("KalshiOrderFlowTracker")
    ofe_actual = sorted(
        m.name for m in ast.iter_child_nodes(ofe_cls) if isinstance(m, ast.FunctionDef)
    )
    koft_actual = sorted(
        m.name for m in ast.iter_child_nodes(koft_cls) if isinstance(m, ast.FunctionDef)
    )
    assert ofe_actual == sorted(OFE_INSTANCE_METHODS), (
        f"OrderFlowEngine method drift: AST={ofe_actual}, expected={sorted(OFE_INSTANCE_METHODS)}"
    )
    assert koft_actual == sorted(KOFT_INSTANCE_METHODS), (
        f"KalshiOrderFlowTracker method drift: AST={koft_actual}, expected={sorted(KOFT_INSTANCE_METHODS)}"
    )


def test_order_flow_no_static_methods():
    """OFE+KOFT have zero @staticmethod / @classmethod (regression seal)."""
    for cls in (_order_flow_class("OrderFlowEngine"), _order_flow_class("KalshiOrderFlowTracker")):
        for m in ast.iter_child_nodes(cls):
            if isinstance(m, ast.FunctionDef):
                decos = [d.id for d in m.decorator_list if isinstance(d, ast.Name)]
                assert "staticmethod" not in decos and "classmethod" not in decos, (
                    f"{cls.name}.{m.name} unexpectedly decorated with @{decos}"
                )


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — Method presence (parametrized)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("method_name", OFE_INSTANCE_METHODS)
def test_orderflowengine_method_present(method_name: str):
    """Every named method survives extraction."""
    import bot
    assert hasattr(bot.order_flow.OrderFlowEngine, method_name), (
        f"OrderFlowEngine.{method_name} missing post-extraction"
    )


@pytest.mark.parametrize("method_name", KOFT_INSTANCE_METHODS)
def test_kalshioft_method_present(method_name: str):
    """Every named method survives extraction."""
    import bot
    assert hasattr(bot.order_flow.KalshiOrderFlowTracker, method_name), (
        f"KalshiOrderFlowTracker.{method_name} missing post-extraction"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 4 — Constants partition (parametrized, 21 entries)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("const_name", ORDER_FLOW_BOT_CONSTANTS_NAMES)
def test_order_flow_constant_partitioned_to_bot_constants(const_name: str):
    """L39 — every named constant lives in bot.constants (not config / market_config)."""
    import bot.constants as bc
    assert hasattr(bc, const_name), (
        f"{const_name} not in bot.constants — partition drift; "
        f"check whether it actually lives in config / market_config."
    )


def test_order_flow_imports_constants_explicitly_not_via_star():
    """L40 — bot/order_flow.py uses explicit `from bot.constants import (...)`,
    NOT star-import. Star-import laundering is the L40 smell."""
    src = ORDER_FLOW_PY.read_text()
    assert "from bot.constants import *" not in src, (
        "bot/order_flow.py uses `from bot.constants import *` — replace with "
        "explicit imports per L40."
    )
    # Positive pin: the explicit import block exists
    assert re.search(r"from bot\.constants import \(", src) or re.search(
        r"from bot\.constants import \w+", src
    ), "bot/order_flow.py missing `from bot.constants import (...)` block"


# ═════════════════════════════════════════════════════════════════════════════
# Section 5 — Forbidden numerical imports
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("forbidden", FORBIDDEN_NUMERICAL_IMPORTS)
def test_order_flow_no_forbidden_numerical_imports(forbidden: str):
    """OFE+KOFT are pure stdlib + bot.constants. No numpy/scipy/torch/sklearn/pandas."""
    tree = _order_flow_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden), (
                    f"bot/order_flow.py imports forbidden numerical library: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or not node.module.startswith(forbidden), (
                f"bot/order_flow.py imports from forbidden numerical library: {node.module}"
            )


# ═════════════════════════════════════════════════════════════════════════════
# Section 6 — .importlinter contract pin
# ═════════════════════════════════════════════════════════════════════════════

def test_order_flow_in_helpers_leaf_forbidden_modules():
    """`bot.order_flow` must be listed in the helpers-leaf contract's forbidden_modules
    so a future bot/helpers/ extension cannot back-edge into the order-flow module."""
    ini = configparser.ConfigParser(strict=False, delimiters=("=",))
    ini.read(IMPORTLINTER_INI)
    helpers_leaf = next(
        s for s in ini.sections() if "helpers-leaf" in s
    )
    forbidden = ini.get(helpers_leaf, "forbidden_modules")
    forbidden_names = {ln.strip() for ln in forbidden.splitlines() if ln.strip()}
    assert "bot.order_flow" in forbidden_names, (
        f"bot.order_flow not in helpers-leaf forbidden_modules; current: {sorted(forbidden_names)}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 7 — Late-binding collapse (Bit 9.3.5 unique surface)
# ═════════════════════════════════════════════════════════════════════════════

def test_main_loop_top_level_imports_order_flow():
    """Bit 9.3.5 collapses the 2 # REMOVE BIT 9.3.5 markers in MainLoop.__init__
    to a top-level `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker`.
    Top-level import is safe because bot.order_flow has zero bot._impl edges."""
    src = MAIN_LOOP_PY.read_text()
    assert re.search(
        r"from bot\.order_flow import \(?\s*OrderFlowEngine\s*,\s*KalshiOrderFlowTracker\s*\)?",
        src,
    ) or (
        "from bot.order_flow import OrderFlowEngine" in src
        and "KalshiOrderFlowTracker" in src
    ), (
        "bot/main_loop.py missing top-level `from bot.order_flow import "
        "OrderFlowEngine, KalshiOrderFlowTracker`"
    )


def test_main_loop_init_no_longer_late_binds_ofe():
    """Bit 9.3.5: OrderFlowEngine is NOT in the late-binding block of MainLoop.__init__."""
    init = _main_loop_method("__init__")
    late_bound = _imports_inside_method(init)
    assert "OrderFlowEngine" not in late_bound, (
        f"OrderFlowEngine still appears in MainLoop.__init__'s late-binding block: {sorted(late_bound)}"
    )


def test_main_loop_init_no_longer_late_binds_koft():
    """Bit 9.3.5: KalshiOrderFlowTracker is NOT in the late-binding block of MainLoop.__init__."""
    init = _main_loop_method("__init__")
    late_bound = _imports_inside_method(init)
    assert "KalshiOrderFlowTracker" not in late_bound, (
        f"KalshiOrderFlowTracker still appears in MainLoop.__init__'s late-binding block: {sorted(late_bound)}"
    )


def test_main_loop_init_has_zero_bot_impl_late_binding_post_9_3_iii_a():
    """Post-Bit-9.3-iii.a (2026-05-11): the Bit 9.3.5-era 2-name late-binding
    block (`_HPSB_MISSING_BLEEDERS` + `_HPSB_VALIDATOR_UNAVAILABLE_REASON`)
    has been ELIMINATED. Both names now live in clean-leaf bot/boot.py and
    bot/main_loop.py top-imports them. MainLoop.__init__ has zero bot._impl
    late-bindings."""
    init = _main_loop_method("__init__")
    bot_impl_imports = set()
    for node in ast.walk(init):
        if isinstance(node, ast.ImportFrom) and node.module == "bot._impl":
            for alias in node.names:
                bot_impl_imports.add(alias.asname or alias.name)
    assert bot_impl_imports == set(), (
        f"MainLoop.__init__ still has bot._impl late-binding: {bot_impl_imports}. "
        f"Bit 9.3-iii.a relocated all 4 boot-time bindings to bot/boot.py."
    )


def test_no_remove_bit_9_3_5_markers_remain():
    """Bit 9.3.5 ships clean: the literal `# REMOVE BIT 9.3.5` marker must
    not appear as a Python comment token in any bot/*.py file. This catches
    a future agent who copy-pastes the marker without realizing Bit 9.3.5
    already shipped. Historical references in module docstrings (string
    literals) are allowed — they document the cleanup contract that happened."""
    import tokenize
    bot_dir = REPO_ROOT / "bot"
    offenders = []
    for py in bot_dir.rglob("*.py"):
        if " " in py.stem:  # L93 iCloud-conflict filter
            continue
        try:
            with open(py, "rb") as f:
                for tok in tokenize.tokenize(f.readline):
                    if tok.type == tokenize.COMMENT and "REMOVE BIT 9.3.5" in tok.string:
                        offenders.append(f"{py.relative_to(REPO_ROOT)}:{tok.start[0]}")
        except (tokenize.TokenizeError, OSError):
            continue
    assert not offenders, (
        f"# REMOVE BIT 9.3.5 comment markers still present at: {offenders}; "
        f"Bit 9.3.5 already shipped — remove these markers."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 8 — Sister-cleanup contract pins (bot/scanner forward-refs unquoted)
# ═════════════════════════════════════════════════════════════════════════════

def test_scanner_imports_order_flow_at_top_level():
    """Bit 9.3.5 sister-cleanup: bot/scanner/__init__.py adds a top-level
    `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker`
    (replacing the prior `Optional["OrderFlowEngine"]` quoted forward-ref
    convention). Safe — bot.order_flow has zero bot.scanner edges."""
    src = SCANNER_PY.read_text()
    assert re.search(
        r"from bot\.order_flow import \(?\s*OrderFlowEngine\s*,\s*KalshiOrderFlowTracker\s*\)?",
        src,
    ) or (
        "from bot.order_flow import OrderFlowEngine" in src
        and "KalshiOrderFlowTracker" in src
    ), (
        "bot/scanner/__init__.py missing top-level `from bot.order_flow import "
        "OrderFlowEngine, KalshiOrderFlowTracker` (sister cleanup)"
    )


def test_scanner_forward_refs_unquoted_post_9_3_5():
    """Bit 9.3.5 sister-cleanup: the `order_flow` and `kalshi_oft` parameter
    annotations on `OpportunityScanner.__init__` are unquoted (post-extraction
    bot.order_flow is a clean leaf, so the top-level import in bot/scanner/__init__.py
    resolves cleanly). Inspect the AST annotation rather than the raw source —
    historical narrative comments may still reference the old quoted form."""
    scanner_tree = _scanner_tree()
    cls = next(
        n for n in ast.iter_child_nodes(scanner_tree)
        if isinstance(n, ast.ClassDef) and n.name == "OpportunityScanner"
    )
    init = next(
        m for m in ast.iter_child_nodes(cls)
        if isinstance(m, ast.FunctionDef) and m.name == "__init__"
    )
    sig_args = list(init.args.args) + list(init.args.kwonlyargs)
    for arg_name in ("order_flow", "kalshi_oft"):
        arg = next((a for a in sig_args if a.arg == arg_name), None)
        assert arg is not None, f"OpportunityScanner.__init__ missing {arg_name} param"
        assert arg.annotation is not None, f"{arg_name} annotation is None"
        ann_src = ast.unparse(arg.annotation)
        # Quoted form would be `Optional["OrderFlowEngine"]` with double-quoted string-literal inside subscript
        assert "'" not in ann_src and '"' not in ann_src, (
            f"OpportunityScanner.__init__ {arg_name} annotation still quoted: {ann_src!r}; "
            f"Bit 9.3.5 unquotes these (bot.order_flow has zero bot.scanner edge)."
        )


def test_bot_impl_re_exports_order_flow_classes():
    """bot/_impl.py must re-export OrderFlowEngine + KalshiOrderFlowTracker
    from bot.order_flow so the proxy chain (bot.X → bot._impl.X → bot.order_flow.X)
    remains stable."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-ii final form)")
    src = BOT_PY.read_text()
    assert re.search(
        r"from bot\.order_flow import \(?\s*OrderFlowEngine\s*,\s*KalshiOrderFlowTracker\s*\)?",
        src,
    ) or (
        "from bot.order_flow import OrderFlowEngine" in src
        and "KalshiOrderFlowTracker" in src
    ), "bot/_impl.py missing `from bot.order_flow import OrderFlowEngine, KalshiOrderFlowTracker` re-export"


# ═════════════════════════════════════════════════════════════════════════════
# Section 9 — Consumer-count seal (mirror Bit 9.3)
# ═════════════════════════════════════════════════════════════════════════════

def test_telegram_state_consumer_count_unchanged_post_9_3_5():
    """The 5-consumer _telegram_state._TELEGRAM enumeration is UNCHANGED by
    Bit 9.3.5: bot/_impl.py + bot/main_loop.py + bot/scanner/__init__.py +
    bot/executor.py + bot/settlement.py. bot/order_flow.py has zero hits
    (OFE+KOFT do not emit Telegram alerts)."""
    # All five existing consumers must have at least one hit
    consumers = (BOT_PY, MAIN_LOOP_PY, SCANNER_PY, EXECUTOR_PY, SETTLEMENT_PY)
    for path in consumers:
        if not path.exists():
            continue  # bot/_impl.py may be deleted by Bit 9.3-ii
        src = path.read_text()
        assert "_telegram_state._TELEGRAM" in src, (
            f"{path.relative_to(REPO_ROOT)} unexpectedly lost all _telegram_state._TELEGRAM "
            f"references — consumer count regression"
        )
    # bot/order_flow.py must have ZERO hits in EXECUTABLE code (proves clean leaf).
    # Module docstring historical references are allowed.
    tree = _order_flow_tree()
    of_code = _executable_source(tree)
    assert "_telegram_state._TELEGRAM" not in of_code, (
        "bot/order_flow.py unexpectedly references _telegram_state._TELEGRAM in "
        "executable code — OFE+KOFT must not emit Telegram alerts (clean leaf)."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 10 — Defensive (L93 iCloud conflicts)
# ═════════════════════════════════════════════════════════════════════════════

def test_no_icloud_conflict_files_in_order_flow_dir():
    """L93 — no iCloud-conflict copies (`bot/order_flow N.py` etc) survive
    the commit. These break contract walkers per Bit 9.3 R2/R3 incident."""
    bot_dir = REPO_ROOT / "bot"
    offenders = []
    for py in bot_dir.glob("order_flow*.py"):
        if " " in py.stem:
            offenders.append(str(py.relative_to(REPO_ROOT)))
    assert not offenders, f"iCloud-conflict copies of bot/order_flow.py found: {offenders}"


# ═════════════════════════════════════════════════════════════════════════════
# Section 11 — Behavioral smoke (light — extraction is structural)
# ═════════════════════════════════════════════════════════════════════════════

def test_orderflowengine_smoke_returns_documented_shape():
    """Construct OFE with MagicMock injected dependencies; assert get_signals
    returns the documented dict shape. Catches a regression where the
    extraction accidentally drops a key."""
    import bot
    cross_feed = MagicMock()
    cross_feed.get_lead_lag.return_value = {
        "consensus_direction": "none",
        "exchanges_above": 0,
        "exchanges_below": 0,
    }
    coinglass = MagicMock()
    coinglass.get_funding_rate.return_value = None
    kalshi_oft = MagicMock()
    kalshi_oft.get_signals.return_value = None
    ofe = bot.order_flow.OrderFlowEngine(cross_feed=cross_feed, coinglass=coinglass, kalshi_oft=kalshi_oft)
    result = ofe.get_signals("BTC", ticker="KXBTC-EXAMPLE-T0")
    assert set(result.keys()) >= {
        "prob_adjustment", "confidence", "signals", "adjustments_applied",
    }, f"OFE.get_signals output shape regression: keys={result.keys()}"
    assert set(result["signals"].keys()) >= {
        "cross_exchange", "funding", "kalshi_orderbook",
    }, f"OFE.get_signals 'signals' sub-dict shape regression: keys={result['signals'].keys()}"
    # Accept int or float — Python's `sum([])` returns 0 (int) when no
    # adjustments apply; the float type is only forced when min/max coerces.
    assert isinstance(result["prob_adjustment"], (int, float))
    assert isinstance(result["adjustments_applied"], list)


def test_kalshioft_smoke_get_tracked_count_starts_zero():
    """KOFT.get_tracked_count returns 0 on a fresh instance."""
    import bot
    koft = bot.order_flow.KalshiOrderFlowTracker()
    assert koft.get_tracked_count() == 0


def test_kalshioft_smoke_record_and_retrieve_signals():
    """KOFT.record_snapshot accepts a depth-5 orderbook dict and
    KOFT.get_signals returns None when buffer below KALSHI_OFT_MIN_SNAPSHOTS."""
    import bot
    from bot.constants import KALSHI_OFT_MIN_SNAPSHOTS
    koft = bot.order_flow.KalshiOrderFlowTracker()
    ob_data = {
        "yes": [[55, 100], [54, 50]],
        "no": [[45, 80], [44, 30]],
    }
    # Single snapshot — should not satisfy min-snapshots
    koft.record_snapshot("KXBTC-EXAMPLE-T0", ob_data, best_ask=55)
    if KALSHI_OFT_MIN_SNAPSHOTS > 1:
        assert koft.get_signals("KXBTC-EXAMPLE-T0") is None
    assert koft.get_tracked_count() == 1
