#!/usr/bin/env python3
"""Monitor 15M market activity and alert when markets go unusually quiet.

Designed to run as a cron job every 15 minutes on VPS:
    */15 * * * * cd /home/botuser/kalshi-bot-repo && /home/botuser/kalshi-bot-repo/venv/bin/python scripts/quiet_market_monitor.py --db state.db

Sends Telegram alerts when:
  - CRITICAL: Zero evals of any kind for 2+ hours during weekday market hours (14-23 UTC)
  - INFO: Zero candidates for 6+ hours during weekday

Uses file-based cooldown to avoid repeat alerts within 4 hours.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

COOLDOWN_FILE = "/tmp/quiet_market_cooldown.json"
COOLDOWN_HOURS = 4

# Telegram config
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_telegram(msg: str, dry_run: bool = False) -> bool:
    """Send Telegram message. Returns True on success."""
    if dry_run:
        print(f"[DRY RUN] Would send Telegram:\n{msg}")
        return True
    if not BOT_TOKEN or not CHAT_ID:
        print(f"[quiet_market_monitor] No Telegram config, skipping: {msg[:80]}")
        return False
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[quiet_market_monitor] Telegram send failed: {e}")
        return False


def load_cooldowns() -> dict:
    """Load cooldown state from file."""
    try:
        with open(COOLDOWN_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cooldowns(cooldowns: dict) -> None:
    """Save cooldown state to file."""
    with open(COOLDOWN_FILE, "w") as f:
        json.dump(cooldowns, f)


def is_on_cooldown(alert_key: str, cooldowns: dict, now: datetime) -> bool:
    """Check if an alert key is still in cooldown."""
    last_sent = cooldowns.get(alert_key)
    if not last_sent:
        return False
    try:
        last_dt = datetime.fromisoformat(last_sent)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        return (now - last_dt) < timedelta(hours=COOLDOWN_HOURS)
    except (ValueError, TypeError):
        return False


def mark_cooldown(alert_key: str, cooldowns: dict, now: datetime) -> None:
    """Mark an alert as sent, updating cooldown."""
    cooldowns[alert_key] = now.isoformat()
    # Prune old entries
    cutoff = now - timedelta(hours=COOLDOWN_HOURS * 2)
    to_remove = []
    for k, v in cooldowns.items():
        try:
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < cutoff:
                to_remove.append(k)
        except (ValueError, TypeError):
            to_remove.append(k)
    for k in to_remove:
        del cooldowns[k]
    save_cooldowns(cooldowns)


def format_time(iso_str: str | None) -> str:
    """Format ISO timestamp to 'HH:MM UTC' or 'N/A'."""
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%H:%M UTC")
    except (ValueError, TypeError):
        return "N/A"


def main():
    parser = argparse.ArgumentParser(description="Monitor 15M market quiet periods")
    parser.add_argument("--db", default="state.db", help="Path to state.db")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of sending Telegram")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[quiet_market_monitor] DB not found: {db_path}")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    is_weekday = now.weekday() < 5  # Mon=0 .. Sun=6
    hour_utc = now.hour
    in_market_hours = 14 <= hour_utc <= 23

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=10000")

    cutoff_4h = (now - timedelta(hours=4)).isoformat()
    cutoff_6h = (now - timedelta(hours=6)).isoformat()
    cutoff_2h = (now - timedelta(hours=2)).isoformat()

    # Count 15M candidates/observation_trades in last 4h
    row = conn.execute(
        """SELECT COUNT(*) FROM evaluated_opportunities
           WHERE product_type = '15m'
             AND filter_stage IN ('candidate', 'observation_trade')
             AND evaluation_time >= ?""",
        (cutoff_4h,),
    ).fetchone()
    candidates_4h = row[0] if row else 0

    # Count ALL 15M evals in last 4h (any filter_stage)
    row = conn.execute(
        """SELECT COUNT(*) FROM evaluated_opportunities
           WHERE product_type = '15m'
             AND evaluation_time >= ?""",
        (cutoff_4h,),
    ).fetchone()
    all_evals_4h = row[0] if row else 0

    # Count ALL 15M evals in last 2h
    row = conn.execute(
        """SELECT COUNT(*) FROM evaluated_opportunities
           WHERE product_type = '15m'
             AND evaluation_time >= ?""",
        (cutoff_2h,),
    ).fetchone()
    all_evals_2h = row[0] if row else 0

    # Count 15M candidates in last 6h
    row = conn.execute(
        """SELECT COUNT(*) FROM evaluated_opportunities
           WHERE product_type = '15m'
             AND filter_stage IN ('candidate', 'observation_trade')
             AND evaluation_time >= ?""",
        (cutoff_6h,),
    ).fetchone()
    candidates_6h = row[0] if row else 0

    # Count settled trades in last 4h
    row = conn.execute(
        """SELECT COUNT(*) FROM settled_trades
           WHERE settled_at >= ?""",
        (cutoff_4h,),
    ).fetchone()
    trades_4h = row[0] if row else 0

    # Last eval info
    last_eval = conn.execute(
        """SELECT evaluation_time, ticker FROM evaluated_opportunities
           WHERE product_type = '15m'
           ORDER BY evaluation_time DESC LIMIT 1""",
    ).fetchone()
    last_eval_time = last_eval[0] if last_eval else None
    last_eval_ticker = last_eval[1] if last_eval else None

    # Last trade info
    last_trade = conn.execute(
        """SELECT settled_at FROM settled_trades
           ORDER BY settled_at DESC LIMIT 1""",
    ).fetchone()
    last_trade_time = last_trade[0] if last_trade else None

    conn.close()

    cooldowns = load_cooldowns()
    alerts_sent = 0

    # CRITICAL: Zero evals for 2+ hours during weekday market hours
    if all_evals_2h == 0 and is_weekday and in_market_hours:
        alert_key = "critical_no_evals"
        if not is_on_cooldown(alert_key, cooldowns, now):
            msg = (
                "\U0001F534 *15M NO ACTIVITY*\n"
                f"0 evals of any kind in 2h\n"
                f"Bot may be down \u2014 check systemctl\n"
                f"Last eval: {format_time(last_eval_time)}"
            )
            if last_eval_ticker:
                msg = msg.rstrip() + f" ({last_eval_ticker})"
            if send_telegram(msg, dry_run=args.dry_run):
                mark_cooldown(alert_key, cooldowns, now)
                alerts_sent += 1

    # INFO: Zero candidates for 6+ hours during weekday
    if candidates_6h == 0 and is_weekday:
        alert_key = "info_no_candidates"
        if not is_on_cooldown(alert_key, cooldowns, now):
            if all_evals_4h > 0:
                status = "Markets exist, no edge found"
            else:
                status = "No market activity at all"
            msg = (
                "\U0001F4CA *15M MARKET QUIET*\n"
                f"0 candidates in last 6h\n"
                f"Last eval: {format_time(last_eval_time)}"
            )
            if last_eval_ticker:
                msg = msg.rstrip() + f" ({last_eval_ticker})"
            msg += (
                f"\nLast trade: {format_time(last_trade_time)}\n"
                f"Status: {status}"
            )
            if send_telegram(msg, dry_run=args.dry_run):
                mark_cooldown(alert_key, cooldowns, now)
                alerts_sent += 1

    # Summary for logging
    print(
        f"[quiet_market_monitor] {now.strftime('%Y-%m-%d %H:%M UTC')} | "
        f"evals_2h={all_evals_2h} evals_4h={all_evals_4h} "
        f"candidates_4h={candidates_4h} candidates_6h={candidates_6h} "
        f"trades_4h={trades_4h} | "
        f"weekday={is_weekday} market_hours={in_market_hours} | "
        f"alerts_sent={alerts_sent}"
    )


if __name__ == "__main__":
    main()
