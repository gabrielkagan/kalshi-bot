"""D1.1 — collector/ scaffolding contract (ticket 86b9ypn49, 2026-05-16).

The Data Corpus initiative (D0.3, kb/decisions/data-corpus-architecture.md
§5 + §6 + §10) locks a STRUCTURAL bot-isolation contract:

    > collector failure  ⇒  bot keeps trading
    > bot failure        ⇒  collector keeps capturing

Mechanism #5 of the §10 isolation table is "Zero ``bot.*`` imports in
``collector/`` (import-linter forbidden contract — see §5)". This file
pins that mechanism at the contract layer:

  1. ``.importlinter`` uses the PLURAL ``root_packages = bot, collector``
     form (singular ``root_package = bot`` would mean grimp does NOT walk
     collector/ at all — the forbidden contract would silently no-op).
  2. ``[importlinter:contract:collector-no-bot]`` exists with
     ``type=forbidden``, ``source_modules=collector``,
     ``forbidden_modules=bot``.
  3. AST defense-in-depth: ``collector/**/*.py`` has zero
     ``from bot[.X] import …`` or ``import bot[.X]`` statements at any
     scope (top-level OR function-body).
  4. The 9 scaffolded files exist
     (``__init__.py`` + ``__main__.py`` + 7 submodules per D0.3 §5).
  5. ``collector-start.sh`` stub exists (D1.5 systemd unit will exec it).
  6. The orchestrator-pin in ``tests/contracts/test_import_linter_contracts.py``
     ``EXPECTED_CONTRACTS`` lists ``collector-no-bot`` — so a future
     rename of the contract id fails loudly at the orchestrator layer too.
  7. Mutation: inject ``from bot.constants import DB_PATH`` into a stub
     copy of ``collector/main_loop.py`` and assert ``lint-imports`` exits
     non-zero with the contract id ``collector-no-bot`` in the output.

Companion to ``tests/contracts/test_import_linter_contracts.py`` (Pillar
2). That file pins the bot/-side surface (helpers-leaf coverage walk,
state-no-impl-toplevel retirement, lint-imports CI wiring). This file
pins the collector/-side: scaffolding presence + the structural
bot-isolation contract that makes off-switch in EITHER direction safe.

If this test fails:
- Intentional collector/ rename or reshape: update the FILES list +
  any sister contract section names in lock-step with .importlinter.
- ``lint-imports`` returns non-zero in the mutation test: GOOD — that's
  what the contract is for. The negative-injection harness is the
  ratchet.
"""
from __future__ import annotations

import ast
import configparser
import os
import shutil
import site
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
IMPORTLINTER_PATH = REPO_ROOT / ".importlinter"
COLLECTOR_DIR = REPO_ROOT / "collector"
COLLECTOR_START_SH = REPO_ROOT / "collector-start.sh"
CONTRACTS_TEST = REPO_ROOT / "tests" / "contracts" / "test_import_linter_contracts.py"

# Per D0.3 §5 source-tree shape — 7 submodules + __init__ + __main__.
EXPECTED_SUBMODULES = (
    "__init__.py",
    "__main__.py",
    "main_loop.py",
    "ws_connection.py",
    "rest_snapshot.py",
    "writer.py",
    "uploader.py",
    "subscription_manager.py",
    # D1.1.5 Phase 4 (2026-05-16, ticket 86b9zdhz2): collector/auth.py
    # was DELETED — auth now flows through kalshi_wire.auth per the
    # 2026-05-16 §5 AMENDMENT ("two sides of the same coin"). The
    # scaffolding source-tree shape goes from 9 → 8 files.
)


# ─── Config helpers (mirror the orchestrator file's pattern) ─────────────────


def _read_importlinter() -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser()
    parsed = cp.read(IMPORTLINTER_PATH)
    assert parsed, f".importlinter at {IMPORTLINTER_PATH} is missing or unreadable."
    return cp


def _multiline_values(cp: configparser.RawConfigParser, section: str, key: str) -> list[str]:
    raw = cp.get(section, key, fallback="")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _lint_imports_cmd() -> list[str] | None:
    """Locate ``lint-imports``; mirror of the orchestrator helper.

    Replicated inline (rather than imported from
    test_import_linter_contracts) so the two contract tests stay
    structurally independent — refactoring the orchestrator helper
    cannot accidentally break this regression.
    """
    binary = shutil.which("lint-imports")
    if binary:
        return [binary]
    try:
        import importlinter  # noqa: F401
    except ImportError:
        return None
    candidates = [
        Path(sys.prefix) / "bin" / "lint-imports",
        Path(site.getuserbase()) / "bin" / "lint-imports",
        Path(sysconfig.get_path("scripts")) / "lint-imports",
    ]
    for cand in candidates:
        if cand.exists():
            return [str(cand)]
    return None


