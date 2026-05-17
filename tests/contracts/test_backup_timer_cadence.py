"""Backup timer cadence contract pins (ticket 86b9zkp89, 2026-05-17).

Bronze durability: state.db + journal_archives sync must run sub-daily
so a VPS failure between scheduled runs loses at most ~4 hours of
training data, NOT 24 hours.

This file pins the systemd `OnCalendar=` values in the timer-installer
scripts under `scripts/ops/`. Tests use plain file reads + regex; no
systemd dep — they verify the installer would emit the documented
cadence when run on the VPS.

Pins:
  1. state.db backup timer fires every 4h (00:00, 04:00, ..., 20:00 UTC).
  2. journal_archives sync timer fires every 4h, 30 min offset
     (00:30, 04:30, ..., 20:30 UTC), so each rotation has 30 min to
     compress before sync.
  3. state.db restore-verify remains weekly (Sun 07:00 UTC) — this
     Bit's scope is BACKUP cadence, not verification cadence.
  4. market_obs archive remains daily 05:30 UTC — out of scope this
     Bit (weekly volume is tiny; ~1 row/sec for 14d only).
  5. No other setup_*_timer.sh OnCalendar value silently drifted in
     this Bit — exhaustive baseline check catches sister-timer typos.

Cadence regex `\\*-\\*-\\* 00/4:MM:SS` is systemd's every-4h-from-00
syntax: start=00, step=4, applied to the hour field. Equivalent forms
(`0/4`, `00/04`) are also accepted to avoid pinning incidental spelling.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_DIR = REPO_ROOT / "scripts" / "ops"


def _read(rel: str) -> str:
    path = OPS_DIR / rel
    assert path.is_file(), f"expected installer at {path}"
    return path.read_text()


def _oncalendar_lines(text: str) -> list[str]:
    """Return all OnCalendar= lines from the installer script (heredoc bodies)."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("OnCalendar=")
    ]


# Accept any of: `00/4`, `0/4`, `00/04`, `0/04` — systemd treats these
# identically. We accept multiple spellings so cosmetic edits don't
# trip the test, but reject single-time daily patterns (no `/`).
_EVERY_4H_HOUR = r"(?:0?0|0)/0?4"


def test_state_db_backup_timer_fires_every_4h():
    """state.db backup OnCalendar must be every-4h on the hour."""
    text = _read("setup_state_db_backup_timer.sh")
    lines = _oncalendar_lines(text)
    # The script emits exactly 2 OnCalendar lines (backup + restore-verify).
    # Pin: backup line uses every-4h pattern; minute=00, second=00.
    backup_pat = re.compile(rf"^OnCalendar=\*-\*-\* {_EVERY_4H_HOUR}:00:00$")
    matches = [ln for ln in lines if backup_pat.match(ln)]
    assert len(matches) == 1, (
        f"setup_state_db_backup_timer.sh must declare exactly ONE every-4h "
        f"OnCalendar= line for the backup unit (pattern: "
        f"`OnCalendar=*-*-* 00/4:00:00`). Found OnCalendar lines: {lines}"
    )
    # And reject single-time daily fallback (defense against partial edit).
    daily_pat = re.compile(r"^OnCalendar=\*-\*-\* 06:00:00$")
    assert not any(daily_pat.match(ln) for ln in lines), (
        f"setup_state_db_backup_timer.sh still contains daily `06:00:00` "
        f"OnCalendar — backup cadence should be every-4h. Lines: {lines}"
    )


def test_journal_sync_timer_fires_every_4h():
    """journal_archives sync OnCalendar must be every-4h, 30-min offset."""
    text = _read("setup_journal_archives_sync_timer.sh")
    lines = _oncalendar_lines(text)
    # Pin: minute=30, every-4h on the hour. Offset is load-bearing (each
    # rotate_journals.sh tick at HH:00 has 30 min to compress before sync).
    sync_pat = re.compile(rf"^OnCalendar=\*-\*-\* {_EVERY_4H_HOUR}:30:00$")
    matches = [ln for ln in lines if sync_pat.match(ln)]
    assert len(matches) == 1, (
        f"setup_journal_archives_sync_timer.sh must declare exactly ONE "
        f"every-4h OnCalendar= line for the sync unit (pattern: "
        f"`OnCalendar=*-*-* 00/4:30:00`, 30 min after each rotation tick). "
        f"Found OnCalendar lines: {lines}"
    )
    daily_pat = re.compile(r"^OnCalendar=\*-\*-\* 04:30:00$")
    assert not any(daily_pat.match(ln) for ln in lines), (
        f"setup_journal_archives_sync_timer.sh still contains daily "
        f"`04:30:00` OnCalendar — sync cadence should be every-4h. "
        f"Lines: {lines}"
    )


