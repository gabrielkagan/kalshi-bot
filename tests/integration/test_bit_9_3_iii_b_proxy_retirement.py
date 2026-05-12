"""Bit 9.3-iii.b — TDD scaffolding for _BotProxy retirement + bundled CALMLP-boot-log fu.

Lands RED before any production code change. Goes GREEN after:
  1. bot/__init__.py body stripped — _BotProxy class deleted, no __class__ swap,
     bot module is a plain types.ModuleType
  2. bot/__main__.py basicConfig hoisted to module top-level (after import bot._thread_env,
     before from bot.main_loop import MainLoop) — so the `[CALMLP] enabled=N at boot`
     log emitted during bot.boot module-load reaches the configured stderr handler
  3. tests/* + bot/* + scripts/* doc-drift sites updated to drop _BotProxy narrative
  4. tests/contracts/public_api.json updated: 2 intentional removals
     (bot._BotProxy + bot._impl_cache), all other entries byte-stable

See kb/decisions/bit-9.3-iii-b-plan-may11.md for the plan.
"""
import ast
import os
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _parse(rel_path: str) -> ast.Module:
    with open(os.path.join(REPO_ROOT, rel_path)) as f:
        return ast.parse(f.read())


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path)) as f:
        return f.read()


# ── 1-3. bot is a plain ModuleType — no _BotProxy class, no _impl_cache attr ──


def test_bot_module_is_plain_ModuleType():
    """After proxy retirement, `import bot` yields a plain types.ModuleType, not a subclass.

    Pre-Bit-9.3-iii.b: bot/__init__.py:261 does `sys.modules[__name__].__class__ = _BotProxy`
    so type(bot) is bot._BotProxy. Post-retirement: bot is a vanilla ModuleType.
    """
    import bot

    assert type(bot) is types.ModuleType, (
        f"Expected type(bot) is types.ModuleType after _BotProxy retirement, "
        f"got {type(bot).__name__} (from {type(bot).__module__})"
    )


def test_no_BotProxy_class_defined():
    """The _BotProxy class itself must be deleted from bot/__init__.py, not just unhooked.

    Even if the __class__ swap is removed, leaving the class definition behind invites
    future regression (someone re-enables the proxy by adding back the swap line).
    """
    import bot

    assert not hasattr(bot, "_BotProxy"), (
        "bot._BotProxy class must be deleted entirely, not just unhooked. "
        "Found _BotProxy as a bot module attribute — class definition still in bot/__init__.py."
    )


def test_no_impl_cache_attribute():
    """The _impl_cache attribute (proxy's caching field) must be gone.

    Pre-retirement: bot/__init__.py:262 does object.__setattr__(bot, '_impl_cache', None).
    Post-retirement: this line is deleted; bot has no _impl_cache attribute.
    """
    import bot

    assert not hasattr(bot, "_impl_cache"), (
        "bot._impl_cache attribute must be removed. Found _impl_cache on bot module — "
        "proxy cache initialization line still in bot/__init__.py:262."
    )


# ── 4. Direct-submodule resolution works without proxy ───────────────────────


def test_MainLoop_resolves_via_bot_main_loop():
    """bot.main_loop.MainLoop is the canonical home — direct access works post-retirement.

    Pre-retirement: bot.MainLoop resolves via _BotProxy.__getattr__ → bot._impl.MainLoop
    (which is itself a re-import from bot.main_loop).
    Post-retirement: callers must use `from bot.main_loop import MainLoop` or `bot.main_loop.MainLoop`.
    """
    import bot.main_loop
    from bot.main_loop import MainLoop

    assert bot.main_loop.MainLoop is MainLoop, (
        "Canonical home pin: bot.main_loop.MainLoop must be the live class object. "
        "Failure here suggests the extraction itself broke, not just the proxy retirement."
    )


# ── 5. Negative pin: bot.X does NOT fall through to bot._impl post-retirement ──