def _require_lint_imports() -> list[str]:
    cmd = _lint_imports_cmd()
    if cmd is not None:
        return cmd
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        pytest.fail(
            "lint-imports binary not on PATH despite CI environment. "
            "Verify `pip install -e '.[dev]'` ran successfully; the "
            "live-behavior gate cannot silently skip in CI."
        )
    pytest.skip("lint-imports binary not on PATH; install via `pip install -e .[dev]`")


# ─── 1. Scaffolding presence ─────────────────────────────────────────────────


def test_collector_dir_exists():
    """``collector/`` is a top-level sibling to ``bot/``.

    Per D0.3 §5: separate Python package, NOT a subpackage of bot/. The
    isolation contract starts at the source tree.
    """
    assert COLLECTOR_DIR.is_dir(), (
        f"{COLLECTOR_DIR} missing. D1.1 (ticket 86b9ypn49) scaffolds the "
        "collector/ top-level dir per kb/decisions/data-corpus-architecture.md §5."
    )


@pytest.mark.parametrize("filename", EXPECTED_SUBMODULES)
def test_collector_scaffolded_files_exist(filename: str):
    """Each of the 8 expected files exists per D0.3 §5 source-tree shape
    (down from 9 post-D1.1.5's auth.py deletion).

    D1.2 (2026-05-16, ticket `86b9ypn66`) shipped writer.py + uploader.py
    + main_loop.py + ws_connection.py bodies. Remaining bodies land at
    D1.3 (subscription_manager) + D1.4 (rest_snapshot) + D1.5 (systemd
    wiring); canonical mapping in ``collector/__init__.py``'s docstring.
    """
    path = COLLECTOR_DIR / filename
    assert path.is_file(), (
        f"{path} missing. D0.3 §5 source-tree shape locks 6 submodules + "
        "__init__.py + __main__.py post-D1.1.5; remaining stub bodies "
        "(subscription_manager, rest_snapshot) land at D1.3-D1.4."
    )


def test_collector_start_sh_exists():
    """``collector-start.sh`` exists as the shell wrapper stub.

    D1.5 (systemd unit, requires-approval) wires
    ``ExecStart=/home/botuser/kalshi-bot-repo/collector-start.sh`` per
    D0.3 §6 unit spec. The stub lands at D1.1 so the path-shape is
    locked before D1.5 — no surprise rename mid-deploy.
    """
    assert COLLECTOR_START_SH.is_file(), (
        f"{COLLECTOR_START_SH} missing. D0.3 §6 locks `collector-start.sh` "
        "as the systemd ExecStart target; D1.5 wires the unit."
    )


# ─── 2. .importlinter shape: plural root_packages + new contract ─────────────


def test_importlinter_root_packages_is_plural_form():
    """``.importlinter`` uses the PLURAL ``root_packages = bot, collector`` form.

    The singular ``root_package = bot`` form (pre-D1.1) means grimp's
    import graph does NOT include collector/ modules at all — the
    forbidden contract below would silently no-op (no source modules
    to scan = no violations to find). The configparser-level check
    fails fast if a future edit reverts to the singular form.

    D0.3 §5 (and the D1.1 pickup prompt) make this explicit:

      > D1.1 must change the config shape from ``root_package = bot``
      > (singular) to ``root_packages =`` (plural) listing both ``bot``
      > and ``collector``.
    """
    cp = _read_importlinter()
    assert cp.has_section("importlinter"), "missing top-level [importlinter] section"
    # Singular MUST be gone (defense-in-depth — import-linter accepts
    # either key but listing only the singular would scope grimp to bot/).
    assert not cp.has_option("importlinter", "root_package"), (
        ".importlinter still has the SINGULAR `root_package =` key. D1.1 "
        "(86b9ypn49) flipped to plural `root_packages = bot, collector` so "
        "grimp's import graph includes collector/ — the collector-no-bot "
        "contract no-ops silently otherwise."
    )
    listed = set(_multiline_values(cp, "importlinter", "root_packages"))
    assert "bot" in listed, (
        f"[importlinter].root_packages must include `bot` — found {sorted(listed)}."
    )
    assert "collector" in listed, (
        f"[importlinter].root_packages must include `collector` — found "
        f"{sorted(listed)}. D0.3 §5 locks the plural form."
    )


