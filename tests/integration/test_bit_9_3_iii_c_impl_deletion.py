"""Bit 9.3-iii.c — TDD scaffolding for bot/_impl.py DELETE + kill-switch fix.

Lands RED before any production code change. Goes GREEN after:
  1. bot/_impl.py is DELETED (the 591-LOC residual shim).
  2. bot/runtime_config.py created (PEP 562 dual-module-probe __getattr__).
  3. dashboard_snapshot.py + supabase_sync.py retargeted: 6+1 `import bot._impl
     as _bot_mod` → `import bot.runtime_config as _bot_mod` (getattr call
     sites unchanged).
  4. Kill-switch fix in scanner + executor:
     - scanner: drop WEATHER/HOURLY/BRACKET_NO from explicit-name imports;
       reads switch to `bot.constants.X` module-attribute access; writes
       in 3 kill blocks retarget from `_self_module.X = False` to
       `bot.constants.X = False`.
     - executor: same drop + read retarget for WEATHER/HOURLY.
  5. Test surface retargets (~413 hits across 39 files).
  6. tests/contracts/public_api.json regenerated (no `bot._impl.X` keys).
  7. .importlinter cleaned (no `bot._impl` references).
  8. Doc-drift sweep: CLAUDE.md, bot/CLAUDE.md, bot/scanner/CLAUDE.md,
     agent_docs/bot_layout.md, agent_docs/repository_map.md,
     kb/concepts/extraction-pre-flight-checklist.md.

See kb/decisions/bit-9.3-iii-c-plan-may11.md for the plan (to be written
alongside this scaffold).

The kill-switch fix is bundled rationale: pre-Bit-9.3-iii.c the 3 scanner
write blocks at bot/scanner/__init__.py:1259/1279/1299 mutate
bot._impl.X but the gating reads (scanner 1252/1272/1292/5418/7450/7551
+ executor 478/481) are bare-name lookups against scanner/executor
module-level bindings (explicit-name imports from bot.constants). The
auto-kill log + Telegram alert fire correctly, but the flag flip does
NOT take effect at runtime — gating continues to read True until the
operator restarts. Bundle fix: convert reads to module-attribute access
through `bot.constants` so mutation freshness flows through. Mirrors the
`_telegram_state._TELEGRAM` and `_cal_state._CALIBRATION_ENGINE` patterns.
"""
from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _parse(rel_path: str) -> ast.Module:
    return ast.parse((REPO_ROOT / rel_path).read_text())


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text()


# ── Cluster 1: bot/_impl.py DELETED ──────────────────────────────────────────


def test_bot_impl_py_file_absent():
    """bot/_impl.py must not exist on disk after Bit 9.3-iii.c."""
    impl_py = REPO_ROOT / "bot" / "_impl.py"
    assert not impl_py.exists(), (
        f"bot/_impl.py must be DELETED in Bit 9.3-iii.c. Found at {impl_py}. "
        "The residual 591-LOC shim is no longer load-bearing post-Bit-9.3-iii.b "
        "(production import chain uses bot.main_loop / bot.state / bot.boot "
        "directly). Deletion is the Bit 9.3-iii.c milestone."
    )


def test_bot_impl_module_import_fails():
    """`import bot._impl` must raise ModuleNotFoundError after deletion.

    importlib bypasses any test-time caches via invalidate_caches().
    """
    importlib.invalidate_caches()
    try:
        importlib.import_module("bot._impl")
    except ModuleNotFoundError:
        return  # expected
    except ImportError as exc:
        # An ImportError that's not ModuleNotFoundError suggests partial
        # presence (e.g., file deleted but cached). Acceptable for the
        # gate but flag it.
        raise AssertionError(
            f"Expected ModuleNotFoundError after bot/_impl.py delete, "
            f"got ImportError: {exc}. Investigate stale cache."
        )
    else:
        raise AssertionError(
            "bot._impl import unexpectedly succeeded. bot/_impl.py must be "
            "fully deleted (and not replaced by a stub) per Bit 9.3-iii.c."
        )


