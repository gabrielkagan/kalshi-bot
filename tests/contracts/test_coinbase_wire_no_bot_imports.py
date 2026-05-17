"""D2.1 / D2.1.5 — ``coinbase_wire/`` is a pure-transport leaf with zero ``bot.*`` imports.

Ticket 86b9zkpc6 (2026-05-17, D2.1 scaffolding) + 86b9zkpny (2026-05-17,
D2.1.5 body) — sub-Bits of the 86b9zkkv4 D2.x Coinbase WS bronzing
umbrella. Mirrors the D1.1.5 ``kalshi_wire/`` pattern for the Coinbase
wire surface, with the same "two sides of the same coin" symmetry between
the trading bot's spot feed and the Coinbase bronze archiver
(``collector/coinbase_archiver.py``, D2.2 SHIPPED 2026-05-17, ticket
86b9zkppk).

D2.1 shipped scaffolding (empty stub modules + docstrings + the test
guards in this file); D2.1.5 lands the bodies (public subscribe helper +
WSClient + Frame + build_envelope). The collector-side
``coinbase_archiver.py`` SHIPPED at D2.2 (ticket 86b9zkppk,
2026-05-17). This file pins the structural isolation contracts so any
current or future implementer cannot accidentally couple the wire layer
to bot-side state.

Two new import-linter forbidden contracts lock the third sibling wire
package (after ``kalshi_wire``) as a pure-transport leaf:

  - ``coinbase_wire-no-bot``       — this file's primary contract
  - ``coinbase_wire-no-collector`` — peer contract pinned by sister test
    (``test_coinbase_wire_no_collector.py``)

If ``coinbase_wire.*`` were allowed to reach ``bot/``, the same defect
class kalshi_wire avoids would land: collector's view of the Coinbase
wire would be filtered through bot-specific machinery (cross-exchange
feed cache, blacklist, schema probes). The wire layer must be bot-state-
free so the bronze tape captured by collector and the live decisions made
by bot agree on what came over the wire.

Mirrors ``tests/contracts/test_kalshi_wire_no_bot.py`` (the D1.1.5
ancestor) byte-for-byte in structure: configparser-level shape pin + AST
defense-in-depth + scaffolding presence + negative-injection mutation
that exercises ``lint-imports``.

If this test fails:
- Intentional ``coinbase_wire/`` rename or reshape: update the FILES
  list + any sister contract section names in lock-step with
  ``.importlinter``.
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
WIRE_DIR = REPO_ROOT / "coinbase_wire"
CONTRACTS_TEST = REPO_ROOT / "tests" / "contracts" / "test_import_linter_contracts.py"

# D2.1 scaffolding shape: __init__ + auth + ws_client (mirrors the
# D1.1.5 kalshi_wire/ minimum). Bodies are stubs at D2.1; D2.1.5 lands
# the implementations. envelope.py is intentionally NOT required — same
# convention as kalshi_wire (may fold into ws_client.py if natural).
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


def test_coinbase_wire_dir_exists():
    """``coinbase_wire/`` is a top-level sibling to ``bot/``, ``collector/``,
    and ``kalshi_wire/``.

    Per D2.1 (ticket 86b9zkpc6, sub-Bit of D2.x umbrella 86b9zkkv4):
    separate Python package, NOT a subpackage of bot/ or collector/.
    The isolation contract starts at the source tree.
    """
    assert WIRE_DIR.is_dir(), (
        f"{WIRE_DIR} missing. D2.1 (ticket 86b9zkpc6) creates the "
        "coinbase_wire/ top-level dir mirroring the D1.1.5 kalshi_wire "
        "scaffolding pattern."
    )


@pytest.mark.parametrize("filename", REQUIRED_SUBMODULES)
def test_coinbase_wire_scaffolded_files_exist(filename: str):
    """Each required submodule exists.

    D2.1 ships ``__init__.py`` + ``auth.py`` + ``ws_client.py`` as
    empty stubs (docstrings only). D2.1.5 lands the bodies (public-
    subscribe helper + WSClient + Frame + build_envelope). Add
    ``envelope.py`` to REQUIRED_SUBMODULES later if it's broken out
    instead of folded into ``ws_client.py``.
    """
    path = WIRE_DIR / filename
    assert path.is_file(), (
        f"{path} missing. D2.1 source-tree shape locks __init__.py + "
        "auth.py + ws_client.py at minimum (mirrors kalshi_wire D1.1.5)."
    )


# ─── 2. .importlinter shape: coinbase_wire in root_packages + new contract ──


def test_importlinter_root_packages_includes_coinbase_wire():
    """``.importlinter`` ``root_packages`` includes ``coinbase_wire`` so
    grimp walks the new package and the two ``coinbase_wire-no-*``
    forbidden contracts have source-modules to scan.

    Same defect class as the D1.1 ``collector`` plural-flip and the
    D1.1.5 ``kalshi_wire`` add: without listing the new package in
    ``root_packages``, grimp's import graph does NOT include it and
    the forbidden contracts silently no-op (no edges to forbid).
    """
    cp = _read_importlinter()
    assert cp.has_section("importlinter"), "missing top-level [importlinter] section"
    listed = set(_multiline_values(cp, "importlinter", "root_packages"))
    assert "coinbase_wire" in listed, (
        f"[importlinter].root_packages must include `coinbase_wire` — "
        f"found {sorted(listed)}. D2.1 (86b9zkpc6) extends the plural "
        "list to admit the new sibling package."
    )


def test_importlinter_declares_coinbase_wire_no_bot_contract():
    """``[importlinter:contract:coinbase_wire-no-bot]`` exists and is correctly typed."""
    cp = _read_importlinter()
    section = "importlinter:contract:coinbase_wire-no-bot"
    assert cp.has_section(section), (
        f"missing [{section}] section. D2.1 (86b9zkpc6) ships the "
        "structural enforcement of zero bot.* imports in coinbase_wire/."
    )
    assert cp.get(section, "type", fallback=None) == "forbidden", (
        "contract coinbase_wire-no-bot: type must be `forbidden`."
    )
    sources = set(_multiline_values(cp, section, "source_modules"))
    forbidden = set(_multiline_values(cp, section, "forbidden_modules"))
    assert sources == {"coinbase_wire"}, (
        f"contract coinbase_wire-no-bot: source_modules must be exactly "
        f"{{coinbase_wire}}; got {sources}."
    )
    assert forbidden == {"bot"}, (
        f"contract coinbase_wire-no-bot: forbidden_modules must be exactly "
        f"{{bot}}; got {forbidden}. Listing just `bot` covers every "
        "bot.X subpackage via grimp's package-tree resolution."
    )


def test_orchestrator_expected_contracts_lists_coinbase_wire_no_bot():
    """Peer-pin: ``EXPECTED_CONTRACTS`` in
    ``tests/contracts/test_import_linter_contracts.py`` includes
    ``coinbase_wire-no-bot``.

    The orchestrator file owns the parametrized contract-shape pins
    (``type=forbidden`` etc.) — if someone renames the contract id
    here but forgets to update the orchestrator tuple, the
    parametrized pin would silently skip the new id.
    """
    src = CONTRACTS_TEST.read_text()
    assert '"coinbase_wire-no-bot"' in src, (
        f"{CONTRACTS_TEST} EXPECTED_CONTRACTS tuple does NOT list "
        "`coinbase_wire-no-bot`. Add it in the same Bit that ships the "
        "contract."
    )


# ─── 3. AST defense-in-depth — zero bot.* imports under coinbase_wire/ ───────


def _walk_wire_py_files():
    if not WIRE_DIR.is_dir():
        return
    for path in sorted(WIRE_DIR.rglob("*.py")):
        # iCloud-conflict filter (L93 — Sprint 10 fu 86b9vr5hu); a
        # `coinbase_wire/auth 2.py` is NOT a real module and should be
        # silently skipped, not analyzed.
        if " " in path.name:
            continue
        yield path


def test_no_coinbase_wire_module_imports_bot():
    """AST defense-in-depth: zero ``from bot[.X] import …`` or
    ``import bot[.X]`` statements anywhere under ``coinbase_wire/`` (at
    any scope — top-level OR function-body, since import-linter's
    ``forbidden`` semantics already cover both).

    The AST walk is the second layer of the same claim — if
    ``lint-imports`` is uninstalled in a dev env (and the live-behavior
    test in the orchestrator file skips), this pure-Python walk still
    fires.
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
        "coinbase_wire/ contains forbidden bot.* imports:\n  " +
        "\n  ".join(offenders) +
        "\nD2.1 locks coinbase_wire as a pure-transport leaf mirroring "
        "kalshi_wire's 'two sides of the same coin' invariant. The wire "
        "layer must be bot-state-free; reaching into bot/ would couple "
        "collector's view of the Coinbase wire to bot-specific machinery."
    )


