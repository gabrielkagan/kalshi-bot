"""D1.6 fu — BronzeArchiver health sidecar + collector_health_monitor
dropped-frames check (ticket 86b9zkktr, 2026-05-17).

Predecessor: D1.3-fu4 (`86b9zk4hz`) closed the 1011 keepalive-ping-timeout
storm class by decoupling BronzeArchiver._on_frame writes onto a bounded
queue + worker thread. The fix exposes ``BronzeArchiver._dropped_frames``
which increments when the write queue hits capacity.

Today the only operator-facing signal that fu4 is working is the absence
of 1011 reconnect alerts. This Bit adds the symmetric positive observable:

  1. BronzeArchiver.get_health_snapshot() returns a per-archiver health
     dict with conn_id, dropped_frames, write_queue_size,
     write_queue_maxsize, write_worker_alive, collector_seq.
  2. collector.main_loop writes an aggregated JSON sidecar to
     COLLECTOR_HEALTH_SIDECAR_PATH (default
     /var/lib/kalshi-collector/bronze_health.json) on every drain-thread
     tick so an external monitor can read in-process state without IPC.
  3. scripts/ops/collector_health_monitor.py gains a check_dropped_frames
     function that reads the sidecar, persists last-seen drop totals to
     a state file, computes delta, and Telegram-alerts when delta
     exceeds threshold over a tracked window.

Pins:

  1. BronzeArchiver.get_health_snapshot exists + returns the 6 required
     keys with correct types.
  2. write_bronze_health_sidecar(archivers, path) writes valid JSON
     matching the schema (schema_version, written_at ISO-8601,
     archivers list, total_dropped_frames, total_queue_size).
  3. check_dropped_frames returns None when delta == 0.
  4. check_dropped_frames returns alert string when delta >= threshold.
  5. check_dropped_frames persists state across calls (subsequent
     deltas correctly computed against the LAST seen value).
  6. check_dropped_frames returns None when sidecar absent (test env,
     pre-bronze-day-zero — don't alert-spam).
  7. check_dropped_frames returns None when sidecar exists but state
     file is fresh-init (first run after install; baseline is the
     current snapshot, not zero — otherwise the first tick would alert
     on the cumulative-since-process-start total).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


# ─── 1. BronzeArchiver.get_health_snapshot ──────────────────────────────────


def test_bronze_archiver_get_health_snapshot_returns_required_keys(monkeypatch):
    """Per-archiver snapshot must expose the 7 keys the sidecar aggregator
    + the monitor's check_dropped_frames depend on (6 D1.6-fu base keys +
    `ack_frames_processed` added in D1.3-fu5 as additive backward-compat).
    """
    from collector import ws_connection as wc

    monkeypatch.setattr(wc, "load_private_key", lambda _p: object())
    from unittest.mock import MagicMock
    fake_wire = MagicMock(name="WSClient")
    monkeypatch.setattr(wc, "WSClient", MagicMock(return_value=fake_wire))

    writer = MagicMock(name="writer")
    archiver = wc.BronzeArchiver(
        api_key="K",
        private_key_path="/nonexistent.pem",
        writers_by_channel={None: writer, "orderbook_delta": writer},
        subscribe_frames=[],
        cmd_id_to_channel={},
        conn_id="A",
        write_queue_maxsize=100,
    )
    snap = archiver.get_health_snapshot()
    required = {
        "conn_id", "dropped_frames", "write_queue_size",
        "write_queue_maxsize", "write_worker_alive", "collector_seq",
        "ack_frames_processed",  # D1.3-fu5 observability addition
    }
    missing = required - set(snap.keys())
    assert not missing, (
        f"get_health_snapshot missing keys: {missing}. Required: {required}."
    )
    assert snap["conn_id"] == "A"
    assert snap["dropped_frames"] == 0  # fresh archiver
    assert snap["write_queue_maxsize"] == 100
    assert snap["collector_seq"] == 0
    # Worker not yet started → False (no leaked thread).
    assert snap["write_worker_alive"] is False


def test_bronze_archiver_health_snapshot_after_start_shows_alive(monkeypatch):
    """After start(), write_worker_alive flips True; after stop(), False."""
    from collector import ws_connection as wc

    monkeypatch.setattr(wc, "load_private_key", lambda _p: object())
    from unittest.mock import MagicMock
    fake_wire = MagicMock(name="WSClient")
    monkeypatch.setattr(wc, "WSClient", MagicMock(return_value=fake_wire))

    writer = MagicMock(name="writer")
    archiver = wc.BronzeArchiver(
        api_key="K",
        private_key_path="/nonexistent.pem",
        writers_by_channel={None: writer, "orderbook_delta": writer},
        subscribe_frames=[],
        cmd_id_to_channel={},
        conn_id="B",
    )
    archiver.start()
    try:
        assert archiver.get_health_snapshot()["write_worker_alive"] is True
    finally:
        archiver.stop()
    assert archiver.get_health_snapshot()["write_worker_alive"] is False


# ─── 2. write_bronze_health_sidecar ──────────────────────────────────────────


def test_write_bronze_health_sidecar_creates_valid_json(monkeypatch, tmp_path):
    """The sidecar JSON must validate against the documented schema."""
    from collector import ws_connection as wc
    from collector.main_loop import write_bronze_health_sidecar

    monkeypatch.setattr(wc, "load_private_key", lambda _p: object())
    from unittest.mock import MagicMock
    fake_wire = MagicMock(name="WSClient")
    monkeypatch.setattr(wc, "WSClient", MagicMock(return_value=fake_wire))

    writer = MagicMock(name="writer")
    archivers = [
        wc.BronzeArchiver(
            api_key="K",
            private_key_path="/nonexistent.pem",
            writers_by_channel={None: writer},
            subscribe_frames=[],
            cmd_id_to_channel={},
            conn_id=cid,
        )
        for cid in ("A", "B", "C")
    ]
    path = tmp_path / "bronze_health.json"
    write_bronze_health_sidecar(archivers, path)
    assert path.is_file()
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert "written_at" in data and data["written_at"].endswith("Z")
    assert isinstance(data["archivers"], list) and len(data["archivers"]) == 3
    assert {a["conn_id"] for a in data["archivers"]} == {"A", "B", "C"}
    assert data["total_dropped_frames"] == 0
    assert data["total_queue_size"] == 0
    # D1.3-fu5: ack_frames_processed flows end-to-end through the JSON
    # file (not just present in the in-memory snapshot dict).
    # Regression-guard against an aggregator strip in
    # write_bronze_health_sidecar.
    for a in data["archivers"]:
        assert "ack_frames_processed" in a, (
            f"archiver {a.get('conn_id')!r} missing ack_frames_processed "
            f"in serialized JSON; D1.3-fu5 observability surface broken."
        )
        assert a["ack_frames_processed"] == 0  # fresh archivers


def test_write_bronze_health_sidecar_atomic_replace(monkeypatch, tmp_path):
    """Reader (the cron-driven monitor) must never observe a torn JSON
    write. write_bronze_health_sidecar must use atomic rename.
    """
    from collector import ws_connection as wc
    from collector.main_loop import write_bronze_health_sidecar

    monkeypatch.setattr(wc, "load_private_key", lambda _p: object())
    from unittest.mock import MagicMock
    fake_wire = MagicMock(name="WSClient")
    monkeypatch.setattr(wc, "WSClient", MagicMock(return_value=fake_wire))

    writer = MagicMock(name="writer")
    archiver = wc.BronzeArchiver(
        api_key="K",
        private_key_path="/nonexistent.pem",
        writers_by_channel={None: writer},
        subscribe_frames=[],
        cmd_id_to_channel={},
        conn_id="A",
    )
    path = tmp_path / "bronze_health.json"
    # Repeated writes must replace cleanly.
    for _ in range(5):
        write_bronze_health_sidecar([archiver], path)
        data = json.loads(path.read_text())
        assert data["schema_version"] == 1


def test_write_bronze_health_sidecar_handles_empty_archivers_list(tmp_path):
    """Edge case: collector booted with zero archivers (shouldn't happen
    in prod, but the helper must not crash on the empty case).
    """
    from collector.main_loop import write_bronze_health_sidecar
    path = tmp_path / "bronze_health.json"
    write_bronze_health_sidecar([], path)
    data = json.loads(path.read_text())
    assert data["archivers"] == []
    assert data["total_dropped_frames"] == 0


# ─── 3. check_dropped_frames monitor function ───────────────────────────────


def _write_sidecar(path, *, archivers_data, total_dropped):
    payload = {
        "schema_version": 1,
        "written_at": "2026-05-17T16:30:00.123456Z",
        "archivers": archivers_data,
        "total_dropped_frames": total_dropped,
        "total_queue_size": sum(a.get("write_queue_size", 0) for a in archivers_data),
    }
    path.write_text(json.dumps(payload))


def test_check_dropped_frames_returns_none_when_sidecar_absent(tmp_path):
    """Pre-bronze-day-zero / test env: sidecar doesn't exist → return None,
    NOT an alert. Same fail-quiet posture as the existing 3 checks.
    """
    from scripts.ops.collector_health_monitor import check_dropped_frames
    sidecar = tmp_path / "missing.json"
    state = tmp_path / "monitor_state.json"
    assert check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=10,
    ) is None


def test_check_dropped_frames_first_run_returns_none(tmp_path):
    """First run after install: state file doesn't exist yet. We baseline
    the current sidecar total + return None. (Otherwise the first tick
    after install would alert on whatever the cumulative-since-process-
    start total happens to be — false alarm.)
    """
    from scripts.ops.collector_health_monitor import check_dropped_frames
    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 100, "write_queue_size": 5},
    ], total_dropped=100)
    result = check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=10,
    )
    assert result is None
    # State file must have been persisted with the baseline.
    assert state.is_file()
    saved = json.loads(state.read_text())
    assert saved["last_total_dropped_frames"] == 100


def test_check_dropped_frames_no_alert_when_delta_below_threshold(tmp_path):
    from scripts.ops.collector_health_monitor import check_dropped_frames
    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    # Tick 1 baseline
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 100, "write_queue_size": 0},
    ], total_dropped=100)
    check_dropped_frames(sidecar_path=sidecar, state_path=state, threshold=10)
    # Tick 2: delta = 5 < threshold 10 → no alert
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 105, "write_queue_size": 0},
    ], total_dropped=105)
    result = check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=10,
    )
    assert result is None
    saved = json.loads(state.read_text())
    assert saved["last_total_dropped_frames"] == 105


def test_check_dropped_frames_alerts_when_delta_at_or_above_threshold(tmp_path):
    from scripts.ops.collector_health_monitor import check_dropped_frames
    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 100, "write_queue_size": 0},
    ], total_dropped=100)
    check_dropped_frames(sidecar_path=sidecar, state_path=state, threshold=10)
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 250, "write_queue_size": 0},
    ], total_dropped=250)
    result = check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=10,
    )
    assert result is not None
    assert "BRONZE_DROPPED_FRAMES" in result
    assert "150" in result  # delta
    # State updated to current total so the next tick measures from the
    # new floor (not double-counting this delta).
    saved = json.loads(state.read_text())
    assert saved["last_total_dropped_frames"] == 250


def test_check_dropped_frames_handles_counter_reset(tmp_path):
    """Collector restart resets _dropped_frames to 0 (per D1.3-fu4 R1-M2
    fix). If state-file total > sidecar total, treat as RESET — re-baseline
    + return None.
    """
    from scripts.ops.collector_health_monitor import check_dropped_frames
    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 500, "write_queue_size": 0},
    ], total_dropped=500)
    check_dropped_frames(sidecar_path=sidecar, state_path=state, threshold=10)
    # Restart: counter resets, new sidecar total < state file
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 0, "write_queue_size": 0},
    ], total_dropped=0)
    result = check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=10,
    )
    assert result is None  # NOT an alert; just rebaseline
    saved = json.loads(state.read_text())
    assert saved["last_total_dropped_frames"] == 0


def test_check_dropped_frames_handles_malformed_sidecar(tmp_path):
    """Sidecar partially-written or corrupt → return None (fail-quiet).
    Same posture as other monitor checks.
    """
    from scripts.ops.collector_health_monitor import check_dropped_frames
    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    sidecar.write_text("not json at all {")
    assert check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=10,
    ) is None


def test_check_dropped_frames_handles_stale_sidecar(tmp_path):
    """Sidecar exists but is N seconds old → return alert that the
    collector may have died (stale sidecar = no fresh writes from drain
    thread = collector process gone or wedged). Threshold: 120s (~2x
    the 60s rotation cadence + drain poll headroom).
    """
    from scripts.ops.collector_health_monitor import check_dropped_frames
    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 0, "write_queue_size": 0},
    ], total_dropped=0)
    # Set mtime to 5 min ago
    old = time.time() - 300
    import os
    os.utime(sidecar, (old, old))
    result = check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=10,
        stale_after_seconds=120,
    )
    assert result is not None
    assert "STALE" in result.upper() or "stale" in result.lower()


# ─── R1-M2: rolling-window sustained-drip alerting ──────────────────────────


def test_check_dropped_frames_alerts_on_sustained_drip(tmp_path):
    """R1-M2 fix: a sustained 50 drops/tick × 6 ticks = 300 cumulative drops
    must alert, even though each individual delta (50) < per-tick threshold (100).

    The monitor now tracks ``pending_drops_since_last_alert`` across ticks
    and alerts when that cumulative sum reaches ``threshold``. This closes
    the silent observability hole where load-balanced steady-state drops
    never trip the per-tick threshold.
    """
    from scripts.ops.collector_health_monitor import check_dropped_frames
    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    # Baseline.
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 0, "write_queue_size": 0},
    ], total_dropped=0)
    check_dropped_frames(sidecar_path=sidecar, state_path=state, threshold=100)
    # 5 ticks of 30 drops each = 150 cumulative; per-tick delta 30 < 100
    # but pending_drops_since_last_alert reaches 150 by tick 5 → alert.
    alerts = []
    for i in range(1, 6):
        _write_sidecar(sidecar, archivers_data=[
            {"conn_id": "A", "dropped_frames": 30 * i, "write_queue_size": 0},
        ], total_dropped=30 * i)
        r = check_dropped_frames(
            sidecar_path=sidecar, state_path=state, threshold=100,
        )
        if r is not None:
            alerts.append((i, r))
    assert alerts, (
        "sustained 30-drops/tick × 5 ticks (150 total) should have alerted "
        "via the pending_drops_since_last_alert running sum, but did not. "
        "R1-M2 fix regression."
    )
    # The alert should reflect the cumulative sustained drops, not just
    # the most-recent per-tick delta. The alert message should mention a
    # count >= 100 (the threshold + at least one tick's worth above).
    _, alert_msg = alerts[0]
    assert "BRONZE_DROPPED_FRAMES" in alert_msg
    # Extract the "N new drops" number from the alert message; should be
    # >= threshold (100). Exact value depends on which tick crossed the
    # bar — for 30/tick, tick 4 has pending=120 → alert "120 new drops".
    import re as _re
    m = _re.search(r"(\d+) new drops", alert_msg)
    assert m is not None, f"alert missing 'N new drops': {alert_msg!r}"
    assert int(m.group(1)) >= 100


def test_check_dropped_frames_pending_resets_after_alert(tmp_path):
    """After an alert fires, ``pending_drops_since_last_alert`` resets to 0
    so the same backlog doesn't re-alert on every subsequent tick. New
    drops accumulate fresh from the next tick.
    """
    from scripts.ops.collector_health_monitor import check_dropped_frames
    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 0, "write_queue_size": 0},
    ], total_dropped=0)
    check_dropped_frames(sidecar_path=sidecar, state_path=state, threshold=100)
    # Burst that alerts.
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 200, "write_queue_size": 0},
    ], total_dropped=200)
    r1 = check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=100,
    )
    assert r1 is not None
    # Same total (no new drops) → no alert.
    r2 = check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=100,
    )
    assert r2 is None
    # +50 more drops (< threshold) → still no alert (pending reset to 0).
    _write_sidecar(sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 250, "write_queue_size": 0},
    ], total_dropped=250)
    r3 = check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=100,
    )
    assert r3 is None


# ─── R1-M1: env-var override propagates to monitor ──────────────────────────


def test_check_dropped_frames_resolves_sidecar_via_env_var(tmp_path, monkeypatch):
    """R1-M1 fix: if operator sets COLLECTOR_HEALTH_SIDECAR_PATH (e.g., to
    relocate the sidecar onto a dedicated mount), the monitor's default
    must resolve THROUGH that env var, NOT against a hardcoded module
    constant. Otherwise the collector writes to the new location and
    the monitor silently polls the absent old location → no alerts ever.
    """
    from scripts.ops import collector_health_monitor as mod
    relocated_sidecar = tmp_path / "relocated_bronze_health.json"
    state = tmp_path / "monitor_state.json"
    monkeypatch.setenv("COLLECTOR_HEALTH_SIDECAR_PATH", str(relocated_sidecar))
    _write_sidecar(relocated_sidecar, archivers_data=[
        {"conn_id": "A", "dropped_frames": 500, "write_queue_size": 0},
    ], total_dropped=500)
    # Call WITHOUT explicit sidecar_path — must resolve env var.
    # First call baselines (returns None) — that itself proves the env
    # var resolved, because the state file gets written with the right
    # baseline.
    result = mod.check_dropped_frames(state_path=state, threshold=10)
    assert result is None
    saved = json.loads(state.read_text())
    assert saved["last_total_dropped_frames"] == 500, (
        "monitor did NOT read the env-var-relocated sidecar — baseline "
        f"is {saved.get('last_total_dropped_frames')} instead of 500. "
        "M1 fix regression: env var COLLECTOR_HEALTH_SIDECAR_PATH not "
        "resolved at call time."
    )


# ─── R1-m1: schema-version validation ───────────────────────────────────────


def test_check_dropped_frames_rejects_future_schema_version(tmp_path):
    """R1-m1 fix: a future Bit that bumps schema_version (renaming or
    removing keys) would silently make the monitor degrade to delta=0
    forever. Validate schema_version == 1; on mismatch return an alert
    instead of fail-quiet so the operator notices the version skew
    rather than losing the dropped-frames signal.
    """
    from scripts.ops.collector_health_monitor import check_dropped_frames
    sidecar = tmp_path / "bronze_health.json"
    state = tmp_path / "monitor_state.json"
    payload = {
        "schema_version": 99,  # future schema
        "written_at": "2026-05-17T16:30:00.123456Z",
        "archivers": [],
        "total_dropped_frames": 0,
        "total_queue_size": 0,
    }
    sidecar.write_text(json.dumps(payload))
    result = check_dropped_frames(
        sidecar_path=sidecar, state_path=state, threshold=10,
    )
    assert result is not None
    assert "schema" in result.lower(), (
        f"schema_version=99 should have surfaced a schema-mismatch alert; "
        f"got: {result!r}"
    )
