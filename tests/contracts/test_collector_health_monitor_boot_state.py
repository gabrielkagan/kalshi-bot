"""`collector_health_monitor.check_boot_state` — alert when the Kalshi
collector reports `state == "booting"` for too long (ticket 86bbvdcat,
2026-09-05).

Before this Bit `bronze_health.json` was only written once the drain
thread ran, i.e. AFTER the synchronous REST page-through (54.9 min on
2026-09-05) — the monitor saw a stale-but-valid sidecar from the previous
process and `collector_active` (systemctl) said active. Nothing could
distinguish "booting for an hour" from "healthy".

Pins:
  1. `check_boot_state(sidecar_path=None, max_boot_seconds=..., now=None)`
     exists; `DEFAULT_MAX_BOOT_SECONDS == 1200` (same figure as the
     existing STALE boot grace — a boot that is still paging after 20 min
     is exactly the class the persisted-ticker boot was built to remove).
  2. None when the sidecar is absent / malformed / has no `state` key
     (older collectors, Coinbase/weather/ESPN sidecars) / is `running`.
  3. None while `booting` is younger than the threshold; alert string
     mentioning BOOTING once older.
  4. `main()` wires the check into the kalshi-collector tier.
"""
from __future__ import annotations

import inspect
import json
import re
import sys
import time
from pathlib import Path


def _import_monitor():
    repo = Path(__file__).resolve().parents[2]
    ops_dir = repo / "scripts" / "ops"
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))
    import collector_health_monitor
    return collector_health_monitor


def _iso(ts: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write(path: Path, **extra):
    payload = {
        "schema_version": 1,
        "written_at": _iso(time.time()),
        "archivers": [],
        "total_dropped_frames": 0,
        "total_queue_size": 0,
    }
    payload.update(extra)
    path.write_text(json.dumps(payload))


def test_check_boot_state_signature_and_default():
    mod = _import_monitor()
    sig = inspect.signature(mod.check_boot_state)
    assert {"sidecar_path", "max_boot_seconds", "now"} <= set(sig.parameters)
    assert mod.DEFAULT_MAX_BOOT_SECONDS == 1200
    assert sig.parameters["max_boot_seconds"].default == mod.DEFAULT_MAX_BOOT_SECONDS


def test_check_boot_state_none_when_absent_malformed_or_stateless(tmp_path: Path):
    mod = _import_monitor()
    missing = tmp_path / "missing.json"
    assert mod.check_boot_state(sidecar_path=missing) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert mod.check_boot_state(sidecar_path=bad) is None
    stateless = tmp_path / "old.json"
    _write(stateless)
    assert mod.check_boot_state(sidecar_path=stateless) is None


def test_check_boot_state_none_when_running_or_young(tmp_path: Path):
    mod = _import_monitor()
    now = time.time()
    running = tmp_path / "running.json"
    _write(running, state="running", state_since=_iso(now - 5000))
    assert mod.check_boot_state(sidecar_path=running, now=now) is None
    young = tmp_path / "young.json"
    _write(young, state="booting", state_since=_iso(now - 100))
    assert mod.check_boot_state(sidecar_path=young, max_boot_seconds=1200, now=now) is None


def test_check_boot_state_alerts_when_booting_too_long(tmp_path: Path):
    mod = _import_monitor()
    now = time.time()
    slow = tmp_path / "slow.json"
    _write(slow, state="booting", state_since=_iso(now - 2000),
           ticker_set_source="rest")
    alert = mod.check_boot_state(sidecar_path=slow, max_boot_seconds=1200, now=now)
    assert alert and "BOOTING" in alert
    # The sidecar stamp round-trips through %f microseconds, so the age can
    # land on 1999.999… → int() 1999; accept the ±1 s window rather than a
    # literal (first full-tier run flaked on exactly this).
    m = re.search(r"for (\d+)s", alert)
    assert m and 1998 <= int(m.group(1)) <= 2001, f"alert should state the boot age: {alert!r}"


def test_main_wires_boot_state_into_kalshi_tier():
    mod = _import_monitor()
    src = inspect.getsource(mod.main)
    kalshi_block = src.split("kalshi_checks = [", 1)[1].split("]", 1)[0]
    assert re.search(r'\("boot_state",\s*lambda:\s*check_boot_state\(', kalshi_block), (
        "kalshi_checks must include ('boot_state', lambda: check_boot_state(...))"
    )
