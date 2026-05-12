"""Bit 3.1 — constants extracted from bot/_impl.py to bot/constants.py.

Locks the bidirectional contract between bot/_impl.py (which does
`from bot.constants import *` near top of file) and bot/constants.py
(the new single source of truth for module-level UPPER_SNAKE constants).

10 invariants:
  1. Every name in bot.constants is bound on bot._impl via star-import.
  2. bot/constants.py imports only `os` (zero numerical deps).
  3. market_config.validate_market_configs() succeeds (exercises proxy +
     54 startup asserts end-to-end).
  4. scripts/postdeploy_verify.read_bot_constants(...) finds the 4 flags
     it gates on (silent-skip regression).
  5. bot/constants.py contains zero FunctionDef/ClassDef/AsyncFunctionDef.
  6. bot/_impl.py has zero UPPER_SNAKE module-level assignments after
     the move (catches future "I'll just add it back to _impl.py" drift).
  7. scripts/ops/extract_config.py main() outputs JSON with non-empty
     TRACKED_CONSTANTS values (locks the CI whitepaper-regen contract).
  8. .github/workflows/post_deploy_verify.yml does not pin
     `--bot-py bot/_impl.py` (overrides the multi-path default).
  9. .github/workflows/whitepaper.yml paths filter includes
     `bot/constants.py` (regen triggers on constant changes).
 10. Makefile ast-check recipe covers BOTH bot/_impl.py AND
     bot/constants.py (catches constants.py syntax errors pre-deploy).

L2 (Bit 3.0.5): tests call production directly. No reimplementing the
contract in test helpers.
"""
import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ─── 1. Re-export contract ──────────────────────────────────────────────────


def test_bot_impl_re_exports_all_constants_via_star_import():
    """`from bot.constants import *` near the top of bot/_impl.py must
    bind every public name from bot.constants into bot._impl's namespace.
    External callers (market_config.py, dashboard_snapshot.py, etc.) read
    via `bot.X` (proxy → bot._impl.X) → resolution depends on the
    star-imported bindings sitting in bot._impl's __dict__.

    Identity check: at module-load time, both modules' bindings point
    at the SAME object. Runtime mutations (e.g.,
    `bot._impl.WEATHER_NO_SIDE_LIVE = False` kill-switch sites) can
    diverge later, but the load-time identity is what we assert here.

    Underscore-prefixed names: Python's `from X import *` skips them
    by default. The migration script's MOVE_DESPITE_UNDERSCORE allowlist
    moves `_CROSS_EXCHANGE_FEEDS_ACTIVE` to bot/constants.py because
    `CROSS_EXCHANGE_CONSENSUS_MIN` references it. To preserve the
    `bot.constants._CROSS_EXCHANGE_FEEDS_ACTIVE` access pattern, bot/_impl.py
    has an explicit `from bot.constants import _CROSS_EXCHANGE_FEEDS_ACTIVE`
    after the star-import. This test enforces that re-export contract.
    """
    import bot.constants
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)

    # Names known to require explicit underscore re-export (mirrors the
    # MOVE_DESPITE_UNDERSCORE set in the migration script).
    EXPLICIT_UNDERSCORE_REEXPORTS = {"_CROSS_EXCHANGE_FEEDS_ACTIVE"}

    missing_or_mismatched = []
    for name in dir(bot.constants):
        if name.startswith("__"):
            continue  # dunder
        if name.startswith("_") and name not in EXPLICIT_UNDERSCORE_REEXPORTS:
            continue  # private to bot.constants — not contracted to re-export
        # bot.constants imports `os` — exclude module imports from the
        # contract (we don't expect bot._impl.os to be the same os).
        val = getattr(bot.constants, name)
        if isinstance(val, type(ast)):  # ModuleType — skip
            continue
        if not hasattr(bot._impl, name):
            missing_or_mismatched.append(f"{name} (missing)")
            continue
        if getattr(bot._impl, name) is not val:
            missing_or_mismatched.append(f"{name} (object identity mismatch)")

    assert not missing_or_mismatched, (
        "bot/_impl.py must re-export every public name from bot/constants.py "
        "via `from bot.constants import *`, plus explicit re-exports for "
        f"underscore-prefixed names in {sorted(EXPLICIT_UNDERSCORE_REEXPORTS)}. "
        "Missing or identity-mismatched: "
        + ", ".join(missing_or_mismatched[:20])
    )


# ─── 2. Zero-numerical-deps invariant on bot/constants.py ──────────────────