def test_bot_dot_attribute_does_not_fall_through_to_bot_impl():
    """Setting a unique attr on bot._impl must NOT make bot.X resolve post-retirement.

    Pre-retirement: _BotProxy.__getattr__ falls through. Inject X onto bot._impl.__dict__,
    then bot.X returns it via the proxy → test FAILS.
    Post-retirement: bot.X raises AttributeError regardless of what's on bot._impl.__dict__,
    because the proxy's __getattr__ is gone and Python's default module lookup doesn't
    route to sibling modules.

    Uses a one-shot canary attribute to avoid polluting bot._impl globals.
    """
    import bot
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)

    canary_name = "_proxy_retirement_canary_attr_9b3"
    canary_value = object()
    bot._impl.__dict__[canary_name] = canary_value
    try:
        sentinel = object()
        result = getattr(bot, canary_name, sentinel)
        assert result is sentinel, (
            f"Expected getattr fallback to return sentinel (AttributeError), "
            f"got the canary value {result!r}. "
            "_BotProxy.__getattr__ is still intercepting and routing bot.X → bot._impl.X."
        )
    finally:
        del bot._impl.__dict__[canary_name]


# ── 6. CALMLP boot log is observable post basicConfig hoist (fu fix) ─────────


def test_calmlp_boot_log_observable_at_production_runtime(monkeypatch, capsys):
    """The `[CALMLP] enabled=N at boot` log line reaches stderr at production startup.

    Pre-Bit-9.3-iii.b (post-9.3-iii.a): basicConfig fires inside `if __name__ == "__main__":`
    block in bot/__main__.py at line 33, AFTER `from bot.main_loop import MainLoop` at line 30.
    When the import triggers bot.boot module-load, the boot log fires against the default root
    logger (no handlers) and is silent-dropped at production runtime.

    Post-fu-fix: basicConfig is at module top-level in bot/__main__.py, ordered AFTER
    bot._thread_env import + BEFORE bot.main_loop import — so the boot log reaches the
    configured stderr handler.

    Verification strategy: walk bot/__main__.py via AST. Find the line numbers of
    `logging.basicConfig` and `from bot.main_loop import MainLoop`. basicConfig must come
    FIRST so the log emitted during the import is captured.

    (caplog can observe INFO logs even when production runtime can't, because pytest installs
    its own handler. So we don't use caplog — we verify the source-code ordering instead.
    The AST guard in test_thread_env_imported_before_basicConfig_before_main_loop is the
    structural pin; this test is a sibling that asserts the source order at file scope.)
    """
    tree = _parse("bot/__main__.py")

    basic_config_lineno = None
    main_loop_import_lineno = None

    for stmt in tree.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            f = stmt.value.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "basicConfig"
                and isinstance(f.value, ast.Name)
                and f.value.id == "logging"
            ):
                basic_config_lineno = stmt.lineno
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "bot.main_loop":
            main_loop_import_lineno = stmt.lineno

    assert basic_config_lineno is not None, (
        "logging.basicConfig() must be a top-level statement in bot/__main__.py for the "
        "[CALMLP] boot log to reach stderr at production startup. Currently nested inside "
        "`if __name__ == \"__main__\":` block (post-9.3-iii.a state)."
    )
    assert main_loop_import_lineno is not None, (
        "from bot.main_loop import MainLoop missing from bot/__main__.py module body"
    )
    assert basic_config_lineno < main_loop_import_lineno, (
        f"Ordering violation: basicConfig at line {basic_config_lineno} must come BEFORE "
        f"`from bot.main_loop import MainLoop` at line {main_loop_import_lineno}. "
        "Otherwise the [CALMLP] enabled=N at boot log fires against the unconfigured root "
        "logger (no handlers) and is silent-dropped at production runtime."
    )


# ── 7. basicConfig hoisted to module top-level in bot/__main__.py ────────────


