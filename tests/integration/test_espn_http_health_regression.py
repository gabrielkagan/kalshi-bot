"""Ticket 86bbvqhyr (2026-09-06) — behavioral regression tests for the ESPN
403 outage fix + the content-class health checks it adds.

Postmortem: kb/failures/espn-403-user-agent-silent-outage-sep06.md.
Plan: kb/decisions/espn-ua-403-fix-plan-sep06.md.

Covers:
  A. ESPNArchiver per-league 1h http-status stats (rolling window,
     non-200 + transport errors counted, lock-guarded) + throttled WARN.
  B. espn_main_loop.write_bronze_health_sidecar carries
     ``espn_http_status_1h`` (schema_version stays 1).
  C. collector_health_monitor.check_espn_http_errors — fires at >50%
     non-200 for a league with ≥ min_polls; fail-quiet otherwise.
  D. collector_health_monitor.check_sports_eval_silence — bot tier:
     journal shows live games, evaluated_opportunities has 0 sports rows.
  E. scripts/ops/espn_live_probe.py — exit codes + UA header.
  F. bot ESPNLiveFeed WARN-logs non-200 (throttled) instead of the
     DEBUG-only swallow that hid 5 weeks of 403s.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
import requests


# ─── helpers ────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"events": []}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error")


class _FakeSession:
    """Scripted per-call responses; an Exception instance is raised."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}

    def get(self, url, timeout=None, **kw):
        self.calls.append((url, timeout, kw))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _Writer:
    def __init__(self):
        self.envelopes = []

    def write(self, env):
        self.envelopes.append(env)


def _make_archiver(script, leagues=("nba",), monotonic=None):
    import collector.espn_archiver as ea
    writers = {lg: _Writer() for lg in leagues}
    kwargs = dict(
        writers_by_channel=writers, leagues=list(leagues),
        inter_league_sleep_seconds=0.0,
    )
    if monotonic is not None:
        kwargs["monotonic_fn"] = monotonic
    arch = ea.ESPNArchiver(**kwargs)
    arch._session = _FakeSession(script)
    return arch, writers


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ─── A. archiver http-status stats ──────────────────────────────────────────

def test_archiver_stats_count_non_200_and_transport_errors():
    clock = _Clock()
    arch, _ = _make_archiver(
        [_Resp(403), _Resp(200), requests.ConnectionError("boom"), _Resp(403)],
        monotonic=clock,
    )
    for _ in range(4):
        arch.poll_once()
        clock.t += 60
    stats = arch.get_http_status_stats()
    nba = stats["nba"]
    assert nba["polls"] == 4
    assert nba["non_200"] == 3
    assert nba["non_200_rate"] == pytest.approx(0.75)
    assert nba["last_status"] == 403
    assert nba["window_seconds"] == 3600


def test_archiver_stats_window_evicts_old_samples():
    clock = _Clock()
    arch, _ = _make_archiver([_Resp(403)] * 3 + [_Resp(200)] * 2, monotonic=clock)
    for _ in range(3):
        arch.poll_once()
        clock.t += 60
    clock.t += 3600  # everything so far falls out of the 1h window
    for _ in range(2):
        arch.poll_once()
        clock.t += 60
    nba = arch.get_http_status_stats()["nba"]
    assert nba["polls"] == 2
    assert nba["non_200"] == 0
    assert nba["non_200_rate"] == 0.0


def test_archiver_stats_empty_league_has_none_rate():
    arch, _ = _make_archiver([])
    nba = arch.get_http_status_stats()["nba"]
    assert nba["polls"] == 0 and nba["non_200"] == 0
    assert nba["non_200_rate"] is None
    assert nba["last_status"] is None


def test_archiver_warns_on_non_200_throttled(caplog):
    clock = _Clock()
    arch, _ = _make_archiver([_Resp(403), _Resp(403), _Resp(403)], monotonic=clock)
    with caplog.at_level(logging.WARNING, logger="collector.espn_archiver"):
        arch.poll_once()
        clock.t += 60
        arch.poll_once()          # inside throttle window → no 2nd warn
        clock.t += 3601
        arch.poll_once()          # throttle expired → warn again
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "403" in r.getMessage()]
    assert len(warns) == 2, [r.getMessage() for r in caplog.records]
    assert "nba" in warns[0].getMessage()


