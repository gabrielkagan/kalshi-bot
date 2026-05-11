"""Bit 9.3-iii.a — TDD scaffolding for boot-time-binding relocation.

Lands RED before bot/boot.py exists. Goes GREEN after:
  1. bot/boot.py created with 4 boot bindings + cal_mlp warmup
  2. bot/_impl.py re-exports from bot.boot (preserves public_api.json)
  3. bot/main_loop.py + bot/state.py + bot/order_flow.py top-import from bot.boot
     (late-binding blocks deleted)
  4. .importlinter adds bot.boot to helpers-leaf + removes state-no-impl-toplevel
     carve-out (bot/state.py no longer has any bot._impl edge)
  5. bot/state.py deletes _get_compute_for_15m_main_path() helper

See kb/decisions/bit-9.3-iii-a-plan-may11.md for the plan.
"""
import ast
import os
import bot.executor  # noqa: F401
import bot.order_flow  # noqa: F401
import bot.scanner  # noqa: F401
import bot.settlement  # noqa: F401
import bot.state  # noqa: F401

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _parse(rel_path: str) -> ast.Module:
    with open(os.path.join(REPO_ROOT, rel_path)) as f:
        return ast.parse(f.read())


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path)) as f:
        return f.read()


# ── Existence + exports ────────────────────────────────────────────────────


def test_bot_boot_module_exists():
    """bot/boot.py is the new clean-leaf module that owns 4 boot-time bindings."""
    import bot.boot  # noqa: F401


def test_boot_exports_hpsb_bindings():
    import bot.boot

    assert hasattr(bot.boot, "_HPSB_MISSING_BLEEDERS"), (
        "bot.boot must export _HPSB_MISSING_BLEEDERS (relocated from bot._impl.py:358)"
    )
    assert hasattr(bot.boot, "_HPSB_VALIDATOR_UNAVAILABLE_REASON"), (
        "bot.boot must export _HPSB_VALIDATOR_UNAVAILABLE_REASON (relocated from bot._impl.py:347)"
    )
    assert hasattr(bot.boot, "_BLEED_BLOCK_MISSING_BLEEDERS"), (
        "bot.boot must export _BLEED_BLOCK_MISSING_BLEEDERS (relocated from bot._impl.py:359)"
    )


def test_boot_exports_compute_for_15m_main_path():
    """compute_for_15m_main_path closure must be bound at bot.boot module load."""
    import bot.boot

    assert callable(bot.boot.compute_for_15m_main_path), (
        "bot.boot.compute_for_15m_main_path must be the closure returned by "
        "make_compute_for_15m_main_path() (relocated from bot._impl.py:380)"
    )


# ── Clean-leaf invariant ───────────────────────────────────────────────────


def test_boot_no_bot_impl_dependency():
    """bot/boot.py must have ZERO bot._impl edges — clean leaf invariant."""
    tree = _parse("bot/boot.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/boot.py:{node.lineno} imports from bot._impl — clean-leaf rule violated"
            )
            if node.module:
                assert not node.module.startswith("bot._impl."), (
                    f"bot/boot.py:{node.lineno} imports from bot._impl.* — clean-leaf rule violated"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/boot.py:{node.lineno} imports bot._impl — clean-leaf rule violated"
                )
                assert not alias.name.startswith("bot._impl."), (
                    f"bot/boot.py:{node.lineno} imports bot._impl.* — clean-leaf rule violated"
                )


