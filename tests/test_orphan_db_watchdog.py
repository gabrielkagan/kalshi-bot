"""Tests for the startup orphan-DB watchdog (Layer 3 of orphan
prevention; see kb/failures/shape-d-contention-explosion-may03.md).

Layer 3 detects non-bot processes holding `state.db` open at bot
startup — exactly the situation that wedged the bot for ~6 minutes
of crash-loop on May 3 2026 when the H-4c CryptoCompare backfill
script orphaned itself and held the writer lock for 2h42m.

The watchdog uses `lsof -t state.db` to enumerate PIDs touching the
DB file, filters out the bot's own PID, and:
  - logs ERROR with each offender's command line
  - sends a Telegram alert (deduped) so the operator sees it
  - does NOT auto-kill (too risky; let the operator triage)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# Bit 9.3-ii (2026-05-10): orphan-DB block extracted to bot/orphan_db_watchdog.py
# (clean leaf). The 11 `monkeypatch.setattr(bot.orphan_db_watchdog, "X", ...)` sites
# below target the new canonical location. The explicit `import bot.orphan_db_watchdog`
# below registers the submodule in `sys.modules['bot'].__dict__` so attribute access
# `bot.orphan_db_watchdog` resolves via the package's own __dict__ (bypassing
# _BotProxy.__getattr__'s fallback to bot._impl — which doesn't have the submodule
# as an attribute, only an explicit-name re-export at line ~120). Without this
# import, `bot.orphan_db_watchdog.X` raises AttributeError because the proxy
# falls through to `getattr(bot._impl, "orphan_db_watchdog")` which is missing.
import bot  # noqa: F401
import bot.orphan_db_watchdog  # noqa: F401 — registers submodule on bot package; required for monkeypatch.setattr(bot.orphan_db_watchdog, ...) to resolve post-Bit-9.3-ii
# Pre-load bot._impl BEFORE any monkeypatch — bot._impl re-exports the orphan-DB
# names via `from bot.orphan_db_watchdog import _run_lsof_for_db, ...` (captured-
# by-value semantics per L83). If bot._impl loads DURING a test that has
# monkeypatched bot.orphan_db_watchdog._run_lsof_for_db, the re-export captures
# the monkeypatched lambda and the binding leaks across tests. Pre-loading
# pre-binds the re-exports to the original function objects, restoring proper
# isolation. See tests/test_orphan_db_watchdog_extraction.py::test_proxy_chain_*
# for the related identity pins.
import bot._impl  # noqa: F401 — pre-bind re-exports to original function objects pre-monkeypatch


def test_orphan_watchdog_returns_no_offenders_when_db_is_held_by_self_only(
    monkeypatch, tmp_path,
):
    """Healthy startup: only the bot itself holds state.db. The
    watchdog must report zero offenders and NOT send a Telegram alert."""
    import bot
    db = tmp_path / "state.db"
    db.touch()

    # lsof reports only the current PID. Watchdog should ignore it.
    self_pid = os.getpid()
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_run_lsof_for_db",
        lambda path: [self_pid] if path == str(db) else [],
    )
    sent = []
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_alert_orphan_db_holder",
        lambda **kw: sent.append(kw),
    )

    offenders = bot.detect_orphan_db_holders(str(db), self_pid=self_pid)
    assert offenders == [], f"healthy case must report []; got {offenders}"
    assert sent == [], f"no alert when only self holds DB; got {sent}"


def test_orphan_watchdog_detects_non_bot_pid_holding_db(monkeypatch, tmp_path):
    """May 3 incident: a `cryptocompare_news_backfill.py` orphan PID
    held state.db. The watchdog must detect this distinct PID and
    return it as an offender — without auto-killing."""
    import bot
    db = tmp_path / "state.db"
    db.touch()

    self_pid = os.getpid()
    orphan_pid = self_pid + 1  # any PID != self
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_run_lsof_for_db",
        lambda path: [self_pid, orphan_pid] if path == str(db) else [],
    )
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_get_pid_cmdline",
        lambda pid: (
            "venv/bin/python3 scripts/cryptocompare_news_backfill.py "
            "--db state.db" if pid == orphan_pid else "bot/_impl.py"
        ),
    )
    # C-9 fix: liveness probe expects the orphan PID to be alive.
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    offenders = bot.detect_orphan_db_holders(str(db), self_pid=self_pid)
    assert orphan_pid in [o["pid"] for o in offenders], (
        f"must detect orphan pid {orphan_pid}; got {offenders}"
    )
    # cmdline of the offender is captured (operator needs to know
    # what to kill).
    o = next(x for x in offenders if x["pid"] == orphan_pid)
    assert "cryptocompare_news_backfill" in o["cmdline"], (
        f"offender cmdline must be captured: {o}"
    )


# ── Adversarial-review C-1: positive-list (no false positives) ────────

def test_orphan_watchdog_skips_alert_for_legitimate_cron_processes(
    monkeypatch, tmp_path,
):
    """C-1 (HIGH from adversarial review): the live VPS has several
    legitimate processes that hold state.db open at any moment —
    `watchdog.py` (every 2 min cron), `auditor.py` (hourly),
    `audit_cron.py` (every 30 min systemd timer), `dashboard_snapshot.py`
    (manual). Pre-fix watchdog would have alerted on every collision,
    habituating the operator to ignore the channel. Post-fix:
    positive-list of orphan-creator scripts only — anything else is
    logged at DEBUG and not alerted."""
    import bot
    db = tmp_path / "state.db"
    db.touch()

    self_pid = os.getpid()
    cron_pid = 7777  # not self, but a legitimate cron-spawned process
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_run_lsof_for_db",
        lambda path: [self_pid, cron_pid] if path == str(db) else [],
    )
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_get_pid_cmdline",
        lambda pid: "/home/botuser/kalshi-bot-repo/venv/bin/python3 watchdog.py",
    )
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    sent = []
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_alert_orphan_db_holder",
        lambda **kw: sent.append(kw),
    )

    offenders = bot.detect_orphan_db_holders(str(db), self_pid=self_pid)
    # The PID IS reported as an offender (returned for caller-side
    # inspection) but NO alert is sent.
    assert any(o["pid"] == cron_pid for o in offenders), (
        f"legitimate cron PID should still be in offenders list "
        f"(for visibility), just without alerting; got {offenders}"
    )
    assert sent == [], (
        f"must NOT alert for `watchdog.py` cron (false-positive class "
        f"per adversarial review C-1); got: {sent}"
    )


def test_orphan_watchdog_alerts_on_h4_backfill_pattern(monkeypatch, tmp_path):
    """The positive-list MUST include all 3 H-4 backfill scripts:
    cryptocompare_news_backfill, gdelt_backfill, glassnode_backfill.
    Each is a known orphan-creation path."""
    import bot
    db = tmp_path / "state.db"
    db.touch()
    self_pid = os.getpid()
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    for script_name in (
        "cryptocompare_news_backfill",
        "gdelt_backfill",
        "glassnode_backfill",
    ):
        sent = []
        orphan_pid = 50000 + hash(script_name) % 10000
        monkeypatch.setattr(
            bot.orphan_db_watchdog, "_run_lsof_for_db",
            lambda path, _o=orphan_pid: [self_pid, _o] if path == str(db) else [],
        )
        monkeypatch.setattr(
            bot.orphan_db_watchdog, "_get_pid_cmdline",
            lambda pid, _s=script_name: f"venv/bin/python3 scripts/{_s}.py --db state.db",
        )
        monkeypatch.setattr(
            bot.orphan_db_watchdog, "_alert_orphan_db_holder",
            lambda **kw: sent.append(kw),
        )
        bot.detect_orphan_db_holders(str(db), self_pid=self_pid)
        assert len(sent) == 1, (
            f"must alert on {script_name}; got: {sent}"
        )


# ── Adversarial-review C-9: PID exited mid-probe → no false alert ────

def test_orphan_watchdog_skips_alert_when_pid_already_exited(
    monkeypatch, tmp_path,
):
    """C-9: the orphan PID may exit between the lsof snapshot and our
    cmdline lookup. `os.kill(pid, 0)` raises ProcessLookupError when
    the PID is gone. Pre-fix: alert fires with cmdline='(unknown)'.
    Post-fix: skip the alert entirely (the orphan is already dead;
    no need to wake the operator)."""
    import bot
    db = tmp_path / "state.db"
    db.touch()
    self_pid = os.getpid()
    orphan_pid = 88888

    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_run_lsof_for_db",
        lambda path: [self_pid, orphan_pid] if path == str(db) else [],
    )
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_get_pid_cmdline",
        lambda pid: "python3 scripts/cryptocompare_news_backfill.py",
    )

    def _kill_raises(pid, sig):
        if pid == orphan_pid:
            raise ProcessLookupError("orphan exited between probes")
    monkeypatch.setattr(os, "kill", _kill_raises)

    sent = []
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_alert_orphan_db_holder",
        lambda **kw: sent.append(kw),
    )

    offenders = bot.detect_orphan_db_holders(str(db), self_pid=self_pid)
    assert offenders == [], (
        f"must drop already-exited PID; got {offenders}"
    )
    assert sent == [], "must not alert on a PID that has already exited"


def test_orphan_watchdog_does_not_kill(monkeypatch, tmp_path):
    """The watchdog detects + alerts but does NOT call `os.kill`
    with a signal SIGNUM other than 0 (signal 0 is the liveness probe
    added per adversarial-review C-9 — counts as "not killing").
    Auto-killing would risk killing legitimate processes (manually-
    launched migrations, debug sessions); the operator triages."""
    import signal as _signal
    import bot
    db = tmp_path / "state.db"
    db.touch()

    self_pid = os.getpid()
    orphan_pid = 99999
    kills = []
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_run_lsof_for_db",
        lambda path: [self_pid, orphan_pid] if path == str(db) else [],
    )
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_get_pid_cmdline",
        lambda pid: "python3 scripts/cryptocompare_news_backfill.py",
    )
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))

    bot.detect_orphan_db_holders(str(db), self_pid=self_pid)
    # Only signal 0 (liveness probe) is allowed.
    non_probe_kills = [(p, s) for (p, s) in kills if s != 0]
    assert non_probe_kills == [], (
        f"watchdog must NOT send any signal other than 0 (liveness "
        f"probe); got: {non_probe_kills}. Auto-kill is too risky — "
        f"let the operator triage via the Telegram alert."
    )


def test_orphan_watchdog_alerts_via_telegram(monkeypatch, tmp_path):
    """When an orphan IS detected, the watchdog sends a Telegram
    alert that includes the PID and the command line so the operator
    can investigate without journalctl."""
    import bot
    db = tmp_path / "state.db"
    db.touch()

    self_pid = os.getpid()
    orphan_pid = 12345
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_run_lsof_for_db",
        lambda path: [self_pid, orphan_pid] if path == str(db) else [],
    )
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_get_pid_cmdline",
        lambda pid: "python3 scripts/cryptocompare_news_backfill.py",
    )
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    sent = []
    monkeypatch.setattr(
        bot.orphan_db_watchdog, "_alert_orphan_db_holder",
        lambda **kw: sent.append(kw),
    )

    bot.detect_orphan_db_holders(str(db), self_pid=self_pid)
    assert len(sent) == 1, f"expected exactly 1 alert; got: {sent}"
    msg_kwargs = sent[0]
    assert msg_kwargs.get("pid") == orphan_pid
    assert "cryptocompare_news_backfill" in msg_kwargs.get("cmdline", "")


def test_orphan_watchdog_handles_lsof_failure_gracefully(
    monkeypatch, tmp_path,
):
    """If `lsof` is missing or fails (returns non-zero), the watchdog
    must NOT crash bot startup — log a warning and proceed. Otherwise
    the watchdog itself becomes a startup-blocker."""
    import bot
    db = tmp_path / "state.db"
    db.touch()

    def _lsof_fail(path):
        raise FileNotFoundError("lsof not installed")

    monkeypatch.setattr(bot.orphan_db_watchdog, "_run_lsof_for_db", _lsof_fail)

    # Must not raise.
    offenders = bot.detect_orphan_db_holders(str(db), self_pid=os.getpid())
    # On lsof failure, we conservatively report no offenders rather
    # than blocking startup.
    assert offenders == []


def test_orphan_watchdog_invoked_on_main_loop_startup():
    """Regression: MainLoop.startup() must call detect_orphan_db_holders
    EARLY in startup (before the 7-day soak begins on each new bot
    process). Verified via AST scan to avoid spinning up a real bot.

    Bit 9.3-ii (2026-05-10): the orphan-DB Layer-3 watchdog block (including
    `detect_orphan_db_holders`) relocated from bot/_impl.py to
    `bot/orphan_db_watchdog.py` clean leaf. MainLoop.startup() now reaches
    `detect_orphan_db_holders` via method-body late-binding
    `from bot.orphan_db_watchdog import detect_orphan_db_holders` (retarget
    atomic with the extraction; previous Bit-9.3 form was
    `from bot._impl import detect_orphan_db_holders`). This test walks
    bot/main_loop.py for the CALL site, independent of import source."""
    import ast
    bot_py = (PROJECT_ROOT / "bot/main_loop.py").read_text()
    tree = ast.parse(bot_py)

    main_loop_cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MainLoop":
            main_loop_cls = node
            break
    assert main_loop_cls is not None, "MainLoop class not found in bot/main_loop.py"

    startup_fn = None
    for node in main_loop_cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == "startup":
            startup_fn = node
            break
    assert startup_fn is not None, "MainLoop.startup not found"

    found_call = False
    for node in ast.walk(startup_fn):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name)
                else None
            )
            if name == "detect_orphan_db_holders":
                found_call = True
                break
    assert found_call, (
        "MainLoop.startup() must call detect_orphan_db_holders "
        "(Layer 3 of orphan prevention; see "
        "kb/failures/shape-d-contention-explosion-may03.md)"
    )
