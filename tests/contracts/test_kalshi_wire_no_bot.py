"""D1.1.5 — ``kalshi_wire/`` is a pure-transport leaf with zero ``bot.*`` imports.

Ticket 86b9zdhz2 (2026-05-16). The 2026-05-16 amendment to D0.3 §5 introduced
``kalshi_wire/`` as a third top-level SIBLING package (peer of ``bot/`` and
``collector/``), housing RSA-PSS auth + WS connect/reconnect/subscribe
protocol + 6-field bronze envelope construction. Both ``bot/feeds/kalshi.py``
and ``collector/ws_connection.py`` consume ``kalshi_wire/``.

The isolation contract (``collector/`` cannot reach ``bot/``) is preserved
because ``kalshi_wire/`` is a sibling, not a ``bot/`` subpackage. Two new
import-linter forbidden contracts lock the third package as a pure-transport
leaf:

  - ``kalshi_wire-no-bot``       — this file's primary contract
  - ``kalshi_wire-no-collector`` — peer contract pinned by sister test

If ``kalshi_wire.*`` were allowed to reach ``bot/``, the advisor's "two
sides of the same coin" symmetry would break: collector's view of the wire
would be filtered through bot-specific state machinery (orderbook cache,
schema probes, blacklist) that the data-corpus pipeline must NOT impose.

Mirrors the D1.1 ``tests/contracts/test_collector_no_bot_imports.py``
pattern: configparser-level shape pin + AST defense-in-depth + scaffolding
presence + negative-injection mutation that exercises ``lint-imports``.

If this test fails:
- Intentional ``kalshi_wire/`` rename or reshape: update the FILES list +
  any sister contract section names in lock-step with ``.importlinter``.
- ``lint-imports`` returns non-zero in the mutation test: GOOD — that's
  what the contract is for.
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
WIRE_DIR = REPO_ROOT / "kalshi_wire"
CONTRACTS_TEST = REPO_ROOT / "tests" / "contracts" / "test_import_linter_contracts.py"

# Per D1.1.5 pickup prompt: at minimum __init__ + auth + ws_client.
# envelope.py is optional (may fold into ws_client.py per pickup prompt
# L34 "fold into ws_client.py if natural").
REQUIRED_SUBMODULES = (
    "__init__.py",
    "auth.py",
    "ws_client.py",
)


def _read_importlinter() -> configparser.RawConfigParser:
    cp = configparser.RawConfigParser()
    parsed = cp.read(IMPORTLINTER_PATH)
    assert parsed, f".importlinter at {IMPORTLINTER_PATH} is missing or unreadable."
    return cp


def _multiline_values(cp: configparser.RawConfigParser, section: str, key: str) -> list[str]:
    raw = cp.get(section, key, fallback="")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _lint_imports_cmd() -> list[str] | None:
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


def test_kalshi_wire_dir_exists():
    """``kalshi_wire/`` is a top-level sibling to ``bot/`` and ``collector/``.

    Per the D0.3 2026-05-16 AMENDMENT (§5 ¶6+7 SUPERSEDED): separate Python
    package, NOT a subpackage of bot/ or collector/. The isolation contract
    starts at the source tree.
    """
    assert WIRE_DIR.is_dir(), (
        f"{WIRE_DIR} missing. D1.1.5 (ticket 86b9zdhz2) creates the "
        "kalshi_wire/ top-level dir per kb/decisions/data-corpus-architecture.md "
        "§5 AMENDMENT 2026-05-16."
    )


@pytest.mark.parametrize("filename", REQUIRED_SUBMODULES)
def test_kalshi_wire_scaffolded_files_exist(filename: str):
    """Each required submodule exists.

    ``envelope.py`` is intentionally NOT in REQUIRED_SUBMODULES — the
    pickup prompt allows folding envelope construction into ws_client.py.
    Add envelope.py to the parametrize tuple if it's broken out.
    """
    path = WIRE_DIR / filename
    assert path.is_file(), (
        f"{path} missing. D1.1.5 source-tree shape locks __init__.py + "
        "auth.py + ws_client.py at minimum (envelope optional, may fold "
        "into ws_client.py per pickup prompt)."
    )


# ─── 2. .importlinter shape: kalshi_wire in root_packages + 2 new contracts ──


def test_importlinter_root_packages_includes_kalshi_wire():
    """``.importlinter`` ``root_packages`` includes ``kalshi_wire`` so grimp
    walks the new package and the two ``kalshi_wire-no-*`` forbidden
    contracts have source-modules to scan.

    Without the addition, grimp's import graph would NOT include
    ``kalshi_wire/`` and the two new forbidden contracts would silently
    no-op (no edges to forbid because grimp wouldn't see kalshi_wire/
    at all). Same defect class as the D1.1 ``collector`` plural-flip.
    """
    cp = _read_importlinter()
    assert cp.has_section("importlinter"), "missing top-level [importlinter] section"
    listed = set(_multiline_values(cp, "importlinter", "root_packages"))
    assert "kalshi_wire" in listed, (
        f"[importlinter].root_packages must include `kalshi_wire` — "
        f"found {sorted(listed)}. D1.1.5 (86b9zdhz2) extends the plural "
        "list to admit the new sibling package."
    )


def test_importlinter_declares_kalshi_wire_no_bot_contract():
    """``[importlinter:contract:kalshi_wire-no-bot]`` exists and is correctly typed."""
    cp = _read_importlinter()
    section = "importlinter:contract:kalshi_wire-no-bot"
    assert cp.has_section(section), (
        f"missing [{section}] section. D1.1.5 (86b9zdhz2) ships the "
        "structural enforcement of zero bot.* imports in kalshi_wire/."
    )
    assert cp.get(section, "type", fallback=None) == "forbidden", (
        "contract kalshi_wire-no-bot: type must be `forbidden`."
    )
    sources = set(_multiline_values(cp, section, "source_modules"))
    forbidden = set(_multiline_values(cp, section, "forbidden_modules"))
    assert sources == {"kalshi_wire"}, (
        f"contract kalshi_wire-no-bot: source_modules must be exactly "
        f"{{kalshi_wire}}; got {sources}."
    )
    assert forbidden == {"bot"}, (
        f"contract kalshi_wire-no-bot: forbidden_modules must be exactly "
        f"{{bot}}; got {forbidden}. Listing just `bot` covers every "
        "bot.X subpackage via grimp's package-tree resolution."
    )


def test_orchestrator_expected_contracts_lists_kalshi_wire_no_bot():
    """Peer-pin: ``EXPECTED_CONTRACTS`` in
    ``tests/contracts/test_import_linter_contracts.py`` includes
    ``kalshi_wire-no-bot``.
    """
    src = CONTRACTS_TEST.read_text()
    assert '"kalshi_wire-no-bot"' in src, (
        f"{CONTRACTS_TEST} EXPECTED_CONTRACTS tuple does NOT list "
        "`kalshi_wire-no-bot`. Add it in the same Bit that ships the "
        "contract."
    )


# ─── 3. AST defense-in-depth — zero bot.* imports under kalshi_wire/ ─────────


def _walk_wire_py_files():
    if not WIRE_DIR.is_dir():
        return
    for path in sorted(WIRE_DIR.rglob("*.py")):
        if " " in path.name:  # iCloud-conflict filter
            continue
        yield path


def test_no_kalshi_wire_module_imports_bot():
    """AST defense-in-depth: zero ``from bot[.X] import …`` or
    ``import bot[.X]`` statements anywhere under ``kalshi_wire/``.
    """
    offenders: list[str] = []
    for path in _walk_wire_py_files():
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
        "kalshi_wire/ contains forbidden bot.* imports:\n  " +
        "\n  ".join(offenders) +
        "\nD1.1.5 §5 AMENDMENT locks kalshi_wire as a pure-transport leaf. "
        "The advisor's 'two sides of the same coin' symmetry requires that "
        "the wire layer is bot-state-free; reaching into bot/ would couple "
        "collector's view to bot's orderbook cache / schema probes / blacklist."
    )


# ─── 4. Live behavior — mutation: injected bot.* edge MUST fail ──────────────


def test_lint_imports_fails_when_kalshi_wire_imports_bot(tmp_path: Path):
    """Negative-injection: inject ``from bot.constants import KALSHI_WS_URL``
    into a stub copy of ``kalshi_wire/ws_client.py`` and assert
    ``lint-imports`` exits non-zero with ``kalshi_wire-no-bot`` mentioned.
    """
    if not WIRE_DIR.is_dir():
        pytest.skip("kalshi_wire/ missing — covered by the scaffolding test")
    ws_client_path = WIRE_DIR / "ws_client.py"
    if not ws_client_path.is_file():
        pytest.skip("kalshi_wire/ws_client.py missing — covered by scaffolding")
    cmd = _require_lint_imports()

    fixture_root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "bot", fixture_root / "bot")
    shutil.copytree(REPO_ROOT / "collector", fixture_root / "collector")
    shutil.copytree(WIRE_DIR, fixture_root / "kalshi_wire")
    # D2.1 (ticket 86b9zkpc6, 2026-05-17): coinbase_wire/ joined the
    # .importlinter `root_packages` plural list; without copying it
    # into the fixture, lint-imports errors with "Could not find
    # package 'coinbase_wire' in your Python path" before it can
    # evaluate any contract — masking the actual kalshi_wire-no-bot
    # enforcement. Same defect class the D1.1.5 add did for
    # kalshi_wire in tests/contracts/test_collector_no_bot_imports.py.
    shutil.copytree(REPO_ROOT / "coinbase_wire", fixture_root / "coinbase_wire")
    # D3.0 (86b9zxc6t, 2026-05-18) — silver joined .importlinter root_packages.
    shutil.copytree(REPO_ROOT / "silver", fixture_root / "silver")
    shutil.copy(IMPORTLINTER_PATH, fixture_root / ".importlinter")

    mutated_path = fixture_root / "kalshi_wire" / "ws_client.py"
    src_pre = mutated_path.read_text()
    mutated = (
        "# D1.1.5 regression smoke: forbidden kalshi_wire->bot edge\n"
        "from bot.constants import KALSHI_WS_URL  # noqa: F401\n"
        "_REGRESSION_PROBE = KALSHI_WS_URL  # noqa\n"
        + src_pre
    )
    assert mutated != src_pre, "mutation no-op — regression-probe insertion failed"
    mutated_path.write_text(mutated)

    result = subprocess.run(
        cmd, cwd=fixture_root, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0, (
        "lint-imports PASSED despite a fresh `from bot.constants import "
        "KALSHI_WS_URL` at the top of kalshi_wire/ws_client.py — the "
        "kalshi_wire-no-bot contract is not enforced.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert ("kalshi_wire-no-bot" in combined or "bot.constants" in combined
            or "kalshi_wire.ws_client" in combined), (
        "lint-imports failed but neither contract id `kalshi_wire-no-bot` "
        "nor the injected edge appears in output.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