def test_basicConfig_hoisted_to_module_top_level_in_bot_main():
    """logging.basicConfig is a top-level statement in bot/__main__.py, not nested in `if __name__`.

    Pre-fu: basicConfig lives inside `if __name__ == "__main__":` block (line 33).
    Post-fu: basicConfig is a module-body statement, ordered after `import bot._thread_env`
    and before `from bot.main_loop import MainLoop`.

    Walk the AST top-level statements. basicConfig Call should appear in the module body,
    not inside any If/For/While/Try block.
    """
    tree = _parse("bot/__main__.py")

    # Find all `logging.basicConfig(...)` Call nodes anywhere in the AST.
    basic_config_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "basicConfig":
                if isinstance(f.value, ast.Name) and f.value.id == "logging":
                    basic_config_calls.append(node)

    assert len(basic_config_calls) == 1, (
        f"Expected exactly 1 logging.basicConfig() call in bot/__main__.py; "
        f"got {len(basic_config_calls)}"
    )

    call_node = basic_config_calls[0]

    # Check that the call is at module top-level — its parent must be an Expr at module body.
    for module_stmt in tree.body:
        if isinstance(module_stmt, ast.Expr) and module_stmt.value is call_node:
            return  # ✓ top-level

    raise AssertionError(
        "logging.basicConfig() must be a top-level Expr statement in bot/__main__.py "
        "(module body), not nested inside any If/For/While/Try block. "
        "Currently nested — fu (CALMLP boot log silent-drop) not yet fixed."
    )


# ── 8. Import ordering: bot._thread_env before basicConfig before bot.main_loop ──


def test_thread_env_imported_before_basicConfig_before_main_loop_in_bot_main():
    """Strict ordering of the 3 boot-critical statements in bot/__main__.py.

    1. `import bot._thread_env` — MUST be the FIRST non-stdlib import.
       Reason: OMP_NUM_THREADS=1 must be in os.environ before numpy/scipy/torch load.
       (kb/failures/cal-mlp-torch-thread-contention-apr29.md)
    2. `logging.basicConfig(...)` — must come before any bot.* import that emits log lines.
       Reason: bot.boot emits `[CALMLP] enabled=N at boot` during module-load.
    3. `from bot.main_loop import MainLoop` — triggers bot.boot load → CALMLP boot log emission.
       Must come AFTER basicConfig so the log line reaches the configured stderr handler.
    """
    tree = _parse("bot/__main__.py")

    thread_env_lineno = None
    basic_config_lineno = None
    main_loop_import_lineno = None

    for stmt in tree.body:
        # `import bot._thread_env` — Import node with `bot._thread_env` in names.
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "bot._thread_env":
                    thread_env_lineno = stmt.lineno
        # `from bot.main_loop import MainLoop` — ImportFrom node.
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "bot.main_loop":
            main_loop_import_lineno = stmt.lineno
        # `logging.basicConfig(...)` — Expr at module level.
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            f = stmt.value.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "basicConfig"
                and isinstance(f.value, ast.Name)
                and f.value.id == "logging"
            ):
                basic_config_lineno = stmt.lineno

    assert thread_env_lineno is not None, "import bot._thread_env missing from bot/__main__.py module body"
    assert basic_config_lineno is not None, (
        "logging.basicConfig() not at module top-level in bot/__main__.py "
        "(see test_basicConfig_hoisted_to_module_top_level_in_bot_main for details)"
    )
    assert main_loop_import_lineno is not None, (
        "from bot.main_loop import MainLoop missing from bot/__main__.py module body"
    )

    assert thread_env_lineno < basic_config_lineno < main_loop_import_lineno, (
        f"Ordering violation in bot/__main__.py: "
        f"bot._thread_env@{thread_env_lineno} → basicConfig@{basic_config_lineno} → "
        f"bot.main_loop@{main_loop_import_lineno}. "
        f"Required: thread_env FIRST, then basicConfig, then bot.main_loop."
    )


# ── 9. _thread_env stays first non-stdlib import (defense-in-depth peer-pin) ──