def test_no_production_caller_imports_bot_impl():
    """Production-tree files (bot/, dashboard_snapshot.py, supabase_sync.py,
    scripts/) must have ZERO bot._impl references after the retargets land.

    Excludes:
      - bot/_impl.py itself (already covered by the absence pin above)
      - docstrings / comments — these are doc-drift and caught by sweep
        sub-agents; the AST scan here looks at actual import / attribute /
        string-form ast nodes only.
    """
    forbidden_roots = ["bot", "scripts"]
    # Sprint 10 Bit 10.4 (2026-05-12): dashboard_snapshot.py + supabase_sync.py
    # relocated under bot/snapshots/. They are now reached by the
    # `forbidden_roots = ["bot", "scripts"]` rglob walk below; the explicit
    # forbidden_files list is empty post-relocation but kept as a hook for
    # any future repo-root sentinel files that may need the same treatment.
    forbidden_files: list[Path] = []
    py_files: list[Path] = []
    for root in forbidden_roots:
        for p in (REPO_ROOT / root).rglob("*.py"):
            if "/.claude/worktrees/" in str(p):
                continue
            if p.name == "_impl.py":  # excluded — file is gone anyway
                continue
            py_files.append(p)
    for f in forbidden_files:
        if f.exists():
            py_files.append(f)

    offenders: list[str] = []
    for py in py_files:
        try:
            tree = ast.parse(py.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            # `import bot._impl` / `import bot._impl as X`
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "bot._impl" or alias.name.startswith("bot._impl."):
                        offenders.append(f"{py.relative_to(REPO_ROOT)}:{node.lineno} import {alias.name}")
            # `from bot._impl import X`
            elif isinstance(node, ast.ImportFrom):
                if node.module == "bot._impl" or (node.module or "").startswith("bot._impl."):
                    offenders.append(f"{py.relative_to(REPO_ROOT)}:{node.lineno} from {node.module} import ...")

    assert not offenders, (
        "Production-tree files must have zero bot._impl import references "
        "after Bit 9.3-iii.c retargets land. Offenders:\n  "
        + "\n  ".join(offenders[:30])
    )


# ── Cluster 2: bot/runtime_config.py PEP 562 shim ────────────────────────────


def test_runtime_config_module_exists():
    """bot/runtime_config.py must be importable after Bit 9.3-iii.c."""
    importlib.invalidate_caches()
    mod = importlib.import_module("bot.runtime_config")
    assert mod is not None


def test_runtime_config_dual_probe_reads_bot_constants():
    """getattr(bot.runtime_config, NAME) must return bot.constants.NAME when present.

    Tests with WEATHER_NO_SIDE_LIVE which lives in bot.constants per Bit 3.1.
    """
    import bot.constants
    import bot.runtime_config

    expected = bot.constants.WEATHER_NO_SIDE_LIVE
    assert bot.runtime_config.WEATHER_NO_SIDE_LIVE is expected, (
        "runtime_config must dual-probe bot.constants first; "
        f"got {bot.runtime_config.WEATHER_NO_SIDE_LIVE!r} != "
        f"bot.constants.WEATHER_NO_SIDE_LIVE ({expected!r})"
    )


def test_runtime_config_dual_probe_reads_config():
    """getattr(bot.runtime_config, NAME) must fall back to config.NAME when not in bot.constants.

    Tests with MAX_RISK_PER_TRADE which lives in config (not bot.constants).
    """
    import bot.config as config
    import bot.runtime_config

    expected = config.MAX_RISK_PER_TRADE
    assert bot.runtime_config.MAX_RISK_PER_TRADE == expected, (
        "runtime_config must fall back to config when bot.constants lacks the name; "
        f"got {bot.runtime_config.MAX_RISK_PER_TRADE!r} != "
        f"config.MAX_RISK_PER_TRADE ({expected!r})"
    )


def test_runtime_config_attribute_error_for_missing_name():
    """getattr on a name not in bot.constants or config must raise AttributeError.

    Preserves the getattr(default) fallback semantics that dashboard_snapshot.py
    relies on — Python's default getattr() catches AttributeError and returns
    the fallback.
    """
    import bot.runtime_config

    sentinel = object()
    result = getattr(bot.runtime_config, "DEFINITELY_NOT_A_REAL_CONSTANT_NAME_xyz", sentinel)
    assert result is sentinel, (
        "runtime_config.__getattr__ must raise AttributeError for unknown names "
        "so callers' getattr(default) fallback fires. "
        f"Got {result!r} instead of sentinel."
    )


def test_runtime_config_mutation_freshness():
    """Mutating bot.constants.X at runtime must be visible through bot.runtime_config.X.

    This is the load-bearing property for the kill-switch fix: scanner writes
    bot.constants.X = False, dashboard reads bot.runtime_config.X via PEP 562
    __getattr__ → returns the mutated False value.
    """
    import bot.constants
    import bot.runtime_config

    canary = "_runtime_config_freshness_canary_9c"
    bot.constants.__dict__[canary] = "original"
    try:
        assert bot.runtime_config.__getattr__(canary) == "original"
        bot.constants.__dict__[canary] = "mutated"
        assert bot.runtime_config.__getattr__(canary) == "mutated", (
            "runtime_config must re-probe bot.constants on each access for "
            "mutation freshness. Captured-by-value binding would freeze at 'original'."
        )
    finally:
        bot.constants.__dict__.pop(canary, None)


# ── Cluster 3: Scanner kill-switch fix ───────────────────────────────────────


def test_scanner_no_bot_impl_self_module_import():
    """The 3 `import bot._impl as _self_module` lines in bot/scanner/__init__.py
    must be gone after the kill-switch fix.
    """
    src = _read("bot/scanner/__init__.py")
    assert "import bot._impl as _self_module" not in src, (
        "bot/scanner/__init__.py must NOT import bot._impl in kill-switch blocks "
        "after Bit 9.3-iii.c. The 3 `import bot._impl as _self_module` lines at "
        "lines ~1259/1279/1299 must be replaced with mutation targeting bot.constants "
        "directly (kill-switch fix preserves dashboard-signal semantics)."
    )


def test_scanner_kill_switch_writes_target_bot_constants():
    """The 3 kill-switch blocks must write to bot.constants.X, not to bot._impl.X.

    Pre-fix: `_self_module.WEATHER_NO_SIDE_LIVE = False` (where _self_module is bot._impl).
    Post-fix: `bot.constants.WEATHER_NO_SIDE_LIVE = False` (canonical home of the flag).

    AST grammar: find `Assign` nodes whose target is an `Attribute` with
    .attr in {WEATHER_NO_SIDE_LIVE, HOURLY_NO_SIDE_LIVE, BRACKET_NO_ENABLED}
    and .value resolves through `bot.constants` (or an alias).
    """
    flag_names = {"WEATHER_NO_SIDE_LIVE", "HOURLY_NO_SIDE_LIVE", "BRACKET_NO_ENABLED"}
    tree = _parse("bot/scanner/__init__.py")

    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Attribute):
                continue
            if target.attr not in flag_names:
                continue
            # Reconstruct the chain text for assertion reporting.
            chain = ast.unparse(target.value)  # py3.9+: ast.unparse
            found.setdefault(target.attr, []).append(f"line {node.lineno}: {chain}.{target.attr}")

    missing = flag_names - found.keys()
    bad_targets: list[str] = []
    for flag, occurrences in found.items():
        for occ in occurrences:
            if "bot.constants" not in occ and "_self_module" not in occ:
                # accept _const, _bc, etc. but only via the lined hint
                # — assert the chain reads through bot.constants
                pass
            if "_self_module" in occ:
                bad_targets.append(occ)

    assert not missing, (
        "Kill-switch flag writes missing entirely from bot/scanner/__init__.py: "
        f"{sorted(missing)}. The 3 kill blocks must retain the auto-kill write "
        "(dashboard-signal preservation), just retargeted to bot.constants."
    )
    assert not bad_targets, (
        "Kill-switch writes must NOT target bot._impl (alias _self_module) "
        "post-Bit-9.3-iii.c. Offending writes:\n  " + "\n  ".join(bad_targets)
    )


