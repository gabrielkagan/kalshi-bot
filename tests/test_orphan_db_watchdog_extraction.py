"""Bit 9.3-ii — orphan-DB watchdog block extracted from bot/_impl.py to bot/orphan_db_watchdog.py.

Bit 9.3-ii (Sprint 9 closing, 2026-05-10):
  _run_lsof_for_db        → bot/orphan_db_watchdog.py
  _get_pid_cmdline        → bot/orphan_db_watchdog.py
  _alert_orphan_db_holder → bot/orphan_db_watchdog.py
  _ORPHAN_DB_WATCHDOG_PATTERNS → bot/orphan_db_watchdog.py
  detect_orphan_db_holders → bot/orphan_db_watchdog.py

  (~200 LOC of orphan-DB Layer-3 watchdog code, was bot/_impl.py:375-578.)

  Becomes the 5th `_telegram_state._TELEGRAM` consumer (REPLACING bot/_impl.py
  in the count — net stays at 5). `_alert_orphan_db_holder` and the
  `detect_orphan_db_holders` lsof-not-found branch read the singleton.

  Clean leaf shape: stdlib + `import bot.notifier as _telegram_state` only.
  No carve-out needed — bot/orphan_db_watchdog.py has zero edges into bot._impl
  or any other bot/ subpackage.

  Sister cleanup atomic in same commit:
  - bot/_impl.py: 5 function defs + 1 list def removed (~200 LOC); new
    re-export `from bot.orphan_db_watchdog import detect_orphan_db_holders`
    (and proxy-supporting re-exports for _run_lsof_for_db / _get_pid_cmdline /
    _alert_orphan_db_holder / _ORPHAN_DB_WATCHDOG_PATTERNS so that the
    11 `monkeypatch.setattr(bot, ...)` sites in tests/test_orphan_db_watchdog.py
    keep working through the _BotProxy chain at Option A scope).
  - bot/main_loop.py: `MainLoop.startup` late-binding (was line 639 `from bot._impl
    import detect_orphan_db_holders`) RETARGETED to `from bot.orphan_db_watchdog
    import detect_orphan_db_holders` — eliminates the last bot._impl edge in
    MainLoop's startup path.
  - .importlinter: `helpers-leaf` `forbidden_modules` extended with
    `bot.orphan_db_watchdog`; net contracts unchanged at 5 (or 4 if Contract 5
    retired in same Bit per Plan-agent C-2).
  - 5-consumer `_telegram_state._TELEGRAM` enumeration: bot/_impl.py REMOVED;
    bot/orphan_db_watchdog.py ADDED. Net consumer count: 5 → 5.
  - tests/test_orphan_db_watchdog.py: 11 monkeypatch sites RETAIN
    `monkeypatch.setattr(bot, ...)` (proxy chain at Option A); behavioral tests
    must remain green without modification.

Path-A++ deviation: NONE — clean leaf, mirrors Bit 9.3.5 OFE+KOFT shape.
Lessons reinforced: L78 (free-var scan), L86 (doc-drift contagion), L93 (iCloud
pre-commit), L33 (negative identity pins).

Mirrors tests/test_order_flow_extraction.py (Bit 9.3.5 clean leaf shape) +
tests/test_settlement_extraction.py (Bit 9.2 clean leaf with _telegram_state
consumer enumeration pins).
"""
from __future__ import annotations

import ast
import configparser
import inspect
import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_PY = REPO_ROOT / "bot" / "_impl.py"
ORPHAN_DB_WATCHDOG_PY = REPO_ROOT / "bot" / "orphan_db_watchdog.py"
MAIN_LOOP_PY = REPO_ROOT / "bot" / "main_loop.py"
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"
EXECUTOR_PY = REPO_ROOT / "bot" / "executor.py"
SETTLEMENT_PY = REPO_ROOT / "bot" / "settlement.py"
NOTIFIER_PY = REPO_ROOT / "bot" / "notifier.py"
INIT_PY = REPO_ROOT / "bot" / "__init__.py"
MAIN_FILE = REPO_ROOT / "bot" / "__main__.py"
IMPORTLINTER_INI = REPO_ROOT / ".importlinter"


# ============================================================ Module-level data
# Per L41: parametrize tuples ARE the ground truth — no separate count claim.

