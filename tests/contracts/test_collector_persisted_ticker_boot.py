"""Collector boots on the last persisted ticker set — option (a) of ticket
86bbvdcat (2026-09-05).

Measured on the 2026-09-05 12:13Z restart: the synchronous REST
page-through in `collector.main_loop.run` took 54.9 min (~18,500 pages,
~3.7M rows → 358,625 tickers after exclusions), so the first
`kalshi_ws_connected` landed 59.8 min after ActiveEnter — every restart
costs an hour of orderbook bronze. Because the drain thread only started
AFTER that fetch, the page-through also parked 17 GB of REST bronze on
local disk (77% → 85% used in 40 min, on the day the disk had just been
recovered from 100%). And `RestSnapshotRefresher._run` fires its first
tick immediately with `_last_tier_map = {}`, so the boot paid a SECOND
page-through and a guaranteed 7-conn reconnect ~1 h later even when the
set was identical.

Pins:
  1. `save_tier_map` / `load_tier_map` round-trip (atomic JSON; age
     reported; missing/malformed → None).
  2. `RestSnapshotRefresher(initial_tier_map=...)` seeds the change
     detector: an unchanged fetch does NOT fire `on_refresh`; a changed
     one does. `cache_path=` persists every successful fetch.
     `status()` exposes in-progress + last-duration + last-count.
  3. `run()` with a cache file constructs + starts the archivers BEFORE
     any REST fetch (`ticker_set_source == "persisted"`), and the
     background refresher's first tick is the only page-through.
  4. `run()` without a cache still fetches synchronously (first boot)
     and writes the cache afterwards (`ticker_set_source == "rest"`).
  5. The sidecar is written during boot with `state == "booting"` while
     the drain thread is already alive (so REST bronze uploads during
     the page-through), and flips to `running` once archivers started.
  6. `write_bronze_health_sidecar(archivers, path, extra=...)` merges
     the additive status keys; `schema_version` stays 1.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _generate_pem_file(tmp_path: Path) -> Path:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "test_collector.pem"
    pem_path.write_bytes(pk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    return pem_path


def _fake_rclone(argv, *args, **kwargs):
    if "size" in argv:
        return subprocess.CompletedProcess(
            args=argv, returncode=0,
            stdout=json.dumps({"count": 0, "bytes": 0}) + "\n", stderr="",
        )
    return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")


# ─── 1. cache helpers ────────────────────────────────────────────────────────


def test_save_and_load_tier_map_roundtrip(tmp_path: Path):
    from collector.rest_snapshot import load_tier_map, save_tier_map
    path = tmp_path / "state" / "last_tickers.json"
    tier_map = {"1": ["KXA-1", "KXB-2", "KXC-3"]}
    assert save_tier_map(path, tier_map) is True
    loaded = load_tier_map(path)
    assert loaded is not None
    got_map, age = loaded
    assert got_map == tier_map
    assert 0 <= age < 60
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1 and "saved_at" in data
    assert not list(path.parent.glob("*.tmp")), "atomic replace must not leave tmp files"


def test_load_tier_map_missing_or_malformed_returns_none(tmp_path: Path):
    from collector.rest_snapshot import load_tier_map
    assert load_tier_map(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_tier_map(bad) is None
    wrong_shape = tmp_path / "shape.json"
    wrong_shape.write_text(json.dumps({"schema_version": 1, "tickers_by_tier": ["x"]}))
    assert load_tier_map(wrong_shape) is None


# ─── 2. refresher seeding / persistence / status ─────────────────────────────


def _refresher(monkeypatch, tmp_path, *, returns, **kwargs):
    import collector.rest_snapshot as rs
    calls = []

    def fake_fetch(**kw):
        calls.append(kw)
        return returns[min(len(calls) - 1, len(returns) - 1)]

    monkeypatch.setattr(rs, "fetch_tickers_by_tier", fake_fetch)
    fired = []
    ref = rs.RestSnapshotRefresher(
        api_key="K", private_key=object(), on_refresh=fired.append,
        shutdown_event=threading.Event(), **kwargs,
    )
    return ref, fired, calls


def test_refresher_seeded_with_initial_map_skips_callback_when_unchanged(monkeypatch, tmp_path):
    same = {"1": ["KXA", "KXB"]}
    changed = {"1": ["KXA", "KXB", "KXC"]}
    ref, fired, _ = _refresher(monkeypatch, tmp_path, returns=[same, changed],
                               initial_tier_map=same)
    ref._do_refresh()
    assert fired == [], "identical set after boot-from-cache must NOT reconnect"
    ref._do_refresh()
    assert fired == [changed]


def test_refresher_persists_cache_after_successful_fetch(monkeypatch, tmp_path):
    from collector.rest_snapshot import load_tier_map
    m = {"1": ["KXA"]}
    cache = tmp_path / "last_tickers.json"
    ref, _, _ = _refresher(monkeypatch, tmp_path, returns=[m], cache_path=cache)
    ref._do_refresh()
    assert load_tier_map(cache)[0] == m


def test_refresher_does_not_persist_failed_fetch(monkeypatch, tmp_path):
    cache = tmp_path / "last_tickers.json"
    ref, _, _ = _refresher(monkeypatch, tmp_path, returns=[None], cache_path=cache)
    ref._do_refresh()
    assert not cache.exists()


def test_refresher_status_reports_duration_and_count(monkeypatch, tmp_path):
    m = {"1": ["KXA", "KXB"]}
    ref, _, _ = _refresher(monkeypatch, tmp_path, returns=[m])
    before = ref.status()
    assert before["in_progress"] is False and before["refresh_count"] == 0
    ref._do_refresh()
    st = ref.status()
    assert st["in_progress"] is False
    assert st["refresh_count"] == 1
    assert st["last_ticker_count"] == 2
    assert isinstance(st["last_duration_seconds"], float) and st["last_duration_seconds"] >= 0.0
    assert st["last_completed_at"] and st["last_completed_at"].endswith("Z")


# ─── 3-5. run() boot path ────────────────────────────────────────────────────


def _boot(tmp_path: Path, monkeypatch, *, cache_map=None, fetch_returns,
          fetch_hook=None, run_for=0.6):
    """Drive collector.main_loop.run with a mocked archiver + rclone.

    Returns (events, ctor, sidecar_path, cache_path). ``events`` records
    ("archiver_ctor"|"fetch", thread_name) in call order.
    """
    from collector.main_loop import run as main_loop_run
    import collector.main_loop as ml
    import collector.rest_snapshot as rs

    bronze_root = tmp_path / "bronze"
    bronze_root.mkdir()
    cache_path = tmp_path / "last_tickers.json"
    sidecar_path = tmp_path / "bronze_health.json"
    if cache_map is not None:
        rs.save_tier_map(cache_path, cache_map)
    monkeypatch.setenv("COLLECTOR_TICKER_CACHE_PATH", str(cache_path))
    monkeypatch.setenv("COLLECTOR_HEALTH_SIDECAR_PATH", str(sidecar_path))
    monkeypatch.delenv("COLLECTOR_TICKERS_FILE", raising=False)
    monkeypatch.setenv("COLLECTOR_CONN_COUNT", "1")

    events = []
    fake_archiver = MagicMock()
    # The sidecar aggregator json-serializes each archiver snapshot; a bare
    # MagicMock is not serializable (the write would fail-quiet and the
    # sidecar would never leave "booting"), so return a real dict.
    fake_archiver.get_health_snapshot.return_value = {
        "conn_id": "A", "dropped_frames": 0, "write_queue_size": 0,
        "write_queue_maxsize": 1, "write_queue_peak_size": 0,
        "write_worker_alive": True, "collector_seq": 0,
        "ack_frames_processed": 0,
    }

    def ctor(**kwargs):
        events.append(("archiver_ctor", threading.current_thread().name))
        return fake_archiver
    archiver_ctor = MagicMock(side_effect=ctor)
    monkeypatch.setattr(ml, "BronzeArchiver", archiver_ctor)

    def fake_fetch(**kwargs):
        events.append(("fetch", threading.current_thread().name))
        if fetch_hook is not None:
            fetch_hook()
        return fetch_returns
    monkeypatch.setattr(ml, "fetch_tickers_by_tier", fake_fetch)
    monkeypatch.setattr(rs, "fetch_tickers_by_tier", fake_fetch)
    monkeypatch.setattr(rs, "fetch_open_tickers_for_series", lambda **kw: set())
    monkeypatch.setattr("collector.uploader.subprocess.run", _fake_rclone)

    shutdown = threading.Event()
    threading.Thread(target=lambda: (time.sleep(run_for), shutdown.set()),
                     daemon=True).start()
    main_loop_run(
        bronze_root=bronze_root, api_key="fake-key-id",
        private_key_path=str(_generate_pem_file(tmp_path)),
        shutdown_event=shutdown,
    )
    return events, archiver_ctor, sidecar_path, cache_path


def test_run_boots_from_persisted_cache_before_any_rest_fetch(tmp_path, monkeypatch):
    cached = {"1": ["KXA-1", "KXB-2", "KXC-3"]}
    events, ctor, sidecar, _ = _boot(tmp_path, monkeypatch, cache_map=cached,
                                     fetch_returns=cached)
    kinds = [k for k, _ in events]
    assert "archiver_ctor" in kinds and "fetch" in kinds, events
    assert kinds.index("archiver_ctor") < kinds.index("fetch"), (
        f"with a persisted ticker set the archivers must be wired BEFORE "
        f"any REST page-through; order was {events}"
    )
    fetch_threads = {t for k, t in events if k == "fetch"}
    assert "MainThread" not in fetch_threads, (
        "the page-through must run on the refresher thread, not block boot"
    )
    frames = list(ctor.call_args.kwargs["subscribe_frames"])
    assert len(frames) >= 3, "subscribe frames must come from the persisted set"
    data = json.loads(sidecar.read_text())
    assert data["ticker_set_source"] == "persisted"
    assert data["state"] == "running"
    assert data["schema_version"] == 1


def test_run_without_cache_fetches_synchronously_and_saves_cache(tmp_path, monkeypatch):
    from collector.rest_snapshot import load_tier_map
    fresh = {"1": ["KXA-1"]}
    events, ctor, sidecar, cache_path = _boot(tmp_path, monkeypatch, cache_map=None,
                                              fetch_returns=fresh)
    kinds = [k for k, _ in events]
    assert kinds.index("fetch") < kinds.index("archiver_ctor"), events
    assert events[0] == ("fetch", "MainThread")
    assert load_tier_map(cache_path)[0] == fresh, "first boot must persist the fetched set"
    data = json.loads(sidecar.read_text())
    assert data["ticker_set_source"] == "rest"
    assert data["state"] == "running"


def test_run_writes_booting_sidecar_with_drain_alive_before_fetch_returns(tmp_path, monkeypatch):
    seen = {}
    sidecar = tmp_path / "bronze_health.json"

    def hook():
        # Called INSIDE the synchronous boot fetch (no cache). The sidecar
        # must already say "booting" and the drain thread must be alive so
        # REST bronze uploads while the page-through runs.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                data = json.loads(sidecar.read_text())
                if data.get("state") == "booting":
                    seen["state"] = "booting"
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        seen["drain_alive"] = any(
            t.name == "bronze-drain" and t.is_alive() for t in threading.enumerate()
        )
    _boot(tmp_path, monkeypatch, cache_map=None, fetch_returns={"1": ["KXA"]},
          fetch_hook=hook)
    assert seen.get("state") == "booting", "sidecar must report booting during the page-through"
    assert seen.get("drain_alive") is True, "drain thread must run during the page-through"
    assert json.loads(sidecar.read_text())["state"] == "running"


# ─── 6. sidecar extra keys ───────────────────────────────────────────────────


def test_write_bronze_health_sidecar_merges_extra_status(tmp_path: Path):
    from collector.main_loop import write_bronze_health_sidecar
    path = tmp_path / "bronze_health.json"
    write_bronze_health_sidecar([], path, extra={
        "state": "booting", "state_since": "2026-09-05T12:13:29.000000Z",
        "ticker_set_source": None,
    })
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1
    assert data["state"] == "booting"
    assert data["archivers"] == [] and data["total_dropped_frames"] == 0
