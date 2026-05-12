"""Bit 9.3-iii.c follow-up — kill-switch runtime regression test gap (ClickUp 86b9wdamc).

Bit 9.3-iii.c bundled a latent-bug fix for the 3 kill-switch flags
(WEATHER_NO_SIDE_LIVE, HOURLY_NO_SIDE_LIVE, BRACKET_NO_ENABLED). Pre-fix
the gating READS were bare-name lookups against scanner/executor
module-level bindings (captured-by-value from `from bot.constants import X`).
The auto-kill WRITE block fired `bot._impl.X = False`, but the next-tick
gate read returned the captured-by-value `True` until operator restart —
the kill-switch was a dead signal.

Post-fix: the gates use `bot.constants.X` module-attribute access
(parallel to `_telegram_state._TELEGRAM` and `_cal_state._CALIBRATION_ENGINE`
patterns). Mutation now propagates.

The existing pins in tests/test_bit_9_3_iii_c_impl_deletion.py lock the
TOPOLOGY via AST (writes target `bot.constants.X`; reads use
module-attribute access; the 3 flags are NOT explicit-name-imported).
This file complements those pins with **end-to-end runtime-freshness
tests** that exercise the actual gate-read expressions extracted from
the source. A future "simplification" regression that re-introduces
bare-name bindings (e.g., `WEATHER_NO_SIDE_LIVE = True` at scanner
module level, masking `bot.constants.WEATHER_NO_SIDE_LIVE`) would pass
the AST pins (which only look at the kill-switch block reads/writes)
but FAIL these tests because they exec the actual gate-read line under
a hostile namespace where the bare name is set to the opposite of the
canonical `bot.constants` value.

Gate-read sites exercised (file:line citations, post-Bit-9.3-iii.c HEAD):
  - bot/scanner/__init__.py:1250  (WEATHER_NO_SIDE_LIVE — auto-kill guard)  [load-bearing]
  - bot/scanner/__init__.py:1269  (HOURLY_NO_SIDE_LIVE  — auto-kill guard)  [load-bearing]
  - bot/scanner/__init__.py:1288  (BRACKET_NO_ENABLED   — auto-kill guard)  [load-bearing]
  - bot/scanner/__init__.py:5413  (BRACKET_NO_ENABLED   — bracket-NO intercept gate)  [load-bearing]
  - bot/scanner/__init__.py:7445  (WEATHER_NO_SIDE_LIVE — weather-NO live candidate gate)  [dead-weight under sentinel — see NOTE]
  - bot/scanner/__init__.py:7546  (HOURLY_NO_SIDE_LIVE  — hourly-NO live candidate gate)  [load-bearing]
  - bot/executor.py:479           (WEATHER_NO_SIDE_LIVE — observation-bypass gate)  [load-bearing]
  - bot/executor.py:482           (HOURLY_NO_SIDE_LIVE  — observation-bypass gate)  [load-bearing]

NOTE on the dead-weight gate (scanner:7445), per R1 adv-reviewer M1
(86b9wdamc): The weather-NO live candidate gate at scanner:7445
contains the compound clause `... and not should_exclude_weather_no_ticker(ticker)`.
Under the `_AlwaysTrue` sentinel namespace this test uses, the callable
returns sentinel (truthy by __bool__), so `not sentinel == False` and the
entire AND-chain short-circuits to False regardless of which flag-binding
mode is in effect (post-fix `bot.constants.X` vs hostile bare-name). The
gate therefore does NOT prove the regression class on its own — but the
test SUITE still catches the regression because the simple `if X:` guards
at scanner:1250 (and the executor gates at 479/482) DO flip True under
the hostile bare-name namespace. Effective coverage: 7 of 8 textually-matched
sites are load-bearing for this regression class. The 7445 dead-weight
is acknowledged here rather than reworking `_AlwaysTrue.__bool__` (which
would re-balance compound `not callable()` clauses everywhere and risk
masking other regression classes).

Closeout: kb/decisions/bit-9.3-iii-c-shipped-may11.md
"""
from __future__ import annotations

import ast
from pathlib import Path

import bot.constants
import bot.executor
import bot.scanner


REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER_SRC = (REPO_ROOT / "bot" / "scanner" / "__init__.py").read_text()
EXECUTOR_SRC = (REPO_ROOT / "bot" / "executor.py").read_text()


# Names this file expects to find on disk (meta-pin uses this list).
EXPECTED_TEST_NAMES = (
    "test_weather_no_side_live_runtime_freshness",
    "test_hourly_no_side_live_runtime_freshness",
    "test_bracket_no_enabled_runtime_freshness",
    "test_meta_expected_test_names_present_in_file",
)


def _extract_gate_expression(src: str, flag_name: str) -> list[ast.expr]:
    """AST-find every gate-bearing expression that textually references `flag_name`.

    Returns expressions from:
      - `if <expr>:` test conditions (scanner gates 1250/1269/1288/5413/7445/7546)
      - `name = <expr>` assignment values (executor gates 479/482, which assign
        a BoolOp to `_is_weather_no_live` / `_is_hourly_no_live`)

    Filters out:
      - The kill-switch WRITE target (`bot.constants.X = False`) — the LHS is
        an `Attribute`, the gate expr we want is the surrounding `If.test`.
        The Assign.targets/Assign.value distinction handles this: when we
        walk Assign, we only look at .value (the RHS).
    """
    tree = ast.parse(src)
    matches: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            try:
                rendered = ast.unparse(node.test)
            except AttributeError:
                continue
            if flag_name in rendered:
                matches.append(node.test)
        elif isinstance(node, ast.Assign):
            # Skip the kill-switch write blocks (those assign False to the flag).
            # Their VALUE is a Constant(False), so flag_name is never on the RHS.
            try:
                rendered = ast.unparse(node.value)
            except AttributeError:
                continue
            if flag_name in rendered:
                matches.append(node.value)
    return matches


def _eval_gate(gate_expr: ast.expr, namespace: dict) -> bool:
    """Compile a gate expression and evaluate it under the supplied namespace.

    Wraps the expression in an `ast.Expression` so `eval` can run it.

    Names referenced by the gate that aren't in the namespace fall back to
    sentinels that make the surrounding conditions tautologically True for
    flag-only short-circuit semantics. The point is to exercise whether the
    *kill-switch flag* portion of the gate observes module-attribute access
    or a bare-name binding — NOT to model the rest of the gate logic.
    """
    expr_node = ast.Expression(body=gate_expr)
    ast.fix_missing_locations(expr_node)
    code = compile(expr_node, "<gate>", "eval")

    class _AlwaysTrue:
        """Permissive sentinel: any comparison / arithmetic / attribute access /
        method call returns self (truthy). Used to fill in non-flag terms of
        the gate so flag-only short-circuit semantics are exercised.

        `__contains__` returns False so `asset not in EXCLUDED_SET` evaluates True.
        """
        def __le__(self, other): return True
        def __ge__(self, other): return True
        def __eq__(self, other): return True
        def __lt__(self, other): return True
        def __gt__(self, other): return True
        def __ne__(self, other): return False
        def __bool__(self): return True
        def __hash__(self): return 0
        def __sub__(self, other): return self
        def __truediv__(self, other): return self
        def __rtruediv__(self, other): return self
        def __rsub__(self, other): return self
        def __mul__(self, other): return self
        def __rmul__(self, other): return self
        def __add__(self, other): return self
        def __radd__(self, other): return self
        def __contains__(self, item): return False  # `asset not in EXCLUDED` → True
        def __iter__(self): return iter([])
        def __call__(self, *a, **kw): return self
        def __getattr__(self, name): return self  # Permissive attribute / method access

    sentinel = _AlwaysTrue()

    class _FallbackDict(dict):
        def __missing__(self, key):
            return sentinel

    # Seed with the supplied namespace, then anything else falls back.
    fallback = _FallbackDict(namespace)
    return bool(eval(code, fallback, fallback))  # noqa: S307 — controlled exec


# ──────────────────────────────────────────────────────────────────────────────
# WEATHER_NO_SIDE_LIVE
# ──────────────────────────────────────────────────────────────────────────────


