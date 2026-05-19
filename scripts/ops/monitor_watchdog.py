#!/usr/bin/env python3
"""Monitor-the-monitor: alert on stale cron-driven monitor logs.

Data-Integrity Stage E.1, ticket 86ba0xq51 (2026-05-19). Structural fix for
the silent-monitor-death class (`feedback_monitor_the_monitor` memory):

- 2026-05-17→19 `data_health_monitor` DEAD (stale script path; cron logged
  `FileNotFoundError` for 2 days; nobody read the log). Fixed in A.1
  (`86ba0xmmq`).
- 2026-05-17→19 `collector_health_monitor` DEAD (sys.path bug; same silent
  pattern). Fixed in `86ba0jvka`.
- 2026-05-19 `quiet_market_monitor` LIKELY DEAD (same drift class as
  data_health). Ticket `86ba0k557` open.

E.1 closes the class structurally: stat each monitor's log file every 10 min;
if mtime is older than the monitor's max_stale window OR the file is
missing, Telegram-alert via the existing `bot.notifier.TelegramNotifier`
with the inherited 60-second in-process dedup. Because the script runs
under cron (one fresh process per tick), the in-process dedup window is
RESET on every tick — so a chronically-dead monitor produces one alert
per cron tick (6/hour at the 10-min cadence). With the verified watchlist
of 4 monitors, max alert burst is 4 alerts/tick × 6 ticks/hour = 24/hr in
the all-dead scenario. Acceptable per cron health-script convention;
operator can throttle by raising the cron interval if false-positives
become noisy. A future Bit may switch to file-sidecar dedup like
`scripts/ops/phantom_reconcile_monitor.py` to suppress cross-tick
duplicates.

Operator install (manual, post-merge):

    # In `crontab -e` (botuser):
    */10 * * * * cd ~/kalshi-bot-repo && source venv/bin/activate && set -a && source ~/.env && set +a && python3 scripts/ops/monitor_watchdog.py >> ~/monitor_watchdog.log 2>&1

Env reads (in main()):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID — from /home/botuser/.env (loaded
    by the shell that invokes this script). Both are passed positionally
    to the TelegramNotifier constructor. If either is missing the
    notifier silently no-ops (its .enabled property is False); the
    script still runs cleanly and prints the per-tick summary.

Exit code: ALWAYS 0 (cron health-script convention; alerts go via Telegram,
not exit code, so a transient health-check failure doesn't flood the
operator's mail spool).

Self-referential blind spot: this script's OWN log freshness is the residual
gap. Mitigations:
  1. Higher cadence (10 min) than any watched monitor (15-120 min) — operator
     notices absence of expected alerts faster.
  2. Future E-followup: external probe or self-referential check.
"""
from __future__ import annotations

import dataclasses
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Bootstrap repo root onto sys.path BEFORE any `from bot.*` reference fires.
# Cron's invocation flow (`cd ~/kalshi-bot-repo && source venv/bin/activate
# && python3 scripts/ops/monitor_watchdog.py`) does NOT auto-add the repo
# root to sys.path — only the script's parent dir (scripts/ops/) is added by
# Python's script-invocation rule. Without this bootstrap the script crashes
# at `from bot.notifier import TelegramNotifier` with
# `ModuleNotFoundError: No module named 'bot'`. Pattern matches
# `scripts/ops/collector_health_monitor.py` (fixed in `86ba0jvka`).
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@dataclasses.dataclass(frozen=True)
class WatchedMonitor:
    """One row of the watch config.

    Attributes:
        name: short monitor identifier, used in dedup key + alert text.
        log_path: filesystem path to the monitor's log. Supports ``~`` /
            ``~user`` expansion via :func:`os.path.expanduser` AND
            ``$VAR`` / ``${VAR}`` expansion via :func:`os.path.expandvars`
            (both applied at check time — see
            :func:`check_log_freshness`). Relative paths resolve against
            the watchdog's cwd.
        max_stale_minutes: alert when log mtime is older than this (in
            minutes). Convention: 2× the monitor's cron interval, providing
            margin for cron skew + monitor runtime.
    """

    name: str
    log_path: str
    max_stale_minutes: int