def test_bot_constants_imports_only_os():
    """bot/constants.py must import only `os` (env-flag pattern). No
    numpy/scipy/torch/sklearn/pandas — preserves the bot._thread_env
    contract (numerical libs cache OMP/MKL thread count at C-extension
    load; importing them before _thread_env's setdefault makes those
    setdefaults no-ops).

    Belt-and-suspenders: bot._thread_env at line 11 of bot/_impl.py runs
    BEFORE numpy at line 37, and the new `from bot.constants import *`
    at ~line 240 lands AFTER both. If bot/constants.py later grows a
    numpy import, this test fails before that change ships.
    """
    src = (REPO_ROOT / "bot" / "constants.py").read_text()
    tree = ast.parse(src)
    forbidden = {"numpy", "scipy", "torch", "sklearn", "pandas"}
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                head = alias.name.split(".")[0]
                if head in forbidden:
                    bad.append(f"line {node.lineno}: import {alias.name}")
                elif head != "os":
                    bad.append(
                        f"line {node.lineno}: import {alias.name} "
                        "(only `os` allowed)"
                    )
        elif isinstance(node, ast.ImportFrom):
            head = (node.module or "").split(".")[0]
            if head in forbidden:
                bad.append(
                    f"line {node.lineno}: from {node.module} import ..."
                )
            elif head and head != "os":
                bad.append(
                    f"line {node.lineno}: from {node.module} import ... "
                    "(only `os` allowed)"
                )

    assert not bad, (
        "bot/constants.py must import only `os` — preserves the "
        "bot._thread_env zero-numerical-deps contract. Violations: "
        + "; ".join(bad)
    )


# ─── 3. market_config startup gate ─────────────────────────────────────────


def test_market_config_validate_succeeds_post_extraction():
    """The 54 `bot.X` references in market_config.py route through
    bot/__init__.py's _BotProxy.__getattr__ → bot._impl.X → resolved
    from the star-imported bot.constants binding. If ANY constant value
    drifted during cut/paste, validate_market_configs() raises
    AssertionError → bot fails to start at boot.

    L2: call production directly. No mirroring.
    """
    from market_config import validate_market_configs

    # Raises AssertionError on any single-constant drift; pytest reports
    # the offending constant via the assertion message in market_config.
    validate_market_configs()


# ─── 4. postdeploy_verify silent-skip regression ───────────────────────────


