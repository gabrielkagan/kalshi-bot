"""Data-Integrity E.1 R1-M5 sister-doc lockstep: `scripts/ops/monitor_watchdog.py`
must be runnable from cron's `cd ~/kalshi-bot-repo && source venv/bin/activate
&& python3 scripts/ops/monitor_watchdog.py` invocation flow without hitting
`ModuleNotFoundError: No module named 'bot'`.

Mirrors the apparatus from `test_collector_health_monitor_runnable.py`
(ticket 86ba0jvka, 2026-05-19). Same `feedback_monitor_the_monitor` class:
cron's activation flow does NOT put repo root on sys.path; `bot/` is not
pip-installed in production. Without an explicit bootstrap, the script
crashes at `from bot.notifier import TelegramNotifier` immediately on
every cron tick.

This file enforces 3 STRUCTURAL invariants via AST on
`scripts/ops/monitor_watchdog.py`:

  1. The bootstrap (`sys.path.insert(0, ...)` or `.append(...)`) exists.
  2. It precedes every `from bot.*` / `import bot.*` statement (including
     lazy imports inside function bodies).
  3. Its argument resolves to `Path(__file__).resolve().parents[2]` —
     the repo root, NOT some other ancestor that would silently fail.

A future regression that drops/moves the bootstrap or changes parents[]
depth will RED-fail one of these pins.
"""
from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ops" / "monitor_watchdog.py"


def _parse() -> ast.Module:
    assert _SCRIPT.is_file(), f"monitor_watchdog.py missing at {_SCRIPT}"
    return ast.parse(_SCRIPT.read_text())


def _is_sys_path_mutation(node: ast.AST) -> bool:
    """Return True iff node is `sys.path.insert(...)` or `sys.path.append(...)`."""
    if not isinstance(node, ast.Expr):
        return False
    call = node.value
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr not in ("insert", "append"):
        return False
    inner = func.value
    if not isinstance(inner, ast.Attribute) or inner.attr != "path":
        return False
    base = inner.value
    return isinstance(base, ast.Name) and base.id == "sys"


def _find_first_bot_import_lineno(tree: ast.Module) -> int | None:
    """Find the lineno of the first `from bot.*` / `import bot.*` statement
    anywhere in the module (including inside function bodies — lazy imports
    still need the bootstrap to have fired first)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "bot" or node.module.startswith("bot.")):
                return node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bot" or alias.name.startswith("bot."):
                    return node.lineno
    return None


def test_monitor_watchdog_has_sys_path_bootstrap():
    """Invariant 1: the module body must contain at least one
    `sys.path.insert(...)` or `sys.path.append(...)` statement at top level.
    """
    tree = _parse()
    bootstraps = [
        node for node in tree.body
        if isinstance(node, ast.If)
        for child in node.body
        if _is_sys_path_mutation(child)
    ] + [
        node for node in tree.body
        if _is_sys_path_mutation(node)
    ]
    assert bootstraps, (
        "monitor_watchdog.py must contain a `sys.path.insert/append(...)` "
        "call at module top level. Without it, cron's invocation flow "
        "(`cd ~/kalshi-bot-repo && source venv/bin/activate && python3 "
        "scripts/ops/monitor_watchdog.py`) crashes at `from bot.notifier "
        "import TelegramNotifier` with ModuleNotFoundError. Pattern: "
        "see scripts/ops/collector_health_monitor.py."
    )


def test_monitor_watchdog_bootstrap_precedes_bot_imports():
    """Invariant 2: every `from bot.*` / `import bot.*` statement must
    appear AFTER the sys.path bootstrap (otherwise the bootstrap is
    ineffective for those imports)."""
    tree = _parse()
    first_bot_lineno = _find_first_bot_import_lineno(tree)
    assert first_bot_lineno is not None, (
        "monitor_watchdog.py is expected to import from bot.* (at least "
        "`bot.notifier.TelegramNotifier`). If it stopped, this test can "
        "be dropped."
    )

    # Find the LAST sys.path mutation lineno (top-level or inside top-level If).
    last_bootstrap_lineno = -1
    for node in tree.body:
        if _is_sys_path_mutation(node):
            last_bootstrap_lineno = max(last_bootstrap_lineno, node.lineno)
        elif isinstance(node, ast.If):
            for child in node.body:
                if _is_sys_path_mutation(child):
                    last_bootstrap_lineno = max(last_bootstrap_lineno, child.lineno)

    assert last_bootstrap_lineno > 0, "no top-level sys.path mutation found"
    assert last_bootstrap_lineno < first_bot_lineno, (
        f"sys.path bootstrap at line {last_bootstrap_lineno} must precede "
        f"the first `from bot.*` import at line {first_bot_lineno}. A "
        f"regression that swaps the order silently re-opens the cron "
        f"ModuleNotFoundError class."
    )


def test_monitor_watchdog_bootstrap_uses_parents_2_depth():
    """Invariant 3: the bootstrap path must derive from
    `Path(__file__).resolve().parents[2]` (or equivalent) — the repo root.

    `parents[2]` is correct for `scripts/ops/monitor_watchdog.py`:
        parents[0] = scripts/ops/
        parents[1] = scripts/
        parents[2] = repo root
    A future relocation that changes the script's depth would invalidate
    `parents[2]` and must update this pin (and the script body) lockstep.
    """
    src = _SCRIPT.read_text()
    # Pin the literal token. The collector sister uses `parents[2]` too;
    # a depth change in either is a deliberate move that requires this
    # test to update — that's the gate.
    assert "parents[2]" in src, (
        "monitor_watchdog.py must compute the repo root via "
        "`Path(__file__).resolve().parents[2]`. This pin catches refactors "
        "that move the script to a different depth without updating the "
        "bootstrap arithmetic (silent ModuleNotFoundError class)."
    )