def test_importlinter_declares_collector_no_bot_contract():
    """``[importlinter:contract:collector-no-bot]`` exists and is correctly typed."""
    cp = _read_importlinter()
    section = "importlinter:contract:collector-no-bot"
    assert cp.has_section(section), (
        f"missing [{section}] section. D0.3 §5 + §10 mechanism #5 lock "
        "this contract as the structural enforcement of zero bot.* "
        "imports in collector/."
    )
    assert cp.get(section, "type", fallback=None) == "forbidden", (
        "contract collector-no-bot: type must be `forbidden`; the "
        "negative-injection mutation test below depends on the forbidden "
        "semantics (any source→forbidden edge fails the contract)."
    )
    sources = set(_multiline_values(cp, section, "source_modules"))
    forbidden = set(_multiline_values(cp, section, "forbidden_modules"))
    assert sources == {"collector"}, (
        f"contract collector-no-bot: source_modules must be exactly "
        f"{{collector}}; got {sources}. Listing the whole package walks "
        "every submodule in the import graph."
    )
    assert forbidden == {"bot"}, (
        f"contract collector-no-bot: forbidden_modules must be exactly "
        f"{{bot}}; got {forbidden}. Listing just `bot` covers every "
        "bot.X subpackage via grimp's package-tree resolution."
    )


def test_orchestrator_expected_contracts_lists_collector_no_bot():
    """Peer-pin: ``EXPECTED_CONTRACTS`` in
    ``tests/contracts/test_import_linter_contracts.py`` includes
    ``collector-no-bot``.

    The orchestrator file owns the parametrized contract-shape pins
    (``type=forbidden`` etc.) — if someone renames the contract id
    here but forgets to update the orchestrator tuple, the
    parametrized pin would silently skip the new id.

    Substring scan is intentional (text-level, not import-time) so this
    test stays independent of the orchestrator's module-load state.
    """
    src = CONTRACTS_TEST.read_text()
    assert '"collector-no-bot"' in src, (
        f"{CONTRACTS_TEST} EXPECTED_CONTRACTS tuple does NOT list "
        "`collector-no-bot`. Add it in the same Bit that ships the "
        "contract — the parametrized shape pins (type=forbidden, etc.) "
        "skip otherwise."
    )


# ─── 3. AST defense-in-depth — zero bot.* imports under collector/ ───────────


def _walk_collector_py_files():
    if not COLLECTOR_DIR.is_dir():
        return
    for path in sorted(COLLECTOR_DIR.rglob("*.py")):
        # iCloud-conflict filter (L93 — Sprint 10 fu 86b9vr5hu); a
        # `collector/main_loop 2.py` is NOT a real module and should be
        # silently skipped, not analyzed.
        if " " in path.name:
            continue
        yield path