def test_archiver_still_writes_bronze_row_for_non_200():
    arch, writers = _make_archiver([_Resp(403)])
    arch.poll_once()
    assert len(writers["nba"].envelopes) == 1
    raw = json.loads(writers["nba"].envelopes[0]["_raw"])
    assert raw["http_status"] == 403


# ─── B. sidecar carries the stats ───────────────────────────────────────────

def test_sidecar_carries_espn_http_status_1h(tmp_path):
    import collector.espn_main_loop as ml
    arch, writers = _make_archiver([_Resp(403), _Resp(200)])
    arch.poll_once()
    arch.poll_once()
    path = tmp_path / "bronze_health.json"
    ml.write_bronze_health_sidecar(arch, list(writers.values()), path)
    data = json.loads(path.read_text())
    assert data["schema_version"] == 1, "additive key — schema stays 1"
    assert "espn_http_status_1h" in data
    assert data["espn_http_status_1h"]["nba"]["polls"] == 2
    assert data["espn_http_status_1h"]["nba"]["non_200"] == 1


# ─── C. check_espn_http_errors ──────────────────────────────────────────────

def _write_espn_sidecar(path: Path, stats: dict, schema_version=1):
    path.write_text(json.dumps({
        "schema_version": schema_version,
        "written_at": "2026-09-06T16:00:00.000000Z",
        "archivers": [], "total_dropped_frames": 0, "total_queue_size": 0,
        "espn_http_status_1h": stats,
    }))


def _stat(polls, non_200, last=403):
    return {"polls": polls, "non_200": non_200,
            "non_200_rate": (non_200 / polls) if polls else None,
            "last_status": last, "window_seconds": 3600}


def test_check_espn_http_errors_none_when_sidecar_absent(tmp_path):
    from scripts.ops.collector_health_monitor import check_espn_http_errors
    assert check_espn_http_errors(sidecar_path=tmp_path / "nope.json") is None


def test_check_espn_http_errors_none_when_key_missing(tmp_path):
    from scripts.ops.collector_health_monitor import check_espn_http_errors
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"schema_version": 1, "total_dropped_frames": 0}))
    assert check_espn_http_errors(sidecar_path=p) is None


def test_check_espn_http_errors_none_when_malformed(tmp_path):
    from scripts.ops.collector_health_monitor import check_espn_http_errors
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert check_espn_http_errors(sidecar_path=p) is None


def test_check_espn_http_errors_none_below_threshold(tmp_path):
    from scripts.ops.collector_health_monitor import check_espn_http_errors
    p = tmp_path / "s.json"
    _write_espn_sidecar(p, {"nba": _stat(60, 30), "nhl": _stat(60, 0, 200)})
    assert check_espn_http_errors(sidecar_path=p, threshold_pct=50) is None


def test_check_espn_http_errors_none_below_min_polls(tmp_path):
    from scripts.ops.collector_health_monitor import check_espn_http_errors
    p = tmp_path / "s.json"
    _write_espn_sidecar(p, {"nba": _stat(3, 3)})  # 100% but only 3 polls
    assert check_espn_http_errors(sidecar_path=p, min_polls=10) is None


def test_check_espn_http_errors_alerts_above_threshold(tmp_path):
    from scripts.ops.collector_health_monitor import check_espn_http_errors
    p = tmp_path / "s.json"
    _write_espn_sidecar(p, {
        "college-football": _stat(60, 60),
        "nba": _stat(60, 31),
        "nhl": _stat(60, 0, 200),
    })
    out = check_espn_http_errors(sidecar_path=p, threshold_pct=50, min_polls=10)
    assert out is not None
    assert "ESPN HTTP" in out
    assert "college-football" in out and "60/60" in out
    assert "nba" in out and "31/60" in out
    assert "nhl" not in out
    assert "2/3" in out  # leagues failing / leagues polled


