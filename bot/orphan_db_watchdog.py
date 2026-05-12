"""Orphan-DB Layer-3 watchdog — Bit 9.3-ii extraction (2026-05-10).

Layer 3 of orphan prevention; see kb/failures/shape-d-contention-explosion-may03.md.

May 3 2026 incident: a `cryptocompare_news_backfill.py` subprocess orphaned
itself and held state.db's writer lock for 2h42m, eventually wedging the live
bot through a 6-minute crash-loop on restart. Layers 1+2 (wrapper signal-
handling + script SIGALRM hard timeout) close the orphan-creation paths from
the H-4 cron infra. Layer 3 is detection at bot startup: enumerate non-bot
PIDs touching state.db and alert the operator. Detection-only by design —
auto-killing is too risky (could kill legitimate manually-launched migrations
or debug sessions); the operator triages from the Telegram alert.

Pre-Bit-9.3-ii this block lived inline at bot/_impl.py:375-578. Bit 9.3-ii
relocated it to this clean-leaf module:
  - Stdlib + bot.notifier alias only (no carve-out needed)
  - Becomes the 5th `_telegram_state._TELEGRAM` consumer (REPLACING
    bot/_impl.py in the count — net stays at 5)
  - bot/main_loop.py::MainLoop.startup() late-binding retargeted from
    `from bot._impl import detect_orphan_db_holders` to
    `from bot.orphan_db_watchdog import detect_orphan_db_holders`

5-consumer `_telegram_state._TELEGRAM` enumeration post-Bit-9.3-ii:
  - bot/orphan_db_watchdog.py (this module) — `_alert_orphan_db_holder`
    + lsof-not-found branch in `detect_orphan_db_holders`
  - bot/main_loop.py — MainLoop reads + WRITE in __init__
  - bot/scanner/__init__.py
  - bot/executor.py
  - bot/settlement.py

Verify with:
  grep -c '_telegram_state\\._TELEGRAM' bot/orphan_db_watchdog.py bot/main_loop.py \\
      bot/scanner/__init__.py bot/executor.py bot/settlement.py

Why `import bot.notifier as _telegram_state` instead of
`from bot.notifier import _TELEGRAM`:
  1. The plain `from`-import captures the binding by value at import time;
     subsequent `bot.notifier._TELEGRAM = X` writes (TelegramNotifier
     `_init_singleton()` does this at MainLoop boot) would not propagate.
     Module-attribute access (`_telegram_state._TELEGRAM`) re-resolves
     every read through `sys.modules['bot.notifier'].__dict__`.
  2. The `from bot import notifier as ...` form (variant) triggers
     `_BotProxy.__getattr__('notifier')` → `_get_impl()` → partial-module
     ImportError chain. Per L84 (Bit 8.1).

Postmortem: kb/failures/shape-d-contention-explosion-may03.md.
"""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Dict, List, Optional

import bot.notifier as _telegram_state


