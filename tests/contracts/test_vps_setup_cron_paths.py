"""Data-Integrity A.1 (ticket 86ba0xmmq, 2026-05-19) — pin VPS_SETUP.md cron paths.

`scripts/VPS_SETUP.md` documents the cron lines installed on the production
VPS (botuser@45.55.181.30). The 2026-05-17 ``data_health_monitor`` silent-death
incident traced to live VPS crontab having a stale script path (`scripts/data_health_monitor.py`)
that no longer existed post-Bit-11.2 (relocated 2026-05-12 to
`scripts/audit/data_health_monitor.py`). VPS_SETUP.md was already correct, but
nothing pinned the documented template against future drift.

This contract test parses the cron block in VPS_SETUP.md and asserts every
Python script invocation references a file that exists in the repo at the
documented path. Going forward, any rename/move of a script invoked from
cron must update VPS_SETUP.md in the same commit or this test fails.

Sister doc protected: `scripts/VPS_SETUP.md` is the canonical cron template.
The live VPS crontab is operational state (not in git); this test pins the
template, monitor-the-monitor cron (Stage E ticket 86ba0xq51) will catch
operational drift.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VPS_SETUP_MD = REPO_ROOT / "scripts" / "VPS_SETUP.md"


def _extract_cron_block(text: str) -> list[str]:
    """Return cron lines from the first ```cron fenced block in VPS_SETUP.md.

    Filters out blank lines and comment-only lines (starting with #).
    """
    match = re.search(r"```cron\n(.*?)```", text, re.DOTALL)
    if not match:
        return []
    lines = []
    for raw in match.group(1).splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def _extract_python_script_paths(cron_line: str) -> list[str]:
    """Extract ``.py`` script paths invoked via python/python3 in a cron line.

    Handles:
      - Bare ``python3 scripts/foo.py``
      - venv-prefixed ``/path/to/venv/bin/python scripts/foo.py``
      - Trailing args after the script (``scripts/foo.py --db state.db``)
      - Single-token flags before the script (``python -u scripts/foo.py``)

    Does NOT handle ``python3 -m <module>`` (module-form): ``-m`` consumes
    the next token as the module name, and any subsequent ``.py`` token is
    a positional arg to the module, not the script being run. No documented
    cron line uses module-form today; if one is added, extend the regex
    and the docstring accordingly.

    Returns paths AS-WRITTEN (not resolved). Caller path-resolves.
    """
    # Match any python/python3 invocation followed by a .py file.
    # Allow optional single-token flags between python and the .py path
    # (e.g. ``python -u``); module-form ``python -m <module>`` is NOT covered.
    pattern = re.compile(
        r"(?:\S*python(?:3|3\.\d+)?)\s+(?:-\S+\s+)*(\S+\.py)\b"
    )
    return pattern.findall(cron_line)


@pytest.fixture(scope="module")
def vps_setup_text() -> str:
    assert VPS_SETUP_MD.is_file(), f"VPS_SETUP.md missing at {VPS_SETUP_MD}"
    return VPS_SETUP_MD.read_text()


@pytest.fixture(scope="module")
def cron_lines(vps_setup_text: str) -> list[str]:
    return _extract_cron_block(vps_setup_text)


def test_vps_setup_cron_block_extractable(cron_lines: list[str]) -> None:
    """Sanity guard: ```cron fenced block exists and has ≥1 line."""
    assert len(cron_lines) >= 1, (
        "No cron lines extracted from scripts/VPS_SETUP.md. Either the ```cron "
        "fenced block was removed/renamed, or all lines are comments. If the "
        "doc was reorganized, update _extract_cron_block() to match."
    )


def test_every_cron_python_invocation_references_existing_script(
    cron_lines: list[str],
) -> None:
    """Every ``.py`` script invoked from cron must exist at the documented path.

    Regression class: Bit 11.2 (2026-05-12) relocated scripts into subdirs;
    live VPS crontab drifted because nothing pinned the documented template.
    """
    missing: list[tuple[str, str]] = []  # (line, missing_path)
    found_any_invocation = False

    for line in cron_lines:
        for script_path in _extract_python_script_paths(line):
            found_any_invocation = True
            resolved = REPO_ROOT / script_path
            if not resolved.is_file():
                missing.append((line, script_path))

    assert found_any_invocation, (
        "No python/python3 script invocations found in the cron block. "
        "Either the doc has no python cron entries, or the regex in "
        "_extract_python_script_paths() needs updating to match new patterns."
    )

    assert not missing, (
        "VPS_SETUP.md documents cron lines that invoke scripts which don't "
        "exist in the repo:\n"
        + "\n".join(f"  path={p!r} in line={l!r}" for l, p in missing)
        + "\n\nEither the script was renamed/moved (update VPS_SETUP.md), or "
        "VPS_SETUP.md has a typo. This pins the doc template against the "
        "drift class that caused the 2026-05-17 data_health_monitor silent death."
    )


def test_data_health_monitor_documented_at_canonical_post_bit_11_2_path(
    vps_setup_text: str,
    cron_lines: list[str],
) -> None:
    """Explicit pin for the specific drift class fixed by Bit 11.2.

    Bit 11.2 (Sprint 11, 2026-05-12) relocated scripts into subdirs;
    ``scripts/data_health_monitor.py`` became ``scripts/audit/data_health_monitor.py``.
    The bare ``scripts/data_health_monitor.py`` path must not appear INSIDE
    A CRON INVOCATION — that's the literal that was wrong on the live VPS
    crontab for 2 days (2026-05-17 → 2026-05-19). The IMPORTANT
    documentation block above the cron fence is allowed to name the stale
    path for explanatory purposes (e.g., "the pre-Bit-11.2 path
    scripts/data_health_monitor.py was relocated to scripts/audit/"). Only
    cron invocations are checked.
    """
    assert "scripts/audit/data_health_monitor.py" in vps_setup_text, (
        "VPS_SETUP.md must reference data_health_monitor at its canonical "
        "post-Bit-11.2 path (`scripts/audit/data_health_monitor.py`). If "
        "Bit 11.2 was reverted or the script relocated again, update this "
        "pin AND update the live VPS crontab."
    )

    # Check cron INVOCATIONS only (not the surrounding prose), so the
    # explanatory IMPORTANT block can reference the stale literal in
    # documentation form without tripping the test.
    bare_pattern = re.compile(r"(?<![/\w])scripts/data_health_monitor\.py")
    offending_invocations = [line for line in cron_lines if bare_pattern.search(line)]
    assert not offending_invocations, (
        "A cron invocation in VPS_SETUP.md references the bare "
        "`scripts/data_health_monitor.py` path. That path was deleted in "
        "Bit 11.2 (2026-05-12) when scripts were reorganized into "
        "`scripts/audit/`. Use `scripts/audit/data_health_monitor.py` "
        "instead. This is the literal drift class that caused the "
        "2026-05-17 silent monitor death. Offending line(s):\n"
        + "\n".join(f"  {l!r}" for l in offending_invocations)
    )
