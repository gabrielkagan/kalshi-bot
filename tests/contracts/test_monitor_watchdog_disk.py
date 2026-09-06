"""`scripts/ops/monitor_watchdog.py` — root-filesystem usage threshold
(ticket 86bbvd50a, 2026-09-05).

RCA: the VPS root filesystem sat at ~95% used from before 2026-08-10 and
hit 100% on 2026-09-04 (kb/failures/vps-disk-full-journal-rotation-collision-sep05.md).
The 80% canary in `collector_health_monitor.py` never fired because its
cron line runs under /bin/sh (it sits above `SHELL=/bin/bash` in the
crontab; `source` is not a dash builtin) — dead since 2026-05-19. The
monitor-the-monitor (`monitor_watchdog.py`) IS alive (its line is below
the SHELL= directive), so it is the right home for a second, independent
disk-usage alert.

Pins:
  1. `WatchedDisk(name, path, max_used_pct)` frozen dataclass.
  2. `WATCHED_DISKS` covers `/` at 85% (the VPS is one filesystem; 85%
     leaves ~7 GB of the 48 GB root ≈ 25 days of the raw-leak growth
     rate (30.2 GB / 109 d ≈ 0.28 GB/d); healthy steady state is ~30%
     used (~14 GB, measured 29% on 2026-09-05 post-recovery), so 85% is
     far above steady state and still actionable).
  3. `check_disk_usage(disk, disk_usage_fn=...)` returns None below the
     threshold and an alert string (mentioning DISK, the path, and the
     percentage) at/above it; a stat failure alerts rather than hides.
     Percentage = used / (used + free), i.e. df's Use% (R1-M4) — NOT
     used / total, which hides the ext4 reserved blocks.
  4. `main()` dispatches disk checks with dedup key
     `monitor_watchdog_disk_<name>` and prints the alert text to stdout
     so the cron log keeps a history (the log-freshness alerts only ever
     reached Telegram — ~47K of them went unactioned).
  5. `check_rotation_errors(log_path)` reads the `Done. ... errors=N`
     footer ops/rotate_journals.sh writes per run and alerts on N>0
     (cron ignores exit codes under the `>> rotation.log` redirect);
     main() dispatches it with dedup key
     `monitor_watchdog_journal_rotation_errors`.
"""
from __future__ import annotations

import collections
import dataclasses
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _import_watchdog():
    repo = Path(__file__).resolve().parents[2]
    ops_dir = repo / "scripts" / "ops"
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))
    import monitor_watchdog
    return monitor_watchdog


_Usage = collections.namedtuple("usage", "total used free")


def _usage_fn(pct: float, reserved_frac: float = 0.05):
    """df-style fixture: Use% = used / (used + free); ``total`` also carries
    the reserved blocks that neither used nor free include."""
    usable = 48 * 1024 ** 3
    used = int(usable * pct / 100.0)
    free = usable - used
    total = int(usable * (1 + reserved_frac))
    return lambda _path: _Usage(total=total, used=used, free=free)


def test_watched_disk_dataclass_shape():
    mod = _import_watchdog()
    fields = {f.name for f in dataclasses.fields(mod.WatchedDisk)}
    assert fields == {"name", "path", "max_used_pct"}
    wd = mod.WatchedDisk("root", "/", 85)
    with pytest.raises(Exception):
        wd.name = "x"  # type: ignore[misc]


def test_default_watches_root_at_85_pct():
    mod = _import_watchdog()
    by_path = {d.path: d for d in mod.WATCHED_DISKS}
    assert "/" in by_path, "root filesystem must be watched (single-fs VPS)"
    assert by_path["/"].max_used_pct == 85


def test_check_disk_usage_silent_below_threshold():
    mod = _import_watchdog()
    disk = mod.WatchedDisk("root", "/", 85)
    assert mod.check_disk_usage(disk, disk_usage_fn=_usage_fn(71.0)) is None


