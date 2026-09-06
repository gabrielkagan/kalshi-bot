"""Ticket 86ba0jvka — `scripts/ops/collector_health_monitor.py` must be
runnable from cron's `cd ~/kalshi-bot-repo && . venv/bin/activate &&
python3 scripts/ops/collector_health_monitor.py` invocation flow without
hitting `ModuleNotFoundError: No module named 'bot'`.

ROOT CAUSE (incident 2026-05-19 disk-full):
The script crashes at `from bot.notifier import TelegramNotifier`
(function-scoped lazy import inside `main()`) because the cron
activation flow does NOT put the repo root on `sys.path` — `bot/` is
not pip-installed. The canary has been dead since D1.6 SHIPPED PR #51
on 2026-05-17, never firing a single alert.

This test pins the STRUCTURAL fix: the script must contain a sys.path
bootstrap NEAR THE TOP of the module body (BEFORE any `from bot.*`
import) that adds the repo root to `sys.path`. Without this bootstrap,
the import chain breaks the moment cron invokes the script.

Why structural-only (no behavioral subprocess test):
A behavioral subprocess test that strips the repo root from sys.path
and invokes the script CANNOT escape the editable-install
MetaPathFinder on any dev machine where `pip install -e .` has run.
The `__editable__.kalshi_bot-0.1.0.pth` file installs a MetaPathFinder
that resolves `bot.*` to the repo path regardless of sys.path
contents — short-circuiting the very mechanism this test is supposed
to certify. R3 + R4 of this Bit's adv-review cycle iterated on
environment-isolation approaches; all failed to bypass the editable
install cleanly without also breaking transitive-dep discovery
(`requests` inside `bot.notifier`). The cron-invocation behavior IS
verified on the VPS post-deploy where no editable install exists —
operators tail `~/collector_health.log` for the first cron tick.

This file therefore enforces 3 STRUCTURAL invariants via AST:
  1. The bootstrap (`sys.path.insert(0, ...)` or `.append(...)`) exists
  2. It precedes every `from bot.*` / `import bot.*` statement
  3. Its argument resolves to `parents[2]` (the repo root, NOT some
     other ancestor that would silently fail on the VPS)

A future regression that drops the bootstrap, moves it below the bot
imports, or changes the parents[] depth will all RED-fail one of
these pins.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import List, Optional


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ops" / "collector_health_monitor.py"


def test_collector_health_monitor_has_sys_path_bootstrap():
    """AST contract pin (R4-C1 ratchet): the source must contain a
    `sys.path.insert/append(...)` call near the top of the module that
    adds the repo root via `Path(__file__).resolve().parents[2]`.
    Position-checked separately by the sister test below.

    Acceptable forms:
      sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
      sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
      sys.path.append(str(Path(__file__).resolve().parents[2]))
    """
    source = _SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)

    found_sys_path_mutation = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("insert", "append")
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
        ):
            found_sys_path_mutation = True
            break

    assert found_sys_path_mutation, (
        f"{_SCRIPT.relative_to(_REPO_ROOT)} must contain a "
        f"`sys.path.insert(...)` or `sys.path.append(...)` call near "
        f"the top of the module to bootstrap the repo root before any "
        f"`from bot.*` import. Without this, the cron invocation flow "
        f"crashes with `ModuleNotFoundError: No module named 'bot'`. "
        f"This canary has been dead since D1.6 SHIPPED 2026-05-17; "
        f"the structural fix is ticket 86ba0jvka 2026-05-19."
    )


def test_collector_health_monitor_sys_path_bootstrap_precedes_bot_imports():
    """AST contract pin: the `sys.path.insert/append(...)` call's line
    number must be STRICTLY LESS THAN every `from bot.*` / `import
    bot.*` statement in the file.

    Position matters: if someone moves the bootstrap below the bot.*
    imports in a future refactor, the script crashes on cron despite
    the bootstrap existing. This pin catches that drift class.
    """
    source = _SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Find the earliest sys.path.insert/append call.
    sys_path_mutation_lineno: Optional[int] = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("insert", "append")
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
        ):
            if sys_path_mutation_lineno is None or node.lineno < sys_path_mutation_lineno:
                sys_path_mutation_lineno = node.lineno

    assert sys_path_mutation_lineno is not None, (
        "Sister test test_collector_health_monitor_has_sys_path_bootstrap "
        "should have caught this — sys.path mutation missing entirely."
    )

    # Find ALL bot.* import statements (top-level OR function-scoped).
    bot_import_linenos: List[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "bot" or node.module.startswith("bot.")):
                bot_import_linenos.append(node.lineno)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "bot" or alias.name.startswith("bot."):
                    bot_import_linenos.append(node.lineno)

    assert bot_import_linenos, (
        f"{_SCRIPT.relative_to(_REPO_ROOT)} has no `from bot.*` or "
        f"`import bot.*` statements; the sys.path bootstrap is no "
        f"longer necessary. Re-evaluate whether this test still "
        f"applies — the bootstrap may be safe to remove."
    )

    earliest_bot_import = min(bot_import_linenos)
    assert sys_path_mutation_lineno < earliest_bot_import, (
        f"sys.path mutation at line {sys_path_mutation_lineno} must "
        f"precede the earliest `from bot.*` import at line "
        f"{earliest_bot_import}. As written, the bot import would "
        f"fire BEFORE sys.path is patched (function-scoped imports "
        f"still fire at call time; module-top imports fire at load "
        f"time — either way, sys.path must be mutated first)."
    )


def test_collector_health_monitor_sys_path_bootstrap_uses_parents_2():
    """AST contract pin (R4-C1 ratchet): the bootstrap's argument must
    derive from `Path(__file__).resolve().parents[2]` (or the
    equivalent `.parent.parent.parent` chain) so the path lands on the
    REPO ROOT.

    `parents[0]` would be `scripts/ops/` — bot still unfindable.
    `parents[1]` would be `scripts/` — bot still unfindable.
    `parents[2]` is the repo root — bot becomes importable.
    `parents[3]+` would be the parent directory of the repo — bot
        accidentally importable iff the repo is laid out a certain
        way on the VPS (a fragile coincidence). Don't rely on it.

    A future regression that changes the depth (e.g., a copy-paste
    from a similar script with a different layout) would silently
    fail on the VPS. This pin catches that.
    """
    source = _SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)

    correct_depth_found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Look for sys.path.insert/append(*, X) calls.
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr in ("insert", "append")
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "path"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "sys"
        ):
            continue
        # The PATH argument is the LAST positional arg
        # (insert(idx, path) or append(path)).
        if not node.args:
            continue
        path_arg = node.args[-1]
        # Walk the path-arg AST looking for `.parents[2]` subscript OR
        # a `.parent.parent.parent` chain.
        if _expression_uses_parents_2(path_arg):
            correct_depth_found = True
            break

    assert correct_depth_found, (
        f"{_SCRIPT.relative_to(_REPO_ROOT)} must derive its sys.path "
        f"bootstrap argument from `Path(__file__).resolve().parents[2]` "
        f"(or equivalent `.parent.parent.parent` chain). Other depths "
        f"would silently fail on the VPS — parents[0] = scripts/ops/, "
        f"parents[1] = scripts/, parents[2] = repo root."
    )


def _expression_uses_parents_2(node: ast.AST) -> bool:
    """Return True if the AST expression contains EXACTLY a
    `.parents[2]` subscript OR a chain of EXACTLY 3 consecutive
    `.parent` Attribute accesses (depth-3 = repo root).

    Critical: must NOT match depth-4+ `.parent` chains (those would
    point to an ancestor-of-repo and silently fail on the VPS). The
    naive ast.walk loop counted from each .parent node, which falsely
    matches depth-3 SUBCHAINS of longer chains. This implementation
    finds chain TOPS (Attribute nodes whose .value is NOT another
    .parent Attribute) and counts the chain depth from THERE.
    """
    # Pattern 1: ast.Subscript with attr 'parents' and constant slice == 2
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Subscript)
            and isinstance(child.value, ast.Attribute)
            and child.value.attr == "parents"
        ):
            # The slice can be ast.Constant(2) (py3.9+) or
            # ast.Index(ast.Num(2)) (older).
            slc = child.slice
            if isinstance(slc, ast.Constant) and slc.value == 2:
                return True
            if isinstance(slc, ast.Num) and slc.n == 2:  # type: ignore[attr-defined]
                return True

    # Pattern 2: chain of EXACTLY 3 consecutive '.parent' Attribute
    # accesses. Find chain TOPS by collecting all `.parent` Attribute
    # nodes that are NOT themselves the .value of another `.parent`
    # Attribute (those are inner subchain nodes — would otherwise
    # double-count depth-N as containing depth-3 sub-chains).
    inner_parent_nodes = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and child.attr == "parent"
            and isinstance(child.value, ast.Attribute)
            and child.value.attr == "parent"
        ):
            inner_parent_nodes.add(id(child.value))

    for child in ast.walk(node):
        if not isinstance(child, ast.Attribute) or child.attr != "parent":
            continue
        if id(child) in inner_parent_nodes:
            continue  # this is an inner node of a longer chain — skip
        # `child` is the TOP of a chain. Count its depth.
        depth = 1
        inner = child.value
        while isinstance(inner, ast.Attribute) and inner.attr == "parent":
            depth += 1
            inner = inner.value
        # depth == 3 = `.parent.parent.parent` = repo root (correct).
        # depth == 1 = `.parent` (parents[0]) — wrong.
        # depth == 2 = `.parent.parent` (parents[1]) — wrong.
        # depth >= 4 = ancestor-of-repo — silent fail on VPS.
        if depth == 3:
            return True
    return False