def test_thread_env_remains_first_non_stdlib_import_in_bot_main():
    """Defense-in-depth: thread_env must still be the FIRST non-stdlib import.

    Allowed stdlib imports before it: none (the bot._thread_env line is line 26 currently;
    `import os, sys` etc. live inside _thread_env itself).

    Mirror of tests/integration/test_cal_mlp_invariants.py::test_thread_env_imported_before_numerical_libs_in_bot_boot
    but for bot/__main__.py — the production entrypoint.
    """
    tree = _parse("bot/__main__.py")

    STDLIB_MODULES = {
        "os", "sys", "logging", "json", "time", "datetime", "math",
        "typing", "collections", "functools", "itertools", "re",
        "warnings", "abc", "enum", "contextlib", "pathlib", "io",
        "threading", "queue", "subprocess", "signal", "traceback",
        "argparse", "configparser",
    }

    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                name = alias.name.split(".")[0]
                if name == "bot":
                    assert alias.name == "bot._thread_env", (
                        f"First bot.* import in bot/__main__.py must be bot._thread_env, "
                        f"got bot.{alias.name.split('.', 1)[1] if '.' in alias.name else ''} at line {stmt.lineno}"
                    )
                    return  # ✓
                if name not in STDLIB_MODULES:
                    raise AssertionError(
                        f"Non-stdlib import before bot._thread_env in bot/__main__.py: "
                        f"`import {alias.name}` at line {stmt.lineno}. "
                        f"bot._thread_env MUST be the first non-stdlib import."
                    )
        elif isinstance(stmt, ast.ImportFrom):
            if stmt.module is None:
                continue
            module_root = stmt.module.split(".")[0]
            if module_root == "bot":
                raise AssertionError(
                    f"bot.* from-import at line {stmt.lineno} in bot/__main__.py before "
                    f"`import bot._thread_env`. `import bot._thread_env` must be first."
                )
            if module_root not in STDLIB_MODULES:
                raise AssertionError(
                    f"Non-stdlib from-import at line {stmt.lineno}: "
                    f"`from {stmt.module} import ...` before bot._thread_env"
                )


# ── 10. No _BotProxy references in production bot/ source (excl. docstrings/comments) ──


def test_no_BotProxy_references_in_production_bot_imports():
    """Walk bot/*.py + bot/**/*.py and confirm no executable code references _BotProxy.

    Uses AST instead of raw grep so docstrings and comments are excluded — those are
    swept separately in the doc-drift phase.
    """
    bot_dir = os.path.join(REPO_ROOT, "bot")
    offenders = []

    for root, dirs, files in os.walk(bot_dir):
        # Skip __pycache__ etc.
        dirs[:] = [d for d in dirs if not d.startswith("__")]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            rel_path = os.path.relpath(os.path.join(root, fname), REPO_ROOT)
            tree = _parse(rel_path)
            for node in ast.walk(tree):
                # Catch Name("_BotProxy") references in executable code
                if isinstance(node, ast.Name) and node.id == "_BotProxy":
                    offenders.append(f"{rel_path}:{node.lineno} — Name('_BotProxy')")
                # Catch Attribute(...)._BotProxy
                if isinstance(node, ast.Attribute) and node.attr == "_BotProxy":
                    offenders.append(f"{rel_path}:{node.lineno} — Attribute(.{node.attr})")

    assert not offenders, (
        "Found _BotProxy references in executable production code (not docstrings/comments):\n  "
        + "\n  ".join(offenders)
    )


# ── 11. No _impl_cache references in production code ────────────────────────


def test_no_impl_cache_references_in_production():
    """Mirror of test 10 for _impl_cache (the proxy's instance-attribute cache field)."""
    bot_dir = os.path.join(REPO_ROOT, "bot")
    offenders = []

    for root, dirs, files in os.walk(bot_dir):
        dirs[:] = [d for d in dirs if not d.startswith("__")]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            rel_path = os.path.relpath(os.path.join(root, fname), REPO_ROOT)
            tree = _parse(rel_path)
            for node in ast.walk(tree):
                # Match string literals (e.g., '_impl_cache' inside setattr/object.__setattr__)
                if isinstance(node, ast.Constant) and node.value == "_impl_cache":
                    offenders.append(f"{rel_path}:{node.lineno} — string '_impl_cache'")
                if isinstance(node, ast.Name) and node.id == "_impl_cache":
                    offenders.append(f"{rel_path}:{node.lineno} — Name('_impl_cache')")
                if isinstance(node, ast.Attribute) and node.attr == "_impl_cache":
                    offenders.append(f"{rel_path}:{node.lineno} — Attribute(.{node.attr})")

    assert not offenders, (
        "Found _impl_cache references in production code:\n  "
        + "\n  ".join(offenders)
    )


# ── 12. public_api.json snapshot: intentional removals only ─────────────────