def test_postdeploy_verify_finds_4_gating_flags_in_constants_py():
    """scripts/audit/postdeploy_verify.py read_bot_constants() regex-scans
    source for 4 flags (WEATHER_NO_SIDE_LIVE, SPORTS_OBSERVATION_ONLY,
    OVERNIGHT_DISCOUNT_LIVE, WEEKEND_DISCOUNT_LIVE). After Bit 3.1 those
    definitions live ONLY in bot/constants.py. If the script wasn't
    updated, the 4-flag dict is empty → checks 2/3/6/7 silently skip
    and the [6/6] postdeploy gate passes regardless of regressions.

    Tests the multi-path scan implementation (preferred over a single
    `bot/_impl.py → bot/constants.py` redirect — survives future moves).
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))
    from postdeploy_verify import read_bot_constants

    # Multi-path scan: pass list of paths matching the new default.
    flags = read_bot_constants(
        [REPO_ROOT / "bot" / "_impl.py", REPO_ROOT / "bot" / "constants.py"]
    )

    expected = {
        "WEATHER_NO_SIDE_LIVE",
        "SPORTS_OBSERVATION_ONLY",
        "OVERNIGHT_DISCOUNT_LIVE",
        "WEEKEND_DISCOUNT_LIVE",
    }
    missing = expected - set(flags.keys())
    assert not missing, (
        "postdeploy_verify.read_bot_constants() multi-path scan didn't find: "
        + ", ".join(sorted(missing))
        + ". Each missing flag silently skips a postdeploy gate check."
    )


# ─── 5. bot/constants.py is constants-only ─────────────────────────────────


def test_bot_constants_has_no_function_or_class_defs():
    """bot/constants.py is constants-only by contract. Any `def` or
    `class` is a smell that helpers/state are creeping into the
    constants module — defeats the modularization goal and risks
    re-introducing the import-ordering hazards Bit 3.1 cleaned up.
    """
    src = (REPO_ROOT / "bot" / "constants.py").read_text()
    tree = ast.parse(src)
    bad = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            bad.append(f"line {node.lineno}: {type(node).__name__} {node.name!r}")

    assert not bad, (
        "bot/constants.py must be constants-only (no def/class). "
        "Violations: " + "; ".join(bad)
    )


# ─── 6. bot/_impl.py UPPER_SNAKE leftover regression ───────────────────────


def test_bot_impl_has_no_module_level_uppercase_assignments():
    """After Bit 3.1 the count of non-underscore UPPER_SNAKE module-level
    assigns in bot/_impl.py must be 0. Catches the future regression
    where someone adds a new constant directly to bot/_impl.py instead
    of bot/constants.py.

    Underscore-prefixed names are EXCLUDED by `startswith("_")` —
    `_FOO.isupper()` is True (Python ignores non-cased chars), so the
    underscore check has to be explicit. The underscore-prefixed
    UPPER_SNAKE names that legitimately stay in bot/_impl.py are
    runtime-mutated state (`_TELEGRAM`, validator outputs
    `_HPSB_MISSING_BLEEDERS` / `_BLEED_BLOCK_MISSING_BLEEDERS`,
    `_HPSB_VALIDATOR_UNAVAILABLE_REASON`) plus the `_ORPHAN_DB_WATCHDOG_PATTERNS`
    co-located helper just above `detect_orphan_db_holders` (5 names
    post-Bit-6.3 path-B). `_CALIBRATION_ENGINE` and `_CAL_REGISTRY`
    were relocated to `bot/engines/calibration.py` in Bit 6.3 path-B
    (2026-05-10); this list previously named them.
    """
    if not (REPO_ROOT / "bot" / "_impl.py").exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = (REPO_ROOT / "bot" / "_impl.py").read_text()
    tree = ast.parse(src)
    leftover = []

    for node in ast.iter_child_nodes(tree):
        name = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        if not name:
            continue
        if name.startswith("_"):
            continue  # runtime state or co-located helper data
        if not name.isupper():
            continue  # mixed-case (not a constant by convention)
        leftover.append(f"line {node.lineno}: {name}")

    assert not leftover, (
        "bot/_impl.py must not have non-underscore UPPER_SNAKE "
        "module-level assigns post-Bit-3.1 — every public constant "
        "moves to bot/constants.py. Leftover (first 20): "
        + ", ".join(leftover[:20])
    )


# ─── 7. extract_config.py CI contract ──────────────────────────────────────


def test_extract_config_main_produces_non_empty_tracked_constants():
    """scripts/ops/extract_config.py is invoked by .github/workflows/whitepaper.yml
    on every push touching bot/_impl.py or bot/constants.py. It outputs
    config.json which feeds whitepaper auto-regen. Pre-Bit-3.1 it
    regex-extracted ~80 TRACKED_CONSTANTS from bot/_impl.py; after
    Bit 3.1 those constants live in bot/constants.py. Without the
    dual-read fix, the script silently writes config.json with empty
    or missing constant values → next [skip ci] auto-regen produces
    a stale whitepaper.

    Run as subprocess to mirror the workflow's actual invocation
    (`python3 scripts/ops/extract_config.py > config.json`).
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ops" / "extract_config.py")],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"extract_config.py exited {result.returncode}; stderr:\n"
        f"{result.stderr}"
    )

    config = json.loads(result.stdout)
    constants = config.get("constants", {})

    # 4 representative constants that move in Bit 3.1. If any of these
    # are missing from the output, the dual-read implementation is
    # broken or the CONSTANTS_PATH wasn't added to extract_config.py.
    expected = {
        "OBSERVATION_MODE",
        "MIN_ENTRY_PRICE",
        "MAX_ENTRY_PRICE",
        "MARKET_BLEND_W",
    }
    missing = expected - set(constants.keys())
    assert not missing, (
        f"extract_config.py main() output missing {missing}. "
        f"Got {len(constants)} constants total. "
        "Whitepaper auto-regen would silently produce stale config.json."
    )


# ─── 8. CI workflow: post_deploy_verify pin regression ─────────────────────


