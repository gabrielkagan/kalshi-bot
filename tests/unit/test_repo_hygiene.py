"""Repo hygiene regression tests.

Sprint 0 of repo modularization plan (kb/decisions/repo-modularization-plan-may05.md).

RCA finding 2026-05-05: repo lives in iCloud Drive at
~/Library/Mobile Documents/com~apple~CloudDocs/Documents/kalshi-bot, surfaced
at ~/Documents/kalshi-bot via the macOS "Desktop & Documents in iCloud"
feature. Concurrent writes from multi-device/multi-session create
conflict-copy duplicates with the iCloud naming pattern: " <digit>" suffix
inserted before the extension (`foo 2.py`) or appended to a directory
(`models/cal_mlp_BTC 3/`). These tests catch regressions fast — they fail
within seconds if iCloud creates new conflict copies, before pytest
collects them as silent test duplicates that mask real failures.

Long-term fix: move repo out of iCloud Drive
(`mv ~/Documents/kalshi-bot ~/code/kalshi-bot`).

Distinguishing iCloud conflict copies from legitimate "version N" file
names: iCloud always creates a copy ALONGSIDE the canonical
(`foo.py` AND `foo 2.py` co-reside in the same directory). Legitimate
"version 2" names typically do not have a canonical sibling. The
`test_no_icloud_duplicate_files` test only flags the co-resident case.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# iCloud conflict pattern: a SPACE then digits, immediately before the
# extension (files) or at end of name (directories).
FILE_DUP_PATTERN = re.compile(r"^(.+) (\d+)(\.[A-Za-z0-9]+)$")
DIR_DUP_PATTERN = re.compile(r"^(.+) (\d+)$")

# Skip dirs that are externally-owned or build/cache dirs.
# Note: kb/ and kb-research/ are NOT excluded — iCloud conflicts land there
# too and noise pollution affects agent context window when reading.
# Co-residence check (canonical-sibling-must-exist) prevents false positives
# on legitimate "v2" naming.
EXCLUDED_DIRS = {
    "venv", ".venv", ".git", ".smart-env",
    ".pytest_cache", "__pycache__", "node_modules", "build", "dist",
    "htmlcov", ".mypy_cache", ".ruff_cache",
}

# Build artifacts whose names match a glob, not an exact directory name.
# Setuptools always emits `<distname>.egg-info/` with a dot prefix; the
# suffix here intentionally includes the leading `.` so a hypothetical
# `tests/fixtures/egg-info/` (no prefix) would NOT be wrongly excluded.
EXCLUDED_DIR_SUFFIXES = (".egg-info",)


def _excluded_part(name: str) -> bool:
    if name in EXCLUDED_DIRS:
        return True
    return any(name.endswith(s) for s in EXCLUDED_DIR_SUFFIXES)


def _walk_paths(want_dirs=False):
    """Yield Path objects for every file (or directory) under REPO_ROOT,
    skipping excluded dirs at any depth."""
    for path in REPO_ROOT.rglob("*"):
        if want_dirs and not path.is_dir():
            continue
        if not want_dirs and not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        if any(_excluded_part(part) for part in rel.parts):
            continue
        yield path


def test_no_icloud_duplicate_files():
    """Fail if any file is co-resident with an iCloud conflict-copy sibling.

    A file `foo 2.py` is flagged ONLY if `foo.py` exists in the same dir.
    This avoids false positives on legitimate "version N" file names.
    """
    dups = []
    for path in _walk_paths():
        m = FILE_DUP_PATTERN.match(path.name)
        if not m:
            continue
        base, _digit, ext = m.groups()
        canonical = path.with_name(base + ext)
        if canonical.exists():
            dups.append(path)
    assert not dups, (
        f"Found {len(dups)} iCloud conflict-copy files (each co-resident with a "
        f"canonical sibling). Root cause: repo is in iCloud Drive. Long-term fix: "
        f"`mv ~/Documents/kalshi-bot ~/code/kalshi-bot`. Immediate cleanup "
        f"(use -regex to match multi-digit suffixes like ` 12.py`):\n"
        f"  find . -maxdepth 6 -type f -regex '.* [0-9]+\\.[a-z]+' "
        f"-not -path './venv/*' -not -path './.smart-env/*' -delete\n"
        f"First 10:\n  " + "\n  ".join(str(p) for p in sorted(dups)[:10])
    )


def test_no_icloud_duplicate_directories():
    """Fail if any directory has an iCloud conflict-copy sibling.

    A directory `models/cal_mlp_BTC 3/` is flagged only if
    `models/cal_mlp_BTC/` also exists. iCloud creates these on
    concurrent dir-level writes (e.g., model bundle drops).
    """
    dups = []
    for path in _walk_paths(want_dirs=True):
        m = DIR_DUP_PATTERN.match(path.name)
        if not m:
            continue
        base, _digit = m.groups()
        canonical = path.with_name(base)
        if canonical.exists() and canonical.is_dir():
            dups.append(path)
    assert not dups, (
        f"Found {len(dups)} iCloud conflict-copy directories (each co-resident "
        f"with a canonical sibling). Cleanup empty ones with `rmdir`; for "
        f"non-empty, merge contents into the canonical first.\n"
        f"  " + "\n  ".join(str(p) for p in sorted(dups))
    )


def test_no_orphan_scheduled_task_locks():
    """Fail if conflict-copy .lock files exist in .claude/."""
    claude_dir = REPO_ROOT / ".claude"
    if not claude_dir.exists():
        return
    orphans = [
        p for p in claude_dir.glob("scheduled_tasks*.lock")
        if FILE_DUP_PATTERN.match(p.name)
    ]
    assert not orphans, (
        f"Found {len(orphans)} orphan scheduled_tasks*.lock conflict copies. "
        f"Same iCloud root cause as the file-dup test."
    )


def test_no_stale_doc_state_at_root():
    """DOC_STATE.md was a one-off extract (2026-03-20). Should not be re-introduced."""
    assert not (REPO_ROOT / "DOC_STATE.md").exists(), (
        "DOC_STATE.md re-appeared. It was deleted as a stale one-off doc dump. "
        "If you need a doc snapshot, prefer running scripts/doc_drift_check.py "
        "or generating fresh content."
    )


def test_no_doc_drift_report_committed_at_root():
    """DOC_DRIFT_REPORT.txt is a transient doc_drift_check.py artifact; must remain gitignored."""
    p = REPO_ROOT / "DOC_DRIFT_REPORT.txt"
    if not p.exists():
        return
    if shutil.which("git") is None:
        return  # Can't verify without git; skip rather than false-pass.
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "DOC_DRIFT_REPORT.txt"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=5,
    )
    assert result.returncode != 0, (
        "DOC_DRIFT_REPORT.txt is tracked by git. It is a transient artifact "
        "of scripts/doc_drift_check.py and must remain gitignored."
    )


def _parse_layout_class_table():
    """Extract (line, class_name) pairs from the layout doc's class table.

    Tolerates ASCII hyphen (-), en-dash (–), em-dash (—) as the range separator.
    """
    layout = (REPO_ROOT / "agent_docs" / "bot_layout.md").read_text()
    pairs = []
    for m in re.finditer(
        r"\|\s*(\d+)[–—\-]\d+\s*\|\s*`([A-Za-z_][A-Za-z0-9_]*)`",
        layout,
    ):
        pairs.append((int(m.group(1)), m.group(2)))
    return pairs


def _parse_bot_class_starts():
    """Return {class_name: start_line} for top-level classes in bot/_impl.py.

    Skips indented class defs (nested classes) — the layout doc only
    catalogues the top-level public classes.

    Bit 9.3-iii.c (2026-05-11): bot/_impl.py DELETED — returns empty when
    the file is absent (matches the existing empty-equals-empty branch in
    test_bot_layout_class_lines_match_bot_impl).
    """
    starts = {}
    bot_py = REPO_ROOT / "bot/_impl.py"
    if not bot_py.exists():
        return starts
    for i, line in enumerate(bot_py.read_text().splitlines(), start=1):
        m = re.match(r"^class ([A-Za-z_][A-Za-z0-9_]*)", line)
        if m:
            starts[m.group(1)] = i
    return starts


def test_bot_layout_class_lines_match_bot_impl():
    """Every class line range in bot_layout.md must point at the actual class def in bot/_impl.py.

    Post-Bit-9.3.5 (2026-05-10), bot/_impl.py is class-free — Sprint 9 closed.
    Both the layout doc's class table and `grep -nE '^class ' bot/_impl.py`
    return empty; the equality is the new ground truth. If a future Bit
    re-adds a class to bot/_impl.py, the layout doc must enumerate it; if a
    future Bit deletes bot/_impl.py entirely (Bit 9.3-ii), this test naturally
    passes the empty-equals-empty branch.
    """
    layout_pairs = _parse_layout_class_table()
    bot_starts = _parse_bot_class_starts()
    if not bot_starts and not layout_pairs:
        return  # Bit 9.3.5 endpoint: bot/_impl.py class-free, layout doc agrees
    assert layout_pairs, (
        "agent_docs/bot_layout.md has no parseable class table but bot/_impl.py "
        f"still has classes: {sorted(bot_starts)}. Regenerate the class table."
    )
    drift = []
    for layout_line, cls in layout_pairs:
        actual = bot_starts.get(cls)
        if actual is None:
            drift.append(f"  {cls}: in bot_layout.md but not in bot/_impl.py")
            continue
        if abs(actual - layout_line) > 5:
            drift.append(
                f"  {cls}: bot_layout says line {layout_line}, bot/_impl.py has it at {actual}"
            )
    assert not drift, (
        "agent_docs/bot_layout.md class line ranges drifted from bot/_impl.py. "
        "Regenerate per the doc's pinned regen command. Drift:\n"
        + "\n".join(drift)
    )


def test_bot_layout_total_lines_close_to_bot_impl():
    """If bot_layout.md cites a total bot/_impl.py line count, it must be within 200 of actual.
    Vacuous post-Bit-9.3-iii.c (bot/_impl.py deleted)."""
    if not (REPO_ROOT / "bot/_impl.py").exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — line-count assertion vacuous")
    layout = (REPO_ROOT / "agent_docs" / "bot_layout.md").read_text()
    if not (REPO_ROOT / "bot/_impl.py").exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c)")
    actual = sum(1 for _ in (REPO_ROOT / "bot/_impl.py").read_text().splitlines())
    matches = re.findall(r"\*?\*?(\d{1,3}[,]?\d{3,})\s*lines\*?\*?", layout)
    if not matches:
        return  # No total cited; nothing to check.
    cited = max(int(m.replace(",", "")) for m in matches)
    assert abs(cited - actual) < 200, (
        f"bot_layout.md cites ~{cited} lines for bot/_impl.py; actual is {actual}. "
        f"Difference {abs(cited - actual)} > 200-line tolerance. Regenerate the doc."
    )


@pytest.mark.xfail(
    reason="Repo lives in iCloud Drive; conflict-copy dups regenerate. "
    "Move with `mv ~/Documents/kalshi-bot ~/code/kalshi-bot` to clear.",
    strict=False,  # If the repo IS moved out, the test starts passing — fine.
)
def test_repo_not_inside_icloud_drive():
    """Warn (xfail) until the repo is moved out of iCloud Drive.

    Detection by inode-equality with the Mobile Documents shadow path.
    macOS "Desktop & Documents in iCloud" doesn't symlink — both paths
    surface the same inode via the fileprovider, so `Path.resolve()`
    alone won't reveal iCloud membership.

    Root cause documented at the top of this module. The fix is operator-side:
    `mv ~/Documents/kalshi-bot ~/code/kalshi-bot` (then update the systemd
    unit on the VPS / clone path on dev machines).

    Until then, every Sprint will be partially undone by iCloud conflict
    re-creation. This test makes that drag visible so it doesn't get forgotten.

    Skips on non-macOS hosts (Linux CI, VPS) where iCloud Drive doesn't apply.
    """
    if sys.platform != "darwin":
        return
    home = Path.home()
    icloud_root = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if not icloud_root.exists():
        return  # iCloud Drive not enabled on this machine.
    repo_name = REPO_ROOT.name
    shadow = icloud_root / "Documents" / repo_name
    if not shadow.exists():
        return  # Repo isn't iCloud-managed; nothing to warn about.
    try:
        same = REPO_ROOT.stat().st_ino == shadow.stat().st_ino
    except OSError:
        return
    assert not same, (
        f"Repo at {REPO_ROOT} is the same inode as iCloud-managed "
        f"{shadow}. Conflict-copy dups will keep regenerating. Fix:\n"
        f"  mv {REPO_ROOT} ~/code/{repo_name}\n"
        f"and update any hardcoded paths (systemd unit on VPS, dev "
        f"scripts in scripts/cal_mlp_mac_drain.py, etc.)."
    )
