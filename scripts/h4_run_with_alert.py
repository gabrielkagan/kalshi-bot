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


def _on_sigterm(signum, frame):
    """Round-2 critique fix: systemd's TimeoutStartSec sends SIGTERM,
    which by default (SIG_DFL) terminates the wrapper before the post-
    subprocess alert fires. This handler intercepts SIGTERM so the
    operator gets an alert when a backfill is killed (timeout, manual
    kill, system shutdown). Without this, multi-day catch-up timeouts
    are exactly the silent-failure mode the alerting was meant to
    prevent.
    """
    elapsed = time.monotonic() - _START_TIME if _START_TIME else 0.0
    label = _LABEL or '<unknown>'
    cmd = _CMD or []
    # Try to terminate the child process so we don't leak it. Best-effort.
    if _PROC is not None and _PROC.poll() is None:
        try:
            _PROC.terminate()
        except Exception:
            pass
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
    """
    cmd_str = " ".join(cmd)
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
    global _LABEL, _CMD, _START_TIME, _PROC
    _LABEL = args.label
    _CMD = cmd
    _START_TIME = time.monotonic()
    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        _PROC = subprocess.Popen(cmd)
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