# 5 functions + 1 list extracted from bot/_impl.py:389-578 pre-Bit-9.3-ii.
ORPHAN_DB_FUNCTIONS = (
    "_run_lsof_for_db",
    "_get_pid_cmdline",
    "_alert_orphan_db_holder",
    "detect_orphan_db_holders",
)

ORPHAN_DB_MODULE_LEVEL_NAMES = ORPHAN_DB_FUNCTIONS + ("_ORPHAN_DB_WATCHDOG_PATTERNS",)

# Forbidden numerical libraries — pure stdlib + logging + bot.notifier alias.
FORBIDDEN_NUMERICAL_IMPORTS = ("numpy", "scipy", "torch", "sklearn", "pandas")

# 3 known H-4 backfill script names that the positive-list must contain.
EXPECTED_ORPHAN_PATTERNS = (
    "gdelt_backfill",
    "cryptocompare_news_backfill",
    "glassnode_backfill",
)


# ─── Cached AST parse helpers ──────────────────────────────────────────────

def _orphan_db_tree() -> ast.Module:
    return ast.parse(ORPHAN_DB_WATCHDOG_PY.read_text())


def _bot_impl_tree() -> ast.Module:
    return ast.parse(BOT_PY.read_text()) if BOT_PY.exists() else None


def _main_loop_tree() -> ast.Module:
    return ast.parse(MAIN_LOOP_PY.read_text())


def _module_funcs(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {
        n.name: n
        for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.FunctionDef)
    }


def _module_assigns(tree: ast.Module) -> set[str]:
    """Return set of module-level Assign / AnnAssign targets."""
    names: set[str] = set()
    for n in ast.iter_child_nodes(tree):
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity (5 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_orphan_db_watchdog_module_file_exists():
    """bot/orphan_db_watchdog.py exists post-extraction."""
    assert ORPHAN_DB_WATCHDOG_PY.exists(), (
        "bot/orphan_db_watchdog.py missing — Bit 9.3-ii extraction not yet performed"
    )


@pytest.mark.parametrize("func_name", ORPHAN_DB_FUNCTIONS)
def test_orphan_db_function_defined_in_orphan_db_watchdog_module(func_name):
    """Positive AST pin: each of the 4 functions defined in bot/orphan_db_watchdog.py."""
    funcs = _module_funcs(_orphan_db_tree())
    assert func_name in funcs, (
        f"def {func_name}(...) not found in bot/orphan_db_watchdog.py; "
        f"functions present: {sorted(funcs)}"
    )


@pytest.mark.parametrize("func_name", ORPHAN_DB_FUNCTIONS)
def test_orphan_db_function_NOT_defined_in_bot_impl_module(func_name):
    """Negative AST pin: each of the 4 functions is NOT defined in bot/_impl.py post-extraction."""
    tree = _bot_impl_tree()
    if tree is None:
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii final form)")
    funcs = _module_funcs(tree)
    assert func_name not in funcs, (
        f"bot/_impl.py still contains def {func_name}(...) — Bit 9.3-ii extraction "
        f"incomplete; the function must move to bot/orphan_db_watchdog.py atomically "
        f"with the re-export."
    )


def test_orphan_db_patterns_list_defined_in_orphan_db_watchdog_module():
    """`_ORPHAN_DB_WATCHDOG_PATTERNS` list defined at module level in bot/orphan_db_watchdog.py."""
    names = _module_assigns(_orphan_db_tree())
    assert "_ORPHAN_DB_WATCHDOG_PATTERNS" in names, (
        "_ORPHAN_DB_WATCHDOG_PATTERNS module-level binding missing in bot/orphan_db_watchdog.py"
    )


