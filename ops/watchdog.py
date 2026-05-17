#!/usr/bin/env python3
"""
Lightweight bot watchdog — runs via cron every 2 minutes.
Catches issues the bot itself can't report (crashes, stalls, OOM).
Sends alerts via Telegram.

Usage: */2 * * * * cd ~/kalshi-bot-repo && source venv/bin/activate && set -a && source ~/.env && set +a && python3 ops/watchdog.py
"""

import os
import sys
import subprocess
import sqlite3
import time
import json
import requests
from pathlib import Path
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = Path(__file__).parent.parent / ".watchdog_state.json"
DB_PATH = Path(__file__).parent.parent / "state.db"

# Thresholds
MAX_LOG_AGE_SECONDS = 300       # alert if no log output for 5 min
LOSS_STREAK_ALERT = 3           # alert on N consecutive losses
BALANCE_ALERT_THRESHOLD = 30.0  # alert if balance drops below $X


def send_telegram(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[watchdog] No Telegram config: {msg}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        print(f"[watchdog] Telegram send failed: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


def check_service_running() -> tuple:
    """Returns (is_running, details)."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "kalshi-bot"],
            capture_output=True, text=True, timeout=5,
        )
        active = result.stdout.strip() == "active"
        if not active:
            return False, f"Service status: {result.stdout.strip()}"
        return True, "running"
    except Exception as e:
        return False, f"systemctl check failed: {e}"


def check_recent_logs() -> tuple:
    """Returns (has_recent_output, seconds_since_last)."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "kalshi-bot", "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5,
        )
        # Check if PID is alive and producing output
        result = subprocess.run(
            ["journalctl", "-u", "kalshi-bot", "--no-pager", "-n", "1",
             "--output=short-unix"],
            capture_output=True, text=True, timeout=5,
        )
        if result.stdout.strip():
            # Parse the unix timestamp from journalctl output
            line = result.stdout.strip().split("\n")[-1]
            try:
                ts = float(line.split()[0])
                age = time.time() - ts
                return age < MAX_LOG_AGE_SECONDS, age
            except (ValueError, IndexError):
                pass
        return False, -1
    except Exception:
        return True, 0  # don't alert on check failure


def check_losses() -> tuple:
    """Returns (recent_loss_streak, last_loss_ticker, last_loss_pnl)."""
    if not DB_PATH.exists():
        return 0, None, None
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        rows = conn.execute(
            "SELECT ticker, pnl_cents FROM settled_trades ORDER BY rowid DESC LIMIT 10"
        ).fetchall()
        conn.close()
        streak = 0
        last_ticker = None
        last_pnl = None
        for r in rows:
            if r[1] < 0:
                streak += 1
                if last_ticker is None:
                    last_ticker = r[0]
                    last_pnl = r[1] / 100
            else:
                break
        return streak, last_ticker, last_pnl
    except Exception:
        return 0, None, None


def check_balance() -> float:
    """Get approximate balance from Kalshi API or recent trade data."""
    if not DB_PATH.exists():
        return 999.0  # don't alert
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        total_pnl = conn.execute(
            "SELECT SUM(pnl_cents - COALESCE(fee_cents, 0)) FROM settled_trades"
        ).fetchone()[0] or 0
        conn.close()
        # This is net PnL, not absolute balance — but we can detect big drops
        return total_pnl / 100
    except Exception:
        return 999.0


# ── Layer 3.5: orphan-DB detection (mid-session) ──────────────────────
#
# Layer 3 (`bot.py:detect_orphan_db_holders`) catches orphan H-4
# backfill processes at bot startup. If an orphan spawns DURING bot
# uptime (operator-triggered manual H-4 run mid-day), Layer 3 misses
# it until the next bot restart. This Layer 3.5 hooks into the
# 2-min watchdog cron so mid-session orphans are detected within
# ≤2 min. Adversarial-review C-8 from the Layer 3 review.
#
# Same positive-list as Layer 3: only alert on cmdlines matching
# known orphan-creator scripts (cryptocompare_news_backfill,
# gdelt_backfill, glassnode_backfill). Legitimate cron processes
# (auditor, audit_cron, dashboard_snapshot) and the watchdog itself
# don't trigger alerts.

_ORPHAN_PATTERNS = (
    "gdelt_backfill",
    "cryptocompare_news_backfill",
    "glassnode_backfill",
)


def _run_lsof_for_db(db_path: str):
    """Return list of PIDs holding `db_path` open. Mirrors
    `bot.py:_run_lsof_for_db`. Separate function so tests can stub
    it out without intercepting subprocess globally."""
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
    """Best-effort cmdline lookup. /proc on Linux, ps fallback."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except (FileNotFoundError, PermissionError, OSError):
        pass
    try:
        return subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            timeout=5, text=True,
        ).strip()
    except Exception:
        return ""


def check_orphan_db_holders(db_path: str):
    """Return Telegram alert text if a known-orphan-class PID holds
    `db_path` open, else None.

    Detection-only — does NOT auto-kill (matches Layer 3 design).

    Robustness: any failure (lsof missing, parse error) returns None
    so the watchdog itself doesn't crash on environmental issues.
    Layer 3 in bot.py separately alerts once if lsof is missing, so
    duplication here would be alert noise."""
    try:
        pids = _run_lsof_for_db(db_path)
    except Exception:
        return None
    self_pid = os.getpid()
    for pid in pids:
        if pid == self_pid:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except OSError:
            pass
        cmdline = _get_pid_cmdline(pid)
        if any(pat in cmdline for pat in _ORPHAN_PATTERNS):
            return (
                f"⚠️ *ORPHAN DB HOLDER (mid-session)*\n"
                f"pid=`{pid}`\n"
                f"cmd: `{cmdline[:200] or '(unknown)'}`\n"
                f"Holds state.db lock from outside the bot. "
                f"`ssh kalshi-vps; ps -p {pid}` and kill if stale "
                f"(May 3 2026 incident pattern)."
            )
    return None


def check_memory() -> tuple:
    """Returns (memory_mb, is_concerning)."""
    try:
        result = subprocess.run(
            ["systemctl", "show", "kalshi-bot", "--property=MemoryCurrent"],
            capture_output=True, text=True, timeout=5,
        )
        mem_str = result.stdout.strip().split("=")[-1]
        if mem_str and mem_str != "[not set]":
            mem_mb = int(mem_str) / (1024 * 1024)
            return mem_mb, mem_mb > 800  # alert above 800MB
        return 0, False
    except Exception:
        return 0, False


def main():
    state = load_state()
    alerts = []
    now = time.time()

    # 1. Service running check
    is_running, details = check_service_running()
    if not is_running:
        last_down_alert = state.get("last_down_alert", 0)
        if now - last_down_alert > 300:  # re-alert every 5 min
            alerts.append(f"\U0001f534 *Bot is DOWN*\n{details}")
            state["last_down_alert"] = now

    # 2. Log staleness check (only if service is "running")
    if is_running:
        has_recent, age = check_recent_logs()
        if not has_recent and age > 0:
            last_stall_alert = state.get("last_stall_alert", 0)
            if now - last_stall_alert > 600:  # re-alert every 10 min
                alerts.append(
                    f"\u26a0\ufe0f *Bot stalled* — no log output for {int(age)}s"
                )
                state["last_stall_alert"] = now

    # 3. Loss streak check
    streak, last_ticker, last_pnl = check_losses()
    prev_streak = state.get("loss_streak", 0)
    if streak >= LOSS_STREAK_ALERT and streak > prev_streak:
        alerts.append(
            f"\U0001f4c9 *{streak} consecutive losses*\n"
            f"Latest: `{last_ticker}` (${last_pnl:+.2f})"
        )
    state["loss_streak"] = streak

    # 4. Orphan-DB check (Layer 3.5 — mid-session orphan detection)
    orphan_alert = check_orphan_db_holders(str(DB_PATH))
    if orphan_alert is not None:
        # Dedup: only re-alert every 30 min for the same orphan PID
        # (the alert text contains the pid). Pattern matches the
        # other deduped alerts in this file.
        last_orphan_alert = state.get("last_orphan_alert", 0)
        if now - last_orphan_alert > 1800:
            alerts.append(orphan_alert)
            state["last_orphan_alert"] = now

    # 5. Memory check
    mem_mb, mem_high = check_memory()
    if mem_high:
        last_mem_alert = state.get("last_mem_alert", 0)
        if now - last_mem_alert > 3600:  # re-alert every hour
            alerts.append(f"\U0001f4be *High memory*: {mem_mb:.0f}MB")
            state["last_mem_alert"] = now

    # Send alerts
    if alerts:
        header = f"\U0001f6a8 *Watchdog Alert* — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
        send_telegram(header + "\n".join(alerts))

    save_state(state)


if __name__ == "__main__":
    main()
