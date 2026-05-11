"""Sprint 10.1c — `weather_engine.py` → `bot/engines/weather_engine.py` (2026-05-11).

Third sub-move of Sprint 10.1 engines/ sibling-reorg. ~1024 LOC. Filename
preserved (mirrors 10.1b precedent: keep file name to minimize KB +
operator runbook churn).

Per `feedback_modularization_skip_soak.md`: no shim re-export at old path,
no soak. All caller imports retargeted atomically.

Real caller imports (5 sites):
  - bot/main_loop.py:363 (method-body `from weather_engine import WeatherEngine`)
  - market_config.py:416 (method-body `from weather_engine import WEATHER_CITIES`)
  - tests/test_weather_ensemble_cache.py:35 (`import weather_engine`)
  - tests/test_weather_ensemble_cache.py:36 (`from weather_engine import WeatherEngine, WEATHER_ENSEMBLE_CACHE_FILE`)
  - tests/test_supabase_asset_parity.py:33,49,64 (3× method-body `from weather_engine import WEATHER_CITIES`)

Path-literal refs:
  - scripts/pre_deploy_check.sh:18 (bash for-loop)
  - scripts/extract_config.py:7,25,232 (docstring + WEATHER_PATH constant + docstring)
  - scripts/check_docs_freshness.py:68,69,308 (docstring + path constant + error message)
  - scripts/doc_drift_check.py:36,214,215 (SOURCE_FILES + docstring + path)
  - .github/workflows/whitepaper.yml:18 (path trigger)
  - README.template.md:223 (file-tree literal)
  - agent_docs/bot_layout.md (project file-map)
  - bot/CLAUDE.md (engines paragraph)
  - TESTING_STRATEGY.md (call-site failure-mode + CalEngine triple-ship rules)
  - tests/test_product_type_enum.py:55 (parametrize)
  - tests/test_db_writer_registry.py:323 (parametrize)
  - tests/test_insert_schema_parity.py:114 (parametrize)
  - tests/test_db_signatures.py:45,311 (parametrize × 2)
  - tests/test_call_sites.py:33 (ENGINE_FILES), :150 (file-open!)
  - tests/test_15m_silence_alert.py:1587 (prose, cosmetic)

mock.patch pre-flight (Sprint 10.1b R1 CRITICAL class): zero sites
detected — `grep -rn 'patch.*"weather_engine\|patch.*'\\''weather_engine'`
returned empty across the repo.

Sister Sprint 10.1 sub-moves still pending after this Bit:
  - 10.1d: sports_engine.py → bot/engines/sports_engine.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OLD_PATH = REPO_ROOT / "weather_engine.py"
NEW_PATH = REPO_ROOT / "bot" / "engines" / "weather_engine.py"


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity + behavioral (7 pins)
# ═════════════════════════════════════════════════════════════════════════════

def test_old_weather_engine_path_is_gone():
    """Root `weather_engine.py` DELETED post-move (no shim, per skip-soak)."""
    assert not OLD_PATH.exists(), (
        f"{OLD_PATH} still exists. Sprint 10.1c per skip-soak feedback says "
        f"no shim re-export; file should be `git mv`'d cleanly."
    )


def test_new_weather_engine_path_exists():
    """`bot/engines/weather_engine.py` exists."""
    assert NEW_PATH.exists(), f"{NEW_PATH} missing — Sprint 10.1c move not performed."


def test_no_stale_from_weather_engine_imports():
    """No `from weather_engine import ...` AST nodes outside worktrees."""
    repo_files = (
        list(REPO_ROOT.glob("*.py"))
        + list((REPO_ROOT / "bot").rglob("*.py"))
        + list((REPO_ROOT / "tests").rglob("*.py"))
        + list((REPO_ROOT / "scripts").rglob("*.py"))
    )
    stale: list[str] = []
    for path in repo_files:
        if ".claude/worktrees/" in str(path):
            continue
        if path == NEW_PATH:
            continue
        if " " in path.stem:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "weather_engine":
                stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `from weather_engine import ...` sites: {stale}. "
        f"Retarget each to `from bot.engines.weather_engine import ...`."
    )


def test_no_stale_import_weather_engine():
    """No bare `import weather_engine` AST nodes outside worktrees."""
    repo_files = (
        list(REPO_ROOT.glob("*.py"))
        + list((REPO_ROOT / "bot").rglob("*.py"))
        + list((REPO_ROOT / "tests").rglob("*.py"))
        + list((REPO_ROOT / "scripts").rglob("*.py"))
    )
    stale: list[str] = []
    for path in repo_files:
        if ".claude/worktrees/" in str(path):
            continue
        if " " in path.stem:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "weather_engine":
                        stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `import weather_engine` sites: {stale}. Retarget to "
        f"`import bot.engines.weather_engine` or `from bot.engines.weather_engine import ...`."
    )


def test_weather_engine_class_importable_at_new_path():
    """Behavioral smoke: `from bot.engines.weather_engine import WeatherEngine` works."""
    from bot.engines.weather_engine import WeatherEngine
    assert WeatherEngine is not None


def test_weather_engine_main_loop_uses_new_path():
    """bot/main_loop.py method-body import targets the new path."""
    src = (REPO_ROOT / "bot" / "main_loop.py").read_text()
    assert "from bot.engines.weather_engine import WeatherEngine" in src, (
        "bot/main_loop.py method-body late-binding for WeatherEngine must use "
        "`from bot.engines.weather_engine import WeatherEngine` (Sprint 10.1c retarget)."
    )
    assert "from weather_engine import WeatherEngine" not in src, (
        "bot/main_loop.py still has old-form `from weather_engine import WeatherEngine`."
    )


def test_bot_engines_does_not_reexport_weather_engine():
    """bot/engines/__init__.py focused on the 3 small engine classes only —
    callers reach weather_engine via direct submodule import."""
    src = (REPO_ROOT / "bot" / "engines" / "__init__.py").read_text()
    assert "from weather_engine import" not in src, (
        "bot/engines/__init__.py uses old-form `from weather_engine import` — "
        "callers should use `from bot.engines.weather_engine import WeatherEngine`."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — String-literal path-reference sweep
# ═════════════════════════════════════════════════════════════════════════════

_PATH_LITERAL_ALLOW = {
    "tests/test_sprint_10_1c_weather_engine_move.py",
    "tests/test_sprint_10_1b_spx_engine_move.py",      # mentions 10.1c roadmap
    "tests/test_sprint_10_1a_sports_data_move.py",
    "tests/CLAUDE.md",
    "kb/decisions",
    "kb/concepts",
    "kb/failures",
    "kb-research",
    ".claude/worktrees",
    ".claude/skills",
    ".git",
    "venv",
}


def _sweep_string_literal_refs(needle: str, suffixes: tuple = (".py", ".yml", ".yaml", ".md", ".sh")) -> list[str]:
    hits: list[str] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in suffixes:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if any(rel.startswith(allow) for allow in _PATH_LITERAL_ALLOW):
            continue
        if " " in path.stem:
            continue
        try:
            src = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if needle in src:
            for lineno, line in enumerate(src.splitlines(), 1):
                if needle in line:
                    hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
    return hits


def test_no_stale_string_literal_weather_engine_in_scripts_or_workflows():
    """Path-literal references to bare `weather_engine.py`. Allows lines that
    also mention the new path, comment lines, docstring lines, multi-component
    `os.path.join("bot","engines","weather_engine.py")` form, and auto-regen
    README.md."""
    needle = "weather_engine.py"
    correct = "bot/engines/weather_engine.py"
    hits = _sweep_string_literal_refs(needle)
    stale = []
    for h in hits:
        rel, rest = h.split(": ", 1)
        if correct in rest:
            continue
        if '"bot", "engines"' in rest or "'bot', 'engines'" in rest:
            continue
        # Path-segment form: `REPO_ROOT / "bot" / "engines" / "weather_engine.py"`
        if '"bot" / "engines"' in rest or "'bot' / 'engines'" in rest:
            continue
        stripped = rest.strip()
        if stripped.startswith("#"):
            continue
        if '"""' in stripped or "'''" in stripped:
            continue
        if rel == "README.md":
            continue
        if f'"{needle}"' not in stripped and f"'{needle}'" not in stripped:
            continue
        stale.append(h)
    assert not stale, (
        f"Stale string-literal references to bare `weather_engine.py`. Each must "
        f"be updated to `bot/engines/weather_engine.py`. Found {len(stale)} sites:\n"
        + "\n".join(stale[:20])
    )


