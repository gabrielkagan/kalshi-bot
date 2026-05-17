"""D2.1 — ``coinbase_wire/`` must NOT import ``collector.*`` either.

Ticket 86b9zkpc6 (2026-05-17). Sister contract to ``coinbase_wire-no-bot``.
Mirrors the D1.1.5 ``kalshi_wire-no-collector`` pattern: the wire library
is a pure-transport leaf consumed by BOTH the bot side (future
``bot/feeds/coinbase.py`` refactor in a later D2 Bit) AND the collector
side (future ``collector/coinbase_archiver.py`` at D2.2). The dependency
arrow is consumer → wire, never wire → consumer:

  bot.feeds.coinbase           ─┐
                                ├─→ coinbase_wire
  collector.coinbase_archiver   ┘

A ``coinbase_wire → collector`` edge would invert the layering, couple
the wire library to the bronze-archival pipeline, and break the symmetry
that makes "two sides of the same coin" actually work. (The symmetric
``coinbase_wire → bot`` ban is pinned by the sister test file
``test_coinbase_wire_no_bot_imports.py``.)

If this test fails:
- Intentional contract change: shouldn't happen — coinbase_wire is meant
  to be a leaf.
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
WIRE_DIR = REPO_ROOT / "coinbase_wire"
COLLECTOR_DIR = REPO_ROOT / "collector"
KALSHI_WIRE_DIR = REPO_ROOT / "kalshi_wire"
CONTRACTS_TEST = REPO_ROOT / "tests" / "contracts" / "test_import_linter_contracts.py"


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
            "Verify `pip install -e '.[dev]'` ran successfully."
        )
    pytest.skip("lint-imports binary not on PATH; install via `pip install -e .[dev]`")


# ─── 1. .importlinter shape: coinbase_wire-no-collector contract ─────────────


def test_importlinter_declares_coinbase_wire_no_collector_contract():
    """``[importlinter:contract:coinbase_wire-no-collector]`` exists and
    is correctly typed."""
    cp = _read_importlinter()
    section = "importlinter:contract:coinbase_wire-no-collector"
    assert cp.has_section(section), (
        f"missing [{section}] section. D2.1 (86b9zkpc6) ships the "
        "structural enforcement of zero collector.* imports in coinbase_wire/."
    )
    assert cp.get(section, "type", fallback=None) == "forbidden", (
        "contract coinbase_wire-no-collector: type must be `forbidden`."
    )
    sources = set(_multiline_values(cp, section, "source_modules"))
    forbidden = set(_multiline_values(cp, section, "forbidden_modules"))
    assert sources == {"coinbase_wire"}, (
        f"contract coinbase_wire-no-collector: source_modules must be exactly "
        f"{{coinbase_wire}}; got {sources}."
    )
    assert forbidden == {"collector"}, (
        f"contract coinbase_wire-no-collector: forbidden_modules must be exactly "
        f"{{collector}}; got {forbidden}."
    )


def test_orchestrator_expected_contracts_lists_coinbase_wire_no_collector():
    """Peer-pin: ``EXPECTED_CONTRACTS`` in
    ``tests/contracts/test_import_linter_contracts.py`` includes
    ``coinbase_wire-no-collector``.
    """
    src = CONTRACTS_TEST.read_text()
    assert '"coinbase_wire-no-collector"' in src, (
        f"{CONTRACTS_TEST} EXPECTED_CONTRACTS tuple does NOT list "
        "`coinbase_wire-no-collector`. Add it in the same Bit that ships."
    )


# ─── 2. AST defense-in-depth — zero collector.* imports under coinbase_wire/ ─


def _walk_wire_py_files():
    if not WIRE_DIR.is_dir():
        return
    for path in sorted(WIRE_DIR.rglob("*.py")):
        if " " in path.name:  # iCloud-conflict filter
            continue
        yield path


def test_no_coinbase_wire_module_imports_collector():
    """AST defense-in-depth: zero ``from collector[.X] import …`` or
    ``import collector[.X]`` statements anywhere under ``coinbase_wire/``.
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
                if mod == "collector" or mod.startswith("collector."):
                    offenders.append(f"{path}:{node.lineno} from {mod} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "collector" or alias.name.startswith("collector."):
                        offenders.append(f"{path}:{node.lineno} import {alias.name}")
    assert not offenders, (
        "coinbase_wire/ contains forbidden collector.* imports:\n  " +
        "\n  ".join(offenders) +
        "\nWire is the LEAF; consumer → wire only, never wire → consumer."
    )


# ─── 3. Live behavior — mutation: injected collector.* edge MUST fail ───────


def test_lint_imports_fails_when_coinbase_wire_imports_collector(tmp_path: Path):
    """Negative-injection: inject ``import collector.writer`` into a stub
    copy of ``coinbase_wire/ws_client.py`` and assert ``lint-imports``
    exits non-zero with ``coinbase_wire-no-collector`` mentioned.
    """
    if not WIRE_DIR.is_dir():
        pytest.skip("coinbase_wire/ missing — covered by the scaffolding test")
    ws_client_path = WIRE_DIR / "ws_client.py"
    if not ws_client_path.is_file():
        pytest.skip("coinbase_wire/ws_client.py missing — covered by scaffolding")
    cmd = _require_lint_imports()

    fixture_root = tmp_path / "project"
    # Mirror the kalshi_wire-no-collector test: copy every package
    # listed in .importlinter root_packages so lint-imports can find them.
    shutil.copytree(REPO_ROOT / "bot", fixture_root / "bot")
    shutil.copytree(COLLECTOR_DIR, fixture_root / "collector")
    shutil.copytree(KALSHI_WIRE_DIR, fixture_root / "kalshi_wire")
    shutil.copytree(WIRE_DIR, fixture_root / "coinbase_wire")
    shutil.copy(IMPORTLINTER_PATH, fixture_root / ".importlinter")

    mutated_path = fixture_root / "coinbase_wire" / "ws_client.py"
    src_pre = mutated_path.read_text()
    # Pick a name in collector/writer.py — post-D1.2 `BronzeWriter` is
    # the canonical writer class; the IMPORT EDGE alone trips the
    # contract regardless of class-body details.
    mutated = (
        "# D2.1 regression smoke: forbidden coinbase_wire->collector edge\n"
        "import collector.writer  # noqa: F401\n"
        "_REGRESSION_PROBE = collector.writer  # noqa\n"
        + src_pre
    )
    assert mutated != src_pre, "mutation no-op — regression-probe insertion failed"
    mutated_path.write_text(mutated)

    result = subprocess.run(
        cmd, cwd=fixture_root, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode != 0, (
        "lint-imports PASSED despite a fresh `import collector.writer` at "
        "the top of coinbase_wire/ws_client.py — the coinbase_wire-no-collector "
        "contract is not enforced.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert ("coinbase_wire-no-collector" in combined or "collector.writer" in combined
            or "coinbase_wire.ws_client" in combined), (
        "lint-imports failed but neither contract id `coinbase_wire-no-collector` "
        "nor the injected edge appears in output.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
