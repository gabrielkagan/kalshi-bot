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


_LIVE_LINE = "Sep 06 16:00:00 host python[1]: SportsEngine tick: 3 live games, 0 signals\n"


def test_sports_silence_none_when_db_missing(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    with patch.object(mod.subprocess, "check_output", return_value=_LIVE_LINE * 30):
        assert mod.check_sports_eval_silence(db_path=tmp_path / "no.db") is None


def test_sports_silence_none_when_journalctl_unavailable(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    with patch.object(mod.subprocess, "check_output", side_effect=FileNotFoundError):
        assert mod.check_sports_eval_silence(db_path=db) is None


def test_sports_silence_none_when_no_live_games(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    with patch.object(mod.subprocess, "check_output", return_value="nothing here\n"):
        assert mod.check_sports_eval_silence(db_path=db) is None


def test_sports_silence_none_below_min_live_ticks(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    with patch.object(mod.subprocess, "check_output", return_value=_LIVE_LINE * 5):
        assert mod.check_sports_eval_silence(db_path=db, min_live_ticks=20) is None


def test_sports_silence_none_when_sports_rows_exist(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [("sports", _iso(10)), ("15m", _iso(5))])
    with patch.object(mod.subprocess, "check_output", return_value=_LIVE_LINE * 30):
        assert mod.check_sports_eval_silence(
            db_path=db, window_min=180, min_live_ticks=20,
        ) is None


def test_sports_silence_alerts_when_live_games_but_zero_sports_rows(tmp_path):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    # Only non-sports rows in-window + a sports row OUTSIDE the window.
    _make_db(db, [("15m", _iso(5)), ("sports", _iso(600))])
    with patch.object(mod.subprocess, "check_output", return_value=_LIVE_LINE * 30):
        out = mod.check_sports_eval_silence(
            db_path=db, window_min=180, min_live_ticks=20,
        )
    assert out is not None
    assert "SPORTS" in out.upper()
    assert "evaluated_opportunities" in out
    assert "30" in out  # live-game tick count surfaced


def test_archiver_deque_bounded_without_reader():
    """R1-m2: eviction happens on write, so a never-read stats deque
    stays bounded to one window."""
    clock = _Clock()
    arch, _ = _make_archiver([_Resp(200)] * 200, monotonic=clock)
    for _ in range(200):
        arch.poll_once()
        clock.t += 60  # 200 min of polls, window is 60 min
    assert len(arch._http_status_samples["nba"]) <= 61


def test_sports_silence_journal_filtered_server_side_with_long_timeout(tmp_path):
    """R1-C1: the unfiltered 3h journal pull took ~14s on the VPS vs a
    10s timeout — the check could never evaluate. Pin -g/-o cat + ≥60s."""
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    seen = {}

    def _co(args, **kw):
        seen["args"] = args
        seen["kw"] = kw
        return ""

    with patch.object(mod.subprocess, "check_output", side_effect=_co):
        mod.check_sports_eval_silence(db_path=db)
    args = seen["args"]
    assert "-g" in args and args[args.index("-g") + 1] == mod.SPORTS_LIVE_TICK_PATTERN
    assert "-o" in args and args[args.index("-o") + 1] == "cat"
    assert seen["kw"]["timeout"] >= 60
    assert "1440 minutes ago" in args, "default window is 24h (R1-M2)"


def test_sports_silence_timeout_prints_skipped_not_silent(tmp_path, capsys):
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    with patch.object(
        mod.subprocess, "check_output",
        side_effect=mod.subprocess.TimeoutExpired(cmd="journalctl", timeout=60),
    ):
        assert mod.check_sports_eval_silence(db_path=db) is None
    assert "SKIPPED" in capsys.readouterr().err


def test_sports_silence_grep_no_match_exit_1_is_quiet(tmp_path, capsys):
    """journalctl -g exits 1 when nothing matches — that is 'no live
    games', not a skip."""
    import scripts.ops.collector_health_monitor as mod
    db = tmp_path / "state.db"
    _make_db(db, [])
    with patch.object(
        mod.subprocess, "check_output",
        side_effect=mod.subprocess.CalledProcessError(1, "journalctl"),
    ):
        assert mod.check_sports_eval_silence(db_path=db) is None
    assert "SKIPPED" not in capsys.readouterr().err


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


def test_bot_feed_poll_all_leagues_survives_403(caplog):
    """poll_all_leagues must still swallow per-league failures (no crash)."""
    import bot.engines.sports_engine as se
    feed = se.ESPNLiveFeed()
    feed._session = _FakeSession([_Resp(403)] * 64)
    with caplog.at_level(logging.WARNING):
        out = feed.poll_all_leagues()
    assert out == {}