def test_public_api_snapshot_post_proxy_drop():
    """Post proxy retirement, the public_api.json snapshot's `__bot_proxy_attrs__` array is empty.

    The snapshot at `tests/contracts/public_api.json` is a dict keyed by qualname
    (e.g., `bot.constants.OBSERVATION_MODE`). Pre-retirement it ALSO contains two
    metadata keys:
    - `__bot_proxy_attrs__` — list of ~615 names that `getattr(bot, name)` resolved
      via `_BotProxy.__getattr__` → `bot._impl.X`.
    - `__impl_canonical_classes__` — class-identity metadata for proxy-resolved classes.

    Post-retirement `getattr(bot, X)` no longer falls through to bot._impl, so the
    `__bot_proxy_attrs__` list shrinks to ~0 (only the names actually bound to bot
    package itself, which post-retirement is none beyond submodule auto-bindings).

    This pin SKIPs until the snapshot is regenerated at commit time. After regen:
    - `__bot_proxy_attrs__` should be empty (or the key may have been dropped by the
      regen script entirely — accept either).
    - All `bot.constants.X` / `bot.main_loop.X` / `bot.boot.X` etc. qualname keys
      remain present (the canonical-submodule surface is unchanged).
    """
    import json

    snapshot_path = os.path.join(REPO_ROOT, "tests/contracts/public_api.json")
    pre_snapshot_path = os.path.join(REPO_ROOT, "tests/contracts/public_api.json.pre-9.3-iii-b")

    if not os.path.exists(pre_snapshot_path):
        import pytest
        pytest.skip(
            "Pre-change snapshot not captured. Run `cp tests/contracts/public_api.json "
            "tests/contracts/public_api.json.pre-9.3-iii-b` before the production edit."
        )

    with open(snapshot_path) as f:
        post = json.load(f)
    with open(pre_snapshot_path) as f:
        pre = json.load(f)

    # Marker: if post snapshot is byte-identical to pre, regen hasn't happened yet → skip.
    if post == pre:
        import pytest
        pytest.skip(
            "Snapshot not yet regenerated post-proxy-retirement. "
            "Run `python3 scripts/dump_public_api.py > tests/contracts/public_api.json` "
            "at commit time."
        )

    # __bot_proxy_attrs__ must be absent or empty post-retirement.
    proxy_attrs = post.get("__bot_proxy_attrs__")
    assert not proxy_attrs, (
        f"__bot_proxy_attrs__ must be empty/absent post-retirement; "
        f"got {len(proxy_attrs) if proxy_attrs else 0} entries: {proxy_attrs[:5] if proxy_attrs else None}. "
        f"The _BotProxy is gone, so `getattr(bot, name)` should NOT find {len(proxy_attrs or [])} proxied names."
    )

    # Canonical-submodule keys must still be present (sample check).
    SANITY_KEYS = (
        "bot.constants.OBSERVATION_MODE",
        "bot.constants.MIN_ENTRY_PRICE",
        "bot.boot.compute_for_15m_main_path",
    )
    for key in SANITY_KEYS:
        assert key in post, f"Canonical-submodule key {key!r} missing from post-regen snapshot"


# ── 13. Constants canonical-home peer-pin ───────────────────────────────────


def test_bot_constants_canonical_home_after_proxy_retirement():
    """The 4 constants pre_deploy_check.sh reads must resolve from bot.constants post-retarget.

    Pre-retirement: scripts/pre_deploy_check.sh does `bot.OBSERVATION_MODE` etc., which
    routes through the proxy to bot._impl.OBSERVATION_MODE, which is itself imported from
    bot.constants via `from bot.constants import *`.

    Post-retirement: scripts/pre_deploy_check.sh must be retargeted to bot.constants.X.
    This pin validates the canonical home is correct.
    """
    import bot.constants

    for name in ("OBSERVATION_MODE", "MIN_ENTRY_PRICE", "MAX_ENTRY_PRICE", "MAX_SECONDS_BEFORE_CLOSE"):
        assert hasattr(bot.constants, name), (
            f"bot.constants.{name} missing — pre_deploy_check.sh retarget would break"
        )

    # Spot-check actual values are sane (not None / not "0" string artifact).
    assert isinstance(bot.constants.MIN_ENTRY_PRICE, int), (
        f"bot.constants.MIN_ENTRY_PRICE expected int, got {type(bot.constants.MIN_ENTRY_PRICE).__name__}"
    )
    assert isinstance(bot.constants.MAX_ENTRY_PRICE, int)
    assert isinstance(bot.constants.MAX_SECONDS_BEFORE_CLOSE, (int, float))
