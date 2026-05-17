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

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


# Defaults (overridable via env for tuning without redeploy).
DEFAULT_DISK_THRESHOLD_PCT = 80
DEFAULT_WS_WINDOW_MIN = 5
DEFAULT_WS_THRESHOLD_COUNT = 10
DEFAULT_BRONZE_ROOT = "/var/lib/kalshi-collector"
DEFAULT_COLLECTOR_UNIT = "kalshi-collector"

# D1.6 fu defaults.
DEFAULT_SIDECAR_PATH = "/var/lib/kalshi-collector/bronze_health.json"
DEFAULT_MONITOR_STATE_PATH = "/var/lib/kalshi-collector/monitor_state.json"
DEFAULT_DROPPED_FRAMES_THRESHOLD = 100
# Stale-sidecar threshold: 2x the drain-thread poll cadence (1s) +
# 2x the cron tick interval (5min = 300s) = ~610s. Use 120s as a tight
# floor so we catch a wedged drain thread within 2 monitor ticks, not 2
# cron intervals. The 60s rotation cadence (D0.3 §4) does NOT bound
# sidecar freshness — sidecar is written every drain-poll, not every
# rotation.
DEFAULT_SIDECAR_STALE_SECONDS = 120


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


def check_dropped_frames(
    sidecar_path: Path = Path(DEFAULT_SIDECAR_PATH),
    state_path: Path = Path(DEFAULT_MONITOR_STATE_PATH),
    threshold: int = DEFAULT_DROPPED_FRAMES_THRESHOLD,
    stale_after_seconds: int = DEFAULT_SIDECAR_STALE_SECONDS,
) -> Optional[str]:
    """Return alert string if BronzeArchiver drops since last tick >=
    threshold, else None. D1.6 fu observability for D1.3-fu4 worker
    queue saturation.

    Two distinct alert classes:
      1. DROPS: aggregated ``total_dropped_frames`` from the sidecar
         increased by >= threshold since the last tick. Indicates
         queue.Full firing — load > throughput.
      2. STALE: sidecar exists but mtime is older than
         ``stale_after_seconds``. Indicates the drain thread has stopped
         writing (collector process gone or wedged). The 3 existing
         checks (disk, ws_reconnects, collector_active) would also
         eventually fire for a dead collector, but STALE catches it
         within ~2 monitor ticks vs. waiting for ``systemctl is-active``
         to flip.

    Fail-quiet posture mirrors the other 3 checks: missing sidecar,
    malformed JSON, missing state file are all logged-and-skipped — the
    cron framework expects exit 0 always and alerts via Telegram.

    State persistence:
      - On FIRST run (state file absent): baseline against current
        sidecar's total + save state. Return None. Otherwise the first
        tick after install would alert on the cumulative-since-process-
        start total — false alarm.
      - On RESET (state's last_total > sidecar's current total): the
        collector restarted (BronzeArchiver.start() resets
        ``_dropped_frames=0`` per D1.3-fu4 R1-M2). Re-baseline against
        the new floor. Return None.

    Args:
        sidecar_path: bronze_health.json written by
            collector.main_loop.write_bronze_health_sidecar.
        state_path: monitor-owned state file persisting
            ``last_total_dropped_frames`` across ticks.
        threshold: alert if delta >= threshold (default 100).
        stale_after_seconds: alert if sidecar mtime older than this
            (default 120s ≈ 2 monitor ticks).

    Returns:
        Alert string (Markdown for Telegram) or None.
    """
    if not sidecar_path.is_file():
        return None

    # STALE check (before reading content — a stale file's content may
    # also be uninformative).
    try:
        mtime = sidecar_path.stat().st_mtime
    except OSError:
        return None
    age = time.time() - mtime
    if age > stale_after_seconds:
        return (
            f"*COLLECTOR BRONZE_HEALTH STALE* — sidecar {sidecar_path} "
            f"not written in {int(age)}s (threshold {stale_after_seconds}s). "
            f"Collector drain thread may be wedged or process dead. "
            f"Check: `systemctl status kalshi-collector` + "
            f"`journalctl -u kalshi-collector --since '5 min ago' | tail`."
        )

    try:
        sidecar_data = json.loads(sidecar_path.read_text())
    except (OSError, ValueError):
        # Malformed (partial write?) — fail quiet; next tick will
        # likely catch the steady-state file.
        return None
    current_total = int(sidecar_data.get("total_dropped_frames", 0))

    # Load + interpret state.
    if not state_path.is_file():
        # First run: baseline, no alert.
        _save_state(state_path, last_total_dropped_frames=current_total)
        return None
    try:
        state_data = json.loads(state_path.read_text())
        last_total = int(state_data.get("last_total_dropped_frames", 0))
    except (OSError, ValueError):
        # Corrupt state file — re-baseline rather than alert-spam.
        _save_state(state_path, last_total_dropped_frames=current_total)
        return None

    # Reset detection (counter went down → collector restarted).
    if current_total < last_total:
        _save_state(state_path, last_total_dropped_frames=current_total)
        return None

    delta = current_total - last_total
    # Always update state so the next tick measures from the new floor
    # (we don't want a single sustained-overload to alert on every tick
    # for the same backlog; one alert per delta-window).
    _save_state(state_path, last_total_dropped_frames=current_total)

    if delta < threshold:
        return None
    return (
        f"*COLLECTOR BRONZE_DROPPED_FRAMES* — {delta} new drops since "
        f"last tick (threshold {threshold}). Total since collector boot: "
        f"{current_total}. Worker queue saturated — D1.3-fu4 bounded "
        f"queue dropped frames on `queue.Full`. Check: per-archiver "
        f"breakdown in {sidecar_path}; collector load (subscribe burst? "
        f"backlog drain?) in `journalctl -u kalshi-collector --since "
        f"'10 min ago' | grep -i 'write_queue full'`."
    )


def _save_state(state_path: Path, *, last_total_dropped_frames: int) -> None:
    """Atomic-replace persist of monitor state. Best-effort (a state-
    write failure means next tick may re-baseline, which is fine for
    monotonic counter semantics)."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "last_total_dropped_frames": last_total_dropped_frames,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }))
        os.replace(tmp, state_path)
    except OSError:
        # Fail-quiet (cron convention).
        pass


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
        ("dropped_frames", check_dropped_frames),
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