def test_no_collector_module_imports_bot():
    """AST defense-in-depth: zero ``from bot[.X] import …`` or
    ``import bot[.X]`` statements anywhere under ``collector/`` (at any
    scope — top-level OR function-body, since import-linter's
    ``forbidden`` semantics already cover both).

    The AST walk is the second layer of the same claim — if
    ``lint-imports`` is uninstalled in a dev env (and the live-behavior
    test in the orchestrator file skips), this pure-Python walk still
    fires.
    """
    offenders: list[str] = []
    for path in _walk_collector_py_files():
        src = path.read_text()
        if not src.strip():
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            pytest.fail(f"{path} has SyntaxError: {exc}")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "bot" or mod.startswith("bot."):
                    offenders.append(f"{path}:{node.lineno} from {mod} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "bot" or alias.name.startswith("bot."):
                        offenders.append(f"{path}:{node.lineno} import {alias.name}")
    assert not offenders, (
        "collector/ contains forbidden bot.* imports:\n  " +
        "\n  ".join(offenders) +
        "\nD0.3 §10 mechanism #5 + §5 lock zero bot.* imports in collector/. "
        "If the import is genuinely required (e.g. shared utility), "
        "either (a) extract the utility to a new shared `kalshi_auth/` "
        "or equivalent top-level package per D0.3 §5 paragraph 6, or "
        "(b) duplicate the minimum (D0.3's explicit decline of cross-"
        "package coupling in v1)."
    )


# ─── 4. collector/__main__.py shim discipline (mirror bot/__main__.py) ──────


def test_collector_main_module_invokes_main_loop():
    """``collector/__main__.py`` is an entrypoint SHIM — it dispatches into
    ``collector/main_loop.py`` and contains no business logic.

    Mirrors the bot/__main__.py sacred-boundary rule per root CLAUDE.md.
    D1.1 shipped this shim as scaffolding; D1.2 (2026-05-16, ticket
    `86b9ypn66`) wired the underlying ``main_loop.run()`` body. The
    entrypoint pattern is locked here regardless of which body is
    present.

    Negative form: __main__.py must NOT define classes or top-level
    business logic (assignment patterns OK for `if __name__ == "__main__":`).
    """
    main_path = COLLECTOR_DIR / "__main__.py"
    if not main_path.is_file():
        pytest.skip("collector/__main__.py missing — covered by the scaffolding test")
    src = main_path.read_text()
    tree = ast.parse(src)
    for node in tree.body:
        # Top-level class definitions or function definitions other than
        # nothing-special are body-content, not entrypoint scaffolding.
        if isinstance(node, ast.ClassDef):
            pytest.fail(
                f"collector/__main__.py defines class {node.name!r} at line "
                f"{node.lineno}. Per the bot/__main__.py sacred-boundary rule "
                "(root CLAUDE.md), entrypoint shims contain no business logic. "
                "Move {node.name} to a collector submodule (collector/main_loop.py "
                "or sibling) and import it here."
            )


# ─── 5. Live behavior — mutation: injected bot.* edge MUST fail ─────────────


def test_lint_imports_fails_when_collector_imports_bot(tmp_path: Path):
    """Negative-injection: inject ``from bot.constants import DB_PATH`` into
    a stub copy of ``collector/main_loop.py`` and assert ``lint-imports``
    exits non-zero with the contract id ``collector-no-bot`` mentioned.

    The mutation is what proves the contract is structurally LOAD-BEARING
    (vs. a configparser-level pin that asserts the string exists but
    doesn't verify the linter actually catches a violation). Mirrors
    the ``test_lint_imports_fails_when_engines_to_impl_edge_re_introduced``
    pattern from the orchestrator file.
    """
    if not COLLECTOR_DIR.is_dir():
        pytest.skip("collector/ missing — covered by the scaffolding test")
    cmd = _require_lint_imports()

    fixture_root = tmp_path / "project"
    # Copy what grimp needs: bot/, collector/, kalshi_wire/, and .importlinter
    # at root. D1.1.5 (ticket 86b9zdhz2, 2026-05-16) added kalshi_wire to
    # the .importlinter `root_packages` plural list; without copying it
    # into the fixture, lint-imports errors with "Could not find package
    # 'kalshi_wire' in your Python path" before it can evaluate any
    # contract — masking the actual collector-no-bot enforcement.
    shutil.copytree(REPO_ROOT / "bot", fixture_root / "bot")
    shutil.copytree(COLLECTOR_DIR, fixture_root / "collector")
    shutil.copytree(REPO_ROOT / "kalshi_wire", fixture_root / "kalshi_wire")
    shutil.copy(IMPORTLINTER_PATH, fixture_root / ".importlinter")

    # Inject a real collector→bot import edge into main_loop.py.
    main_loop_path = fixture_root / "collector" / "main_loop.py"
    src_pre = main_loop_path.read_text()
    mutated = (
        "# D1.1 regression smoke: forbidden collector→bot edge\n"
        "from bot.constants import DB_PATH  # noqa: F401\n"
        "_REGRESSION_PROBE = DB_PATH  # noqa\n"
        + src_pre
    )
    assert mutated != src_pre, "mutation no-op — regression-probe insertion failed"
    main_loop_path.write_text(mutated)

    result = subprocess.run(
        cmd,
        cwd=fixture_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, (
        "lint-imports PASSED despite a fresh `from bot.constants import "
        "DB_PATH` at the top of collector/main_loop.py — the "
        "collector-no-bot contract is not enforced. Investigate before "
        "merging: either the contract was loosened, the root_packages "
        "list lost `collector`, or an ignore_imports entry subsumes "
        "this edge.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "collector-no-bot" in combined or "bot.constants" in combined or "collector.main_loop" in combined, (
        "lint-imports failed but neither the contract id `collector-no-bot` "
        "nor the injected edge appears in the output — failure may be "
        f"unrelated.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
