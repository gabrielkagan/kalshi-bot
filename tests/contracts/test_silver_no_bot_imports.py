"""D3.0 — ``silver/`` is a pure off-VPS ETL surface with zero ``bot.*`` / ``collector.*`` imports.

Ticket 86b9zxc6t (2026-05-18). First Bit of Phase 2 (Silver). Mirrors the
D1.1 (collector) + D1.1.5 (kalshi_wire) + D2.1 (coinbase_wire) precedent
for sibling-package isolation. Plan doc: ``kb/decisions/d3-0-silver-foundations-plan.md``.

Silver is the dbt-style normalization layer reading bronze JSONL.zst chunks
from S3 and writing partitioned Parquet to ``s3://kalshi-bot-archive/silver/v1/<source>/``.
It runs OFF the bot VPS (Mac launchd nightly @ 02:00 local). The
isolation contract is identical to the wire / collector pattern: silver
cannot reach into bot/ or collector/ because (a) it runs on different
compute (different process, different OS), (b) the dbt-style transform
DAG must operate purely on bronze + silver state — not on trading state.

Two new import-linter forbidden contracts lock the fifth sibling package
(after bot/, collector/, kalshi_wire/, coinbase_wire/) as pure-ETL:

  - ``silver-no-bot``       — primary contract
  - ``silver-no-collector`` — peer contract

Plus a third configparser-level pin per the D1.1.5 + D2.1 precedent:
``test_importlinter_root_packages_includes_silver``. Without this,
grimp would not walk silver/ and the two forbidden contracts above would
silently no-op (the exact failure mode that R1-C4 of the plan-doc
adv-gate caught).

If this test fails:
- Intentional ``silver/`` rename or reshape: update the FILES list +
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
SILVER_DIR = REPO_ROOT / "silver"


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


# ─── 1. Scaffolding presence ────────────────────────────────────────────────


def test_silver_dir_exists():
    """``silver/`` is a top-level sibling to ``bot/``, ``collector/``,
    ``kalshi_wire/``, ``coinbase_wire/``.

    Per D3.0 (ticket 86b9zxc6t): separate top-level package, NOT a
    subpackage of any existing dir. Off-VPS ETL surface.
    """
    assert SILVER_DIR.is_dir(), (
        f"{SILVER_DIR} missing. D3.0 (86b9zxc6t) creates the "
        "silver/ top-level dir for the dbt-style silver ETL layer."
    )


def test_silver_has_init_py():
    """``silver/__init__.py`` exists so grimp can walk the package.

    Without ``__init__.py``, silver/ isn't an importable Python package
    and the importlinter contracts can't enforce its import boundary.
    """
    assert (SILVER_DIR / "__init__.py").is_file(), (
        f"{SILVER_DIR}/__init__.py missing. Required so grimp resolves "
        "silver/ as an importable package; without it the silver-no-bot "
        "+ silver-no-collector contracts have no module tree to scan."
    )


# ─── 2. .importlinter shape: silver in root_packages + new contracts ────────


def test_importlinter_root_packages_includes_silver():
    """``.importlinter`` ``root_packages`` includes ``silver`` so grimp
    walks the new package and the two ``silver-no-*`` forbidden contracts
    have source-modules to scan.

    Same defect class as the D1.1 ``collector`` plural-flip + D1.1.5
    ``kalshi_wire`` add + D2.1 ``coinbase_wire`` add: without listing
    the new package in ``root_packages``, grimp's import graph does NOT
    include it and the forbidden contracts silently no-op (no edges to
    forbid). This is R1-C4 of the plan-doc adv-gate — exact failure
    mode each prior wire-sibling Bit had to learn the hard way.
    """
    cp = _read_importlinter()
    assert cp.has_section("importlinter"), "missing top-level [importlinter] section"
    listed = set(_multiline_values(cp, "importlinter", "root_packages"))
    assert "silver" in listed, (
        f"[importlinter].root_packages must include `silver` — "
        f"found {sorted(listed)}. D3.0 (86b9zxc6t) extends the plural "
        "list to admit the new sibling package; otherwise the two new "
        "silver-no-{bot,collector} contracts silently no-op."
    )


def test_importlinter_declares_silver_no_bot_contract():
    """``[importlinter:contract:silver-no-bot]`` exists and is correctly typed."""
    cp = _read_importlinter()
    section = "importlinter:contract:silver-no-bot"
    assert cp.has_section(section), (
        f"missing [{section}] section. D3.0 (86b9zxc6t) ships the "
        "structural enforcement of zero bot.* imports in silver/."
    )
    assert cp.get(section, "type", fallback=None) == "forbidden", (
        "contract silver-no-bot: type must be `forbidden`."
    )
    sources = set(_multiline_values(cp, section, "source_modules"))
    forbidden = set(_multiline_values(cp, section, "forbidden_modules"))
    assert sources == {"silver"}, (
        f"contract silver-no-bot: source_modules must be exactly "
        f"{{silver}}; got {sources}."
    )
    assert forbidden == {"bot"}, (
        f"contract silver-no-bot: forbidden_modules must be exactly "
        f"{{bot}}; got {forbidden}. Listing just `bot` covers every "
        "bot.X subpackage via grimp's package-tree resolution."
    )


def test_importlinter_declares_silver_no_collector_contract():
    """``[importlinter:contract:silver-no-collector]`` exists and is correctly typed."""
    cp = _read_importlinter()
    section = "importlinter:contract:silver-no-collector"
    assert cp.has_section(section), (
        f"missing [{section}] section. D3.0 (86b9zxc6t) ships the "
        "structural enforcement of zero collector.* imports in silver/."
    )
    assert cp.get(section, "type", fallback=None) == "forbidden", (
        "contract silver-no-collector: type must be `forbidden`."
    )
    sources = set(_multiline_values(cp, section, "source_modules"))
    forbidden = set(_multiline_values(cp, section, "forbidden_modules"))
    assert sources == {"silver"}, (
        f"contract silver-no-collector: source_modules must be exactly "
        f"{{silver}}; got {sources}."
    )
    assert forbidden == {"collector"}, (
        f"contract silver-no-collector: forbidden_modules must be exactly "
        f"{{collector}}; got {forbidden}."
    )


# ─── 3. AST defense-in-depth — zero bot.*/collector.* imports under silver/ ─


def _walk_silver_py_files():
    if not SILVER_DIR.is_dir():
        return
    for path in sorted(SILVER_DIR.rglob("*.py")):
        # iCloud-conflict filter (L93)
        if " " in path.name:
            continue
        yield path


def test_no_silver_module_imports_bot():
    """AST defense-in-depth: zero ``from bot[.X] import …`` / ``import bot[.X]``
    anywhere under ``silver/`` at any scope.

    Second layer of the same claim — if ``lint-imports`` is uninstalled
    in a dev env, this pure-Python walk still fires.
    """
    offenders: list[str] = []
    for path in _walk_silver_py_files():
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
        "silver/ contains forbidden bot.* imports:\n  " +
        "\n  ".join(offenders) +
        "\nD3.0 locks silver as a pure off-VPS ETL surface — silver must "
        "not depend on bot trading state."
    )


def test_no_silver_module_imports_collector():
    """AST defense-in-depth: zero ``from collector[.X] import …`` /
    ``import collector[.X]`` anywhere under ``silver/`` at any scope.

    Silver reads bronze via DuckDB's S3 connector (`read_json('s3://...')`),
    NOT via collector code. The two have parallel views of bronze format
    (D0.3 §2 envelope) — that's the contract, not a shared import.
    """
    offenders: list[str] = []
    for path in _walk_silver_py_files():
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
        "silver/ contains forbidden collector.* imports:\n  " +
        "\n  ".join(offenders) +
        "\nD3.0 locks silver as a pure ETL surface — silver reads bronze "
        "via DuckDB's S3 connector, NOT via collector code."
    )


# ─── 4. Live behavior — mutation: injected bot.* edge MUST fail ─────────────


def test_lint_imports_fails_when_silver_imports_bot(tmp_path: Path):
    """Negative-injection: inject ``from bot.constants import KALSHI_WS_URL``
    into a stub copy of a silver/ Python file and assert ``lint-imports``
    exits non-zero with ``silver-no-bot`` mentioned.

    Proves the contract is structurally LOAD-BEARING (vs. a configparser-
    level pin that asserts the string exists but doesn't verify the linter
    catches a violation). Mirrors D1.1.5 + D2.1 patterns.
    """
    if not SILVER_DIR.is_dir():
        pytest.skip("silver/ missing — covered by the scaffolding test")
    # Pick a silver Python file to mutate. Default to silver/__init__.py
    # since it MUST exist per test_silver_has_init_py; if there are
    # additional scripts, prefer those (more realistic mutation target).
    candidates = [
        SILVER_DIR / "scripts" / "etl_run.py",
        SILVER_DIR / "__init__.py",
    ]
    target = next((p for p in candidates if p.is_file()), None)
    if target is None:
        pytest.skip("silver/ has no Python file to mutate — scaffolding gap")
    cmd = _require_lint_imports()

    fixture_root = tmp_path / "project"
    # Copy what grimp needs: bot/, collector/, kalshi_wire/, coinbase_wire/,
    # silver/, and .importlinter at root. Without copying every package
    # listed in .importlinter root_packages, lint-imports errors with
    # "Could not find package 'X' in your Python path" before evaluating
    # any contract — masking the actual silver-no-bot enforcement.
    shutil.copytree(REPO_ROOT / "bot", fixture_root / "bot")
    shutil.copytree(REPO_ROOT / "collector", fixture_root / "collector")
    shutil.copytree(REPO_ROOT / "kalshi_wire", fixture_root / "kalshi_wire")
    shutil.copytree(REPO_ROOT / "coinbase_wire", fixture_root / "coinbase_wire")
    shutil.copytree(SILVER_DIR, fixture_root / "silver")
    shutil.copy(IMPORTLINTER_PATH, fixture_root / ".importlinter")

    mutated_path = fixture_root / target.relative_to(REPO_ROOT)
    src_pre = mutated_path.read_text()
    mutated = (
        "# D3.0 regression smoke: forbidden silver->bot edge\n"
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
        "KALSHI_WS_URL` injected into silver/ — the silver-no-bot "
        "contract is not enforced.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert ("silver-no-bot" in combined or "bot.constants" in combined), (
        "lint-imports failed but neither `silver-no-bot` nor the injected "
        "edge appears in output.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