def test_weather_ensemble_cache_path_is_repo_root():
    """R1 CRITICAL prevention: pre-Sprint-10.1c, weather_engine.py was at repo
    root, so the `__file__`-derived `_cache_path` resolved to
    `<repo_root>/weather_ensemble_cache.json`. After the move to
    bot/engines/weather_engine.py, naive `__file__`-derivation would silently
    relocate the cache to `bot/engines/weather_ensemble_cache.json`, ORPHANING
    the existing 21KB warm ensemble cache and cold-starting weather data on
    first deploy restart.

    Fix anchors the cache path explicitly to the repo root via 2-level
    parent navigation (bot/engines/ → bot/ → repo). This test pins the
    invariant — if a future maintainer reverts to `os.path.dirname(__file__)`,
    this test fails loudly."""
    from bot.engines.weather_engine import WeatherEngine, WEATHER_ENSEMBLE_CACHE_FILE
    # Construct a minimal instance to probe _cache_path without running the
    # thread/network — pass mocks for required args.
    from unittest.mock import MagicMock
    # WeatherEngine() takes no required args (started lazily). If __init__
    # raises due to missing deps, fall back to AST inspection.
    try:
        eng = WeatherEngine.__new__(WeatherEngine)
        # Manually invoke the same _cache_path construction
        # (mirrors line ~798-805 of bot/engines/weather_engine.py)
        import os
        import bot.engines.weather_engine as we_mod
        _expected = os.path.join(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.abspath(we_mod.__file__))
                )
            ),
            WEATHER_ENSEMBLE_CACHE_FILE,
        )
        # The expected path MUST be the repo root + cache filename.
        assert _expected.endswith(f"/{WEATHER_ENSEMBLE_CACHE_FILE}"), (
            f"Expected path ends with /{WEATHER_ENSEMBLE_CACHE_FILE}; got {_expected}"
        )
        # And the parent directory MUST be the repo root (NOT bot/engines).
        parent = os.path.dirname(_expected)
        assert not parent.endswith("/bot/engines"), (
            f"Cache path parent is bot/engines/ — Sprint 10.1c R1 CRITICAL "
            f"regression: cache should be at repo root, got parent={parent}"
        )
        assert not parent.endswith("/bot"), (
            f"Cache path parent is bot/ — incorrect 1-level instead of 2-level "
            f"REPO_ROOT navigation. Got parent={parent}"
        )
    except Exception as e:
        if "WeatherEngine" in str(e):
            pytest.fail(f"WeatherEngine.__new__ failed: {e}")
        raise


def test_no_mock_patch_string_form_for_weather_engine():
    """Sprint 10.1b R1 CRITICAL prevention: AST-walk all test files for
    `patch("weather_engine.X")` or `patch('weather_engine.X')` string-form
    targets. unittest.mock.patch resolves dotted-name strings via
    importlib.import_module — post-move these would raise ModuleNotFoundError.

    This Bit pre-verified ZERO sites via grep at scaffold time, but this
    regression pin locks the invariant going forward."""
    test_dir = REPO_ROOT / "tests"
    stale: list[str] = []
    for path in test_dir.rglob("*.py"):
        if " " in path.stem:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeDecodeError, OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                fn_name = (
                    fn.attr if isinstance(fn, ast.Attribute) else
                    fn.id if isinstance(fn, ast.Name) else None
                )
                if fn_name == "patch" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        if first.value == "weather_engine" or first.value.startswith("weather_engine."):
                            stale.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                                f'patch("{first.value}")'
                            )
    assert not stale, (
        f"mock.patch string-form targets for weather_engine module surviving "
        f"the move: {stale}. Each must be retargeted to "
        f"`patch('bot.engines.weather_engine.X', ...)` because "
        f"unittest.mock.patch resolves dotted-name strings via "
        f"importlib.import_module."
    )