def test_boot_no_main_loop_or_state_dependency():
    """bot/boot.py must NOT import bot.main_loop / bot.state / bot.order_flow / bot.scanner
    / bot.executor / bot.settlement — preserves clean-leaf shape (no circular risk)."""
    tree = _parse("bot/boot.py")
    forbidden = {
        "bot.main_loop",
        "bot.state",
        "bot.order_flow",
        "bot.scanner",
        "bot.executor",
        "bot.settlement",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in forbidden:
            raise AssertionError(
                f"bot/boot.py:{node.lineno} imports from {node.module!r} — "
                f"clean-leaf rule violated"
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden:
                    raise AssertionError(
                        f"bot/boot.py:{node.lineno} imports {alias.name!r} — "
                        f"clean-leaf rule violated"
                    )


# ── Sister modules top-import from bot.boot (no late-binding) ──────────────


def _has_top_level_import_from(rel_path: str, module: str, names: set[str]) -> bool:
    tree = _parse(rel_path)
    for node in ast.iter_child_nodes(tree):  # top-level only
        if isinstance(node, ast.ImportFrom) and node.module == module:
            imported = {a.name for a in node.names}
            if names.issubset(imported):
                return True
    return False


def test_main_loop_top_imports_hpsb_from_bot_boot():
    """bot/main_loop.py top-imports HPSB names from bot.boot — no more method-body late-binding."""
    assert _has_top_level_import_from(
        "bot/main_loop.py",
        "bot.boot",
        {"_HPSB_MISSING_BLEEDERS", "_HPSB_VALIDATOR_UNAVAILABLE_REASON"},
    ), (
        "bot/main_loop.py must top-import _HPSB_MISSING_BLEEDERS + "
        "_HPSB_VALIDATOR_UNAVAILABLE_REASON from bot.boot. The Bit 9.3 method-body "
        "late-binding block (was lines 226-229) should be DELETED in Bit 9.3-iii.a."
    )


def test_main_loop_no_longer_late_binds_hpsb_from_bot_impl():
    """The Bit 9.3 method-body late-binding block must be GONE from bot/main_loop.py."""
    src = _read("bot/main_loop.py")
    # The late-binding pattern was:
    #   from bot._impl import (
    #       _HPSB_MISSING_BLEEDERS,
    #       _HPSB_VALIDATOR_UNAVAILABLE_REASON,
    #   )
    assert "from bot._impl import (\n            _HPSB_MISSING_BLEEDERS" not in src, (
        "bot/main_loop.py still has the Bit 9.3 method-body late-binding block "
        "`from bot._impl import (_HPSB_MISSING_BLEEDERS, ...)`. Bit 9.3-iii.a should "
        "have replaced it with a top-level `from bot.boot import ...`."
    )


def test_state_top_imports_compute_from_bot_boot():
    """bot/state.py top-imports compute_for_15m_main_path from bot.boot."""
    assert _has_top_level_import_from(
        "bot/state.py", "bot.boot", {"compute_for_15m_main_path"}
    ), (
        "bot/state.py must top-import compute_for_15m_main_path from bot.boot. "
        "The Bit 7.1 `_get_compute_for_15m_main_path()` late-binding helper should be "
        "DELETED in Bit 9.3-iii.a (no longer needed once compute_for_15m_main_path "
        "lives in clean-leaf bot.boot)."
    )


def test_state_drops_get_compute_helper():
    """bot/state.py no longer defines _get_compute_for_15m_main_path()."""
    src = _read("bot/state.py")
    assert "def _get_compute_for_15m_main_path" not in src, (
        "bot/state.py still defines _get_compute_for_15m_main_path() — Bit 9.3-iii.a "
        "should have deleted the late-binding helper and use top-level "
        "`from bot.boot import compute_for_15m_main_path`."
    )


def test_state_has_zero_bot_impl_edges():
    """bot/state.py has ZERO bot._impl references post-Bit-9.3-iii.a (clean-leaf claim).

    This was the goal stated in `tests/test_state_extraction.py` from Bit 7.1 ("only
    bot._impl dependency"). Bit 9.3-iii.a delivers on it."""
    tree = _parse("bot/state.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/state.py:{node.lineno} still imports from bot._impl — "
                f"Bit 9.3-iii.a should have eliminated the last edge."
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/state.py:{node.lineno} still imports bot._impl"
                )


def test_order_flow_has_no_hpsb_code_references():
    """bot/order_flow.py has zero EXECUTABLE references to HPSB names.

    Plan-agent m1 flagged bot/order_flow.py:31-32 as late-binding HPSB names from
    bot._impl — but those are docstring lines showing the OLD bot/main_loop.py
    pre-9.3.5 shape as documentation. Walk the AST to confirm no Name/ImportFrom
    nodes reference HPSB.
    """
    tree = _parse("bot/order_flow.py")
    forbidden = {"_HPSB_MISSING_BLEEDERS", "_HPSB_VALIDATOR_UNAVAILABLE_REASON"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden, (
                    f"bot/order_flow.py:{node.lineno} imports {alias.name} from "
                    f"{node.module} — unexpected (was supposed to be docstring-only)"
                )
        if isinstance(node, ast.Name):
            assert node.id not in forbidden, (
                f"bot/order_flow.py:{node.lineno} references {node.id} as a Name — "
                f"unexpected (was supposed to be docstring-only)"
            )


# ── public_api.json snapshot preservation via re-export ────────────────────


def test_impl_reexports_compute_for_15m_main_path_from_bot_boot():
    """bot/_impl.py re-exports compute_for_15m_main_path from bot.boot (preserves snapshot)."""
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.boot

    assert bot._impl.compute_for_15m_main_path is bot.boot.compute_for_15m_main_path, (
        "bot._impl.compute_for_15m_main_path must be the SAME object as "
        "bot.boot.compute_for_15m_main_path (re-export, not regen). Preserves "
        "tests/contracts/public_api.json byte-identical until proxy retirement in 9.3-iii.b/c."
    )


def test_impl_reexports_hpsb_bindings_from_bot_boot():
    """bot/_impl.py re-exports HPSB bindings from bot.boot (preserves snapshot)."""
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.boot

    assert bot._impl._HPSB_MISSING_BLEEDERS is bot.boot._HPSB_MISSING_BLEEDERS
    assert (
        bot._impl._HPSB_VALIDATOR_UNAVAILABLE_REASON
        is bot.boot._HPSB_VALIDATOR_UNAVAILABLE_REASON
    )
    assert (
        bot._impl._BLEED_BLOCK_MISSING_BLEEDERS
        is bot.boot._BLEED_BLOCK_MISSING_BLEEDERS
    )


def test_impl_no_longer_has_direct_warmup_call():
    """bot/_impl.py no longer calls _calmlp_warmup_cache() — moved to bot/boot.py.

    Vacuous post-Bit-9.3-iii.c: bot/_impl.py was DELETED entirely. The
    stronger seal is `test_bot_impl_py_file_absent` in
    tests/test_bit_9_3_iii_c_impl_deletion.py."""
    if not os.path.exists(os.path.join(REPO_ROOT, "bot/_impl.py")):
        import pytest
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — assertion vacuous")
    src = _read("bot/_impl.py")
    assert "_calmlp_enabled_at_boot, _calmlp_warmed = _calmlp_warmup_cache()" not in src, (
        "bot/_impl.py still has the cal_mlp warmup call (was lines 416-417). "
        "Bit 9.3-iii.a should have relocated it to bot/boot.py."
    )


def test_impl_drops_dead_calmlp_start_posthoc_import():
    """bot/_impl.py drops the dead-weight `_calmlp_start_posthoc` import.

    Per Plan-agent M1: bot/main_loop.py ALREADY top-imports this; the
    bot/_impl.py re-import was shadowed dead weight. AST walk catches the
    actual import statement (not search-anchor substrings in comments).

    Vacuous post-Bit-9.3-iii.c: bot/_impl.py was DELETED entirely."""
    if not os.path.exists(os.path.join(REPO_ROOT, "bot/_impl.py")):
        import pytest
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — assertion vacuous")
    tree = ast.parse(_read("bot/_impl.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.asname != "_calmlp_start_posthoc", (
                    f"bot/_impl.py:{node.lineno} still imports "
                    f"`{alias.name} as {alias.asname}` from {node.module!r}. "
                    f"bot/main_loop.py already top-imports it; bot/_impl.py "
                    f"copy was shadowed dead weight and should be dropped."
                )


# ── .importlinter changes (helpers-leaf + state carve-out removal) ─────────


def test_importlinter_adds_bot_boot_to_helpers_leaf():
    """bot.boot is in helpers-leaf forbidden_modules (Plan-agent M3)."""
    src = _read(".importlinter")
    assert "bot.boot" in src, (
        ".importlinter must mention bot.boot — Bit 9.3-iii.a adds it to helpers-leaf "
        "forbidden_modules per the auto-walk regression "
        "test_helpers_leaf_forbidden_modules_covers_all_bot_top_level."
    )


def test_importlinter_drops_state_no_impl_toplevel_carveout():
    """state-no-impl-toplevel carve-out removed (Plan-agent M2).

    bot/state.py has zero bot._impl edges post-Bit-9.3-iii.a, so the carve-out
    is no longer needed. Check for the actual contract section header — historical
    references to the retired contract name in explanatory comments are fine."""
    src = _read(".importlinter")
    assert "[importlinter:contract:state-no-impl-toplevel]" not in src, (
        ".importlinter still has the [importlinter:contract:state-no-impl-toplevel] "
        "section. Bit 9.3-iii.a should have removed it — bot/state.py no longer has "
        "any bot._impl dependency."
    )


# ── Thread-env ordering (Plan-agent C1) ────────────────────────────────────


def test_boot_loads_after_thread_env_in_production_chain():
    """The production import chain `__main__ → _thread_env → main_loop → boot →
    integration → numpy` preserves OMP_NUM_THREADS=1-before-numpy.

    We can't simulate the full chain here, but we can pin two structural invariants:
      1. bot/__main__.py imports bot._thread_env BEFORE bot.main_loop.
      2. bot/boot.py is reachable transitively from bot.main_loop (top-level edge)."""
    # Invariant 1
    main_src = _read("bot/__main__.py")
    thread_env_idx = main_src.find("import bot._thread_env")
    main_loop_idx = main_src.find("from bot.main_loop import")
    assert thread_env_idx >= 0, "bot/__main__.py must import bot._thread_env"
    assert main_loop_idx >= 0, "bot/__main__.py must import bot.main_loop"
    assert thread_env_idx < main_loop_idx, (
        "bot/__main__.py must import bot._thread_env BEFORE bot.main_loop "
        "(R-p7-deploy-r7 critical ordering)"
    )

    # Invariant 2: bot/main_loop.py top-imports bot.boot (transitive load triggers boot)
    assert _has_top_level_import_from(
        "bot/main_loop.py", "bot.boot", {"_HPSB_MISSING_BLEEDERS"}
    ), "bot/main_loop.py must top-import from bot.boot to trigger boot-time bindings"