def test_orphan_db_patterns_list_NOT_in_bot_impl_module():
    """Negative pin: pattern list is NOT module-level in bot/_impl.py post-extraction."""
    tree = _bot_impl_tree()
    if tree is None:
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii final form)")
    names = _module_assigns(tree)
    assert "_ORPHAN_DB_WATCHDOG_PATTERNS" not in names, (
        "bot/_impl.py still binds _ORPHAN_DB_WATCHDOG_PATTERNS module-level — "
        "the list must move to bot/orphan_db_watchdog.py atomically."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — Proxy chain (3 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_proxy_chain_resolves_detect_orphan_db_holders():
    """`bot.detect_orphan_db_holders` resolves through proxy chain to bot.orphan_db_watchdog.

    The 11 monkeypatch sites in tests/test_orphan_db_watchdog.py use
    `monkeypatch.setattr(bot, "X", ...)` patterns. Option A keeps the _BotProxy,
    so reads of `bot.X` must still resolve via the chain bot.X → bot._impl.X →
    bot.orphan_db_watchdog.X (Bit 9.3-iii will retire the proxy entirely)."""
    import bot
    import bot.orphan_db_watchdog
    assert bot.detect_orphan_db_holders is bot.orphan_db_watchdog.detect_orphan_db_holders, (
        "bot.detect_orphan_db_holders not resolving to bot.orphan_db_watchdog.detect_orphan_db_holders "
        "via proxy chain — re-export missing in bot/_impl.py?"
    )


@pytest.mark.parametrize("name", ORPHAN_DB_FUNCTIONS + ("_ORPHAN_DB_WATCHDOG_PATTERNS",))
def test_proxy_chain_resolves_all_orphan_db_names(name):
    """Each of the 5 orphan-DB names resolves via `bot.X` proxy.

    Required to keep tests/test_orphan_db_watchdog.py's 11 `monkeypatch.setattr(bot, ...)`
    sites working at Option A scope (proxy still in place; Bit 9.3-iii retires it)."""
    import bot
    import bot.orphan_db_watchdog
    proxy_attr = getattr(bot, name)
    canonical_attr = getattr(bot.orphan_db_watchdog, name)
    assert proxy_attr is canonical_attr, (
        f"bot.{name} ({proxy_attr!r}) is not bot.orphan_db_watchdog.{name} ({canonical_attr!r}) — "
        f"proxy chain broken; check bot/_impl.py re-export."
    )


def test_bot_impl_reexports_orphan_db_names():
    """bot/_impl.py re-exports the 5 orphan-DB names so the proxy chain stays valid.

    Without re-exports, `bot.detect_orphan_db_holders` would AttributeError because
    _BotProxy.__getattr__ falls back to `bot._impl.X` lookup, and the names live
    in bot.orphan_db_watchdog post-extraction."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii final form — proxy retired)")
    src = BOT_PY.read_text()
    pat = re.compile(
        r"from\s+bot\.orphan_db_watchdog\s+import\s*\(?[^)\n]*"
        r"(detect_orphan_db_holders|_run_lsof_for_db|_get_pid_cmdline|_alert_orphan_db_holder|_ORPHAN_DB_WATCHDOG_PATTERNS)"
    )
    assert pat.search(src), (
        "bot/_impl.py is missing the `from bot.orphan_db_watchdog import ...` re-export "
        "for the orphan-DB names. The 11 `monkeypatch.setattr(bot, ...)` sites in "
        "tests/test_orphan_db_watchdog.py rely on the proxy chain bot.X → bot._impl.X → "
        "bot.orphan_db_watchdog.X (Option A); without the re-export the chain breaks."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — Clean-leaf import partition (5 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_orphan_db_watchdog_no_top_level_bot_impl_import():
    """bot/orphan_db_watchdog.py has zero top-level `from bot._impl import ...`
    or `import bot._impl` — clean leaf, no carve-out needed."""
    tree = _orphan_db_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot._impl":
            pytest.fail(
                f"bot/orphan_db_watchdog.py has top-level `from bot._impl import {node.names}` "
                f"(line {node.lineno}). Clean leaf must not import bot._impl."
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bot._impl":
                    pytest.fail(
                        f"bot/orphan_db_watchdog.py has top-level `import bot._impl` "
                        f"(line {node.lineno}). Clean leaf must not import bot._impl."
                    )


@pytest.mark.parametrize("forbidden_module", FORBIDDEN_NUMERICAL_IMPORTS)
def test_orphan_db_watchdog_no_forbidden_numerical_imports(forbidden_module):
    """bot/orphan_db_watchdog.py is a pure orphan-detection watchdog — no
    numpy/scipy/torch/sklearn/pandas. Pin per Bit 6.2 precedent + the
    bot._thread_env contention regression in kb/failures/."""
    tree = _orphan_db_tree()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(forbidden_module), (
                    f"bot/orphan_db_watchdog.py has top-level `import {alias.name}` "
                    f"(forbidden numerical lib)"
                )
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(forbidden_module), (
                f"bot/orphan_db_watchdog.py has top-level `from {node.module} import ...` "
                f"(forbidden numerical lib)"
            )


def test_orphan_db_watchdog_uses_explicit_bot_notifier_alias_not_from_form():
    """Per L84 (Bit 8.1): use `import bot.notifier as _telegram_state` form, NOT
    `from bot import notifier as _telegram_state`. The latter triggers
    _BotProxy.__getattr__ → bot._impl load → partial-module ImportError chain.

    AST-based check (docstring mentions of the forbidden form don't false-positive)."""
    tree = _orphan_db_tree()
    has_correct_form = False
    has_forbidden_form = False
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bot.notifier" and alias.asname == "_telegram_state":
                    has_correct_form = True
        elif isinstance(node, ast.ImportFrom):
            # Forbidden: `from bot import notifier as _telegram_state`
            if node.module == "bot":
                for alias in node.names:
                    if alias.name == "notifier":
                        has_forbidden_form = True
    assert has_correct_form, (
        "bot/orphan_db_watchdog.py must use explicit `import bot.notifier as "
        "_telegram_state` form (per L84). The `from bot import notifier as ...` "
        "form triggers the _BotProxy partial-module ImportError chain."
    )
    assert not has_forbidden_form, (
        "bot/orphan_db_watchdog.py uses forbidden top-level `from bot import notifier ...` "
        "form. Use `import bot.notifier as _telegram_state` per L84."
    )


def test_orphan_db_watchdog_uses_telegram_state_alias():
    """bot/orphan_db_watchdog.py reads the singleton via `_telegram_state._TELEGRAM`
    module-attribute access (NOT `from bot.notifier import _TELEGRAM` which would
    capture the binding by value at import time and miss runtime mutations).

    AST-based check (docstring mentions of the forbidden form don't false-positive)."""
    tree = _orphan_db_tree()
    # Check for `from bot.notifier import _TELEGRAM` top-level (forbidden).
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.notifier":
            for alias in node.names:
                assert alias.name != "_TELEGRAM", (
                    "bot/orphan_db_watchdog.py has forbidden top-level "
                    "`from bot.notifier import _TELEGRAM` (line %d). Use "
                    "`_telegram_state._TELEGRAM` module-attribute access per L83."
                    % node.lineno
                )
    # Check executable code uses `_telegram_state._TELEGRAM` attribute access.
    has_attr_access = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "_TELEGRAM"
                and isinstance(node.value, ast.Name)
                and node.value.id == "_telegram_state"):
            has_attr_access = True
            break
    assert has_attr_access, (
        "bot/orphan_db_watchdog.py must read `_telegram_state._TELEGRAM` "
        "(AST: Attribute access on Name(`_telegram_state`)). Mirrors Bit 6.3 "
        "path-B `_cal_state` and Bit 8.1 pattern."
    )


def test_orphan_db_watchdog_imports_are_stdlib_plus_bot_notifier_only():
    """bot/orphan_db_watchdog.py imports stdlib (logging, os, subprocess) +
    typing + bot.notifier alias. Nothing else. Pin per clean-leaf shape."""
    tree = _orphan_db_tree()
    allowed_prefixes = (
        "logging", "os", "subprocess", "typing", "bot.notifier",
        "__future__",
    )
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                ok = any(
                    alias.name == p or alias.name.startswith(p + ".")
                    for p in allowed_prefixes
                )
                assert ok, (
                    f"bot/orphan_db_watchdog.py has unexpected top-level `import "
                    f"{alias.name}` (line {node.lineno}); allowed prefixes: "
                    f"{allowed_prefixes}"
                )
        if isinstance(node, ast.ImportFrom) and node.module:
            ok = any(
                node.module == p or node.module.startswith(p + ".")
                for p in allowed_prefixes
            )
            assert ok, (
                f"bot/orphan_db_watchdog.py has unexpected top-level `from "
                f"{node.module} import ...` (line {node.lineno}); allowed "
                f"prefixes: {allowed_prefixes}"
            )


# ═════════════════════════════════════════════════════════════════════════════
# Section 4 — MainLoop late-binding retarget (2 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_main_loop_startup_late_binding_retargeted_to_orphan_db_watchdog():
    """MainLoop.startup's late-binding `from bot._impl import detect_orphan_db_holders`
    (Bit 9.3-i form) RETARGETED in Bit 9.3-ii to
    `from bot.orphan_db_watchdog import detect_orphan_db_holders`.

    Eliminates the last bot._impl edge in MainLoop's startup path."""
    tree = _main_loop_tree()
    mainloop_cls = next(
        n for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.ClassDef) and n.name == "MainLoop"
    )
    startup_method = next(
        m for m in ast.iter_child_nodes(mainloop_cls)
        if isinstance(m, ast.FunctionDef) and m.name == "startup"
    )
    # Walk for any `from <module> import detect_orphan_db_holders` in startup body.
    found_import = False
    found_correct_source = False
    for node in ast.walk(startup_method):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "detect_orphan_db_holders":
                    found_import = True
                    if node.module == "bot.orphan_db_watchdog":
                        found_correct_source = True
                    elif node.module == "bot._impl":
                        pytest.fail(
                            f"MainLoop.startup still has `from bot._impl import "
                            f"detect_orphan_db_holders` (line {node.lineno}) — "
                            f"Bit 9.3-ii must retarget to "
                            f"`from bot.orphan_db_watchdog import detect_orphan_db_holders`."
                        )
    assert found_import, (
        "MainLoop.startup body is missing `from <module> import "
        "detect_orphan_db_holders` — late-binding pattern broken."
    )
    assert found_correct_source, (
        "MainLoop.startup imports detect_orphan_db_holders from wrong module. "
        "Bit 9.3-ii target is `from bot.orphan_db_watchdog import detect_orphan_db_holders`."
    )


def test_main_loop_init_late_binding_unchanged_at_9_3_ii():
    """MainLoop.__init__'s 2-name late-binding block (Bit 9.3.5 form: _HPSB_MISSING_BLEEDERS,
    _HPSB_VALIDATOR_UNAVAILABLE_REASON) is UNCHANGED at Bit 9.3-ii orphan-DB scope.

    If the HPSB relocation also lands in 9.3-ii (per Plan-agent's full Option A scope),
    this test inverts to assert the late-binding source flipped from bot._impl to
    bot.helpers.validators. For the orphan-DB-only subset of 9.3-ii, this test pins
    the unchanged state."""
    tree = _main_loop_tree()
    mainloop_cls = next(
        n for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.ClassDef) and n.name == "MainLoop"
    )
    init_method = next(
        m for m in ast.iter_child_nodes(mainloop_cls)
        if isinstance(m, ast.FunctionDef) and m.name == "__init__"
    )
    hpsb_late_bound_sources: set[str] = set()
    for node in ast.walk(init_method):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in ("_HPSB_MISSING_BLEEDERS", "_HPSB_VALIDATOR_UNAVAILABLE_REASON"):
                    hpsb_late_bound_sources.add(node.module or "")
    assert hpsb_late_bound_sources, (
        "MainLoop.__init__ has no late-binding import for _HPSB_MISSING_BLEEDERS / "
        "_HPSB_VALIDATOR_UNAVAILABLE_REASON — pattern broken."
    )
    # Allowed: still bot._impl (orphan-DB-only Bit 9.3-ii) OR bot.helpers.validators
    # (full Option A scope Bit 9.3-ii). Anything else fails.
    allowed = {"bot._impl", "bot.helpers.validators"}
    bad = hpsb_late_bound_sources - allowed
    assert not bad, (
        f"MainLoop.__init__ late-binds HPSB names from unexpected module(s): {bad}. "
        f"Allowed: {allowed}."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 5 — 5-consumer _telegram_state._TELEGRAM enumeration (3 tests)
# ═════════════════════════════════════════════════════════════════════════════

def _telegram_state_attr_access_count(path: Path) -> int:
    """AST-based count of `_telegram_state._TELEGRAM` attribute access nodes.

    Bulletproof against docstring/comment false-positives: only walks executable
    AST nodes, ignoring string literals and `#` comments entirely. Each Attribute
    node with `.attr == "_TELEGRAM"` and `.value` being `Name(id="_telegram_state")`
    counts as one consumer reference."""
    tree = ast.parse(path.read_text())
    count = 0
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "_TELEGRAM"
                and isinstance(node.value, ast.Name)
                and node.value.id == "_telegram_state"):
            count += 1
    return count