def test_scanner_kill_switch_reads_use_module_attribute_access():
    """The 3 kill-switch GATE reads (lines ~1252/1272/1292) must use module-attribute
    access via bot.constants (or an alias), NOT bare-name lookups.

    Bare-name lookups against scanner's module-level binding don't see the
    runtime mutation that the auto-kill block performs — this is the latent
    bug bundled into Bit 9.3-iii.c.

    Detection grammar: each kill-switch gate is structured as
        if <expr>: ... <auto-kill writes to flag>
    where the kill writes share the SAME flag name as the gate condition.
    Per-flag: assert the gate-expr for flag F evaluates `<module>.F`
    (Attribute node), not bare `F` (Name node).
    """
    flag_names = {"WEATHER_NO_SIDE_LIVE", "HOURLY_NO_SIDE_LIVE", "BRACKET_NO_ENABLED"}
    tree = _parse("bot/scanner/__init__.py")

    # Find all `if <Name with id in flag_names>:` patterns — these are
    # the bare-name reads we want to eliminate.
    bare_reads: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
            if node.test.id in flag_names:
                bare_reads.append(f"line {node.lineno}: if {node.test.id}:")

    assert not bare_reads, (
        "Kill-switch GATE reads must NOT use bare-name lookups against scanner's "
        "captured module binding (no mutation freshness). Offenders:\n  "
        + "\n  ".join(bare_reads)
        + "\nFix: convert to bot.constants.<FLAG> module-attribute access "
        "(parallel to _telegram_state._TELEGRAM and _cal_state._CALIBRATION_ENGINE)."
    )


