#!/usr/bin/env python3
"""H-4 backfill wrapper with Telegram failure alerting.

Wraps a single H-4 backfill script invocation so non-zero exits
(uncaught exceptions, GdeltFetchError, GlassnodeAuthError, network
errors, etc.) trigger a Telegram alert instead of silently failing
inside journalctl. Without this, a multi-day H-4 outage stays invisible
until the v2 calibrator deploy gate fires (`null_pct < 0.10` per
kb/decisions/phase-h-forward-going-capture-required-may02.md), by
which time 1-3 weeks of evaluated_opportunities rows have accumulated
with NULL features — silently blocking v2 ship.

Usage:
    h4_run_with_alert.py --label gdelt -- python3 scripts/gdelt_backfill.py --db state.db

The `--label` is the human-readable backfill name that appears in the
Telegram alert. Args after `--` are the actual command to run; the
wrapper passes them through unchanged.

Exit code mirrors the wrapped script's exit code so systemd still marks
the unit `failed` on non-zero — the alert is supplemental, not a
substitute for journalctl/systemd state.

Telegram creds come from TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID env
vars. If unset, the wrapper logs a warning to stderr but does NOT
fail (matches doc_drift_check.py behavior).
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from typing import List, Optional


# Module-level state for the SIGTERM handler. Populated by main() before
# the subprocess starts. The handler reads these to format the alert.
_LABEL: Optional[str] = None
_CMD: Optional[List[str]] = None
_START_TIME: Optional[float] = None
_PROC: Optional[subprocess.Popen] = None


# Grace periods for child-process termination on signal. Tuned for the
# H-4 backfill scripts (which do API calls + DB writes — should respond
# to SIGTERM within seconds if cooperative). After _TERM_GRACE_S without
# child exit, escalate to SIGKILL.
_TERM_GRACE_S = 10
_KILL_GRACE_S = 5


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """Poll for `pid` to exit AND be reaped, using
    `os.waitpid(pid, WNOHANG)`. Returns True if the process is gone
    within `timeout`, False if it's still alive after.

    Why not `_PROC.wait(timeout=N)`: when this runs from inside the
    SIGTERM/SIGHUP handler, the wrapper's main thread is already
    blocked in `_PROC.wait()` (line `exit_code = _PROC.wait()` below).
    Python's subprocess.Popen uses a non-reentrant `_waitpid_lock` to
    serialize waitpid() calls, and `Popen.wait(timeout)` / `Popen.poll()`
    both try to acquire it. The outer wait() holds it; our inner call
    deadlocks. Raw `os.waitpid(pid, WNOHANG)` skips Popen's lock —
    it's just a syscall — so it's signal-handler-safe.

    Why not `os.kill(pid, 0)`: that returns success on a ZOMBIE (the
    child exited but no one has called waitpid to reap it). The outer
    Popen.wait() can't reap because it's suspended by our signal
    handler. So `os.kill(pid, 0)` would loop until timeout. Using
    `os.waitpid(WNOHANG)` actively reaps the zombie, satisfying both
    the "is it gone" question and the "release the zombie" duty in one
    call. The OUTER wait() that we never return to may see ECHILD
    afterward — that's fine because we sys.exit() before resuming."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            wpid, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            # ECHILD: child already reaped (e.g., by outer wait that
            # raced us, or because the child wasn't ours to begin with).
            return True
        except OSError:
            # Defensive: treat unexpected errors as "process gone"
            # rather than spinning forever.
            return True
        if wpid == pid:
            return True  # reaped
        # wpid == 0 → child still running.
        time.sleep(0.05)
    return False


def _on_sigterm(signum, frame):
    """Round-2 critique fix: systemd's TimeoutStartSec sends SIGTERM,
    which by default (SIG_DFL) terminates the wrapper before the post-
    subprocess alert fires. This handler intercepts SIGTERM so the
    operator gets an alert when a backfill is killed (timeout, manual
    kill, system shutdown). Without this, multi-day catch-up timeouts
    are exactly the silent-failure mode the alerting was meant to
    prevent.

    May 3 2026 fix (orphan prevention): the original implementation
    called `_PROC.terminate()` and then immediately `sys.exit(143)`
    without waiting for the child to actually exit. A
    `cryptocompare_news_backfill.py` instance survived 2h42m holding
    state.db's writer lock, eventually wedging the live bot's
    eval-write path. See `kb/decisions/h4-backfill-bugs-may04.md`
    (Bug 2 SEVERITY UPGRADE).

    Escalation policy: terminate() → wait up to `_TERM_GRACE_S` →
    if still alive, kill() → wait up to `_KILL_GRACE_S` → exit. The
    waits give the child time to release its sqlite3 connection and
    flush, while the SIGKILL escalation guarantees we don't leak the
    process even if it ignores SIGTERM.
    """
    elapsed = time.monotonic() - _START_TIME if _START_TIME else 0.0
    label = _LABEL or '<unknown>'
    cmd = _CMD or []
    # Reap the child. terminate() → poll-wait → kill() → poll-wait. We
    # use `os.kill(pid, 0)` polling instead of `_PROC.wait(timeout=N)`
    # because we're inside a signal handler that interrupted the outer
    # `_PROC.wait()` — Popen's `_waitpid_lock` is non-reentrant, so any
    # call into Popen.wait()/poll() from here would deadlock. See
    # `_wait_for_exit` docstring. Exceptions are swallowed because the
    # alert + exit must happen regardless.
    if _PROC is not None:
        pid = _PROC.pid
        # Use raw `os.kill` instead of `_PROC.terminate()/kill()` because
        # `Popen.send_signal` calls `Popen.poll()` first which tries to
        # acquire `_waitpid_lock`. The outer `_PROC.wait()` (in main())
        # holds that lock; `acquire(False)` in poll() returns False, so
        # send_signal falls through and signals the PID anyway — but
        # the PID-recycle protection is bypassed. Raw `os.kill(pid, sig)`
        # is no worse on the recycle front and skips the misleading
        # detour through Popen's lock. Adversarial-review critique #1.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already dead
        except OSError:
            pass
        if not _wait_for_exit(pid, _TERM_GRACE_S):
            # Child ignored SIGTERM; escalate to SIGKILL.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                pass
            _wait_for_exit(pid, _KILL_GRACE_S)
    msg = (
        f"H-4 backfill SIGTERM'd: {label}\n"
        f"ran {elapsed:.0f}s\n"
        f"likely cause: TimeoutStartSec hit (systemd) or operator kill\n"
        f"cmd: {' '.join(cmd)}\n"
        f"View logs: journalctl -u kalshi-h4-{label}.service -n 100"
    )
    send_telegram_alert(msg)
    # 143 = 128 + 15 (SIGTERM); standard convention for "killed by SIGTERM"
    # so systemd sees a non-zero exit consistent with normal failure path.
    sys.exit(143)