# Inline config (over YAML) — single-source-of-truth + zero new deps. Adjust
# thresholds via git edit + commit, same Bit gate as code change. Cron
# interval annotations are documentation; the watchdog only consumes
# `max_stale_minutes`.
#
# Each entry MUST correspond to a cron line that uses `>> <log_path> 2>&1`
# redirection (verified against live `crontab -l` on the VPS 2026-05-19).
# The log path can be `/tmp/`, `~/`, or any absolute path — what matters
# is that the cron line redirects stdout+stderr to the file referenced
# below, so the watchdog's mtime check has a meaningful signal. Adding an
# entry without a matching cron-line redirect would Telegram-storm with
# false-positive MISSING alerts on every tick.
#
# Excluded by design (no `>> <log> 2>&1` cron-line redirect on the VPS today):
#   - watchdog (ops/watchdog.py)  — stdout goes to cron mail spool
#   - researcher / analyst / auditor — these are bot-RUNTIME classes
#     (bot/ai/*), not cron scripts; their `.log` files are written by
#     the bot service, not by cron — different freshness semantics.
# Promoting any of these requires (a) adding `>> <log_path> 2>&1` to
# its cron entry in ops/CLAUDE.md + live crontab AND (b) extending the
# watchlist below in the same Bit.
WATCHED_MONITORS: tuple[WatchedMonitor, ...] = (
    WatchedMonitor("data_health",       "/tmp/data_health.log",       60),  # cron */30 — 2× margin
    WatchedMonitor("quiet_market",      "/tmp/quiet_market.log",      45),  # cron */15 — 3× margin
    WatchedMonitor("collector_health",  "~/collector_health.log",     15),  # cron */5  — 3× margin
    WatchedMonitor("phantom_reconcile", "~/phantom_reconcile.log",   120),  # cron 7 * — 2× margin
)


def check_log_freshness(monitor: WatchedMonitor,
                        now_unix: Optional[float] = None) -> Optional[str]:
    """Check one monitor's log freshness.

    Returns:
        None if the log exists and mtime is within ``max_stale_minutes`` of
        now. Otherwise a non-empty alert string describing the staleness or
        absence. Caller is responsible for sending the alert via Telegram.

    Path expansion: ``log_path`` accepts ``~`` (HOME) and ``$VAR`` (env)
    patterns. :func:`check_log_freshness` chains
    :func:`os.path.expandvars` (env vars first, so ``$HOME`` resolves)
    then :func:`os.path.expanduser` (so any remaining literal ``~`` is
    resolved). Relative paths resolve against the watchdog's cwd.

    Time source: caller-supplied ``now_unix`` (test seam); defaults to
    :func:`time.time` for production.
    """
    if now_unix is None:
        now_unix = time.time()
    expanded = os.path.expanduser(os.path.expandvars(monitor.log_path))
    path = Path(expanded)
    if not path.exists():
        return (
            f"[MONITOR WATCHDOG] `{monitor.name}` log MISSING at "
            f"`{expanded}` — cron may have never run, or log was wiped. "
            f"Investigate: `ssh botuser@vps; crontab -l | grep "
            f"{monitor.name}`."
        )
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        return (
            f"[MONITOR WATCHDOG] `{monitor.name}` log at `{expanded}` "
            f"stat() failed: {exc!r}. Investigate file permissions."
        )
    age_sec = now_unix - mtime
    age_min = age_sec / 60.0
    if age_min <= monitor.max_stale_minutes:
        return None
    return (
        f"[MONITOR WATCHDOG] `{monitor.name}` log STALE: last mtime "
        f"{age_min:.1f} min ago (threshold {monitor.max_stale_minutes} "
        f"min). Cron likely failed silently. "
        f"Check `tail -50 {expanded}` for the crash trace."
    )


def main(monitors: tuple[WatchedMonitor, ...] = WATCHED_MONITORS,
         notifier: Optional[object] = None) -> int:
    """Check all monitors, send alerts for stale ones, exit 0.

    ``notifier`` is a test seam; production reads
    ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID`` from the environment
    (sourced from ``~/.env`` by the cron shell) and constructs a
    :class:`bot.notifier.TelegramNotifier`. If either env var is
    missing, the notifier silently no-ops via its ``.enabled`` flag —
    the script still runs cleanly and prints the per-tick summary
    (useful for operators staging the install before adding Telegram
    creds). The import is lazy so test collection is free of the bot
    dependency tree; matches the ``collector_health_monitor.py`` pattern.

    The dedup key ``f"monitor_watchdog_{monitor.name}"`` participates in
    :class:`TelegramNotifier`'s in-process 60-second dedup window — but
    because each cron tick spawns a fresh interpreter, the in-process
    dedup resets on every tick. A chronically-dead monitor produces one
    alert per cron tick (6/hour at the 10-min cadence; bounded by
    ``len(monitors) × 6 = 24/hour`` in the all-dead scenario for the
    current 4-monitor watchlist). Acceptable per cron-tier convention;
    future Bit may switch to file-sidecar dedup like
    ``phantom_reconcile_monitor.py``.
    """
    if notifier is None:
        from bot.notifier import TelegramNotifier  # noqa: PLC0415 — lazy intentional
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    alerts_sent = 0
    monitors_ok = 0
    for monitor in monitors:
        alert = check_log_freshness(monitor)
        if alert is None:
            monitors_ok += 1
            continue
        notifier.send(
            alert,
            silent=False,
            dedup_key=f"monitor_watchdog_{monitor.name}",
        )
        alerts_sent += 1

    print(
        f"[monitor_watchdog] checked={len(monitors)} ok={monitors_ok} "
        f"alerts_sent={alerts_sent}",
        flush=True,
    )
    return 0  # cron convention — alerts via Telegram, not exit code


if __name__ == "__main__":
    sys.exit(main())