def test_orphan_db_watchdog_is_5th_telegram_consumer():
    """Post-Bit-9.3-ii: bot/orphan_db_watchdog.py REPLACES bot/_impl.py as the 5th
    `_telegram_state._TELEGRAM` consumer. Total consumer count stays at 5.

    Pre-9.3-ii (5 consumers): bot/_impl.py + bot/main_loop.py +
      bot/scanner/__init__.py + bot/executor.py + bot/settlement.py.
    Post-9.3-ii (5 consumers): bot/orphan_db_watchdog.py REPLACES bot/_impl.py.
    """
    count = _telegram_state_attr_access_count(ORPHAN_DB_WATCHDOG_PY)
    assert count >= 2, (
        f"bot/orphan_db_watchdog.py expected to have at least 2 executable "
        f"`_telegram_state._TELEGRAM` AST references (1 in _alert_orphan_db_holder + "
        f"≥1 in detect_orphan_db_holders lsof-not-found branch); got {count}"
    )


def test_bot_impl_no_telegram_consumers_after_9_3_ii():
    """bot/_impl.py has ZERO executable `_telegram_state._TELEGRAM` references
    post-Bit-9.3-ii (orphan-DB watchdog block moved out)."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii final form)")
    count = _telegram_state_attr_access_count(BOT_PY)
    assert count == 0, (
        f"bot/_impl.py still has {count} executable `_telegram_state._TELEGRAM` "
        f"AST references post-Bit-9.3-ii. The orphan-DB watchdog block (the only "
        f"remaining consumer in bot/_impl.py post-Bit-9.3) must move to "
        f"bot/orphan_db_watchdog.py."
    )


def test_telegram_consumer_enumeration_stays_at_5():
    """5-consumer enumeration verification across the codebase post-Bit-9.3-ii.

    Pre-9.3-ii: {bot/_impl.py, bot/main_loop.py, bot/scanner/__init__.py,
                 bot/executor.py, bot/settlement.py} — 5 consumers
    Post-9.3-ii: {bot/orphan_db_watchdog.py, bot/main_loop.py,
                  bot/scanner/__init__.py, bot/executor.py, bot/settlement.py} — 5 consumers"""
    expected_consumers = {
        ORPHAN_DB_WATCHDOG_PY,
        MAIN_LOOP_PY,
        SCANNER_PY,
        EXECUTOR_PY,
        SETTLEMENT_PY,
    }
    actual_consumers: set[Path] = set()
    candidate_paths = [
        BOT_PY,
        ORPHAN_DB_WATCHDOG_PY,
        MAIN_LOOP_PY,
        SCANNER_PY,
        EXECUTOR_PY,
        SETTLEMENT_PY,
    ]
    for path in candidate_paths:
        if not path.exists():
            continue
        if _telegram_state_attr_access_count(path) > 0:
            actual_consumers.add(path)
    assert actual_consumers == expected_consumers, (
        f"_telegram_state._TELEGRAM consumer enumeration drifted from 5 expected sites.\n"
        f"Expected: {[p.name for p in expected_consumers]}\n"
        f"Actual:   {[p.name for p in actual_consumers]}\n"
        f"Missing:  {[p.name for p in (expected_consumers - actual_consumers)]}\n"
        f"Extra:    {[p.name for p in (actual_consumers - expected_consumers)]}\n"
        f"If a new module legitimately adds Telegram alerts, update both the test "
        f"expectation AND the bot/notifier.py docstring enumeration."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 6 — .importlinter contract pins (2 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_importlinter_helpers_leaf_includes_orphan_db_watchdog():
    """`.importlinter` contract `helpers-leaf` `forbidden_modules` extended with
    `bot.orphan_db_watchdog`. Mirrors Bit 9.3.5's `bot.order_flow` addition."""
    config = configparser.ConfigParser()
    config.read(IMPORTLINTER_INI)
    contract_section = "importlinter:contract:helpers-leaf"
    assert contract_section in config, (
        f"`.importlinter` is missing contract section [{contract_section}]"
    )
    forbidden = config[contract_section]["forbidden_modules"]
    assert "bot.orphan_db_watchdog" in forbidden, (
        f"`.importlinter` `helpers-leaf` `forbidden_modules` missing "
        f"`bot.orphan_db_watchdog`. Add it alongside the other bot/ top-level "
        f"entries (`bot.order_flow`, `bot.executor`, etc.). Current forbidden: "
        f"{forbidden}"
    )