def test_post_deploy_verify_yml_does_not_pin_bot_impl_only():
    """.github/workflows/post_deploy_verify.yml step [5/6] invokes
    postdeploy_verify.py. Before Bit 3.1 it passed `--bot-py bot/_impl.py`
    explicitly. Even with multi-path support in the script, the explicit
    flag overrides the default → silent regression: 0 of 4 flags found,
    checks 2/3/6/7 silently skip.

    Fix: drop the `--bot-py` flag entirely; rely on the script's new
    multi-path default. This test asserts the workflow does NOT pin the
    flag to bot/_impl.py only.

    String-grep only — no PyYAML dependency (CI doesn't have PyYAML;
    Bit 2.3 R2 lesson on what-works-on-Mac-vs-CI).
    """
    yml_path = REPO_ROOT / ".github" / "workflows" / "post_deploy_verify.yml"
    text = yml_path.read_text()
    assert "--bot-py bot/_impl.py" not in text, (
        f"{yml_path.relative_to(REPO_ROOT)} pins `--bot-py bot/_impl.py` — "
        "this overrides postdeploy_verify.py's multi-path default and "
        "silently skips 4 gating-flag checks post-Bit-3.1. Drop the flag."
    )


# ─── 9. CI workflow: whitepaper paths filter ───────────────────────────────


def test_whitepaper_yml_paths_filter_includes_constants_py():
    """.github/workflows/whitepaper.yml triggers whitepaper auto-regen
    on push when files matching `paths` change. Pre-Bit-3.1 the filter
    listed `bot/_impl.py` only. After Bit 3.1, constant changes happen
    in bot/constants.py — must be in the paths filter or whitepaper
    silently de-syncs from the live config.

    Regex-only — no PyYAML dependency (CI doesn't have PyYAML;
    Bit 2.3 R2 lesson). Locates the `paths:` block under `on.push`
    and asserts `bot/constants.py` appears as a list entry.
    """
    import re

    yml_path = REPO_ROOT / ".github" / "workflows" / "whitepaper.yml"
    text = yml_path.read_text()

    # Find the `paths:` block (under `on.push.paths`) and pull its
    # `- '<entry>'` entries. The block ends at the next less-indented key.
    # The whitepaper.yml file structure is `on:\n  push:\n    paths:\n
    # - '...'\n      - '...'`. Extract everything from `paths:` to the
    # next non-list line.
    m = re.search(
        r"^\s*paths:\s*\n((?:\s*-\s*['\"]?[^\n]+['\"]?\n)+)",
        text,
        re.MULTILINE,
    )
    assert m, (
        f"{yml_path.relative_to(REPO_ROOT)}: could not find `paths:` "
        "block under `on.push`. YAML structure regression."
    )
    paths_block = m.group(1)
    paths = re.findall(r"-\s*['\"]([^'\"]+)['\"]", paths_block)
    assert "bot/constants.py" in paths, (
        f"{yml_path.relative_to(REPO_ROOT)}: `on.push.paths` filter must "
        "include 'bot/constants.py' so whitepaper regen triggers on "
        "constant changes. Current paths: " + ", ".join(paths)
    )


# ─── 10. Makefile ast-check covers both files ──────────────────────────────


def test_makefile_ast_check_covers_constants_py():
    """`make ast-check` is documented in CLAUDE.md (sacred-file rule)
    and invoked by .github/workflows/deploy.yml as a pre-deploy syntax
    gate. Pre-Bit-3.1 it ran `ast.parse(open('bot/_impl.py').read())`.
    After Bit 3.1, bot/constants.py is equally load-bearing — a syntax
    error there crashes bot start. ast-check must cover BOTH files.
    """
    text = (REPO_ROOT / "Makefile").read_text()
    # Find the ast-check recipe block. Recipe lines are tab-indented and
    # follow the `ast-check:` target line until the next non-tab line.
    lines = text.splitlines()
    recipe = []
    in_recipe = False
    for ln in lines:
        if ln.strip().startswith("ast-check:"):
            in_recipe = True
            continue
        if in_recipe:
            if ln.startswith("\t"):
                recipe.append(ln)
            elif ln.strip() == "":
                continue  # blank line inside recipe is allowed
            else:
                break  # next target reached

    recipe_text = "\n".join(recipe)
    # Bit 9.3-iii.c (2026-05-11): bot/_impl.py DELETED. ast-check now targets
    # bot/constants.py + bot/main_loop.py + bot/scanner/__init__.py.
    assert "bot/constants.py" in recipe_text, (
        "Makefile ast-check recipe must syntax-check bot/constants.py "
        "post-Bit-3.1. Without this, a constants.py syntax error escapes "
        "the pre-deploy gate and crashes the bot at boot.\n"
        f"Current recipe:\n{recipe_text}"
    )
    assert "bot/main_loop.py" in recipe_text, (
        "Makefile ast-check recipe must syntax-check bot/main_loop.py "
        "post-Bit-9.3. Without this, a main_loop.py syntax error escapes "
        "the pre-deploy gate.\n"
        f"Current recipe:\n{recipe_text}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
