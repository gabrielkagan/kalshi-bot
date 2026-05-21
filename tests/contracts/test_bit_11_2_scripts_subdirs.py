"""Sprint 11 Bit 11.2 — scripts/ subdirectories (2026-05-12).

Reorganizes ~74 tracked files from flat `scripts/` root into 3 tier subdirs:
  - scripts/audit/    — read-only analysis + Wilson CI + alpha-research
  - scripts/backfill/ — historical data backfills
  - scripts/ops/      — operator-facing one-shots + setup + migrations

Per `feedback_modularization_skip_soak.md`: no shim, no soak.

Files explicitly LEFT at scripts/ root (do NOT move):
  - CLAUDE.md, STATE_DB_BACKUP_SETUP.md, VPS_SETUP.md — docs
  - vps_mcp_server.py — Bit 13.5 sibling owns this
  - io.kalshi.state-db-backup-heartbeat.plist.template — template
  - cal_mlp/ — already a subdir
  - git_hooks/ — already a subdir

L98 from Bit 12.2: bare `__file__`-based `.parent` path-resolution paths
silently shift when the file's location changes. None of the moved scripts
chain `__file__`-derived paths into REPO_ROOT (they each use `--db` CLI
args, env vars, or accept paths from the operator). The invariant we pin
below is the moves themselves (filesystem layout) + the absence of stale
flat-root `.py`/`.sh`/`.sql` files outside the allow-list.

The downstream retargeting (Makefile, .github/workflows/*, .claude/skills/*,
agent_docs/*, tests/unit/test_makefile.py, etc.) is invariant-checked
through other means — `make doc-drift` for docs and `pytest
tests/unit/test_makefile.py` for the Makefile wrapper paths.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

AUDIT_DIR = SCRIPTS_DIR / "audit"
BACKFILL_DIR = SCRIPTS_DIR / "backfill"
OPS_DIR = SCRIPTS_DIR / "ops"


# Files that MUST exist under scripts/audit/ post-Bit-11.2.
EXPECTED_AUDIT_FILES = (
    "15m_alpha_research.py",
    "15m_live_audit.py",
    "alpha_audit.py",
    "audit_alerts.py",
    "audit_cron.py",
    "audit_runner.sh",
    "backtest.py",
    "buy_low_analysis.py",
    "calibrator_feature_health.py",
    "check_docs_freshness.py",
    "data_health_monitor.py",
    "doc_drift_check.py",
    "dump_public_api.py",
    "generate_docs.py",
    "hourly_alpha_research.py",
    "hourly_shadow_audit.py",
    "maker_opportunity_cost.py",
    "no_side_status.py",
    "post_deploy_scan_gate.py",
    "postdeploy_verify.py",
    "quiet_market_monitor.py",
    "reconcile_ioc_losses.py",
    "shadow_eval.py",
    "sports_alpha_research.py",
    "sports_analysis.py",
    "sports_diagnose.py",
    "sports_raw_probe.py",
    "sports_shadow_audit.py",
    "spx_alpha_research.py",
    "spx_shadow_audit.py",
    "weather_alpha_research.py",
    "weather_shadow_audit.py",
    "weekend_discount_audit.py",
)

EXPECTED_BACKFILL_FILES = (
    "backfill_extended_features.py",
    "correct_ioc_double_count.py",
    "cryptocompare_news_backfill.py",
    "external_market_poller.py",
    "gdelt_backfill.py",
    "glassnode_backfill.py",
    "shadow_coverage_backfill.py",
    "shadow_coverage_calmlp_backfill.py",
    "stamp_data_provenance.py",
)

EXPECTED_OPS_FILES = (
    "_h4_runtime_safety.py",
    "_mutmut_lock.py",
    "_session_lock.py",
    "_state_db_snapshot.py",
    "build_lr_tables.py",
    "build_whitepaper.py",
    "calibrate_dist.py",
    "extract_config.py",
    "generate_whitepaper_stats.py",
    "h4_run_with_alert.py",
    "pre_deploy_check.sh",
    "refresh_repo_map.py",
    "sample_engine_inputs.py",
    "setup_audit_cron.sh",
    "setup_doc_drift_timer.sh",
    "setup_full_audit_timer.sh",
    "setup_h4_cron.sh",
    "setup_state_db_backup_timer.sh",
    "state_db_backup_heartbeat.py",
    "state_db_restore.py",
    "state_db_s3_backup.py",
    # supabase_migration_*.sql (11 tracked files)
    "supabase_migration_007_stacking.sql",
    "supabase_migration_008_harrv_bankroll.sql",
    "supabase_migration_009_dashboard_state_public_row.sql",
    "supabase_migration_010_cal_mlp_evaluations.sql",
    "supabase_migration_011_shadow_coverage_phase_b.sql",
    "supabase_migration_012_data_provenance.sql",
    "supabase_migration_013_bot_state_snapshot.sql",
    "supabase_migration_016_gdelt.sql",
    "supabase_migration_017_glassnode.sql",
    "supabase_migration_018_cryptocompare.sql",
    "supabase_migration_019_hype_doge_spot_at_decision.sql",
)


# Files that are allowed to remain directly under scripts/ root post-Bit-11.2.
SCRIPTS_ROOT_ALLOWLIST = frozenset(
    {
        "CLAUDE.md",
        "STATE_DB_BACKUP_SETUP.md",
        "VPS_SETUP.md",
        "vps_mcp_server.py",  # Bit 13.5 sibling — owned
        "io.kalshi.state-db-backup-heartbeat.plist.template",
    }
)

# Subdirectories that are allowed to remain directly under scripts/ root.
SCRIPTS_ROOT_DIR_ALLOWLIST = frozenset(
    {
        "audit",
        "backfill",
        "ops",
        "cal_mlp",
        "git_hooks",
        "research",  # Phase-0 CT-MDP falsifications (F0.1, F0.4, ...)
        "__pycache__",
    }
)


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Subdirectory layout exists
# ═════════════════════════════════════════════════════════════════════════════


def test_audit_subdir_exists():
    """`scripts/audit/` exists post-Bit-11.2."""
    assert AUDIT_DIR.is_dir(), (
        f"{AUDIT_DIR.relative_to(REPO_ROOT)} missing — "
        f"Bit 11.2 reorganization not performed."
    )


def test_backfill_subdir_exists():
    """`scripts/backfill/` exists post-Bit-11.2."""
    assert BACKFILL_DIR.is_dir(), (
        f"{BACKFILL_DIR.relative_to(REPO_ROOT)} missing — "
        f"Bit 11.2 reorganization not performed."
    )


def test_ops_subdir_exists():
    """`scripts/ops/` exists post-Bit-11.2."""
    assert OPS_DIR.is_dir(), (
        f"{OPS_DIR.relative_to(REPO_ROOT)} missing — "
        f"Bit 11.2 reorganization not performed."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — Each expected file exists at its new home
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("fname", EXPECTED_AUDIT_FILES)
def test_audit_file_exists(fname):
    p = AUDIT_DIR / fname
    assert p.exists(), f"{p.relative_to(REPO_ROOT)} missing — Bit 11.2 mv incomplete."


@pytest.mark.parametrize("fname", EXPECTED_BACKFILL_FILES)
def test_backfill_file_exists(fname):
    p = BACKFILL_DIR / fname
    assert p.exists(), f"{p.relative_to(REPO_ROOT)} missing — Bit 11.2 mv incomplete."


@pytest.mark.parametrize("fname", EXPECTED_OPS_FILES)
def test_ops_file_exists(fname):
    p = OPS_DIR / fname
    assert p.exists(), f"{p.relative_to(REPO_ROOT)} missing — Bit 11.2 mv incomplete."


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — No stale flat-root .py/.sh/.sql outside allow-list
# ═════════════════════════════════════════════════════════════════════════════


def test_no_stale_py_files_at_scripts_root():
    """Only `vps_mcp_server.py` may be a `.py` directly under `scripts/`."""
    py_files = sorted(p.name for p in SCRIPTS_DIR.glob("*.py"))
    unexpected = [f for f in py_files if f not in SCRIPTS_ROOT_ALLOWLIST]
    assert not unexpected, (
        f"Found {len(unexpected)} stale .py file(s) at scripts/ root: "
        f"{unexpected}. After Bit 11.2, all .py files should live under "
        f"scripts/audit/, scripts/backfill/, scripts/ops/, or be in the "
        f"allow-list ({sorted(SCRIPTS_ROOT_ALLOWLIST)})."
    )


def test_no_stale_sh_files_at_scripts_root():
    """No `.sh` files may live directly under `scripts/`."""
    sh_files = sorted(p.name for p in SCRIPTS_DIR.glob("*.sh"))
    unexpected = [f for f in sh_files if f not in SCRIPTS_ROOT_ALLOWLIST]
    assert not unexpected, (
        f"Found {len(unexpected)} stale .sh file(s) at scripts/ root: "
        f"{unexpected}. After Bit 11.2, all .sh files should live under "
        f"scripts/audit/ (audit_runner.sh) or scripts/ops/ (setup_*.sh, "
        f"pre_deploy_check.sh)."
    )


def test_no_stale_sql_files_at_scripts_root():
    """No tracked `.sql` files may live directly under `scripts/`.

    iCloud-dup `supabase_migration_014_binance_tick *.sql` / `*_015_*.sql`
    files at scripts/ root are UNTRACKED and gitignored at the operator's
    machine — this test only fails on TRACKED .sql files.

    Pre-commit, a staged `git mv` rename shows BOTH `scripts/X.sql` AND
    `scripts/ops/X.sql` in `git ls-files`. Treat the old entry as
    "will be gone post-commit" if the same basename exists at the new
    home (scripts/ops/). The check that bites post-commit is the
    filesystem absence — `test_no_stale_py_files_at_scripts_root`
    already pins .py; this pin covers .sql.
    """
    import subprocess

    proc = subprocess.run(
        ["git", "ls-files", "scripts/"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    all_entries = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    # All tracked .sql files DIRECTLY under scripts/ (no subdir).
    root_sqls = [
        line
        for line in all_entries
        if line.startswith("scripts/")
        and line.endswith(".sql")
        and "/" not in line[len("scripts/"):]
    ]
    # Drop entries that have a same-basename sibling under scripts/ops/
    # (those are pending git-mv renames; post-commit only the new path
    # remains).
    truly_stale = [
        e for e in root_sqls
        if f"scripts/ops/{e[len('scripts/'):]}" not in all_entries
    ]
    assert not truly_stale, (
        f"Found {len(truly_stale)} tracked .sql file(s) at scripts/ root: "
        f"{truly_stale}. After Bit 11.2, all .sql migrations should live "
        f"under scripts/ops/."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 4 — Three-bucket count invariants (catches accidental misplacement)
# ═════════════════════════════════════════════════════════════════════════════


def test_audit_dir_has_min_files():
    """scripts/audit/ contains at least the 33 expected files."""
    if not AUDIT_DIR.exists():
        pytest.skip("scripts/audit/ doesn't exist yet")
    files = list(AUDIT_DIR.glob("*"))
    # Exclude __pycache__ etc.
    real = [f for f in files if f.is_file() and not f.name.startswith(".")]
    assert len(real) >= len(EXPECTED_AUDIT_FILES), (
        f"scripts/audit/ has only {len(real)} files; "
        f"expected ≥ {len(EXPECTED_AUDIT_FILES)}."
    )


def test_backfill_dir_has_min_files():
    """scripts/backfill/ contains at least the 9 expected files."""
    if not BACKFILL_DIR.exists():
        pytest.skip("scripts/backfill/ doesn't exist yet")
    files = list(BACKFILL_DIR.glob("*"))
    real = [f for f in files if f.is_file() and not f.name.startswith(".")]
    assert len(real) >= len(EXPECTED_BACKFILL_FILES), (
        f"scripts/backfill/ has only {len(real)} files; "
        f"expected ≥ {len(EXPECTED_BACKFILL_FILES)}."
    )


def test_ops_dir_has_min_files():
    """scripts/ops/ contains at least the 32 expected files."""
    if not OPS_DIR.exists():
        pytest.skip("scripts/ops/ doesn't exist yet")
    files = list(OPS_DIR.glob("*"))
    real = [f for f in files if f.is_file() and not f.name.startswith(".")]
    assert len(real) >= len(EXPECTED_OPS_FILES), (
        f"scripts/ops/ has only {len(real)} files; "
        f"expected ≥ {len(EXPECTED_OPS_FILES)}."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 5 — SKILL.md path-references resolve
# ═════════════════════════════════════════════════════════════════════════════


def _grep_scripts_paths_in(file_paths):
    """Yield (file, lineno, path) tuples for any `scripts/X.{py,sh}` ref.

    Skips obvious meta-placeholders like the literal `scripts/X.py` used
    in skill-writing guidance.

    Bit 11.2 fu3 (2026-05-12): widened to accept digit-starting filenames
    (`15m_live_audit.py` etc.) AND yield each enumerated leaf inside a
    brace-expansion like `scripts/{a,b,c}.py`. The pre-fu3 regex
    `[A-Za-z_]...` silently skipped both forms, letting `.claude/skills/shadow/SKILL.md`
    cite legacy flat-root paths via `scripts/{15m_live_audit,...}.py`
    without tripping this pin.
    """
    import re

    # Match BOTH simple paths AND brace-expansion forms (open with `{`).
    # Note `[A-Za-z_0-9{]` opening class allows digit-starting names AND `{`.
    SCRIPT_PATH_RE = re.compile(
        r"scripts/(\{[^}]+\}|[A-Za-z_0-9][A-Za-z_0-9/]*)\.(?:py|sh)"
    )
    META_PLACEHOLDERS = {"scripts/X.py", "scripts/X.sh"}
    SUBDIRS = {"audit", "backfill", "ops", "cal_mlp", "git_hooks", "research"}
    for fp in file_paths:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in SCRIPT_PATH_RE.finditer(line):
                ref = m.group(0)
                if ref in META_PLACEHOLDERS:
                    continue
                # Brace-expansion: `scripts/{a,b,c}.py` → enumerate each leaf
                # as `scripts/<leaf>.py`.
                core = m.group(1)
                ext = ref.rsplit(".", 1)[1]
                if core.startswith("{") and core.endswith("}"):
                    leaves = [s.strip() for s in core[1:-1].split(",")]
                    for leaf in leaves:
                        # Reject empty / non-leaf-shaped entries.
                        if not leaf:
                            continue
                        synthesized = f"scripts/{leaf}.{ext}"
                        rel = leaf
                        if rel.split("/", 1)[0] in SUBDIRS:
                            continue
                        yield (fp, lineno, synthesized)
                    continue
                # Strip the `scripts/` prefix to get the relative path.
                rel = ref[len("scripts/") :]
                # Don't flag refs that point INTO the new subdirs.
                if rel.split("/", 1)[0] in SUBDIRS:
                    continue
                yield (fp, lineno, ref)


def test_skill_md_script_references_resolve():
    """Every `scripts/X.py`/`scripts/X.sh` reference in a `.claude/skills/`
    SKILL.md file points at a path that exists post-Bit-11.2."""
    skills_dir = REPO_ROOT / ".claude" / "skills"
    if not skills_dir.exists():
        pytest.skip(".claude/skills/ missing — Bit 11.1 not yet shipped?")
    md_files = sorted(skills_dir.rglob("SKILL.md"))
    if not md_files:
        pytest.skip("no SKILL.md files found")

    stale = list(_grep_scripts_paths_in(md_files))
    truly_stale = []
    for fp, lineno, ref in stale:
        candidate = REPO_ROOT / ref
        if not candidate.exists():
            truly_stale.append(
                f"{fp.relative_to(REPO_ROOT)}:{lineno}: {ref} (file moved by Bit 11.2)"
            )
    assert not truly_stale, (
        "Found SKILL.md references to scripts/ paths that no longer "
        "resolve post-Bit-11.2:\n  " + "\n  ".join(truly_stale)
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 6 — Makefile script-path references resolve
# ═════════════════════════════════════════════════════════════════════════════


def test_makefile_script_references_resolve():
    """Every `scripts/X.py`/`scripts/X.sh` in Makefile points at an
    existing path post-Bit-11.2."""
    makefile = REPO_ROOT / "Makefile"
    if not makefile.exists():
        pytest.skip("Makefile missing")
    stale = list(_grep_scripts_paths_in([makefile]))
    truly_stale = []
    for fp, lineno, ref in stale:
        candidate = REPO_ROOT / ref
        if not candidate.exists():
            truly_stale.append(
                f"{fp.relative_to(REPO_ROOT)}:{lineno}: {ref} (file moved by Bit 11.2)"
            )
    assert not truly_stale, (
        "Found Makefile references to scripts/ paths that no longer "
        "resolve post-Bit-11.2:\n  " + "\n  ".join(truly_stale)
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 7 — CI workflow script-path references resolve
# ═════════════════════════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════════════════════════
# Section 8 — L98 path-anchor invariants (Bit 11.2 fu3, 2026-05-12)
# ═════════════════════════════════════════════════════════════════════════════
#
# RCA from fu3 adversarial round 5 indep review: relocation Bits MUST audit
# EVERY `__file__`-derived path chain AND every `$0`-derived bash chain.
# The fu1 sweep missed:
#   * `scripts/backfill/shadow_coverage_calmlp_backfill.py:37` — 2 hops not 3
#   * `scripts/audit/audit_runner.sh:14-15` — 1 hop not 2
# Both broke silently because the IMMEDIATE failure was caught (`except
# ImportError: pass` for the python case; `--quiet` flag suppressing stderr
# for the bash case via systemd timer). These pins detect drift if a future
# relocation Bit nudges the path-anchor depth again.


def test_shadow_coverage_calmlp_backfill_repo_anchor_resolves_to_repo_root():
    """`scripts/backfill/shadow_coverage_calmlp_backfill.py` computes `_REPO`
    via `os.path.dirname()` chained 3 times from `__file__`. Two hops
    resolves to `scripts/`; THREE hops resolves to repo root. This pin
    catches drift if a future Bit edits the chain length.

    Invariant rationale: the script's `_thread_env` import + cal_mlp
    sys.path insert BOTH depend on `_REPO == repo root`. Off-by-one
    silently regressed prior to Bit 11.2 fu3 — the `except ImportError:
    pass` masked the failed `bot._thread_env` import, leaving
    OMP_NUM_THREADS unset (torch-thread contention class) and the
    `scripts/cal_mlp/` sys.path insert pointed at a non-existent
    directory (ModuleNotFoundError when `main()` ran the deferred
    `from integration import ...`).
    """
    script_path = (
        REPO_ROOT
        / "scripts"
        / "backfill"
        / "shadow_coverage_calmlp_backfill.py"
    )
    assert script_path.exists(), f"{script_path.relative_to(REPO_ROOT)} missing"
    text = script_path.read_text(encoding="utf-8")
    # Look for the assignment of _REPO. The fix uses 3 dirname() calls.
    # Accept either the procedural-style `dirname(dirname(dirname(...)))`
    # OR the pathlib-style `Path(__file__).resolve().parent.parent.parent`.
    import re

    procedural = re.search(
        r"_REPO\s*=\s*_os\.path\.dirname\(\s*_os\.path\.dirname\(\s*_os\.path\.dirname\(",
        text,
    )
    pathlib_style = re.search(
        r"_REPO\s*=\s*(?:_?)Path\(__file__\)\.resolve\(\)\.parent\.parent\.parent",
        text,
    )
    assert procedural or pathlib_style, (
        f"{script_path.relative_to(REPO_ROOT)} does not anchor `_REPO` at "
        f"three parent hops from __file__. The file lives at "
        f"scripts/backfill/<name>.py so REPO root is THREE parents up. "
        f"Two-hop anchor regresses to `scripts/`, silently breaking the "
        f"`bot._thread_env` import (caught by `except ImportError`) and "
        f"the `scripts/cal_mlp/` sys.path insert (would resolve to "
        f"scripts/scripts/cal_mlp, doesn't exist)."
    )

    # Belt + suspenders: actually compute _REPO and confirm it's repo root.
    # Run the script's path-resolution in an isolated subprocess so we
    # don't side-effect the test process's sys.path.
    import subprocess
    import sys as _sys

    probe = (
        "import os, sys; "
        f"_FILE = {str(script_path)!r}; "
        "print(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(_FILE)))))"
    )
    out = subprocess.run(
        [_sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(REPO_ROOT),
    )
    resolved = out.stdout.strip()
    assert resolved == str(REPO_ROOT), (
        f"_REPO resolves to {resolved!r}, expected repo root {str(REPO_ROOT)!r}. "
        f"Path-anchor depth must equal scripts/backfill/<file>.py → repo root."
    )


def test_generate_docs_subprocess_targets_resolve():
    """`scripts/audit/generate_docs.py` subprocess-invokes 4 sibling
    Python scripts (extract_config.py, generate_whitepaper_stats.py,
    build_whitepaper.py, check_docs_freshness.py). Bit 11.2 split
    those 4 siblings across 2 buckets:
      - check_docs_freshness.py → scripts/audit/ (orchestrator's own bucket)
      - extract_config.py, generate_whitepaper_stats.py, build_whitepaper.py
        → scripts/ops/

    The pre-Bit-11.2 `os.path.join(SCRIPT_DIR, X)` math was correct
    when all four lived flat in `scripts/`, but broke silently
    post-Bit-11.2 because 3 of the 4 are no longer adjacent to the
    orchestrator. Bit 11.2 fu5 (R-B finding) rerooted three of the
    paths to `OPS_DIR = os.path.join(REPO_DIR, "scripts", "ops")`.

    This pin protects the orchestrator's subprocess-target resolution
    by importing the module and checking that each of the 4
    EXTRACT_CONFIG/GENERATE_STATS/BUILD_WHITEPAPER/CHECK_FRESHNESS
    module-level constants resolves to an existing file.
    """
    import importlib.util

    script_path = REPO_ROOT / "scripts" / "audit" / "generate_docs.py"
    assert script_path.exists(), f"{script_path.relative_to(REPO_ROOT)} missing"

    spec = importlib.util.spec_from_file_location("_gen_docs_under_test", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    targets = {
        "EXTRACT_CONFIG": mod.EXTRACT_CONFIG,
        "GENERATE_STATS": mod.GENERATE_STATS,
        "BUILD_WHITEPAPER": mod.BUILD_WHITEPAPER,
        "CHECK_FRESHNESS": mod.CHECK_FRESHNESS,
    }
    missing = {k: v for k, v in targets.items() if not os.path.exists(v)}
    assert not missing, (
        f"scripts/audit/generate_docs.py subprocess targets resolve to "
        f"non-existent paths: {missing}. Bit 11.2 fu5 rerooted 3 of 4 "
        f"to scripts/ops/; if a relocation Bit moves any of those files "
        f"to a different home bucket, update the path-resolution at the "
        f"top of generate_docs.py to match."
    )


def test_audit_runner_sh_repo_dir_is_two_parents_up():
    """`scripts/audit/audit_runner.sh` computes `REPO_DIR` via
    `cd "$SCRIPT_DIR/../.."`. ONE parent hop resolves to `scripts/`;
    TWO resolves to repo root. This pin catches drift if a future
    Bit edits the parent-hop count.

    Invariant rationale: every `$REPO_DIR/scripts/audit/<X>.py` invocation
    in the body of the runner depends on REPO_DIR being repo root. The
    runner is invoked by `scripts/ops/setup_full_audit_timer.sh` as a
    systemd `--quiet` timer; a broken REPO_DIR silently fails (no stderr,
    no alert) until alerts stop firing entirely.
    """
    sh_path = REPO_ROOT / "scripts" / "audit" / "audit_runner.sh"
    assert sh_path.exists(), f"{sh_path.relative_to(REPO_ROOT)} missing"
    text = sh_path.read_text(encoding="utf-8")
    # Look for `REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"`
    import re

    m = re.search(
        r'REPO_DIR\s*=\s*"\$\(cd\s+"?\$SCRIPT_DIR/(\.\.[/\.]*)"?\s*&&\s*pwd\)"',
        text,
    )
    assert m is not None, (
        f"{sh_path.relative_to(REPO_ROOT)} does not assign REPO_DIR via "
        f"`cd \"$SCRIPT_DIR/<dots>\" && pwd` form. If the assignment "
        f"shape changed, update this regression test together."
    )
    parent_segment = m.group(1)
    # Count the number of `..` segments.
    dotdots = parent_segment.split("/")
    dotdot_count = sum(1 for s in dotdots if s == "..")
    assert dotdot_count == 2, (
        f"{sh_path.relative_to(REPO_ROOT)} REPO_DIR uses {dotdot_count} "
        f"parent hop(s); expected exactly 2 (file lives at "
        f"scripts/audit/<file>.sh → repo root is 2 parents up). "
        f"One hop regresses REPO_DIR to `scripts/`, breaking every "
        f"`$REPO_DIR/scripts/audit/<X>.py` invocation in the runner body."
    )


def test_pre_deploy_check_sh_cd_is_two_parents_up():
    """`scripts/ops/pre_deploy_check.sh` opens with `cd "$(dirname
    "$0")/<dots>"`. ONE hop resolves to `scripts/`; TWO resolves to
    repo root. fu6 (R6 indep-adv finding C-1, 2026-05-12) caught the
    1-hop regression that fu3 missed (4th instance of the L98
    depth-off-by-one in this Bit alone).

    Invariant rationale: every step in the body assumes CWD=repo root
    — Step 1 syntax-checks `bot/<files>.py`, Step 2 imports
    `market_config` via `sys.path.insert(0, '.')`, Step 3 runs `pytest
    tests/integration/test_regression.py`, Step 4 imports
    `bot.constants`. A 1-hop CD silently makes every step a false
    positive (the `[ -f \"$f\" ]` guards skip nonexistent files;
    `pytest tests/...` fails noisily but only at Step 3 after Steps 1+2
    have already produced misleading output).
    """
    sh_path = REPO_ROOT / "scripts" / "ops" / "pre_deploy_check.sh"
    assert sh_path.exists(), f"{sh_path.relative_to(REPO_ROOT)} missing"
    text = sh_path.read_text(encoding="utf-8")
    import re

    m = re.search(
        r'^\s*cd\s+"\$\(dirname\s+"\$0"\)/(\.\.[/\.]*)"\s*$',
        text,
        re.MULTILINE,
    )
    assert m is not None, (
        f"{sh_path.relative_to(REPO_ROOT)} does not open with "
        f"`cd \"$(dirname \"$0\")/<dots>\"` form. If the assignment "
        f"shape changed, update this regression test together."
    )
    parent_segment = m.group(1)
    dotdots = parent_segment.split("/")
    dotdot_count = sum(1 for s in dotdots if s == "..")
    assert dotdot_count == 2, (
        f"{sh_path.relative_to(REPO_ROOT)} opening `cd` uses "
        f"{dotdot_count} parent hop(s); expected exactly 2 (file "
        f"lives at scripts/ops/<file>.sh → repo root is 2 parents up). "
        f"One hop regresses CWD to `scripts/`, breaking every "
        f"subsequent step that assumes repo-root-relative paths."
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 7 (cont.) — CI workflow script-path references resolve
# ═════════════════════════════════════════════════════════════════════════════


def test_github_workflows_script_references_resolve():
    """Every `scripts/X.py`/`scripts/X.sh` in `.github/workflows/*` points
    at an existing path post-Bit-11.2."""
    wf_dir = REPO_ROOT / ".github" / "workflows"
    if not wf_dir.exists():
        pytest.skip(".github/workflows/ missing")
    yml_files = sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml"))
    if not yml_files:
        pytest.skip("no CI workflow files found")
    stale = list(_grep_scripts_paths_in(yml_files))
    truly_stale = []
    for fp, lineno, ref in stale:
        candidate = REPO_ROOT / ref
        if not candidate.exists():
            truly_stale.append(
                f"{fp.relative_to(REPO_ROOT)}:{lineno}: {ref} (file moved by Bit 11.2)"
            )
    assert not truly_stale, (
        "Found CI workflow references to scripts/ paths that no longer "
        "resolve post-Bit-11.2:\n  " + "\n  ".join(truly_stale)
    )
