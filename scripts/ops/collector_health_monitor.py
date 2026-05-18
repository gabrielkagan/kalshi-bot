"""D1.6 + D1.6 fu + D2.5: collector health monitor — disk + WS-conn-loss + service-down + bronze-dropped-frames alerts.

Ticket 86b9zk4we (D1.6, 2026-05-17) + 86b9zkktr (D1.6 fu, 2026-05-17)
+ 86b9znq4w (D2.5, 2026-05-18 — extends to also poll
kalshi-coinbase-collector). Standalone CLI run via cron on the VPS.
Polls 4 health surfaces × 2 collectors = 8 total alert classes; sends
Telegram alerts via the existing ``bot.notifier.TelegramNotifier`` (no
Telegram client re-implementation).

D0.3 §6 isolation contract enumerated 2 failure modes with NO alert
surface pre-D1.6:
  - Disk pressure (2 GB root volume can fill in minutes if S3 upload
    falls behind; silent OOM + bronze loss).
  - WS conn-loss (multiple disconnect classes; manual audit was the
    only surface).

D1.6 adds 3 checks:
  - check_disk: alert if /var/lib/kalshi-collector/ partition >= threshold_pct
  - check_ws_reconnects: alert if kalshi_ws_disconnected count over
    last N minutes >= threshold_count (with 1006/1009/1011 class breakdown)
  - check_collector_active: alert if `systemctl is-active kalshi-collector`
    returns non-zero

D1.6 fu adds the 4th check:
  - check_dropped_frames: positive observability for D1.3-fu4 worker
    queue saturation. Reads ``bronze_health.json`` sidecar written by
    the collector drain thread; alerts on:
      * cumulative dropped-frames delta >= threshold (default 100)
        across ticks (rolling-window catches sustained drip-drops the
        per-tick design would miss)
      * sidecar STALE (mtime > 120s; catches wedged drain thread / dead
        collector within ~2 monitor ticks vs waiting for systemctl flip)
      * sidecar SCHEMA SKEW (schema_version != 1; future bumps would
        silently degrade the signal otherwise)

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

# D2.5 Coinbase-side defaults. Mirror the Kalshi-side defaults but
# point at the kalshi-coinbase-collector's separate process / unit /
# bronze root / sidecar / monitor-state. Option B isolation:
# separate disk path, separate systemd unit, separate sidecar so
# Coinbase disk-full / WS-storm / drops don't dedup-collide with
# Kalshi's. Same threshold values — they're per-collector signal
# bounds, not per-source.
COINBASE_BRONZE_ROOT = "/var/lib/kalshi-coinbase-collector"
COINBASE_COLLECTOR_UNIT = "kalshi-coinbase-collector"
COINBASE_SIDECAR_PATH = "/var/lib/kalshi-coinbase-collector/bronze_health.json"
COINBASE_MONITOR_STATE_PATH = "/var/lib/kalshi-coinbase-collector/monitor_state.json"
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
    sidecar_path: Optional[Path] = None,
    state_path: Path = Path(DEFAULT_MONITOR_STATE_PATH),
    threshold: int = DEFAULT_DROPPED_FRAMES_THRESHOLD,
    stale_after_seconds: int = DEFAULT_SIDECAR_STALE_SECONDS,
) -> Optional[str]:
    """Return alert string if BronzeArchiver drops cross ``threshold``
    cumulatively-since-last-alert, else None. D1.6 fu observability for
    D1.3-fu4 worker queue saturation.

    Three distinct alert classes:
      1. DROPS: cumulative ``total_dropped_frames`` delta accumulated
         across ticks reaches ``threshold``. Reset on alert. R1-M2 fix:
         this is a ROLLING-WINDOW running sum, NOT per-tick delta —
         a sustained 30 drops/tick × 4 ticks = 120 cumulative drops
         WILL alert at threshold=100, even though no single tick
         crossed the bar alone. Pre-fix per-tick design missed this
         steady-state-drip class.
      2. STALE: sidecar mtime older than ``stale_after_seconds``.
         Catches wedged drain thread / dead collector within ~2 monitor
         ticks (vs waiting for systemctl-is-active flip).
      3. SCHEMA: sidecar schema_version != 1 — a future schema bump
         that adds/renames keys would silently degrade this check to
         delta=0 forever; alert instead of fail-quiet so the operator
         notices the version skew.

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
        the new floor + zero pending_drops. Return None.
      - On NO-ALERT tick (pending below threshold): accumulate the new
        delta into ``pending_drops_since_last_alert`` and persist.
      - On ALERT tick: reset ``pending_drops_since_last_alert`` to 0
        so the same backlog doesn't re-alert.

    Args:
        sidecar_path: bronze_health.json written by
            collector.main_loop.write_bronze_health_sidecar. When None
            (default), resolves via ``COLLECTOR_HEALTH_SIDECAR_PATH``
            env var at CALL time (NOT module-import time — R1-M1 fix).
            Falls back to ``DEFAULT_SIDECAR_PATH``. Coupling-by-env-var
            keeps the monitor aligned with the collector's chosen path
            without requiring config-file synchronization.
        state_path: monitor-owned state file persisting
            ``last_total_dropped_frames`` + ``pending_drops_since_last_alert``.
        threshold: alert if pending_drops_since_last_alert >= threshold
            (default 100).
        stale_after_seconds: alert if sidecar mtime older than this
            (default 120s ≈ 2 monitor ticks).

    Returns:
        Alert string (Markdown for Telegram) or None.
    """
    # R1-M1: env-var resolution at CALL time (not function-def default,
    # which would freeze the value at module import).
    if sidecar_path is None:
        sidecar_path = Path(os.environ.get(
            "COLLECTOR_HEALTH_SIDECAR_PATH", DEFAULT_SIDECAR_PATH,
        ))

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
            f"not written in {int(age)}s (threshold {stale_after_seconds}s; "
            f"next monitor tick fires alert within 5min of staleness onset). "
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

    # R1-m1: schema-version check. Future-bump would silently degrade
    # the dropped-frames signal to delta=0 (since missing keys default
    # to 0); alert instead of fail-quiet.
    sidecar_schema = sidecar_data.get("schema_version")
    if sidecar_schema != 1:
        return (
            f"*COLLECTOR BRONZE_HEALTH SCHEMA SKEW* — sidecar "
            f"{sidecar_path} schema_version={sidecar_schema!r} "
            f"(monitor expects 1). The dropped-frames signal is "
            f"disabled until the monitor + collector schemas align. "
            f"Update scripts/ops/collector_health_monitor.py to handle "
            f"the new schema OR pin the collector to the old schema."
        )

    current_total = int(sidecar_data.get("total_dropped_frames", 0))

    # Load + interpret state.
    if not state_path.is_file():
        # First run: baseline, no alert.
        _save_state(
            state_path,
            last_total_dropped_frames=current_total,
            pending_drops_since_last_alert=0,
        )
        return None
    try:
        state_data = json.loads(state_path.read_text())
        last_total = int(state_data.get("last_total_dropped_frames", 0))
        pending = int(state_data.get("pending_drops_since_last_alert", 0))
    except (OSError, ValueError):
        # Corrupt state file — re-baseline rather than alert-spam.
        _save_state(
            state_path,
            last_total_dropped_frames=current_total,
            pending_drops_since_last_alert=0,
        )
        return None

    # Reset detection (counter went down → collector restarted).
    if current_total < last_total:
        _save_state(
            state_path,
            last_total_dropped_frames=current_total,
            pending_drops_since_last_alert=0,
        )
        return None

    delta = current_total - last_total
    pending += delta

    # R1-M2: accumulate-then-check. Sustained drip-drop reaches threshold
    # over multiple ticks rather than requiring a single-tick burst.
    if pending < threshold:
        _save_state(
            state_path,
            last_total_dropped_frames=current_total,
            pending_drops_since_last_alert=pending,
        )
        return None

    # ALERT — reset pending so the same backlog doesn't re-alert next tick.
    _save_state(
        state_path,
        last_total_dropped_frames=current_total,
        pending_drops_since_last_alert=0,
    )
    return (
        f"*COLLECTOR BRONZE_DROPPED_FRAMES* — {pending} new drops "
        f"accumulated since last alert (threshold {threshold}). "
        f"Total since collector boot: {current_total}. Worker queue "
        f"saturated — D1.3-fu4 bounded queue dropped frames on "
        f"`queue.Full`. Check: per-archiver breakdown in {sidecar_path}; "
        f"collector load (subscribe burst? backlog drain?) in "
        f"`journalctl -u kalshi-collector --since '10 min ago' | "
        f"grep -i 'write_queue full'`."
    )


def _save_state(
    state_path: Path,
    *,
    last_total_dropped_frames: int,
    pending_drops_since_last_alert: int = 0,
) -> None:
    """Atomic-replace persist of monitor state. Best-effort (a state-
    write failure means next tick may re-baseline, which is fine for
    monotonic counter semantics)."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "last_total_dropped_frames": last_total_dropped_frames,
            "pending_drops_since_last_alert": pending_drops_since_last_alert,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }))
        os.replace(tmp, state_path)
    except OSError:
        # Fail-quiet (cron convention).
        pass


