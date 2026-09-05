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
     leaves ~7 GB of the 48 GB root — about one day of the 30 GB raw
     leak's growth rate is NOT enough, but the collector's 16 GB local
     bronze buffer makes ~71% the healthy steady state, so 85% is the
     first level that is both above steady state and actionable).
  3. `check_disk_usage(disk, disk_usage_fn=...)` returns None below the
     threshold and an alert string (mentioning DISK, the path, and the
     percentage) at/above it; a stat failure alerts rather than hides.
  4. `main()` dispatches disk checks with dedup key
     `monitor_watchdog_disk_<name>` and prints the alert text to stdout
     so the cron log keeps a history (the log-freshness alerts only ever
     reached Telegram — ~47K of them went unactioned).
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


def _usage_fn(pct: float):
    total = 48 * 1024 ** 3
    used = int(total * pct / 100.0)
    return lambda _path: _Usage(total=total, used=used, free=total - used)


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


def test_main_dispatches_disk_alert_with_dedup_key_and_logs_text(capsys):
    mod = _import_watchdog()
    fake_notifier = MagicMock()
    disks = (mod.WatchedDisk("root", "/", 0),)  # 0% → any usage trips
    rc = mod.main(monitors=(), notifier=fake_notifier, disks=disks)
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
    rc = mod.main(monitors=(), notifier=fake_notifier, disks=disks)
    assert rc == 0
    fake_notifier.send.assert_not_called()