def test_weather_no_side_live_runtime_freshness():
    """End-to-end runtime-freshness for WEATHER_NO_SIDE_LIVE.

    Exercises the scanner kill-switch auto-kill guard at line 1250
    (`if bot.constants.WEATHER_NO_SIDE_LIVE:`) AND the executor
    observation-bypass gate at line 479
    (`bot.constants.WEATHER_NO_SIDE_LIVE`).

    The test simulates a hostile regression by setting a bare-name binding
    on the scanner+executor modules to True; the gate must still see False
    when bot.constants.WEATHER_NO_SIDE_LIVE is flipped to False.

    A bare-name re-binding regression (e.g., an agent adding
    `WEATHER_NO_SIDE_LIVE = True` at scanner module level) would PASS the
    AST pins in test_bit_9_3_iii_c_impl_deletion.py (those pins guard the
    kill-switch BLOCK reads/writes, not arbitrary module-level bindings)
    but FAIL here because eval against the bare-name namespace returns
    True while bot.constants reports False.
    """
    # ── Setup ────────────────────────────────────────────────────────────
    # SECOND-tick semantics: tick 1 fires the write block (mutates
    # bot.constants.WEATHER_NO_SIDE_LIVE = False). Tick 2 reads the gate.
    # We simulate that by mutating bot.constants directly; in production
    # the write block at scanner:1257 does the same.
    original = bot.constants.WEATHER_NO_SIDE_LIVE
    # Bare-name hostile binding (simulates a regression): scanner+executor
    # module attribute set to TRUE explicitly. The post-fix gate must
    # IGNORE this binding and read bot.constants instead.
    had_scanner_attr = hasattr(bot.scanner, "WEATHER_NO_SIDE_LIVE")
    had_executor_attr = hasattr(bot.executor, "WEATHER_NO_SIDE_LIVE")
    scanner_prev = getattr(bot.scanner, "WEATHER_NO_SIDE_LIVE", None)
    executor_prev = getattr(bot.executor, "WEATHER_NO_SIDE_LIVE", None)
    try:
        # Tick-1 simulation: kill-switch fires, writes bot.constants = False.
        bot.constants.WEATHER_NO_SIDE_LIVE = True  # live state at start of tick 1
        assert bot.constants.WEATHER_NO_SIDE_LIVE is True
        bot.constants.WEATHER_NO_SIDE_LIVE = False  # auto-kill writes False
        # Sanity: write landed.
        assert bot.constants.WEATHER_NO_SIDE_LIVE is False, (
            "Sanity: kill-switch write to bot.constants.WEATHER_NO_SIDE_LIVE "
            "did not land. This indicates a deep Python-module-system issue, "
            "not a Bit-9.3-iii.c regression."
        )

        # Hostile bare-name binding — would mask the fix if reads were bare-name.
        bot.scanner.WEATHER_NO_SIDE_LIVE = True
        bot.executor.WEATHER_NO_SIDE_LIVE = True

        # ── Gate-read exercise (scanner kill-switch auto-kill guard) ─────
        # Find every scanner `if ...WEATHER_NO_SIDE_LIVE...` and evaluate.
        # The post-fix form is `if bot.constants.WEATHER_NO_SIDE_LIVE:` (and
        # similar inside compound boolean expressions at 7445).
        gate_exprs = _extract_gate_expression(SCANNER_SRC, "WEATHER_NO_SIDE_LIVE")
        assert gate_exprs, (
            "Failed to extract any `if ...WEATHER_NO_SIDE_LIVE...` gates from "
            "bot/scanner/__init__.py. Code drift? Re-anchor this test against "
            "the current source layout."
        )

        for gate_expr in gate_exprs:
            # Evaluate the actual source expression under a namespace where
            # bot.constants holds False but bare-name `WEATHER_NO_SIDE_LIVE`
            # is True.
            ns = {"bot": __import__("bot"), "WEATHER_NO_SIDE_LIVE": True}
            result = _eval_gate(gate_expr, ns)
            assert result is False, (
                f"WEATHER_NO_SIDE_LIVE gate at scanner expression "
                f"`{ast.unparse(gate_expr)}` evaluated TRUE while "
                f"bot.constants.WEATHER_NO_SIDE_LIVE=False. This means the "
                f"gate reads a bare-name binding instead of "
                f"bot.constants.WEATHER_NO_SIDE_LIVE — Bit 9.3-iii.c "
                f"regression. Restore module-attribute access."
            )

        # ── Gate-read exercise (executor observation-bypass gate at 479) ─
        exec_gate_exprs = _extract_gate_expression(EXECUTOR_SRC, "WEATHER_NO_SIDE_LIVE")
        assert exec_gate_exprs, (
            "Failed to extract any executor gate referencing WEATHER_NO_SIDE_LIVE. "
            "Re-anchor against current source."
        )
        for gate_expr in exec_gate_exprs:
            ns = {"bot": __import__("bot"), "WEATHER_NO_SIDE_LIVE": True}
            result = _eval_gate(gate_expr, ns)
            assert result is False, (
                f"WEATHER_NO_SIDE_LIVE gate at executor expression "
                f"`{ast.unparse(gate_expr)}` evaluated TRUE while "
                f"bot.constants.WEATHER_NO_SIDE_LIVE=False. Regression — "
                f"executor must use bot.constants.X module-attribute access."
            )
    finally:
        # Restore exactly.
        bot.constants.WEATHER_NO_SIDE_LIVE = original
        if had_scanner_attr:
            bot.scanner.WEATHER_NO_SIDE_LIVE = scanner_prev
        else:
            try:
                delattr(bot.scanner, "WEATHER_NO_SIDE_LIVE")
            except AttributeError:
                pass
        if had_executor_attr:
            bot.executor.WEATHER_NO_SIDE_LIVE = executor_prev
        else:
            try:
                delattr(bot.executor, "WEATHER_NO_SIDE_LIVE")
            except AttributeError:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# HOURLY_NO_SIDE_LIVE