def main() -> int:
    """Entry point. Runs all 4 checks × 2 collectors; sends Telegram
    alerts as needed.

    D2.5 (ticket 86b9znq4w, 2026-05-18) extends the original single-
    collector loop to poll BOTH kalshi-collector AND kalshi-coinbase-
    collector. Per-tier dedup keys (``d1_6_<check>`` for the Kalshi
    side / ``d2_5_<check>`` for the Coinbase side) keep alert dedup
    independent — a Kalshi disk-pressure alert does NOT dedup-suppress
    a Coinbase disk-pressure alert (their underlying mount points are
    structurally separate per the Option B isolation posture).

    Returns 0 always (cron convention — exit code reserved for cron's
    own error handling, NOT for application health signaling; that
    goes via Telegram).
    """
    from bot.notifier import TelegramNotifier  # imported lazily so tests can mock

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    # Per-collector check invocations. Each entry is a (check_name,
    # callable) pair where the callable takes no args at call time
    # (closures bind the per-collector kwargs). The Kalshi-side
    # callables match the pre-D2.5 default behavior; the Coinbase-side
    # callables forward unit / bronze-root / sidecar / state-path kwargs
    # to the same check functions (which already accept these — D1.6
    # parametrized them by default-arg, D2.5 just calls them with
    # explicit kwargs).
    kalshi_checks = [
        ("disk", lambda: check_disk(
            path=DEFAULT_BRONZE_ROOT,
        )),
        ("ws_reconnects", lambda: check_ws_reconnects(
            unit=DEFAULT_COLLECTOR_UNIT,
        )),
        ("collector_active", lambda: check_collector_active(
            unit=DEFAULT_COLLECTOR_UNIT,
        )),
        ("dropped_frames", lambda: check_dropped_frames(
            sidecar_path=Path(DEFAULT_SIDECAR_PATH),
            state_path=Path(DEFAULT_MONITOR_STATE_PATH),
        )),
    ]
    coinbase_checks = [
        ("disk", lambda: check_disk(
            path=COINBASE_BRONZE_ROOT,
        )),
        ("ws_reconnects", lambda: check_ws_reconnects(
            unit=COINBASE_COLLECTOR_UNIT,
        )),
        ("collector_active", lambda: check_collector_active(
            unit=COINBASE_COLLECTOR_UNIT,
        )),
        ("dropped_frames", lambda: check_dropped_frames(
            sidecar_path=Path(COINBASE_SIDECAR_PATH),
            state_path=Path(COINBASE_MONITOR_STATE_PATH),
        )),
    ]

    # Per-tier dedup-key prefix. Kalshi-side keeps the D1.6-era prefix
    # (`d1_6_<check>`) so an in-flight alert dedup window from a pre-
    # D2.5 deploy doesn't reset on D2.5 ship — operators see continuous
    # dedup semantics across the upgrade. Coinbase-side uses the D2.5
    # prefix (`d2_5_<check>`) so a Coinbase alert can fire even while
    # the matching Kalshi alert is still within its dedup window.
    tiers = [
        ("kalshi-collector", "d1_6", kalshi_checks),
        ("kalshi-coinbase-collector", "d2_5", coinbase_checks),
    ]
    for tier_name, dedup_prefix, checks in tiers:
        for check_name, check_fn in checks:
            try:
                alert = check_fn()
            except Exception as exc:
                # Defensive: a check raising is itself a regression (the
                # check functions return None on missing-tooling / missing-
                # files). Log + continue so one broken check doesn't
                # silence the others. Cron convention = exit 0 always;
                # operator sees the exception in /var/log/... .
                print(
                    f"[{tier_name}/{check_name}] EXCEPTION: {exc!r}",
                    file=sys.stderr,
                )
                continue
            if alert:
                print(
                    f"[{tier_name}/{check_name}] ALERT: {alert}",
                    file=sys.stderr,
                )
                notifier.send(
                    alert,
                    dedup_key=f"{dedup_prefix}_{check_name}",
                )
            else:
                print(f"[{tier_name}/{check_name}] OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