# ─── 4. Live behavior — mutation: injected bot.* edge MUST fail ──────────────


def test_lint_imports_fails_when_coinbase_wire_imports_bot(tmp_path: Path):
    """Negative-injection: inject ``from bot.constants import KALSHI_WS_URL``
    into a stub copy of ``coinbase_wire/ws_client.py`` and assert
    ``lint-imports`` exits non-zero with ``coinbase_wire-no-bot`` mentioned.

    The mutation is what proves the contract is structurally LOAD-BEARING
    (vs. a configparser-level pin that asserts the string exists but
    doesn't verify the linter actually catches a violation). Mirrors
    the D1.1.5 ``test_lint_imports_fails_when_kalshi_wire_imports_bot``
    pattern.
    """
    if not WIRE_DIR.is_dir():
        pytest.skip("coinbase_wire/ missing — covered by the scaffolding test")
    ws_client_path = WIRE_DIR / "ws_client.py"
    if not ws_client_path.is_file():
        pytest.skip("coinbase_wire/ws_client.py missing — covered by scaffolding")
    cmd = _require_lint_imports()

    fixture_root = tmp_path / "project"
    # Copy what grimp needs: bot/, collector/, kalshi_wire/, coinbase_wire/,
    # and .importlinter at root. Without copying every package listed in
    # .importlinter root_packages, lint-imports errors with "Could not
    # find package 'X' in your Python path" before it can evaluate any
    # contract — masking the actual coinbase_wire-no-bot enforcement.
    shutil.copytree(REPO_ROOT / "bot", fixture_root / "bot")
    shutil.copytree(REPO_ROOT / "collector", fixture_root / "collector")
    shutil.copytree(REPO_ROOT / "kalshi_wire", fixture_root / "kalshi_wire")
    shutil.copytree(WIRE_DIR, fixture_root / "coinbase_wire")
    shutil.copy(IMPORTLINTER_PATH, fixture_root / ".importlinter")

    mutated_path = fixture_root / "coinbase_wire" / "ws_client.py"
    src_pre = mutated_path.read_text()
    # Pick a name that exists in bot.constants — the IMPORT EDGE alone
    # trips the contract regardless of whether the name is actually
    # referenced. `KALSHI_WS_URL` is a stable module-level constant.
    mutated = (
        "# D2.1 regression smoke: forbidden coinbase_wire->bot edge\n"
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
        "KALSHI_WS_URL` at the top of coinbase_wire/ws_client.py — the "
        "coinbase_wire-no-bot contract is not enforced.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert ("coinbase_wire-no-bot" in combined or "bot.constants" in combined
            or "coinbase_wire.ws_client" in combined), (
        "lint-imports failed but neither contract id `coinbase_wire-no-bot` "
        "nor the injected edge appears in output.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