def _run_lsof_for_db(db_path: str) -> List[int]:
    """Return PIDs that have `db_path` open. Implementation: shells
    out to `lsof -t <db_path>`. Separated into its own function so
    tests can stub it out without mocking subprocess globally."""
    out = subprocess.check_output(
        ["lsof", "-t", db_path], timeout=10, text=True,
    )
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _get_pid_cmdline(pid: int) -> str:
    """Best-effort fetch of the command line for `pid`. Reads
    `/proc/<pid>/cmdline` on Linux, falls back to `ps -p <pid> -o
    command=` on other platforms. Returns empty string on failure
    (the operator still has the PID even without cmdline)."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            timeout=5, text=True,
        )
        return out.strip()
    except Exception:
        return ""


def _alert_orphan_db_holder(*, pid: int, cmdline: str) -> None:
    """Send a Telegram alert about an orphan PID holding state.db.
    Best-effort — failure to send must not block bot startup."""
    try:
        if _telegram_state._TELEGRAM is None:
            return
        msg = (
            f"⚠️ ORPHAN DB HOLDER detected at startup\n"
            f"pid={pid}\n"
            f"cmd: {cmdline[:300] or '(unknown)'}\n"
            f"This process is holding state.db's lock from outside the "
            f"bot. Investigate via `ssh kalshi-vps; ps -p {pid}` and kill "
            f"if stale. May 3 2026 incident: an H-4c backfill orphan "
            f"wedged the bot for 6 min via this exact pattern."
        )
        _telegram_state._TELEGRAM.send(msg, dedup_key=f"orphan_db_pid_{pid}")
    except Exception:
        logging.debug("orphan-DB Telegram alert failed", exc_info=True)


# Positive-list of cmdline substrings that indicate an orphan we care
# about. We deliberately do NOT alert on legitimate cron-spawned
# cohabitants (watchdog.py, auditor.py, audit_cron.py,
# bot/snapshots/dashboard_snapshot.py — see adversarial review C-1) because their
# overlap with bot startup is routine and would habituate the operator
# to ignore alerts. The May 3 2026 incident was an H-4 backfill orphan,
# and that's the specific class we're guarding against.
_ORPHAN_DB_WATCHDOG_PATTERNS: List[str] = [
    "gdelt_backfill",
    "cryptocompare_news_backfill",
    "glassnode_backfill",
]


def detect_orphan_db_holders(
    db_path: str, self_pid: Optional[int] = None,
) -> List[Dict[str, object]]:
    """Layer 3 orphan-prevention watchdog.

    Enumerates PIDs holding `db_path` open via `lsof -t`. Filters out
    `self_pid` (defaults to `os.getpid()`). For each remaining PID,
    captures cmdline. Alerts ONLY on PIDs whose cmdline matches a
    known H-4 backfill script (positive-list — see
    `_ORPHAN_DB_WATCHDOG_PATTERNS`). Returns the list of offender
    records `{"pid": int, "cmdline": str}` (the full filtered list,
    pre-alert) for caller-side logging / tests.

    Why positive-list and not "anything not bot/_impl.py": the live VPS has
    several legitimate cron-spawned `state.db` openers (watchdog.py
    every 2 min, auditor.py hourly, audit_cron.py every 30 min,
    operator-run bot/snapshots/dashboard_snapshot.py). Any of them can collide
    with the watchdog's lsof probe at bot startup. A negative-list
    design would generate alerts on every overlap → alert fatigue →
    the operator stops looking at the channel. Positive-list keeps
    signal high.

    Detection-only — does NOT call `os.kill`. The operator triages
    from the alert.

    Also probes `db_path` + `db_path-wal` so SQLite writers that have
    only the WAL FD open (rare, possible during shutdown races) are
    caught. Adversarial-review C-7."""
    if self_pid is None:
        self_pid = os.getpid()
    try:
        pids = _run_lsof_for_db(db_path)
    except FileNotFoundError as e:
        # Adversarial-review C-5: lsof binary not installed → watchdog
        # is silently no-op for the entire deploy lifetime. Log loud
        # AND emit a one-shot Telegram so the operator knows the
        # safety net is off.
        logging.warning(
            "orphan_db_watchdog: lsof not found (%s); watchdog DISABLED. "
            "Install lsof to re-enable.", e,
        )
        try:
            if _telegram_state._TELEGRAM is not None:
                _telegram_state._TELEGRAM.send(
                    "⚠️ orphan-DB watchdog DISABLED: lsof not installed "
                    "on VPS. May 3 2026 orphan-class incidents are "
                    "undetected until lsof is available.",
                    dedup_key="orphan_watchdog_disabled",
                )
        except Exception:
            pass
        return []
    except Exception as e:
        # Includes CalledProcessError (lsof exit 1 = "no holders found",
        # which on macOS is exit 0 + empty stdout, and on Linux is
        # exit 1) — both indicate "no PIDs," not a failure mode.
        logging.warning(
            "orphan_db_watchdog: lsof probe failed (%s); skipping check",
            e,
        )
        return []
    # Adversarial-review C-7: also probe the WAL sibling so a writer
    # holding only the WAL FD is caught. Union with main probe.
    try:
        wal_pids = _run_lsof_for_db(db_path + "-wal")
        for wp in wal_pids:
            if wp not in pids:
                pids.append(wp)
    except Exception:
        # WAL probe failure is non-fatal; we still have main-DB pids.
        pass
    # Adversarial-review C-4: dedup PIDs (lsof currently dedups for
    # single-file probes but the WAL union above can re-introduce dupes).
    pids = list(dict.fromkeys(pids))
    offenders: List[Dict[str, object]] = []
    for pid in pids:
        if pid == self_pid:
            continue
        cmdline = _get_pid_cmdline(pid)
        # Adversarial-review C-9: the PID may have exited between the
        # lsof snapshot and now — `os.kill(pid, 0)` is a stat-cheap
        # liveness probe; if it raises ProcessLookupError, the orphan
        # is already gone and no alert is needed.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError:
            # EPERM (different uid) — process is alive but we can't
            # signal it. Continue with the alert flow.
            pass
        offenders.append({"pid": pid, "cmdline": cmdline})
        # Adversarial-review C-1: only alert on PIDs that match the
        # known orphan-creator patterns. Otherwise log debug-only and
        # move on — legitimate cron processes (watchdog.py, auditor.py,
        # audit_cron.py, bot/snapshots/dashboard_snapshot.py) routinely collide with
        # bot startup.
        is_orphan_class = any(
            pat in cmdline for pat in _ORPHAN_DB_WATCHDOG_PATTERNS
        )
        if is_orphan_class:
            logging.error(
                "ORPHAN_DB_HOLDER: pid=%d cmd=%s holds state.db at "
                "startup — investigate (May 3 2026 incident pattern)",
                pid, cmdline[:300] or "(unknown)",
            )
            _alert_orphan_db_holder(pid=pid, cmdline=cmdline)
        else:
            logging.debug(
                "orphan_db_watchdog: pid=%d cmd=%s holds state.db but "
                "is NOT in the orphan-creator allow-list; skipping alert",
                pid, cmdline[:200],
            )
    return offenders