def test_check_disk_usage_alerts_at_or_above_threshold():
    mod = _import_watchdog()
    disk = mod.WatchedDisk("root", "/", 85)
    for pct in (85.0, 96.0, 100.0):
        alert = mod.check_disk_usage(disk, disk_usage_fn=_usage_fn(pct))
        assert alert, f"expected an alert at {pct}%"
        assert "DISK" in alert and "/" in alert and "%" in alert
        assert "85" in alert, "alert must state the threshold for operator context"


def test_check_disk_usage_alerts_when_stat_fails():
    mod = _import_watchdog()
    disk = mod.WatchedDisk("root", "/nonexistent-mount", 85)

    def boom(_path):
        raise OSError("no such mount")

    alert = mod.check_disk_usage(disk, disk_usage_fn=boom)
    assert alert and "DISK" in alert


def test_check_disk_usage_matches_df_not_used_over_total():
    """used/total would read 81% here (5% reserved); df reads 85%."""
    mod = _import_watchdog()
    disk = mod.WatchedDisk("root", "/", 85)
    assert mod.check_disk_usage(disk, disk_usage_fn=_usage_fn(85.0, reserved_frac=0.05))


def test_main_dispatches_disk_alert_with_dedup_key_and_logs_text(capsys):
    mod = _import_watchdog()
    fake_notifier = MagicMock()
    disks = (mod.WatchedDisk("root", "/", 0),)  # 0% → any usage trips
    rc = mod.main(monitors=(), notifier=fake_notifier, disks=disks, rotation_log=None)
    assert rc == 0
    fake_notifier.send.assert_called_once()
    _, kwargs = fake_notifier.send.call_args
    assert kwargs.get("dedup_key") == "monitor_watchdog_disk_root"
    out = capsys.readouterr().out
    assert "DISK" in out, "alert text must be printed so the cron log keeps history"


def test_main_no_disk_alert_when_below_threshold():
    mod = _import_watchdog()
    fake_notifier = MagicMock()
    disks = (mod.WatchedDisk("root", "/", 101),)  # never trips
    rc = mod.main(monitors=(), notifier=fake_notifier, disks=disks, rotation_log=None)
    assert rc == 0
    fake_notifier.send.assert_not_called()


# ─── rotation errors marker ──────────────────────────────────────────────────


def test_check_rotation_errors_none_when_missing_or_clean(tmp_path):
    mod = _import_watchdog()
    assert mod.check_rotation_errors(str(tmp_path / "nope.log")) is None
    log = tmp_path / "rotation.log"
    log.write_text("SKIP x\nROTATED y\nDone. Disk free: 30G errors=0\n")
    assert mod.check_rotation_errors(str(log)) is None
    legacy = tmp_path / "legacy.log"
    legacy.write_text("Done. Disk free: 30G\n")  # pre-Bit script, no marker
    assert mod.check_rotation_errors(str(legacy)) is None


def test_check_rotation_errors_alerts_on_last_run_errors(tmp_path):
    mod = _import_watchdog()
    log = tmp_path / "rotation.log"
    log.write_text(
        "Done. Disk free: 30G errors=0\n"
        "ERROR opportunity_journal.jsonl: compress failed\n"
        "Done. Disk free: 30G errors=1\n"
    )
    alert = mod.check_rotation_errors(str(log))
    assert alert and "ROTATION" in alert and "errors=1" in alert
    # A later clean run clears it.
    log.write_text(log.read_text() + "Done. Disk free: 30G errors=0\n")
    assert mod.check_rotation_errors(str(log)) is None


def test_main_dispatches_rotation_errors_alert(tmp_path):
    mod = _import_watchdog()
    log = tmp_path / "rotation.log"
    log.write_text("Done. Disk free: 1G errors=2\n")
    fake_notifier = MagicMock()
    rc = mod.main(monitors=(), notifier=fake_notifier, disks=(), rotation_log=str(log))
    assert rc == 0
    fake_notifier.send.assert_called_once()
    assert fake_notifier.send.call_args.kwargs["dedup_key"] == "monitor_watchdog_journal_rotation_errors"