def test_check_espn_http_errors_resolves_sidecar_via_env(tmp_path, monkeypatch):
    from scripts.ops.collector_health_monitor import check_espn_http_errors
    p = tmp_path / "s.json"
    _write_espn_sidecar(p, {"nba": _stat(60, 60)})
    monkeypatch.setenv("ESPN_HEALTH_SIDECAR_PATH", str(p))
    assert check_espn_http_errors() is not None


def test_main_dispatches_http_errors_with_d1_11_dedup_key(monkeypatch):
    import scripts.ops.collector_health_monitor as mod
    sent = []

    class _N:
        def __init__(self, *a, **k):
            pass

        def send(self, msg, dedup_key=None, **k):
            sent.append(dedup_key)

    monkeypatch.setattr("bot.notifier.TelegramNotifier", _N)
    # Every check returns None except the ESPN http_errors one.
    for name in dir(mod):
        if name.startswith("check_") and name != "check_espn_http_errors":
            monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(mod, "check_espn_http_errors", lambda *a, **k: "*X*")
    assert mod.main() == 0
    assert sent == ["d1_11_http_errors"]


# ─── D. check_sports_eval_silence ───────────────────────────────────────────

def _make_db(path: Path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE evaluated_opportunities (id INTEGER PRIMARY KEY, "
        "product_type TEXT, evaluation_time TEXT)"
    )
    conn.executemany(
        "INSERT INTO evaluated_opportunities (product_type, evaluation_time) "
        "VALUES (?, ?)", rows,
    )
    conn.commit()
    conn.close()


def _iso(minutes_ago: int) -> str:
    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_sports_sidecar(path: Path, live_ticks: int, statuses=None,
                          window_s: int = 86400):
    path.write_text(json.dumps({
        "schema_version": 1,
        "written_at": _iso(0),
        "live_ticks_window_seconds": window_s,
        "live_ticks_in_window": live_ticks,
        "last_live_at": _iso(30),
        "last_poll_games": 3, "last_poll_live": 1,
        "espn_last_poll_status": statuses or {"nba": 200},
        "tick_interval_seconds": 30,
    }))


def test_sports_silence_none_when_sidecar_missing(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    assert mod.check_sports_eval_silence(db_path=db) is None


def test_sports_silence_none_when_sidecar_malformed(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    (tmp_path / "sports_health.json").write_text("{nope")
    assert mod.check_sports_eval_silence(db_path=db) is None


def test_sports_silence_none_when_sidecar_stale(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    import os
    db = tmp_path / "state.db"
    _make_db(db, [])
    sc = tmp_path / "sports_health.json"
    _write_sports_sidecar(sc, live_ticks=500)
    old = _dt.datetime.now().timestamp() - 3600
    os.utime(sc, (old, old))
    assert mod.check_sports_eval_silence(db_path=db, stale_after_seconds=900) is None


def test_sports_silence_none_when_db_missing(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    _write_sports_sidecar(tmp_path / "sports_health.json", live_ticks=500)
    assert mod.check_sports_eval_silence(db_path=tmp_path / "no.db") is None


def test_sports_silence_none_below_min_live_ticks(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    _write_sports_sidecar(tmp_path / "sports_health.json", live_ticks=5)
    assert mod.check_sports_eval_silence(db_path=db, min_live_ticks=20) is None


def test_sports_silence_none_when_sports_rows_exist(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [("sports", _iso(10)), ("15m", _iso(5))])
    _write_sports_sidecar(tmp_path / "sports_health.json", live_ticks=500)
    assert mod.check_sports_eval_silence(db_path=db, min_live_ticks=20) is None


def test_sports_silence_alerts_when_live_games_but_zero_sports_rows(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    # Only non-sports rows in-window + a sports row OUTSIDE the 24h window.
    _make_db(db, [("15m", _iso(5)), ("sports", _iso(3000))])
    _write_sports_sidecar(
        tmp_path / "sports_health.json", live_ticks=500,
        statuses={"nba": 403, "nhl": 200, "mlb": None},
    )
    out = mod.check_sports_eval_silence(db_path=db, min_live_ticks=20)
    assert out is not None
    assert "SPORTS" in out.upper()
    assert "evaluated_opportunities" in out
    assert "500" in out
    assert "nba=403" in out and "mlb=None" in out and "nhl" not in out


def test_sports_silence_uses_sidecar_window_for_db_cutoff(tmp_path):
    """A sports row 2h old counts as healthy when the sidecar window is
    24h, but NOT when the sidecar says its window is 1h."""
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [("sports", _iso(120))])
    sc = tmp_path / "sports_health.json"
    _write_sports_sidecar(sc, live_ticks=500, window_s=86400)
    assert mod.check_sports_eval_silence(db_path=db) is None
    _write_sports_sidecar(sc, live_ticks=500, window_s=3600)
    assert mod.check_sports_eval_silence(db_path=db) is not None


def test_sports_silence_resolves_sidecar_via_env(tmp_path, monkeypatch):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    sc = tmp_path / "elsewhere" / "sh.json"
    sc.parent.mkdir()
    _write_sports_sidecar(sc, live_ticks=500)
    monkeypatch.setenv("SPORTS_HEALTH_SIDECAR_PATH", str(sc))
    assert mod.check_sports_eval_silence(db_path=db) is not None


def test_sports_silence_never_shells_out(tmp_path):
    """R2-C1: no journalctl decode on the cron path (24h pull ≈ 50s on VPS)."""
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    _write_sports_sidecar(tmp_path / "sports_health.json", live_ticks=500)
    with patch.object(mod.subprocess, "check_output", side_effect=AssertionError("shelled out")), \
            patch.object(mod.subprocess, "run", side_effect=AssertionError("shelled out")):
        assert mod.check_sports_eval_silence(db_path=db) is not None


# ─── D2. R6-CRITICAL-1: the 403 class must alert on the BOT side ────────────

def test_incident_replay_403_everywhere_alerts_bot_tier(tmp_path):
    """Replay of the 2026-08-05 outage against the real SportsEngine.

    Every ESPN request 403s -> _poll_league raises -> poll_all_leagues
    swallows -> games={} -> _note_tick(n_live=0) -> live_ticks_in_window
    stays 0 forever. check_sports_eval_silence therefore CANNOT fire
    (its >=20-live-tick precondition never arms) — that was R6-CRITICAL-1.
    check_bot_espn_poll_errors must catch it.
    """
    import bot.engines.sports_engine as se
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    eng = se.SportsEngine(db_path=str(db))
    n_leagues = len(eng._espn._session.headers) * 0 + 64
    eng._espn._session = _FakeSession([_Resp(403)] * n_leagues)
    eng._tick()
    sc = tmp_path / "sports_health.json"
    data = json.loads(sc.read_text())
    assert data["live_ticks_in_window"] == 0, "precondition of the bug"
    assert data["espn_last_poll_status"], "engine must record last-poll statuses"
    assert all(v == 403 for v in data["espn_last_poll_status"].values())

    # The silence check is structurally blind here — pin that, so nobody
    # "fixes" this test by weakening the other check.
    assert mod.check_sports_eval_silence(db_path=db) is None

    out = mod.check_bot_espn_poll_errors(db_path=db)
    assert out is not None, (
        "bot tier must alert during a 100% ESPN 403 outage — this is the "
        "exact 5-week silent failure the ticket exists to close"
    )
    assert "BOT ESPN POLL ERRORS" in out
    assert "403" in out


def test_bot_espn_poll_errors_quiet_when_all_200(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    _write_sports_sidecar(tmp_path / "sports_health.json", live_ticks=5,
                          statuses={"nba": 200, "nhl": 200, "mlb": 200})
    assert mod.check_bot_espn_poll_errors(db_path=db) is None


def test_bot_espn_poll_errors_quiet_below_threshold(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    _write_sports_sidecar(tmp_path / "sports_health.json", live_ticks=5,
                          statuses={"a": 403, "b": 200, "c": 200, "d": 200})
    assert mod.check_bot_espn_poll_errors(db_path=db) is None


def test_bot_espn_poll_errors_counts_transport_errors_as_bad(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    _write_sports_sidecar(tmp_path / "sports_health.json", live_ticks=5,
                          statuses={"a": None, "b": None, "c": 200})
    out = mod.check_bot_espn_poll_errors(db_path=db)
    assert out is not None and "a=None" in out


def test_bot_espn_poll_errors_quiet_on_missing_stale_or_empty(tmp_path):
    import os
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    assert mod.check_bot_espn_poll_errors(db_path=db) is None      # missing
    sc = tmp_path / "sports_health.json"
    _write_sports_sidecar(sc, live_ticks=5, statuses={})
    assert mod.check_bot_espn_poll_errors(db_path=db) is None      # empty map
    _write_sports_sidecar(sc, live_ticks=5, statuses={"a": 403})
    old = _dt.datetime.now().timestamp() - 3600
    os.utime(sc, (old, old))
    assert mod.check_bot_espn_poll_errors(db_path=db) is None      # stale


def test_main_dispatches_bot_espn_poll_errors_dedup_key(monkeypatch):
    import scripts.ops.collector_health_monitor as mod
    sent = []

    class _N:
        def __init__(self, *a, **k):
            pass

        def send(self, msg, dedup_key=None, **k):
            sent.append(dedup_key)

    monkeypatch.setattr("bot.notifier.TelegramNotifier", _N)
    for name in dir(mod):
        if name.startswith("check_") and name != "check_bot_espn_poll_errors":
            monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(mod, "check_bot_espn_poll_errors", lambda *a, **k: "*X*")
    assert mod.main() == 0
    assert sent == ["b3_fu3_espn_poll_errors"]


# ─── D3. R6-MAJOR-1: dead/wedged sports thread inside a live bot ────────────

def _patch_bot_up(mod, monkeypatch, active=True, uptime=5000.0):
    monkeypatch.setattr(mod, "check_collector_active",
                        lambda unit=None: None if active else "*DOWN*")
    monkeypatch.setattr(mod, "_collector_uptime_seconds", lambda unit: uptime)


def test_sports_silence_alerts_when_sidecar_missing_and_bot_active(tmp_path, monkeypatch):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    _patch_bot_up(mod, monkeypatch)
    out = mod.check_sports_eval_silence(db_path=db)
    assert out is not None and "SPORTS ENGINE SILENT" in out


def test_sports_silence_alerts_when_sidecar_stale_and_bot_active(tmp_path, monkeypatch):
    import os
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    sc = tmp_path / "sports_health.json"
    _write_sports_sidecar(sc, live_ticks=500)
    old = _dt.datetime.now().timestamp() - 3600
    os.utime(sc, (old, old))
    _patch_bot_up(mod, monkeypatch)
    out = mod.check_sports_eval_silence(db_path=db)
    assert out is not None and "SPORTS ENGINE WEDGED" in out


def test_sports_silence_quiet_when_bot_stopped_or_booting(tmp_path, monkeypatch):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    _patch_bot_up(mod, monkeypatch, active=False)
    assert mod.check_sports_eval_silence(db_path=db) is None   # operator stopped
    _patch_bot_up(mod, monkeypatch, active=True, uptime=60.0)
    assert mod.check_sports_eval_silence(db_path=db) is None   # boot grace
    _patch_bot_up(mod, monkeypatch, active=True, uptime=None)
    assert mod.check_sports_eval_silence(db_path=db) is None   # no systemctl


# ─── D4. R6-MINOR-5: wedged collector poll loop with a fresh sidecar ────────

def test_espn_http_errors_alerts_when_all_leagues_have_zero_polls(tmp_path, monkeypatch):
    import scripts.ops.collector_health_monitor as mod
    p = tmp_path / "s.json"
    _write_espn_sidecar(p, {"nba": _stat(0, 0, None), "nhl": _stat(0, 0, None)})
    monkeypatch.setattr(mod, "_collector_uptime_seconds", lambda unit: 5000.0)
    out = mod.check_espn_http_errors(sidecar_path=p)
    assert out is not None and "POLL LOOP WEDGED" in out


def test_espn_http_errors_zero_polls_quiet_during_boot_grace(tmp_path, monkeypatch):
    import scripts.ops.collector_health_monitor as mod
    p = tmp_path / "s.json"
    _write_espn_sidecar(p, {"nba": _stat(0, 0, None)})
    monkeypatch.setattr(mod, "_collector_uptime_seconds", lambda unit: 60.0)
    assert mod.check_espn_http_errors(sidecar_path=p) is None
    monkeypatch.setattr(mod, "_collector_uptime_seconds", lambda unit: None)
    assert mod.check_espn_http_errors(sidecar_path=p) is None


def test_sports_silence_opens_db_read_only(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    src = Path(mod.__file__).read_text()
    assert "mode=ro" in src, "state.db must be opened read-only from cron"


def test_main_dispatches_sports_silence_with_b3_fu3_dedup_key(monkeypatch):
    import scripts.ops.collector_health_monitor as mod
    sent = []

    class _N:
        def __init__(self, *a, **k):
            pass

        def send(self, msg, dedup_key=None, **k):
            sent.append(dedup_key)

    monkeypatch.setattr("bot.notifier.TelegramNotifier", _N)
    for name in dir(mod):
        if name.startswith("check_") and name != "check_sports_eval_silence":
            monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(mod, "check_sports_eval_silence", lambda *a, **k: "*X*")
    assert mod.main() == 0
    assert sent == ["b3_fu3_sports_eval_silence"]


# ─── E. live probe script ───────────────────────────────────────────────────

def test_live_probe_exit_codes_and_ua():
    from scripts.ops.espn_live_probe import main
    import bot.engines.sports_engine as se
    seen = {}

    def _get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        return _Resp(200, {"events": [1, 2]})

    assert main(["--sport", "basketball", "--league", "nba"], get_fn=_get) == 0
    assert seen["headers"]["User-Agent"] == se.ESPN_USER_AGENT
    assert seen["url"] == f"{se.ESPN_BASE}/basketball/nba/scoreboard"

    assert main([], get_fn=lambda *a, **k: _Resp(403)) == 1
    assert main([], get_fn=lambda *a, **k: _Resp(500)) == 1

    def _boom(*a, **k):
        raise requests.ConnectionError("down")

    assert main([], get_fn=_boom) == 2


def test_live_probe_ua_override_flag():
    from scripts.ops.espn_live_probe import main
    seen = {}

    def _get(url, headers=None, timeout=None):
        seen["ua"] = headers["User-Agent"]
        return _Resp(200)

    assert main(["--ua", "curl/8.7.1"], get_fn=_get) == 0
    assert seen["ua"] == "curl/8.7.1"


def test_live_probe_bootstraps_sys_path_before_bot_import():
    """AST: a ``sys.path.insert(...)`` statement precedes the first
    ``from bot.… import`` (cron/operator invocation has no editable
    install — feedback_monitor_the_monitor)."""
    import ast
    repo = Path(__file__).resolve().parents[2]
    tree = ast.parse((repo / "scripts" / "ops" / "espn_live_probe.py").read_text())
    bootstrap_line = None
    bot_import_line = None
    for node in ast.iter_child_nodes(tree):
        if (
            bootstrap_line is None and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and ast.unparse(node.value.func) in ("sys.path.insert", "sys.path.append")
        ):
            bootstrap_line = node.lineno
        if (
            bot_import_line is None and isinstance(node, ast.ImportFrom)
            and node.module and node.module.split(".")[0] == "bot"
        ):
            bot_import_line = node.lineno
    assert bootstrap_line is not None, "no sys.path bootstrap in espn_live_probe.py"
    assert bot_import_line is not None, "probe must import from bot.engines.sports_engine"
    assert bootstrap_line < bot_import_line


# ─── F. bot ESPNLiveFeed WARN on non-200 ────────────────────────────────────

def _first_espn_league_cfg():
    import bot.engines.sports_data as sd
    for _series, cfg in sd.LEAGUES.items():
        if cfg.enabled and cfg.espn_league:
            return cfg
    raise AssertionError("no ESPN-eligible league in sports_data.LEAGUES")


def test_bot_feed_warns_on_non_200_throttled(caplog):
    import bot.engines.sports_engine as se
    clock = _Clock()
    feed = se.ESPNLiveFeed(monotonic_fn=clock)
    feed._session = _FakeSession([_Resp(403), _Resp(403), _Resp(403)])
    cfg = _first_espn_league_cfg()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(requests.HTTPError):
            feed._poll_league(cfg)
        clock.t += 60
        with pytest.raises(requests.HTTPError):
            feed._poll_league(cfg)          # throttled
        clock.t += 3601
        with pytest.raises(requests.HTTPError):
            feed._poll_league(cfg)          # warns again
    warns = [r for r in caplog.records
             if r.levelno == logging.WARNING and "403" in r.getMessage()]
    assert len(warns) == 2, [r.getMessage() for r in caplog.records]
    assert cfg.espn_league in warns[0].getMessage()


def test_sports_engine_note_tick_counts_live_ticks_in_24h_window(tmp_path):
    import bot.engines.sports_engine as se
    clock = _Clock()
    eng = se.SportsEngine(db_path=str(tmp_path / "state.db"), monotonic_fn=clock)
    for _ in range(25):
        eng._note_tick(n_games=5, n_live=2)
        clock.t += 30
    eng._note_tick(n_games=5, n_live=0)  # a tick with no live games
    sc = json.loads((tmp_path / "sports_health.json").read_text())
    assert sc["schema_version"] == 1
    assert sc["live_ticks_in_window"] == 25
    assert sc["live_ticks_window_seconds"] == se.SPORTS_HEALTH_WINDOW_SECONDS
    assert sc["last_poll_live"] == 0 and sc["last_poll_games"] == 5
    assert sc["last_live_at"] is not None
    clock.t += se.SPORTS_HEALTH_WINDOW_SECONDS + 1
    eng._note_tick(n_games=0, n_live=0)
    sc = json.loads((tmp_path / "sports_health.json").read_text())
    assert sc["live_ticks_in_window"] == 0


def test_sports_engine_tick_writes_sidecar_even_when_espn_403s(tmp_path):
    """The early `if not games: return` must not skip the health stamp —
    a 403'd ESPN is exactly when the sidecar matters."""
    import bot.engines.sports_engine as se
    eng = se.SportsEngine(db_path=str(tmp_path / "state.db"))
    eng._espn._session = _FakeSession([_Resp(403)] * 64)
    eng._tick()
    sc = json.loads((tmp_path / "sports_health.json").read_text())
    assert sc["last_poll_games"] == 0 and sc["live_ticks_in_window"] == 0
    statuses = sc["espn_last_poll_status"]
    assert statuses and all(v == 403 for v in statuses.values())


def test_sports_engine_memory_db_has_no_sidecar():
    import bot.engines.sports_engine as se
    eng = se.SportsEngine(db_path=":memory:")
    assert eng._health_sidecar_path is None
    eng._note_tick(n_games=1, n_live=1)  # must not raise


def test_bot_feed_poll_all_leagues_survives_403(caplog):
    """poll_all_leagues must still swallow per-league failures (no crash)."""
    import bot.engines.sports_engine as se
    feed = se.ESPNLiveFeed()
    feed._session = _FakeSession([_Resp(403)] * 64)
    with caplog.at_level(logging.WARNING):
        out = feed.poll_all_leagues()
    assert out == {}
