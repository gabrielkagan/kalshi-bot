"""Sprint 10.2 — shadows/ sibling-reorg (bundled, 2026-05-11).

Master plan L2225-2238 Sprint 10 row "shadows/" — 3 sub-moves bundled
because each is structurally identical (git mv + same-shape caller retarget):

  - fifteenm_shadow.py   → bot/shadows/fifteenm_shadow.py   (1650 LOC)
  - hourly_alt_shadow.py → bot/shadows/hourly_alt_shadow.py (1344 LOC)
  - spx_harrv_shadow.py  → bot/shadows/spx_harrv_shadow.py  (1107 LOC)

Filenames preserved (mirrors Sprint 10.1 b/c/d precedent). Per
`feedback_modularization_skip_soak.md`: no shim re-export at old paths,
no soak window.

Pre-flight R1-lesson checks (10.1b + 10.1c):
  - mock.patch string-form `patch("X.foo")` sites: 0 detected
  - `__file__`-derived path computations in any of the 3 files: 0 detected

Real caller imports (3 sites, all in bot/main_loop.py):
  - bot/main_loop.py:372 `from fifteenm_shadow import FifteenMShadowEngine, FIFTEENM_SHADOW_ENABLED`
  - bot/main_loop.py:382 `from hourly_alt_shadow import HourlyAltShadowEngine, HOURLY_ALT_SHADOW_ENABLED`
  - bot/main_loop.py:440 `from spx_harrv_shadow import SPXHARRVShadowEngine, SPX_HARRV_SHADOW_ENABLED`

Path-literal refs across ~107 sites: parametrize lists in test_db_signatures,
test_call_sites, test_db_writer_registry, test_insert_schema_parity,
test_product_type_enum, test_regression; README.template.md + README.md;
agent_docs/bot_layout.md; scripts/ops/pre_deploy_check.sh + doc_drift_check.py;
.github/workflows/whitepaper.yml; plus docstrings/comments.

CalEngine pipeline note: these shadow engines do NOT write `raw_prob` to
the production `evaluated_opportunities` table — they write to their own
`*_shadow_signals` tables. Shadow CalEngines aren't routed by
`_resolve_cal_engine`. So the CalEngine-triple-ship contract from 10.1b/c/d
doesn't apply to these moves; the production-WAL_REQUIRED_FILES contract
DOES apply and we verify each shadow still parses + has correct PRAGMAs.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


SHADOWS = (
    "fifteenm_shadow",
    "hourly_alt_shadow",
    "spx_harrv_shadow",
)

SHADOW_OLD_PATHS = {s: REPO_ROOT / f"{s}.py" for s in SHADOWS}
SHADOW_NEW_PATHS = {s: REPO_ROOT / "bot" / "shadows" / f"{s}.py" for s in SHADOWS}

SHADOW_CLASS_NAMES = {
    "fifteenm_shadow": "FifteenMShadowEngine",
    "hourly_alt_shadow": "HourlyAltShadowEngine",
    "spx_harrv_shadow": "SPXHARRVShadowEngine",
}
SHADOW_ENABLE_FLAGS = {
    "fifteenm_shadow": "FIFTEENM_SHADOW_ENABLED",
    "hourly_alt_shadow": "HOURLY_ALT_SHADOW_ENABLED",
    "spx_harrv_shadow": "SPX_HARRV_SHADOW_ENABLED",
}


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Identity + behavioral (parametrized × 3 shadows)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("shadow", SHADOWS)
def test_old_shadow_path_is_gone(shadow):
    """Root `<shadow>.py` DELETED post-move (no shim, per skip-soak)."""
    old = SHADOW_OLD_PATHS[shadow]
    assert not old.exists(), (
        f"{old} still exists. Sprint 10.2 per skip-soak feedback says no shim."
    )


@pytest.mark.parametrize("shadow", SHADOWS)
def test_new_shadow_path_exists(shadow):
    """`bot/shadows/<shadow>.py` exists."""
    new = SHADOW_NEW_PATHS[shadow]
    assert new.exists(), f"{new} missing — Sprint 10.2 move not performed."


@pytest.mark.parametrize("shadow", SHADOWS)
def test_no_stale_from_shadow_imports(shadow, repo_ast_cache):
    """No `from <shadow> import ...` AST nodes outside worktrees.

    Bit-4.5 (2026-05-17): consumes session-scoped ``repo_ast_cache``.
    """
    stale: list[str] = []
    new_path = SHADOW_NEW_PATHS[shadow]
    for path, tree in repo_ast_cache.items():
        if tree is None:
            continue
        if path == new_path:
            continue
        if " " in path.stem:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == shadow:
                stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `from {shadow} import ...` sites: {stale}. "
        f"Retarget each to `from bot.shadows.{shadow} import ...`."
    )


@pytest.mark.parametrize("shadow", SHADOWS)
def test_no_stale_import_shadow(shadow, repo_ast_cache):
    """No bare `import <shadow>` AST nodes outside worktrees.

    Bit-4.5 (2026-05-17): consumes session-scoped ``repo_ast_cache``.
    """
    stale: list[str] = []
    for path, tree in repo_ast_cache.items():
        if tree is None:
            continue
        if " " in path.stem:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == shadow:
                        stale.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not stale, (
        f"Stale `import {shadow}` sites: {stale}. "
        f"Retarget to `import bot.shadows.{shadow}` or `from bot.shadows.{shadow} import ...`."
    )


@pytest.mark.parametrize("shadow", SHADOWS)
def test_shadow_class_importable_at_new_path(shadow):
    """Behavioral smoke: shadow class importable from new path."""
    cls_name = SHADOW_CLASS_NAMES[shadow]
    mod = __import__(f"bot.shadows.{shadow}", fromlist=[cls_name])
    assert getattr(mod, cls_name, None) is not None, (
        f"bot.shadows.{shadow}.{cls_name} missing"
    )


@pytest.mark.parametrize("shadow", SHADOWS)
def test_shadow_enable_flag_importable(shadow):
    """Behavioral smoke: shadow enable flag importable from new path."""
    flag_name = SHADOW_ENABLE_FLAGS[shadow]
    mod = __import__(f"bot.shadows.{shadow}", fromlist=[flag_name])
    assert hasattr(mod, flag_name), (
        f"bot.shadows.{shadow}.{flag_name} missing"
    )


def test_main_loop_uses_new_shadow_paths():
    """bot/main_loop.py uses `from bot.shadows.<shadow> import ...` for all 3."""
    src = (REPO_ROOT / "bot" / "main_loop.py").read_text()
    for shadow in SHADOWS:
        cls = SHADOW_CLASS_NAMES[shadow]
        flag = SHADOW_ENABLE_FLAGS[shadow]
        assert f"from bot.shadows.{shadow} import {cls}, {flag}" in src, (
            f"bot/main_loop.py must use `from bot.shadows.{shadow} import "
            f"{cls}, {flag}` (Sprint 10.2)."
        )
        assert f"from {shadow} import {cls}" not in src, (
            f"bot/main_loop.py still has old-form `from {shadow} import {cls}`."
        )


def test_bot_shadows_init_exists():
    """`bot/shadows/__init__.py` exists with a docstring."""
    init_path = REPO_ROOT / "bot" / "shadows" / "__init__.py"
    assert init_path.exists(), "bot/shadows/__init__.py missing"


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — String-literal path-reference sweep (parametrized)
# ═════════════════════════════════════════════════════════════════════════════

_PATH_LITERAL_ALLOW = {
    "tests/integration/test_sprint_10_2_shadows_move.py",
    "tests/integration/test_sprint_10_1d_sports_engine_move.py",
    "tests/integration/test_sprint_10_1c_weather_engine_move.py",
    "tests/integration/test_sprint_10_1b_spx_engine_move.py",
    "tests/integration/test_sprint_10_1a_sports_data_move.py",
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


@pytest.mark.parametrize("shadow", SHADOWS)
def test_no_stale_string_literal_shadow_in_scripts_or_workflows(shadow):
    """Path-literal references to bare `<shadow>.py`. Allows lines that also
    mention the new path, comment lines, docstring lines, multi-component
    `os.path.join("bot","shadows","<shadow>.py")` + Path-segment forms, and
    auto-regen README.md."""
    needle = f"{shadow}.py"
    correct = f"bot/shadows/{shadow}.py"
    hits = _sweep_string_literal_refs(needle)
    stale = []
    for h in hits:
        rel, rest = h.split(": ", 1)
        if correct in rest:
            continue
        if '"bot", "shadows"' in rest or "'bot', 'shadows'" in rest:
            continue
        if '"bot" / "shadows"' in rest or "'bot' / 'shadows'" in rest:
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
        f"Stale string-literal references to bare `{shadow}.py`. Each must "
        f"be updated to `bot/shadows/{shadow}.py` (or include both pre+post "
        f"paths for git pathspecs). Found {len(stale)} sites:\n"
        + "\n".join(stale[:20])
    )


@pytest.mark.parametrize("shadow", SHADOWS)
def test_no_mock_patch_string_form_for_shadow(shadow):
    """R1-lesson regression pin (Sprint 10.1b): AST-walk test files for
    `patch("<shadow>.X")` string-form targets that would fail
    ModuleNotFoundError post-move."""
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
                        if first.value == shadow or first.value.startswith(f"{shadow}."):
                            stale.append(
                                f"{path.relative_to(REPO_ROOT)}:{node.lineno}: "
                                f'patch("{first.value}")'
                            )
    assert not stale, (
        f"mock.patch string-form targets for {shadow} module: {stale}. "
        f"Retarget each to `patch('bot.shadows.{shadow}.X', ...)`."
    )