# ──────────────────────────────────────────────────────────────────────────────


def test_hourly_no_side_live_runtime_freshness():
    """End-to-end runtime-freshness for HOURLY_NO_SIDE_LIVE.

    Exercises scanner gate at line 1269 (auto-kill guard) + scanner gate
    at 7546 (hourly-NO live-candidate gate) + executor gate at 482
    (observation-bypass).
    """
    original = bot.constants.HOURLY_NO_SIDE_LIVE
    had_scanner_attr = hasattr(bot.scanner, "HOURLY_NO_SIDE_LIVE")
    had_executor_attr = hasattr(bot.executor, "HOURLY_NO_SIDE_LIVE")
    scanner_prev = getattr(bot.scanner, "HOURLY_NO_SIDE_LIVE", None)
    executor_prev = getattr(bot.executor, "HOURLY_NO_SIDE_LIVE", None)
    try:
        # Tick-1 simulation: write block fires.
        bot.constants.HOURLY_NO_SIDE_LIVE = True
        assert bot.constants.HOURLY_NO_SIDE_LIVE is True
        bot.constants.HOURLY_NO_SIDE_LIVE = False
        assert bot.constants.HOURLY_NO_SIDE_LIVE is False, (
            "Sanity: kill-switch write to bot.constants.HOURLY_NO_SIDE_LIVE "
            "did not land."
        )

        # Hostile bare-name bindings.
        bot.scanner.HOURLY_NO_SIDE_LIVE = True
        bot.executor.HOURLY_NO_SIDE_LIVE = True

        gate_exprs = _extract_gate_expression(SCANNER_SRC, "HOURLY_NO_SIDE_LIVE")
        assert gate_exprs, "Failed to extract scanner HOURLY_NO_SIDE_LIVE gates."
        for gate_expr in gate_exprs:
            ns = {"bot": __import__("bot"), "HOURLY_NO_SIDE_LIVE": True}
            result = _eval_gate(gate_expr, ns)
            assert result is False, (
                f"HOURLY_NO_SIDE_LIVE scanner gate `{ast.unparse(gate_expr)}` "
                f"read TRUE while bot.constants.HOURLY_NO_SIDE_LIVE=False. "
                f"Regression — restore bot.constants.X module-attribute access."
            )

        exec_gate_exprs = _extract_gate_expression(EXECUTOR_SRC, "HOURLY_NO_SIDE_LIVE")
        assert exec_gate_exprs, "Failed to extract executor HOURLY_NO_SIDE_LIVE gates."
        for gate_expr in exec_gate_exprs:
            ns = {"bot": __import__("bot"), "HOURLY_NO_SIDE_LIVE": True}
            result = _eval_gate(gate_expr, ns)
            assert result is False, (
                f"HOURLY_NO_SIDE_LIVE executor gate `{ast.unparse(gate_expr)}` "
                f"read TRUE while bot.constants.HOURLY_NO_SIDE_LIVE=False."
            )
    finally:
        bot.constants.HOURLY_NO_SIDE_LIVE = original
        if had_scanner_attr:
            bot.scanner.HOURLY_NO_SIDE_LIVE = scanner_prev
        else:
            try:
                delattr(bot.scanner, "HOURLY_NO_SIDE_LIVE")
            except AttributeError:
                pass
        if had_executor_attr:
            bot.executor.HOURLY_NO_SIDE_LIVE = executor_prev
        else:
            try:
                delattr(bot.executor, "HOURLY_NO_SIDE_LIVE")
            except AttributeError:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# BRACKET_NO_ENABLED