def send_telegram_alert(message: str) -> bool:
    """Post `message` to Telegram. Returns True on success.

    Mirrors doc_drift_check.send_telegram contract: if env creds are
    missing, log to stderr and return False without raising — alert
    failure must NOT compound a backfill failure.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print(
            "h4_alert: TELEGRAM_BOT_TOKEN/CHAT_ID not set; skipping alert",
            file=sys.stderr,
        )
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        "text": message[:4096],
    }).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"h4_alert: telegram POST failed: {e}", file=sys.stderr)
        return False


def format_failure_message(label: str, exit_code: int, cmd: List[str]) -> str:
    """Compact Telegram message for a backfill failure.

    Plain text (no Markdown) to avoid escaping foot-guns on file paths.

    Special-case exit_code == 124 (GNU `timeout` convention, used by
    `_h4_runtime_safety.install_hard_timeout`): label as "HARD TIMEOUT"
    instead of "FAILED" so the operator knows this is the orphan-
    prevention ceiling firing — not a fault that needs investigation.
    First-run on a multi-day historical backfill is expected to hit
    this ceiling repeatedly until the historical NULL rows are drained.
    """
    cmd_str = " ".join(cmd)
    if exit_code == 124:
        return (
            f"H-4 backfill HARD TIMEOUT (will resume next run): {label}\n"
            f"exit_code=124\n"
            f"cmd: {cmd_str}\n"
            f"View logs: journalctl -u kalshi-h4-{label}.service -n 100"
        )
    return (
        f"H-4 backfill FAILED: {label}\n"
        f"exit_code={exit_code}\n"
        f"cmd: {cmd_str}\n"
        f"View logs: journalctl -u kalshi-h4-{label}.service -n 100"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="H-4 backfill wrapper with Telegram failure alerting",
    )
    parser.add_argument(
        "--label", required=True,
        help="backfill name shown in alerts (e.g. gdelt, glassnode, cryptocompare)",
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER,
        help="command to run after `--` (e.g. python3 scripts/gdelt_backfill.py --db state.db)",
    )
    args = parser.parse_args(argv)

    cmd = args.command
    # argparse REMAINDER includes the leading `--` separator — strip it.
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print(
            "h4_alert: no command after `--`; refusing to proceed",
            file=sys.stderr,
        )
        return 2

    # Round-2 fix: install SIGTERM handler BEFORE starting subprocess so
    # systemd's TimeoutStartSec doesn't kill us before the alert fires.
    # H-4 GH-Actions pivot round-1 critique #5: also handle SIGHUP — when
    # appleboy/ssh-action's command_timeout fires, it tears down the SSH
    # channel; the controlling terminal goes away → child process group
    # receives SIGHUP, not SIGTERM. Without SIGHUP handling, the GH
    # Actions timeout case bypasses alerting (the very failure mode the
    # alert is for).
    global _LABEL, _CMD, _START_TIME, _PROC
    _LABEL = args.label
    _CMD = cmd
    _START_TIME = time.monotonic()
    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGHUP, _on_sigterm)

    try:
        # `start_new_session=True` puts the child in its own session +
        # process group. Without this, if the wrapper itself is SIGKILLed
        # (uncatchable, no handler runs), the child is reparented to
        # init/systemd and may linger as an orphan — the May 3 incident
        # signature. With its own session, systemd's cgroup cleanup OR
        # the SIGHUP that propagates on SSH session teardown reaches the
        # child reliably. Adversarial-review critique #6.
        _PROC = subprocess.Popen(cmd, start_new_session=True)
    except FileNotFoundError as e:
        print(f"h4_alert: command not found: {e}", file=sys.stderr)
        send_telegram_alert(format_failure_message(args.label, 127, cmd))
        return 127

    exit_code = _PROC.wait()
    if exit_code != 0:
        send_telegram_alert(format_failure_message(args.label, exit_code, cmd))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
