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
of 5 monitors + 1 disk check + 1 rotation-errors check (ticket 86bbvd50a),
max alert burst is 7 alerts/tick × 6 ticks/hour = 42/hr in the all-dead
scenario. Acceptable per cron health-script convention;
operator can throttle by raising the cron interval if false-positives
become noisy. A future Bit may switch to file-sidecar dedup like
`scripts/ops/phantom_reconcile_monitor.py` to suppress cross-tick
duplicates.

Operator install (manual, post-merge):

    # In `crontab -e` (botuser):
    */10 * * * * cd ~/kalshi-bot-repo && . venv/bin/activate && set -a && . ~/.env && set +a && python3 scripts/ops/monitor_watchdog.py >> ~/monitor_watchdog.log 2>&1
    # (`.` not `source`, and SHELL=/bin/bash on the crontab's FIRST line —
    #  see ops/CLAUDE.md "Crontab SHELL ordering"; the live line still says
    #  `source` and works only because it sits below the SHELL= directive.)

Env reads (in main()):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID — from /home/botuser/.env (loaded
    by the shell that invokes this script). Both are passed positionally
    to the TelegramNotifier constructor. If either is missing the
    notifier silently no-ops (its .enabled property is False); the
    script still runs cleanly and prints the per-tick summary.

Exit code: ALWAYS 0 (cron health-script convention; alerts go via Telegram,
not exit code, so a transient health-check failure doesn't flood the
operator's mail spool).

Disk-usage threshold (ticket 86bbvd50a, 2026-09-05): the VPS root
filesystem sat at ~95% used from before 2026-08-10 and hit 100% on
2026-09-04 (kb/failures/vps-disk-full-journal-rotation-collision-sep05.md).
The 80% canary in `collector_health_monitor.py` never fired because its
cron line sits ABOVE `SHELL=/bin/bash` in the crontab and therefore runs
under /bin/sh (dash), where `source` is not a builtin — that job (and
data_health + quiet_market) has been dead since 2026-05-19 21:2x UTC, and
this watchdog has been reporting `alerts_sent=3` every 10 minutes since
(~47K Telegram alerts, unactioned). This script's own cron line is BELOW
the SHELL= directive and alive, so it carries an independent
`WATCHED_DISKS` check (`/` at 85%). Alert text is printed to stdout too,
so the cron log keeps a history instead of Telegram-only.

Self-referential blind spot: this script's OWN log freshness is the residual
gap. Mitigations:
  1. Higher cadence (10 min) than any watched monitor (15-500 min) — operator
     notices absence of expected alerts faster.
  2. Future E-followup: external probe or self-referential check.
"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
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
    # Ticket 86bbvd50a (2026-09-05): ops/rotate_journals.sh, cron 0 */4 with
    # `>> journal_archives/rotation.log 2>&1` (verified on the live crontab).
    # 500 min ≈ 2× the 240-min cadence. 1,106 `zstd: already exists` lines
    # sat unread in this file for 109 days — freshness + the errors= marker
    # below are what make rotation failures non-silent.
    WatchedMonitor("journal_rotation",  "~/kalshi-bot-repo/journal_archives/rotation.log", 500),
)

# Ticket 86bbvd50a: the rotation script ends every run with
# `Done. Disk free: <x> errors=<N>`. N>0 means a chunk was refused, could
# not be moved, or could not be compressed — cron ignores the exit code
# under the log redirect, so the watchdog reads the marker instead.
ROTATION_LOG_PATH = "~/kalshi-bot-repo/journal_archives/rotation.log"
_ROTATION_DONE_RE = re.compile(r"^Done\..*\berrors=(\d+)")


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


@dataclasses.dataclass(frozen=True)
class WatchedDisk:
    """One filesystem to watch for usage pressure (ticket 86bbvd50a).

    Attributes:
        name: short identifier, used in the dedup key + alert text.
        path: any path on the filesystem (``shutil.disk_usage`` resolves
            the mount). The VPS is a single 48 GB root filesystem.
        max_used_pct: alert when used% >= this. 85% for ``/``: the
            collector's ~16 GB local bronze buffer makes ~71% the healthy
            steady state, so 85% (~7 GB free) is the first level that is
            both above steady state and still actionable before writers
            start failing.
    """

    name: str
    path: str
    max_used_pct: int


WATCHED_DISKS: tuple[WatchedDisk, ...] = (
    WatchedDisk("root", "/", 85),
)