def test_state_db_restore_verify_remains_weekly():
    """Weekly restore-verify cadence is OUT OF SCOPE — guard against
    this Bit accidentally bumping the weekly verify to sub-daily."""
    text = _read("setup_state_db_backup_timer.sh")
    lines = _oncalendar_lines(text)
    weekly_pat = re.compile(r"^OnCalendar=Sun \*-\*-\* 07:00:00$")
    matches = [ln for ln in lines if weekly_pat.match(ln)]
    assert len(matches) == 1, (
        f"setup_state_db_backup_timer.sh must keep the weekly restore-verify "
        f"line `OnCalendar=Sun *-*-* 07:00:00` (this Bit's scope is BACKUP "
        f"cadence only). Found OnCalendar lines: {lines}"
    )


def test_market_obs_archive_remains_daily():
    """market_obs archive cadence is OUT OF SCOPE — guard against
    this Bit accidentally touching the sister timer."""
    text = _read("setup_market_obs_archive_timer.sh")
    lines = _oncalendar_lines(text)
    daily_pat = re.compile(r"^OnCalendar=\*-\*-\* 05:30:00$")
    matches = [ln for ln in lines if daily_pat.match(ln)]
    assert len(matches) == 1, (
        f"setup_market_obs_archive_timer.sh must keep its daily 05:30 UTC "
        f"OnCalendar (out of scope for this Bit). Found OnCalendar lines: "
        f"{lines}"
    )


def test_no_other_oncalendar_lines_drifted():
    """Exhaustive scan over scripts/ops/setup_*_timer.sh: each OnCalendar
    value must match the documented baseline. Catches typos in sister
    timers (full_audit, doc_drift) that share the directory."""
    # Baseline = the union of expected OnCalendar values across ALL
    # setup_*_timer.sh installers. Any deviation = either this Bit
    # touched a sister timer it shouldn't, OR a future Bit added a
    # new timer without updating this baseline.
    expected_per_file = {
        "setup_state_db_backup_timer.sh": {
            # backup (every-4h on the hour) + restore-verify (weekly Sun 07:00)
            re.compile(rf"^OnCalendar=\*-\*-\* {_EVERY_4H_HOUR}:00:00$"),
            re.compile(r"^OnCalendar=Sun \*-\*-\* 07:00:00$"),
        },
        "setup_journal_archives_sync_timer.sh": {
            re.compile(rf"^OnCalendar=\*-\*-\* {_EVERY_4H_HOUR}:30:00$"),
        },
        "setup_market_obs_archive_timer.sh": {
            re.compile(r"^OnCalendar=\*-\*-\* 05:30:00$"),
        },
        "setup_doc_drift_timer.sh": {
            re.compile(r"^OnCalendar=\*-\*-\* 06:00:00$"),
        },
        "setup_full_audit_timer.sh": {
            re.compile(r"^OnCalendar=\*-\*-\* 00,06,12,18:15:00 UTC$"),
        },
    }
    installers = sorted(OPS_DIR.glob("setup_*_timer.sh"))
    found_names = {p.name for p in installers}
    expected_names = set(expected_per_file.keys())
    assert found_names == expected_names, (
        f"setup_*_timer.sh inventory drifted from baseline. "
        f"Found: {sorted(found_names)}. Expected: {sorted(expected_names)}. "
        f"If a new timer was added, extend this test's baseline."
    )
    for name, expected_patterns in expected_per_file.items():
        text = _read(name)
        lines = _oncalendar_lines(text)
        # Every OnCalendar line must match one of the expected patterns.
        unmatched = [
            ln for ln in lines
            if not any(p.match(ln) for p in expected_patterns)
        ]
        assert not unmatched, (
            f"{name} contains OnCalendar lines that don't match the "
            f"documented baseline: {unmatched}. Expected patterns: "
            f"{[p.pattern for p in expected_patterns]}"
        )
        # Every expected pattern must match at least one line.
        unused_patterns = [
            p for p in expected_patterns
            if not any(p.match(ln) for ln in lines)
        ]
        assert not unused_patterns, (
            f"{name} is MISSING OnCalendar lines for expected patterns: "
            f"{[p.pattern for p in unused_patterns]}. Found lines: {lines}"
        )