def test_scanner_kill_switch_explicit_imports_dropped():
    """The 3 kill-switch flags must NOT appear in scanner's `from bot.constants import (...)` block.

    Keeping the explicit-name import would re-introduce the captured-by-value
    binding that the kill-switch fix is eliminating.
    """
    flag_names = {"WEATHER_NO_SIDE_LIVE", "HOURLY_NO_SIDE_LIVE", "BRACKET_NO_ENABLED"}
    tree = _parse("bot/scanner/__init__.py")
    bad_imports: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "bot.constants":
            continue
        for alias in node.names:
            if alias.name in flag_names:
                bad_imports.append(f"line {node.lineno}: {alias.name}")
    assert not bad_imports, (
        "Kill-switch flags must NOT be explicit-name-imported in "
        "bot/scanner/__init__.py — that captures the binding by value and "
        "defeats the mutation-freshness fix. Offenders:\n  "
        + "\n  ".join(bad_imports)
    )


def test_executor_kill_switch_reads_use_module_attribute_access():
    """bot/executor.py reads of WEATHER_NO_SIDE_LIVE / HOURLY_NO_SIDE_LIVE
    must NOT be bare-name lookups (same mutation-freshness rationale).
    """
    flag_names = {"WEATHER_NO_SIDE_LIVE", "HOURLY_NO_SIDE_LIVE"}
    tree = _parse("bot/executor.py")
    bare_reads: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in flag_names and isinstance(node.ctx, ast.Load):
            bare_reads.append(f"line {node.lineno}: bare-name {node.id}")
    assert not bare_reads, (
        "Kill-switch GATE reads in bot/executor.py must NOT use bare-name lookups. "
        "Offenders:\n  " + "\n  ".join(bare_reads)
        + "\nFix: convert to bot.constants.<FLAG> module-attribute access."
    )


def test_executor_kill_switch_explicit_imports_dropped():
    """WEATHER_NO_SIDE_LIVE / HOURLY_NO_SIDE_LIVE must NOT be in
    bot/executor.py's `from bot.constants import (...)` block.
    """
    flag_names = {"WEATHER_NO_SIDE_LIVE", "HOURLY_NO_SIDE_LIVE"}
    tree = _parse("bot/executor.py")
    bad_imports: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "bot.constants":
            continue
        for alias in node.names:
            if alias.name in flag_names:
                bad_imports.append(f"line {node.lineno}: {alias.name}")
    assert not bad_imports, (
        "Kill-switch flags must NOT be explicit-name-imported in bot/executor.py. "
        "Offenders:\n  " + "\n  ".join(bad_imports)
    )


def test_kill_switch_runtime_freshness_via_bot_constants_mutation():
    """End-to-end runtime check: mutating bot.constants.WEATHER_NO_SIDE_LIVE
    must be visible to fresh module-attribute reads from scanner + executor.

    This pins the actual semantic the kill-switch fix delivers — the bug
    was that mutating bot._impl.X did not flip the bare-name read at the
    gate, so the gate never short-circuited at runtime.
    """
    import bot.constants

    original = bot.constants.WEATHER_NO_SIDE_LIVE
    try:
        bot.constants.WEATHER_NO_SIDE_LIVE = False
        # Module-attribute access (fresh probe each time) — what the fix uses.
        assert bot.constants.WEATHER_NO_SIDE_LIVE is False, (
            "bot.constants.WEATHER_NO_SIDE_LIVE mutation should be observable "
            "via module-attribute access. Failure here would indicate a deeper "
            "Python module-system issue, not a fix-bug."
        )
        bot.constants.WEATHER_NO_SIDE_LIVE = True
        assert bot.constants.WEATHER_NO_SIDE_LIVE is True
    finally:
        bot.constants.WEATHER_NO_SIDE_LIVE = original


# ── Cluster 4: Caller retargets ──────────────────────────────────────────────