def check_disk_usage(disk: WatchedDisk,
                     disk_usage_fn=shutil.disk_usage) -> Optional[str]:
    """Check one filesystem's usage.

    Returns None below ``max_used_pct``; otherwise a non-empty alert
    string. A failed stat ALSO alerts (fail-loud) — a mount we cannot
    measure is not evidence of health. ``disk_usage_fn`` is a test seam.
    """
    try:
        usage = disk_usage_fn(disk.path)
    except OSError as exc:
        return (
            f"[MONITOR WATCHDOG] DISK check for `{disk.path}` failed: "
            f"{exc!r}. Investigate the mount."
        )
    total = float(getattr(usage, "total", 0) or 0)
    used = float(getattr(usage, "used", 0) or 0)
    free = float(getattr(usage, "free", 0) or 0)
    # df's Use% = used / (used + avail) — excludes the ext4 reserved blocks
    # (5% on the droplet), so 85% here == the 85% the operator sees in
    # `df -h`. used/total would be ~4 points laxer (R1-M4).
    denom = used + free
    used_pct = round((used / denom * 100.0), 1) if denom else 0.0
    if used_pct < disk.max_used_pct:
        return None
    free_gb = free / (1024 ** 3)
    total_gb = total / (1024 ** 3)
    return (
        f"[MONITOR WATCHDOG] DISK `{disk.path}` at {used_pct:.0f}% used "
        f"({free_gb:.1f} GB free of {total_gb:.1f} GB; threshold "
        f"{disk.max_used_pct}%). Every writer on the VPS fails silently at "
        f"100% (2026-09-04 incident). Check: `du -sh "
        f"~/kalshi-bot-repo/journal_archives /var/lib/kalshi-*collector*/bronze` "
        f"+ `ls -la ~/kalshi-bot-repo/journal_archives/*.jsonl`."
    )


def check_rotation_errors(log_path: str = ROTATION_LOG_PATH,
                          tail_bytes: int = 65536) -> Optional[str]:
    """Alert when the LAST completed rotation run reported ``errors=N>0``.

    Reads the tail of ``rotation.log`` and finds the most recent
    ``Done. ... errors=N`` line (the script's per-run footer). None when
    the log is missing (freshness is a separate WatchedMonitor), has no
    footer yet (legacy date-only script still installed — the crontab
    edit is an operator action), or N == 0.
    """
    expanded = os.path.expanduser(os.path.expandvars(log_path))
    try:
        with open(expanded, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    last = None
    for line in tail.splitlines():
        m = _ROTATION_DONE_RE.match(line.strip())
        if m:
            last = int(m.group(1))
    if not last:
        return None
    return (
        f"[MONITOR WATCHDOG] JOURNAL ROTATION reported errors={last} on its "
        f"last run — a journal chunk was refused (name collision), could not "
        f"be moved, or could not be compressed. Raw archives never reach S3. "
        f"Check `grep -n ERROR {expanded} | tail`."
    )


def main(monitors: tuple[WatchedMonitor, ...] = WATCHED_MONITORS,
         notifier: Optional[object] = None,
         disks: tuple[WatchedDisk, ...] = WATCHED_DISKS,
         rotation_log: Optional[str] = ROTATION_LOG_PATH) -> int:
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
    ``(len(monitors) + len(disks) + 1) × 6 = 42/hour`` in the all-dead
    scenario for the current 5-monitor watchlist + 1 disk + the
    rotation-errors check). Acceptable per cron-tier convention;
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
        print(alert, flush=True)  # keep a history in the cron log, not just Telegram
        notifier.send(
            alert,
            silent=False,
            dedup_key=f"monitor_watchdog_{monitor.name}",
        )
        alerts_sent += 1

    disks_ok = 0
    for disk in disks:
        alert = check_disk_usage(disk)
        if alert is None:
            disks_ok += 1
            continue
        print(alert, flush=True)
        notifier.send(
            alert,
            silent=False,
            dedup_key=f"monitor_watchdog_disk_{disk.name}",
        )
        alerts_sent += 1

    if rotation_log:
        alert = check_rotation_errors(rotation_log)
        if alert is not None:
            print(alert, flush=True)
            notifier.send(
                alert,
                silent=False,
                dedup_key="monitor_watchdog_journal_rotation_errors",
            )
            alerts_sent += 1

    print(
        f"[monitor_watchdog] checked={len(monitors)} ok={monitors_ok} "
        f"disks_checked={len(disks)} disks_ok={disks_ok} "
        f"alerts_sent={alerts_sent}",
        flush=True,
    )
    return 0  # cron convention — alerts via Telegram, not exit code


if __name__ == "__main__":
    sys.exit(main())
