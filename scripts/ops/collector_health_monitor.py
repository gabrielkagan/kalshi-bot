"""D1.6: collector health monitor — disk + WS-conn-loss + service-down alerts.

Ticket 86b9zk4we (2026-05-17). Standalone CLI run via cron on the VPS.
Polls 3 health surfaces and sends Telegram alerts via the existing
``bot.notifier.TelegramNotifier`` (no Telegram client re-implementation).

D0.3 §6 isolation contract enumerated 2 failure modes with NO alert
surface pre-D1.6:
  - Disk pressure (2 GB root volume can fill in minutes if S3 upload
    falls behind; silent OOM + bronze loss).
  - WS conn-loss (multiple disconnect classes; manual audit was the
    only surface).

D1.6 adds:
  - check_disk: alert if /var/lib/kalshi-collector/ partition >= threshold_pct
  - check_ws_reconnects: alert if kalshi_ws_disconnected count over
    last N minutes >= threshold_count (with 1006/1009/1011 class breakdown)
  - check_collector_active: alert if `systemctl is-active kalshi-collector`
    returns non-zero

Operator install (manual, post-D1.6 merge):
    # In /etc/cron.d/kalshi-collector-health or `crontab -e` (botuser):
    */5 * * * * cd /home/botuser/kalshi-bot-repo && source venv/bin/activate && python3 scripts/ops/collector_health_monitor.py >> /var/log/kalshi-collector-health.log 2>&1

Env reads:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID — from /home/botuser/.env (loaded
    by the shell that invokes this script).

Exit code: ALWAYS 0 (cron health-script convention; alerts go via
Telegram, not exit code, so a transient health-check failure doesn't
flood the operator's mail spool).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


# Defaults (overridable via env for tuning without redeploy).
DEFAULT_DISK_THRESHOLD_PCT = 80
DEFAULT_WS_WINDOW_MIN = 5
DEFAULT_WS_THRESHOLD_COUNT = 10
DEFAULT_BRONZE_ROOT = "/var/lib/kalshi-collector"
DEFAULT_COLLECTOR_UNIT = "kalshi-collector"


def check_disk(
    path: str = DEFAULT_BRONZE_ROOT,
    threshold_pct: int = DEFAULT_DISK_THRESHOLD_PCT,
) -> Optional[str]:
    """Return alert string if disk usage at ``path`` >= ``threshold_pct``, else None.

    Falls back to `/` if ``path`` doesn't exist (pre-bronze-day-zero
    deploy, test environment, etc.). Uses ``shutil.disk_usage`` rather
    than parsing ``df`` for portability + structured result.
    """
    target = Path(path) if Path(path).exists() else Path("/")
    usage = shutil.disk_usage(target)
    used_pct = int((usage.used / usage.total) * 100)
    if used_pct < threshold_pct:
        return None
    used_gb = usage.used / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
    return (
        f"*COLLECTOR DISK ALERT* — {target} at {used_pct}% "
        f"({used_gb:.2f}GB / {total_gb:.2f}GB used; threshold {threshold_pct}%). "
        f"Check S3 upload health: `journalctl -u kalshi-collector --since '10 min ago' | grep -i rclone`"
    )


def check_ws_reconnects(
    window_min: int = DEFAULT_WS_WINDOW_MIN,
    threshold_count: int = DEFAULT_WS_THRESHOLD_COUNT,
    unit: str = DEFAULT_COLLECTOR_UNIT,
) -> Optional[str]:
    """Return alert string if `kalshi_ws_disconnected` count in last
    ``window_min`` minutes >= ``threshold_count``, else None.

    Includes class breakdown (1006 abnormal / 1009 message-too-big /
    1011 ping timeout) so the operator can route to the correct fix:
    - 1006: kalshi-side outage or our network blip
    - 1009: ws_max_size config (D1.3-fu1 should have closed this)
    - 1011: asyncio loop blockage (D1.3-fu3 stopgap + D1.3-fu4 proper fix)
    """
    try:
        out = subprocess.check_output(
            [
                "journalctl",
                "-u", unit,
                "--since", f"{window_min} minutes ago",
                "-q", "--no-pager",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        # journalctl absent (test env) or errored — return None rather
        # than alert-spam.
        return None
    disconnect_lines = [
        line for line in out.splitlines()
        if "kalshi_ws_disconnected" in line
    ]
    if len(disconnect_lines) < threshold_count:
        return None
    # Match `sent 1006|1009|1011` exactly to avoid PID/TID double-count
    # (R3-Mn3 from D1.6 adv round 3): bare "1006" substring would also
    # match `pid=1006` or `tid=10060` co-occurring with `sent 1011`.
    n_1006 = sum(1 for line in disconnect_lines if "sent 1006" in line)
    n_1009 = sum(1 for line in disconnect_lines if "sent 1009" in line)
    n_1011 = sum(1 for line in disconnect_lines if "sent 1011" in line)
    return (
        f"*COLLECTOR WS RECONNECT STORM* — {len(disconnect_lines)} "
        f"disconnects in last {window_min}min (threshold {threshold_count}). "
        f"Breakdown: 1006={n_1006} 1009={n_1009} 1011={n_1011}. "
        f"Check: `journalctl -u {unit} --since '{window_min} min ago' | grep disconnect | tail`"
    )


def check_collector_active(unit: str = DEFAULT_COLLECTOR_UNIT) -> Optional[str]:
    """Return alert string if `systemctl is-active <unit>` reports inactive."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None  # systemctl absent / hung — return None not alert-spam
    if result.returncode == 0:
        return None
    return (
        f"*COLLECTOR DOWN* — `systemctl is-active {unit}` returned "
        f"exit {result.returncode}. Recovery: `sudo -n /bin/systemctl "
        f"restart {unit}` (NOPASSWD assumed; see "
        f"feedback_vps_sudoers_collector_gap_may17)."
    )


def main() -> int:
    """Entry point. Runs all 3 checks; sends Telegram alerts as needed.

    Returns 0 always (cron convention — exit code reserved for cron's
    own error handling, NOT for application health signaling; that goes
    via Telegram).
    """
    from bot.notifier import TelegramNotifier  # imported lazily so tests can mock

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    checks = [
        ("disk", check_disk),
        ("ws_reconnects", check_ws_reconnects),
        ("collector_active", check_collector_active),
    ]
    for check_name, check_fn in checks:
        alert = check_fn()
        if alert:
            print(f"[{check_name}] ALERT: {alert}", file=sys.stderr)
            notifier.send(alert, dedup_key=f"d1_6_{check_name}")
        else:
            print(f"[{check_name}] OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