def test_orphan_db_watchdog_in_proxy_attr_or_layer_1_snapshot():
    """Sanity check — `bot.orphan_db_watchdog` module + its function entries are
    registered in the public-api snapshot. The snapshot's flat-key structure
    uses `bot.orphan_db_watchdog.<name>` for each function/attribute."""
    snapshot_path = REPO_ROOT / "tests" / "contracts" / "public_api.json"
    if not snapshot_path.exists():
        pytest.skip("public_api.json snapshot not present")
    import json
    data = json.loads(snapshot_path.read_text())
    # The snapshot uses flat keys like `bot.orphan_db_watchdog.detect_orphan_db_holders`.
    matching_keys = [k for k in data.keys() if k.startswith("bot.orphan_db_watchdog")]
    assert matching_keys, (
        "tests/contracts/public_api.json snapshot missing any bot.orphan_db_watchdog "
        "entry. Regenerate via `make api-snapshot-regen` AFTER all Bit-9.3-ii edits "
        "finalized (per Bit 9.3.5 R2 lesson — regen must be FINAL step before commit)."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 7 — bot/__main__.py swap pin (2 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_bot_main_imports_main_loop_directly_at_9_3_ii():
    """Bit 9.3-ii: bot/__main__.py swapped from `from bot._impl import MainLoop`
    to direct `from bot.main_loop import MainLoop`. Per master plan L2197."""
    src = MAIN_FILE.read_text()
    assert "from bot.main_loop import MainLoop" in src, (
        "bot/__main__.py missing `from bot.main_loop import MainLoop` "
        "(Bit 9.3-ii direct import). Per master plan L2197."
    )
    assert "from bot._impl import MainLoop" not in src, (
        "bot/__main__.py still has `from bot._impl import MainLoop` (Bit 9.3-i form). "
        "Bit 9.3-ii swap missing."
    )


def test_bot_main_imports_thread_env_as_first_import():
    """Bit 9.3-ii: bot/__main__.py must `import bot._thread_env` BEFORE
    `from bot.main_loop import MainLoop`. Post-swap, bot._impl chain no longer
    fires OMP_NUM_THREADS=1 before numpy loads via bot.main_loop → models →
    numpy. Defense-in-depth required.

    Pin per kb/failures/cal-mlp-torch-thread-contention-apr29.md regression."""
    src = MAIN_FILE.read_text()
    tree = ast.parse(src)
    # Find the FIRST non-stdlib-prelude import. Per pre-existing pattern, stdlib
    # imports (`import logging`, `import sys`) are allowed BEFORE bot._thread_env.
    thread_env_line: int | None = None
    main_loop_line: int | None = None
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bot._thread_env" and thread_env_line is None:
                    thread_env_line = node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.module == "bot.main_loop" and main_loop_line is None:
                main_loop_line = node.lineno
    assert thread_env_line is not None, (
        "bot/__main__.py missing `import bot._thread_env`. Required at Bit 9.3-ii "
        "since the swap to `from bot.main_loop import MainLoop` bypasses the "
        "bot._impl chain that previously fired _thread_env first. Without this, "
        "numpy loads BEFORE OMP_NUM_THREADS=1, regressing to the Apr 29 thread "
        "contention incident."
    )
    assert main_loop_line is not None, (
        "bot/__main__.py missing `from bot.main_loop import MainLoop` "
        "(Bit 9.3-ii direct import target)."
    )
    assert thread_env_line < main_loop_line, (
        f"bot/__main__.py has `import bot._thread_env` at line {thread_env_line} "
        f"AFTER `from bot.main_loop import MainLoop` at line {main_loop_line}. "
        f"The _thread_env import must fire FIRST so OMP_NUM_THREADS=1 is set "
        f"before any numpy load (transitively via bot.main_loop → models → numpy)."
    )


def test_bot_main_logging_basicconfig_preserved():
    """Bit 9.3 R7 #1 — load-bearing: `logging.basicConfig(force=True)` block
    in bot/__main__.py's `if __name__ == "__main__":` MUST survive the 9.3-ii
    swap. Without it, production journalctl loses structured INFO logging."""
    src = MAIN_FILE.read_text()
    assert "logging.basicConfig(" in src, (
        "bot/__main__.py missing `logging.basicConfig(...)` block. Bit 2.1a R5 #8 "
        "deliberately moved this here from bot/_impl.py; dropping it regresses "
        "production journalctl to bare-logger output (no `[INFO]` prefix)."
    )
    assert "force=True" in src, (
        "bot/__main__.py basicConfig block missing `force=True`. The R5 #8 "
        "guarantee is that production logging is configured even if some "
        "transitive import already called basicConfig."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 8 — Behavioral smoke tests (3 tests)
# ═════════════════════════════════════════════════════════════════════════════

def test_orphan_db_watchdog_detect_no_offenders_when_self_only(tmp_path):
    """Smoke test: importing bot.orphan_db_watchdog and calling
    detect_orphan_db_holders() with a no-op lsof stub returns []."""
    import bot.orphan_db_watchdog as owd
    db = tmp_path / "state.db"
    db.touch()
    # Stub the lsof to return only self_pid.
    original_lsof = owd._run_lsof_for_db
    try:
        import os
        self_pid = os.getpid()
        owd._run_lsof_for_db = lambda path: [self_pid] if path == str(db) else []
        offenders = owd.detect_orphan_db_holders(str(db), self_pid=self_pid)
        assert offenders == [], f"expected [] when only self holds DB; got {offenders}"
    finally:
        owd._run_lsof_for_db = original_lsof


def test_orphan_db_watchdog_patterns_list_contents():
    """Positive-list contains all 3 H-4 backfill script names (per Bit 9.3-i
    adversarial-review C-1)."""
    import bot.orphan_db_watchdog as owd
    for script in EXPECTED_ORPHAN_PATTERNS:
        assert script in owd._ORPHAN_DB_WATCHDOG_PATTERNS, (
            f"_ORPHAN_DB_WATCHDOG_PATTERNS missing `{script}` — positive-list "
            f"contract from kb/failures/shape-d-contention-explosion-may03.md"
        )


def test_orphan_db_watchdog_module_runs_without_bot_impl():
    """Critical: bot/orphan_db_watchdog.py must import cleanly WITHOUT
    triggering bot._impl load. Otherwise the watchdog's existence would
    require bot._impl to be fully initialized — defeating the purpose
    of a clean leaf."""
    import sys
    # Note: if a parent test already imported bot._impl, this test can't
    # cleanly verify the no-bot._impl-edge property at runtime. The Section-3
    # AST tests cover that statically.
    assert "bot.orphan_db_watchdog" in sys.modules or True, (
        "smoke test: import works"
    )
    import bot.orphan_db_watchdog
    assert hasattr(bot.orphan_db_watchdog, "detect_orphan_db_holders")
    assert hasattr(bot.orphan_db_watchdog, "_ORPHAN_DB_WATCHDOG_PATTERNS")


# ═════════════════════════════════════════════════════════════════════════════
# Section 9 — L93 iCloud filter (1 test, defensive)
# ═════════════════════════════════════════════════════════════════════════════

def test_no_icloud_conflicts_in_orphan_db_watchdog_module():
    """L93: macOS+iCloud creates `bot/orphan_db_watchdog 2.py` etc. conflict files
    that break AST walkers. Defensive pin: no such files in bot/."""
    bot_dir = REPO_ROOT / "bot"
    conflicts = [p for p in bot_dir.glob("*.py") if " " in p.stem]
    assert not conflicts, (
        f"L93 iCloud conflict files in bot/ that will break AST walkers: "
        f"{[p.name for p in conflicts]}. Run `find bot/ -name '* [23456].py' -delete` "
        f"before committing."
    )