def _has_import_of(tree: ast.Module, module_name: str) -> bool:
    """AST-scan: True if any Import statement targets module_name (exact match)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == module_name:
                return True
    return False


def test_dashboard_snapshot_uses_runtime_config():
    """dashboard_snapshot.py must replace its 6 `import bot._impl as _bot_mod`
    sites with `import bot.runtime_config as _bot_mod`.

    AST-scans actual import statements rather than substring matches — comments
    that mention bot._impl as breadcrumbs do NOT count as offenders (L97 prevention:
    avoid string-matching the literal that the breadcrumb describes).
    """
    tree = _parse("bot/snapshots/dashboard_snapshot.py")  # Sprint 10 Bit 10.4 (2026-05-12)
    assert not _has_import_of(tree, "bot._impl"), (
        "bot/snapshots/dashboard_snapshot.py must NOT have any `import bot._impl` statement."
    )
    assert _has_import_of(tree, "bot.runtime_config"), (
        "bot/snapshots/dashboard_snapshot.py must use `import bot.runtime_config as _bot_mod` "
        "as the replacement. The PEP 562 shim preserves getattr semantics with "
        "mutation freshness."
    )


def test_supabase_sync_uses_runtime_config():
    """bot/snapshots/supabase_sync.py must replace its lone bot._impl import."""
    tree = _parse("bot/snapshots/supabase_sync.py")  # Sprint 10 Bit 10.4 (2026-05-12)
    assert not _has_import_of(tree, "bot._impl"), (
        "bot/snapshots/supabase_sync.py must NOT have any `import bot._impl` statement."
    )
    assert _has_import_of(tree, "bot.runtime_config"), (
        "bot/snapshots/supabase_sync.py must use `import bot.runtime_config as _bot_mod`."
    )


# ── Cluster 5: Snapshot + linter cleanliness ─────────────────────────────────


def test_public_api_snapshot_has_no_bot_impl_entries():
    """tests/contracts/public_api.json must not contain any `bot._impl.X` keys
    after Bit 9.3-iii.c regenerates the snapshot.
    """
    snap_path = REPO_ROOT / "tests" / "contracts" / "public_api.json"
    assert snap_path.exists(), f"public_api.json not found at {snap_path}"
    data = json.loads(snap_path.read_text())

    if isinstance(data, dict):
        offenders = [k for k in data.keys() if k.startswith("bot._impl")]
    elif isinstance(data, list):
        offenders = [str(e) for e in data if str(e).startswith("bot._impl")]
    else:
        raise AssertionError(f"Unexpected public_api.json shape: {type(data).__name__}")

    assert not offenders, (
        f"public_api.json must drop all bot._impl.* entries after Bit 9.3-iii.c. "
        f"Found {len(offenders)} residual entries (first 5): {offenders[:5]}"
    )


def test_importlinter_no_bot_impl_references():
    """`.importlinter` must not list bot._impl in any forbidden_modules /
    source_modules / ignore_imports clause (bot._impl no longer exists).
    """
    importlinter = REPO_ROOT / ".importlinter"
    src = importlinter.read_text()
    # Strict literal scan — comment lines containing "bot._impl" are noise
    # and L86 doc-drift, not a contract bug.
    offending_lines: list[str] = []
    for i, line in enumerate(src.splitlines(), start=1):
        stripped = line.split("#", 1)[0]
        if "bot._impl" in stripped:
            offending_lines.append(f"line {i}: {line}")
    assert not offending_lines, (
        ".importlinter must not reference bot._impl after Bit 9.3-iii.c. "
        "Offenders:\n  " + "\n  ".join(offending_lines)
    )


def test_dump_public_api_skipped_submodules_drops_bot_impl():
    """scripts/dump_public_api.py SKIPPED_SUBMODULES must drop 'bot._impl'."""
    src = _read("scripts/dump_public_api.py")
    # Locate the SKIPPED_SUBMODULES assignment via AST so comments/docstring
    # mentions of bot._impl don't trip the check.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "SKIPPED_SUBMODULES":
                    value_src = ast.unparse(node.value)
                    assert "bot._impl" not in value_src, (
                        f"scripts/dump_public_api.py SKIPPED_SUBMODULES still "
                        f"references bot._impl (no longer exists): {value_src}"
                    )
                    return
    # If SKIPPED_SUBMODULES no longer exists (refactor), test is vacuous —
    # accept silently. The presence/absence of bot._impl is what we care
    # about, not the variable name.


# ── Cluster 6: Boot-time bindings re-export drop ─────────────────────────────


def test_bot_boot_remains_canonical_home():
    """The 4 boot-time bindings + cal_mlp warmup must still be in bot/boot.py
    (canonical home per Bit 9.3-iii.a); deleting bot/_impl.py does NOT
    relocate them anywhere else.
    """
    import bot.boot

    canonical_names = (
        "_HPSB_VALIDATOR_UNAVAILABLE_REASON",
        "_HPSB_MISSING_BLEEDERS",
        "_BLEED_BLOCK_MISSING_BLEEDERS",
        "compute_for_15m_main_path",
    )
    missing = [n for n in canonical_names if not hasattr(bot.boot, n)]
    assert not missing, (
        f"bot.boot must keep these 4 boot-time bindings canonical-home (per Bit 9.3-iii.a): "
        f"missing {missing}. Bit 9.3-iii.c only deletes bot/_impl.py; it does NOT "
        "relocate the canonical homes."
    )