# ──────────────────────────────────────────────────────────────────────────────


def test_bracket_no_enabled_runtime_freshness():
    """End-to-end runtime-freshness for BRACKET_NO_ENABLED.

    Exercises scanner gate at line 1288 (auto-kill guard) + scanner gate
    at 5413 (bracket-NO intercept gate inside scan loop). Executor has no
    BRACKET_NO_ENABLED gate.
    """
    original = bot.constants.BRACKET_NO_ENABLED
    had_scanner_attr = hasattr(bot.scanner, "BRACKET_NO_ENABLED")
    scanner_prev = getattr(bot.scanner, "BRACKET_NO_ENABLED", None)
    try:
        bot.constants.BRACKET_NO_ENABLED = True
        assert bot.constants.BRACKET_NO_ENABLED is True
        bot.constants.BRACKET_NO_ENABLED = False
        assert bot.constants.BRACKET_NO_ENABLED is False, (
            "Sanity: kill-switch write to bot.constants.BRACKET_NO_ENABLED "
            "did not land."
        )

        bot.scanner.BRACKET_NO_ENABLED = True

        gate_exprs = _extract_gate_expression(SCANNER_SRC, "BRACKET_NO_ENABLED")
        assert gate_exprs, "Failed to extract scanner BRACKET_NO_ENABLED gates."
        for gate_expr in gate_exprs:
            ns = {"bot": __import__("bot"), "BRACKET_NO_ENABLED": True}
            result = _eval_gate(gate_expr, ns)
            assert result is False, (
                f"BRACKET_NO_ENABLED scanner gate `{ast.unparse(gate_expr)}` "
                f"read TRUE while bot.constants.BRACKET_NO_ENABLED=False. "
                f"Regression — restore bot.constants.X module-attribute access."
            )
    finally:
        bot.constants.BRACKET_NO_ENABLED = original
        if had_scanner_attr:
            bot.scanner.BRACKET_NO_ENABLED = scanner_prev
        else:
            try:
                delattr(bot.scanner, "BRACKET_NO_ENABLED")
            except AttributeError:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# Meta pin: cheap drift-guard against silent test removal
# ──────────────────────────────────────────────────────────────────────────────


def test_meta_expected_test_names_present_in_file():
    """Drift-guard: the 3 runtime-freshness tests + this meta pin must
    remain defined in this file. Catches silent removal by an agent that
    "simplifies" the suite without understanding the load-bearing intent.
    """
    src = Path(__file__).read_text()
    tree = ast.parse(src)
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }
    missing = set(EXPECTED_TEST_NAMES) - defined
    assert not missing, (
        f"Expected test names missing from this file (silent removal?): "
        f"{sorted(missing)}. EXPECTED_TEST_NAMES locks the 3 "
        f"runtime-freshness tests + this meta pin."
    )
