"""D1.6 + D1.6 fu + D2.5 + D1.8 + D1.11.a + B2a-1: collector health monitor — disk + WS-conn-loss + service-down + bronze-dropped-frames alerts.

Ticket 86b9zk4we (D1.6, 2026-05-17) + 86b9zkktr (D1.6 fu, 2026-05-17)
+ 86b9znq4w (D2.5, 2026-05-18 — extends to also poll
kalshi-coinbase-collector) + 86ba0duck (D1.8, 2026-05-18 — extends
to also poll kalshi-weather-collector with a subset of checks: disk
+ collector_active + dropped_frames; NO ws_reconnects since weather
is HTTP-polled and has no persistent WS conn) + 86ba0ppy0 (D1.11.a,
2026-05-19 — extends to also poll kalshi-espn-collector with the
same HTTP-poll subset as weather) + 86ba1zf5j (B2a-1, 2026-05-28 —
extends to also poll kalshi-venue-l2-collector with the FULL WS
subset: disk + ws_reconnects + collector_active + dropped_frames,
since the venue-L2 recorder runs 3 persistent WS conns). Standalone
CLI run via cron on the VPS. Polls 4 health surfaces × 3 WS-collectors
+ 3 health surfaces × 2 HTTP-poll-collectors + 1 bot check (B3-fu3,
2026-05-18) = 19 total alert classes; sends Telegram alerts via the
existing ``bot.notifier.TelegramNotifier`` (no Telegram client
re-implementation).

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

Ticket 86bbvdcat (2026-09-05) adds, Kalshi tier only:
  - check_boot_state: alert if bronze_health.json reports state=booting for
    > DEFAULT_MAX_BOOT_SECONDS

D1.6 fu adds the 4th D1.6-era check:
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

Operator install (manual, post-D1.6 merge — log path canonicalized by
Bit 86ba0jvka 2026-05-19 incident to `~/collector_health.log`; the
prior D1.6 docstring referenced `/var/log/...` which would have
required sudo NOPASSWD that botuser doesn't have for write access,
AND `/tmp/` was a worse choice yet because `systemd-tmpfiles-clean
.timer` periodically wipes it, hiding crash traces — the actual VPS
crontab at incident time used `/tmp/collector_health.log` which
explains why the canary's `ModuleNotFoundError: No module named 'bot'`
crashes went undetected for 2 days):
    # In `crontab -e` (botuser):
    */5 * * * * cd /home/botuser/kalshi-bot-repo && source venv/bin/activate && python3 scripts/ops/collector_health_monitor.py >> ~/collector_health.log 2>&1

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

# Bit 86ba0jvka (2026-05-19): bootstrap repo root onto sys.path BEFORE
# any `from bot.*` / `import bot.*` reference fires below. Cron's
# invocation flow (`cd ~/kalshi-bot-repo && source venv/bin/activate
# && python3 scripts/ops/collector_health_monitor.py`) does NOT
# auto-add the repo root to sys.path — only the script's parent dir
# (scripts/ops/) is added by Python's script-invocation rule. Without
# this bootstrap the script crashes at `from bot.notifier import
# TelegramNotifier` (the function-scoped lazy import inside `main()`)
# with `ModuleNotFoundError: No module named 'bot'`. This silent-fail
# mode kept the disk-pressure canary DEAD from D1.6 ship (2026-05-17)
# through the 2026-05-19 disk-full incident — zero alerts fired
# across 2 days of an 80% watermark threshold being crossed. Pinned
# by tests/contracts/test_collector_health_monitor_runnable.py via 3
# AST guards: (1) bootstrap exists, (2) precedes every `from bot.*` /
# `import bot.*` statement, (3) derives from `parents[2]` (= repo
# root; parents[0]/[1]/[3+] would silently fail on the VPS). The
# AST-only approach replaced a behavioral subprocess test that
# couldn't escape editable-install MetaPathFinders on dev machines
# (R4-C1 ratchet of this Bit's adv-review cycle).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


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

# D1.8 Weather-side defaults (2026-05-18, ticket 86ba0duck). Mirror
# the Kalshi + Coinbase shapes but point at the kalshi-weather-collector's
# separate process / unit / bronze root / sidecar. Subset of checks
# (no ws_reconnects — HTTP polling has no persistent WS conn);
# include disk + collector_active + dropped_frames.
WEATHER_BRONZE_ROOT = "/var/lib/kalshi-weather-collector"
WEATHER_COLLECTOR_UNIT = "kalshi-weather-collector"
WEATHER_SIDECAR_PATH = "/var/lib/kalshi-weather-collector/bronze_health.json"
WEATHER_MONITOR_STATE_PATH = "/var/lib/kalshi-weather-collector/monitor_state.json"

# D1.11.a ESPN-side defaults (2026-05-19, ticket 86ba0ppy0). Mirror
# the Weather shape exactly — both are HTTP-poll sources with the same
# disk + collector_active + dropped_frames subset (no ws_reconnects).
ESPN_BRONZE_ROOT = "/var/lib/kalshi-espn-collector"
ESPN_COLLECTOR_UNIT = "kalshi-espn-collector"
ESPN_SIDECAR_PATH = "/var/lib/kalshi-espn-collector/bronze_health.json"
ESPN_MONITOR_STATE_PATH = "/var/lib/kalshi-espn-collector/monitor_state.json"

# B2a-1 Venue-L2-side defaults (2026-05-28, ticket 86ba1zf5j). UNLIKE the
# weather/ESPN HTTP-poll tiers, the venue-L2 recorder runs THREE persistent
# WS conns (Kraken + Bitstamp + Gemini), so its tier gets the FULL WS
# check set (incl. ws_reconnects). The recorder emits the
# ``venue_l2_ws_disconnected`` journal marker on every venue disconnect;
# the ws_reconnects check below MUST be passed that marker (the Kalshi
# default would never match → always-OK false negative).
VENUE_L2_BRONZE_ROOT = "/var/lib/kalshi-venue-l2-collector"
VENUE_L2_COLLECTOR_UNIT = "kalshi-venue-l2-collector"
VENUE_L2_SIDECAR_PATH = "/var/lib/kalshi-venue-l2-collector/bronze_health.json"
VENUE_L2_MONITOR_STATE_PATH = "/var/lib/kalshi-venue-l2-collector/monitor_state.json"
VENUE_L2_WS_DISCONNECT_MARKER = "venue_l2_ws_disconnected"

# B3-fu3 (ticket 86b9zxb4c, 2026-05-18) — alert on
# `insert_evaluated_opportunity failed` WARNINGs from the bot journal.
# Post-B3-fu7 (`86ba067mg`, 2026-05-18) the marker substring matches WARN
# sites across bot/scanner + bot/state, of which 46 are narrowed to
# `sqlite3.OperationalError` (2 B3-fu2/fu6 + 44 B3-fu7) and 12 COMPLEX
# sites still use bare `except Exception:` (deferred per-site review).
# Either way the alert is real-signal: a hit at a narrowed site is a
# genuine DB error; a hit at one of the 12 bare-except sister sites is
# an exception (DB or otherwise — NameError / UnboundLocalError /
# AttributeError / KeyError class) — both warrant operator attention.
# Threshold defaults to 1 — these WARNs should be 0/day under healthy
# operation, so even one hit fires.
BOT_UNIT = "kalshi-bot"
DEFAULT_INSERT_EVAL_FAILURE_WINDOW_MIN = 5
DEFAULT_INSERT_EVAL_FAILURE_THRESHOLD = 1
DEFAULT_INSERT_EVAL_FAILURE_LOG_MARKER = "insert_evaluated_opportunity failed"
# Stale-sidecar threshold: 2x the drain-thread poll cadence (1s) +
# 2x the cron tick interval (5min = 300s) = ~610s. Use 120s as a tight
# floor so we catch a wedged drain thread within 2 monitor ticks, not 2
# cron intervals. The 60s rotation cadence (D0.3 §4) does NOT bound
# sidecar freshness — sidecar is written every drain-poll, not every
# rotation.
DEFAULT_SIDECAR_STALE_SECONDS = 120

# Boot-grace window (umbrella `86ba12rf0` / RCA-F `86ba12xr6`,
# 2026-05-20). The collector's boot sequence is dominated by a ~10-min
# REST snapshot (754K-ticker pagination) + ~7-min per-conn wire-up
# (60s/conn × 7 conns). During this window the drain thread isn't
# running yet, so bronze_health.json is legitimately stale — alerting
# on the 120s threshold here is a false-positive that fires every
# deploy / collector restart (~3 times today on 2026-05-19, per
# umbrella ticket). The grace skips ONLY the STALE alert until the
# unit's `ActiveEnterTimestamp` is at least this many seconds old.
# SCHEMA + DROPS checks remain active throughout — they read the
# file's CONTENT, not its mtime, and bug classes there (version-skew,
# wedged-but-fresh-sidecar drain) deserve to alert even during boot.
DEFAULT_BOOT_GRACE_SECONDS = 1200

# Ticket 86bbvdcat (2026-09-05): the collector now writes
# ``state: booting|running`` + ``state_since`` into bronze_health.json
# from the moment the drain thread starts (BEFORE any REST page-through).
# ``check_boot_state`` alerts when the process has been ``booting`` for
# longer than this. Same figure as the STALE boot grace above: with the
# persisted ticker set a boot reaches WS in ~3 min; a boot still paging
# after 20 min is the first-boot-after-deploy (no last_tickers.json yet)
# or a regression — either way the operator should know, because the
# 2026-09-05 restart spent 59.8 min with all six units "active" and zero
# orderbook bronze flowing.
DEFAULT_MAX_BOOT_SECONDS = 1200


def _systemctl_show_property(unit: str, prop: str) -> Optional[str]:
    """Read a single systemctl ``show`` property; return value or None.

    Pure helper. Used by ``_collector_uptime_seconds`` (boot-grace
    check). Returns None on subprocess failure; the caller decides
    whether to treat None as fail-open (continue with other checks) or
    fail-quiet (skip the dependent check).
    """
    try:
        result = subprocess.run(
            ["systemctl", "show", unit, "--property=" + prop],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    # Output shape: "Key=Value\n" (trailing newline). Strip + split once.
    line = result.stdout.strip()
    if "=" not in line:
        return None
    _, _, value = line.partition("=")
    return value


def _collector_uptime_seconds(unit: str) -> Optional[float]:
    """Return seconds since the unit's ActiveEnterTimestamp, or None.

    Used by ``check_dropped_frames`` to skip the STALE-sidecar alert
    during the collector's ~17-min boot window. Returns None on any
    systemctl-show failure — caller treats as "no grace, run normal
    check" (fail-open posture matches the cron framework's bias).

    ActiveEnterTimestampMonotonic is monotonic-clock μs since boot.
    Converting to wall-clock requires subtracting against the kernel's
    BootTime, but we want age-since-active-entered which is simpler:
    the difference between systemd's reported ``ActiveEnterTimestamp``
    (UTC wall-clock) and ``time.time()``.
    """
    ts_str = _systemctl_show_property(unit, "ActiveEnterTimestamp")
    if not ts_str:
        return None
    # systemctl emits "Wed 2026-05-20 00:04:44 UTC" (or local TZ if
    # `set-default-timezone` differs from UTC; the VPS runs UTC).
    # Strptime with explicit UTC handling.
    try:
        import datetime as _dt
        # Common systemctl format: "Day YYYY-MM-DD HH:MM:SS TZ"
        # Split off the leading day-of-week (variable length) + parse rest.
        parts = ts_str.split(maxsplit=1)
        if len(parts) != 2:
            return None
        tail = parts[1]  # "2026-05-20 00:04:44 UTC"
        active_ts = _dt.datetime.strptime(
            tail, "%Y-%m-%d %H:%M:%S %Z",
        ).replace(tzinfo=_dt.timezone.utc)
        active_epoch = active_ts.timestamp()
        return time.time() - active_epoch
    except (ValueError, ImportError):
        return None


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
    # df's Use% = used / (used + avail); used/total hid the ext4 reserved
    # blocks (~4 points laxer on the 48 GB root). Ticket 86bbvd50a R1-M4.
    denom = usage.used + usage.free
    used_pct = int((usage.used / denom) * 100) if denom else 0
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
    log_marker: str = "kalshi_ws_disconnected",
) -> Optional[str]:
    """Return alert string if `kalshi_ws_disconnected` count in last
    ``window_min`` minutes >= ``threshold_count``, else None.

    Includes class breakdown (1006 abnormal / 1009 message-too-big /
    1011 ping timeout) so the operator can route to the correct fix:
    - 1006: upstream outage or our network blip
    - 1009: ws_max_size config (D1.3-fu1 should have closed this on Kalshi)
    - 1011: asyncio loop blockage (D1.3-fu3 stopgap + D1.3-fu4 proper fix)

    D2.5 R2-C1: ``log_marker`` parameterizes the per-line filter
    substring. Kalshi-tier callers use the default
    ``"kalshi_ws_disconnected"``; Coinbase-tier callers MUST pass
    ``log_marker="coinbase_ws_disconnected"`` — the Coinbase wire
    library writes a different marker (``coinbase_wire/ws_client.py``
    line ~654) and a hardcoded Kalshi-only substring filter would
    silently never match Coinbase logs, producing an always-OK signal
    even during a sustained Coinbase reconnect storm.
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
        if log_marker in line
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


def check_insert_evaluated_opportunity_failures(
    window_min: int = DEFAULT_INSERT_EVAL_FAILURE_WINDOW_MIN,
    threshold_count: int = DEFAULT_INSERT_EVAL_FAILURE_THRESHOLD,
    unit: str = BOT_UNIT,
    log_marker: str = DEFAULT_INSERT_EVAL_FAILURE_LOG_MARKER,
) -> Optional[str]:
    """Return alert string if `insert_evaluated_opportunity failed` WARN
    count in last ``window_min`` minutes >= ``threshold_count``, else None.

    Post-B3-fu7 (`86ba067mg`, 2026-05-18) the marker substring matches
    WARN sites across `bot/scanner/__init__.py` + `bot/state.py`, of
    which 46 are narrowed to `sqlite3.OperationalError` (2 B3-fu2/fu6
    + 44 B3-fu7) and 12 COMPLEX sites still use bare `except Exception:`
    (deferred per-site review — try-bodies contain non-DB compute that
    needs case-by-case judgment). A hit at a narrowed site is a genuine
    DB error; a hit at one of the 12 bare-except sister sites is an
    exception (DB or otherwise — NameError / UnboundLocalError /
    AttributeError / KeyError class). Both warrant operator attention
    within minutes (B3 itself was 42 days of silent LPNE row drops
    behind the pre-narrow bare-`except Exception:` swallow at the
    LPNE site).

    Fail-quiet posture mirrors `check_ws_reconnects`: journalctl
    absent (test env), timeout, or non-zero exit returns None rather
    than alert-spamming.
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
        return None
    hits = [line for line in out.splitlines() if log_marker in line]
    if len(hits) < threshold_count:
        return None
    return (
        f"*BOT INSERT_EVALUATED_OPPORTUNITY FAILED* — {len(hits)} hits "
        f"of `{log_marker}` in last {window_min}min (threshold {threshold_count}). "
        f"Post-B3-fu7 the marker matches 46 narrowed sites + 12 COMPLEX "
        f"bare-except sites (deferred per-site review). A hit at a "
        f"narrowed site is a genuine DB error; a hit at one of the 12 "
        f"bare-except sites is an exception (DB or otherwise — "
        f"NameError / UnboundLocalError / AttributeError / KeyError "
        f"class) — both worth investigating. "
        f"Check: `journalctl -u {unit} --since '{window_min} min ago' | "
        f"grep -i 'insert_evaluated_opportunity failed' | tail`. "
        f"Then trace to `bot/state.py::insert_evaluated_opportunity` + "
        f"the emitting `bot/scanner/__init__.py` strategy block."
    )


def _parse_sidecar_utc(value) -> Optional[float]:
    """Parse the sidecar's ``%Y-%m-%dT%H:%M:%S.%fZ`` timestamps → epoch."""
    if not isinstance(value, str):
        return None
    try:
        import datetime as _dt
        return _dt.datetime.strptime(
            value, "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


def check_boot_state(
    sidecar_path: Optional[Path] = None,
    max_boot_seconds: int = DEFAULT_MAX_BOOT_SECONDS,
    now: Optional[float] = None,
) -> Optional[str]:
    """Alert when bronze_health.json reports ``state == "booting"`` for
    longer than ``max_boot_seconds`` (ticket 86bbvdcat).

    Fail-quiet on: missing sidecar, malformed JSON, no ``state`` key
    (pre-Bit collectors and the Coinbase / weather / ESPN sidecars, which
    never carry the key), unparseable ``state_since``, or any state other
    than ``booting``. ``now`` is a test seam (defaults to ``time.time()``).
    """
    if sidecar_path is None:
        sidecar_path = Path(os.environ.get(
            "COLLECTOR_HEALTH_SIDECAR_PATH", DEFAULT_SIDECAR_PATH,
        ))
    if not sidecar_path.is_file():
        return None
    try:
        data = json.loads(sidecar_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("state") != "booting":
        return None
    since = _parse_sidecar_utc(data.get("state_since"))
    if since is None:
        return None
    if now is None:
        now = time.time()
    age = now - since
    if age <= max_boot_seconds:
        return None
    return (
        f"*COLLECTOR STILL BOOTING* — {sidecar_path} reports state=booting "
        f"for {int(age)}s (threshold {max_boot_seconds}s; "
        f"ticker_set_source={data.get('ticker_set_source')!r}). No WS "
        f"conn / no orderbook bronze until boot completes. If "
        f"`last_tickers.json` is missing this is the first boot after "
        f"deploy (synchronous REST page-through, ~55 min on 2026-09-05); "
        f"otherwise check `journalctl -u kalshi-collector --since '30 min "
        f"ago' | grep -E 'Boot|REST|wired|booted'`."
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
    unit: str = DEFAULT_COLLECTOR_UNIT,
    boot_grace_seconds: int = DEFAULT_BOOT_GRACE_SECONDS,
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
        unit: systemd unit name to query for boot-grace uptime
            (default ``kalshi-collector``). Dispatcher closures for
            Coinbase / Weather / ESPN MUST pass their respective unit
            names; otherwise the boot-grace check cross-couples to
            Kalshi's uptime (R1-M1 fix, 2026-05-20).
        boot_grace_seconds: skip the STALE alert if the unit's
            ActiveEnterTimestamp is younger than this (default 1200s
            ≈ 20 min, covers the observed ~17-min boot window with
            safety margin). SCHEMA + DROPS checks remain active
            during the grace.

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
    # also be uninformative). RCA-F `86ba12xr6` (2026-05-20): skip the
    # STALE alert during the collector's ~17-min boot window. The drain
    # thread + sidecar writer don't run until AFTER all archivers are
    # wired (boot sequence: salvage → REST snapshot 10 min → 60s/conn ×
    # 7 conns wire-up = ~17 min total). Alerting STALE during boot fires
    # a false-positive on every deploy + every restart cycle.
    #
    # CRITICAL: the grace SKIPS ONLY the STALE alert (R1-C1 fix). SCHEMA
    # + DROPS checks below MUST still run during boot grace — they read
    # the file's CONTENT (not its mtime), and would silently regress
    # observability for 20-min windows if short-circuited here.
    try:
        mtime = sidecar_path.stat().st_mtime
    except OSError:
        return None
    age = time.time() - mtime
    if age > stale_after_seconds:
        uptime = _collector_uptime_seconds(unit)
        in_boot_grace = uptime is not None and uptime < boot_grace_seconds
        if not in_boot_grace:
            return (
                f"*COLLECTOR BRONZE_HEALTH STALE* — sidecar {sidecar_path} "
                f"not written in {int(age)}s (threshold {stale_after_seconds}s; "
                f"next monitor tick fires alert within 5min of staleness onset). "
                f"Collector drain thread may be wedged or process dead. "
                f"Check: `systemctl status kalshi-collector` + "
                f"`journalctl -u kalshi-collector --since '5 min ago' | tail`."
            )
        # In boot grace — drop through to SCHEMA + DROPS checks below.

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
    """Entry point. Runs collector checks (4) × 3 WS-collector tiers +
    collector checks (3) × 2 HTTP-poll-collector tiers + bot checks
    (1) × 1 bot tier = 19 total check dispatches per tick; sends
    Telegram alerts as needed.

    D2.5 (ticket 86b9znq4w, 2026-05-18) extended the original single-
    collector loop to poll BOTH kalshi-collector AND kalshi-coinbase-
    collector (dual-tier dispatch).

    B3-fu3 (ticket 86b9zxb4c, 2026-05-18) extended to TRIPLE-TIER
    dispatch: kalshi-bot is the 3rd tier with a single new check
    function (``check_insert_evaluated_opportunity_failures``).

    D1.8 (ticket 86ba0duck, 2026-05-18) extended to FOUR-TIER
    dispatch: kalshi-weather-collector is the 4th tier (first non-WS
    bronze source). Subset of the WS-collector checks — disk +
    collector_active + dropped_frames; NO ws_reconnects.

    D1.11.a (ticket 86ba0ppy0, 2026-05-19) extended to FIVE-TIER
    dispatch: kalshi-espn-collector is the 5th tier (second non-WS
    bronze source). Same HTTP-poll subset as weather.

    B2a-1 (ticket 86ba1zf5j, 2026-05-28) extended to SIX-TIER dispatch:
    kalshi-venue-l2-collector is the 6th tier (multi-venue lean L2 WS
    recorder). FULL WS check set incl. ws_reconnects (3 persistent WS
    conns), passing log_marker="venue_l2_ws_disconnected".

    Per-tier dedup-key prefixes (``d1_6_<check>`` for Kalshi
    collector / ``d2_5_<check>`` for Coinbase collector /
    ``b3_fu3_<check>`` for bot tier / ``d1_8_<check>`` for weather
    collector / ``d1_11_<check>`` for ESPN collector / ``b2a_<check>``
    for venue-L2 collector) keep alert dedup independent across tiers —
    a Kalshi disk-pressure alert does NOT dedup-suppress a Coinbase or
    weather disk-pressure alert (their underlying mount points are
    structurally separate per the Option B isolation posture), and
    the bot-tier alert never collides with any collector tier.

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
            unit=DEFAULT_COLLECTOR_UNIT,
        )),
        # Ticket 86bbvdcat: "booting for 30 min" vs "healthy" — only the
        # Kalshi collector writes the boot state (REST page-through boot).
        ("boot_state", lambda: check_boot_state(
            sidecar_path=Path(DEFAULT_SIDECAR_PATH),
        )),
    ]
    # R4-M2 + R5-M1: resolve the Coinbase sidecar path at call time
    # mirroring the writer's two-knob derivation exactly
    # (collector/coinbase_main_loop.py:302-310).
    #
    # Writer logic:
    #   if COINBASE_HEALTH_SIDECAR_PATH set: use it.
    #   else if COINBASE_BRONZE_ROOT set: derive parent / bronze_health.json.
    #   else (both unset): fall back to canonical path (default
    #     /var/lib/kalshi-coinbase-collector/bronze defaults to
    #     /var/lib/kalshi-coinbase-collector/bronze_health.json).
    #
    # Without symmetric reader derivation, an operator who relocates
    # the bronze root via COINBASE_BRONZE_ROOT alone (without also
    # setting COINBASE_HEALTH_SIDECAR_PATH) would have the writer +
    # monitor referencing different paths — STALE alert spam or no
    # signal at all. R4-M2 closed the explicit-knob half (HEALTH_
    # SIDECAR_PATH); R5-M1 closes the derived-from-bronze-root half.
    _coinbase_bronze_root_env = os.environ.get(
        "COINBASE_BRONZE_ROOT", "",
    ).strip()
    if _coinbase_bronze_root_env:
        # Writer derives sidecar as bronze_root.parent /
        # "bronze_health.json"; mirror exactly.
        _coinbase_default_sidecar = str(
            Path(_coinbase_bronze_root_env).parent / "bronze_health.json"
        )
    else:
        _coinbase_default_sidecar = COINBASE_SIDECAR_PATH
    _coinbase_sidecar_resolved = os.environ.get(
        "COINBASE_HEALTH_SIDECAR_PATH", _coinbase_default_sidecar,
    ).strip() or _coinbase_default_sidecar
    coinbase_checks = [
        ("disk", lambda: check_disk(
            path=COINBASE_BRONZE_ROOT,
        )),
        ("ws_reconnects", lambda: check_ws_reconnects(
            unit=COINBASE_COLLECTOR_UNIT,
            # R2-C1: Coinbase wire emits coinbase_ws_disconnected
            # (NOT the Kalshi default). Without this kwarg the filter
            # substring would never match Coinbase logs and the
            # reconnect-storm alert would be silently broken.
            log_marker="coinbase_ws_disconnected",
        )),
        ("collector_active", lambda: check_collector_active(
            unit=COINBASE_COLLECTOR_UNIT,
        )),
        ("dropped_frames", lambda: check_dropped_frames(
            sidecar_path=Path(_coinbase_sidecar_resolved),
            state_path=Path(COINBASE_MONITOR_STATE_PATH),
            unit=COINBASE_COLLECTOR_UNIT,
        )),
    ]

    # Per-tier dedup-key prefix. Kalshi-side keeps the D1.6-era prefix
    # (`d1_6_<check>`) so an in-flight alert dedup window from a pre-
    # D2.5 deploy doesn't reset on D2.5 ship — operators see continuous
    # dedup semantics across the upgrade. Coinbase-side uses the D2.5
    # prefix (`d2_5_<check>`) so a Coinbase alert can fire even while
    # the matching Kalshi alert is still within its dedup window.
    # B3-fu3 (ticket 86b9zxb4c, 2026-05-18): bot-tier alert on
    # `insert_evaluated_opportunity failed` WARNINGs. Dedup prefix
    # `b3_fu3` keeps it independent of d1_6 (Kalshi collector) and
    # d2_5 (Coinbase collector) dedup windows. Only one check today —
    # disk + ws_reconnects + collector_active are NOT useful for the
    # bot tier (bot disk pressure is structurally different; bot
    # restarts are operator-initiated; bot uses Kalshi WS reconnect
    # logic through a different code path with its own observability).
    bot_checks = [
        ("insert_eval_failures", lambda: check_insert_evaluated_opportunity_failures(
            unit=BOT_UNIT,
        )),
    ]
    # D1.8 (2026-05-18, ticket 86ba0duck): weather collector tier.
    # SUBSET of the WS-collector checks — NO ws_reconnects because HTTP
    # polling has no persistent WS conn (the log_marker filter would
    # never match and produce an always-OK false negative). The 3
    # checks that DO apply:
    #   - disk: weather bronze accumulates on /var/lib/kalshi-weather-collector
    #   - collector_active: systemctl is-active gate
    #   - dropped_frames: schema-parity with Kalshi+Coinbase sidecars
    #     (weather has no worker-queue drops by design; sidecar emits
    #     total_dropped_frames=0 unconditionally so the monitor's
    #     tier-uniform shape works without per-tier branches)
    #
    # R1-M2: resolve the Weather sidecar path at call time mirroring the
    # writer's two-knob derivation exactly (collector/weather_main_loop.py
    # lines 312-319). Without symmetric reader derivation, an operator
    # who relocates the bronze root via WEATHER_BRONZE_ROOT alone (without
    # also setting WEATHER_HEALTH_SIDECAR_PATH) would have the writer +
    # monitor referencing different paths — STALE alert spam or no
    # signal at all. Mirrors the Coinbase R4-M2 + R5-M1 fix above for
    # the same class.
    _weather_bronze_root_env = os.environ.get(
        "WEATHER_BRONZE_ROOT", "",
    ).strip()
    if _weather_bronze_root_env:
        # Writer derives sidecar as bronze_root.parent / "bronze_health.json".
        _weather_default_sidecar = str(
            Path(_weather_bronze_root_env).parent / "bronze_health.json"
        )
    else:
        _weather_default_sidecar = WEATHER_SIDECAR_PATH
    _weather_sidecar_resolved = os.environ.get(
        "WEATHER_HEALTH_SIDECAR_PATH", _weather_default_sidecar,
    ).strip() or _weather_default_sidecar
    weather_checks = [
        ("disk", lambda: check_disk(
            path=WEATHER_BRONZE_ROOT,
        )),
        ("collector_active", lambda: check_collector_active(
            unit=WEATHER_COLLECTOR_UNIT,
        )),
        ("dropped_frames", lambda: check_dropped_frames(
            sidecar_path=Path(_weather_sidecar_resolved),
            state_path=Path(WEATHER_MONITOR_STATE_PATH),
            unit=WEATHER_COLLECTOR_UNIT,
        )),
    ]
    # D1.11.a (2026-05-19, ticket 86ba0ppy0): ESPN collector tier.
    # Same HTTP-poll subset as weather.
    _espn_bronze_root_env = os.environ.get(
        "ESPN_BRONZE_ROOT", "",
    ).strip()
    if _espn_bronze_root_env:
        _espn_default_sidecar = str(
            Path(_espn_bronze_root_env).parent / "bronze_health.json"
        )
    else:
        _espn_default_sidecar = ESPN_SIDECAR_PATH
    _espn_sidecar_resolved = os.environ.get(
        "ESPN_HEALTH_SIDECAR_PATH", _espn_default_sidecar,
    ).strip() or _espn_default_sidecar
    espn_checks = [
        ("disk", lambda: check_disk(
            path=ESPN_BRONZE_ROOT,
        )),
        ("collector_active", lambda: check_collector_active(
            unit=ESPN_COLLECTOR_UNIT,
        )),
        ("dropped_frames", lambda: check_dropped_frames(
            sidecar_path=Path(_espn_sidecar_resolved),
            state_path=Path(ESPN_MONITOR_STATE_PATH),
            unit=ESPN_COLLECTOR_UNIT,
        )),
    ]
    # B2a-1 (2026-05-28, ticket 86ba1zf5j): venue-L2 collector tier. FULL
    # WS check set (incl. ws_reconnects) — the recorder runs 3 persistent
    # WS conns, so reconnect-storm detection applies (unlike the HTTP-poll
    # weather/ESPN tiers). The ws_reconnects check is passed
    # log_marker=VENUE_L2_WS_DISCONNECT_MARKER; the Kalshi-default
    # "kalshi_ws_disconnected" substring would never match the recorder's
    # journal lines (always-OK false negative). Sidecar path resolves at
    # call time mirroring the writer's two-knob derivation
    # (collector/venue_l2_main_loop.py VENUE_L2_BRONZE_ROOT /
    # VENUE_L2_HEALTH_SIDECAR_PATH), same R5-M1-class fix as Coinbase/Weather.
    _venue_l2_bronze_root_env = os.environ.get(
        "VENUE_L2_BRONZE_ROOT", "",
    ).strip()
    if _venue_l2_bronze_root_env:
        _venue_l2_default_sidecar = str(
            Path(_venue_l2_bronze_root_env).parent / "bronze_health.json"
        )
    else:
        _venue_l2_default_sidecar = VENUE_L2_SIDECAR_PATH
    _venue_l2_sidecar_resolved = os.environ.get(
        "VENUE_L2_HEALTH_SIDECAR_PATH", _venue_l2_default_sidecar,
    ).strip() or _venue_l2_default_sidecar
    venue_l2_checks = [
        ("disk", lambda: check_disk(
            path=VENUE_L2_BRONZE_ROOT,
        )),
        ("ws_reconnects", lambda: check_ws_reconnects(
            unit=VENUE_L2_COLLECTOR_UNIT,
            log_marker=VENUE_L2_WS_DISCONNECT_MARKER,
        )),
        ("collector_active", lambda: check_collector_active(
            unit=VENUE_L2_COLLECTOR_UNIT,
        )),
        ("dropped_frames", lambda: check_dropped_frames(
            sidecar_path=Path(_venue_l2_sidecar_resolved),
            state_path=Path(VENUE_L2_MONITOR_STATE_PATH),
            unit=VENUE_L2_COLLECTOR_UNIT,
        )),
    ]
    tiers = [
        ("kalshi-collector", "d1_6", kalshi_checks),
        ("kalshi-coinbase-collector", "d2_5", coinbase_checks),
        ("kalshi-bot", "b3_fu3", bot_checks),
        ("kalshi-weather-collector", "d1_8", weather_checks),
        ("kalshi-espn-collector", "d1_11", espn_checks),
        ("kalshi-venue-l2-collector", "b2a", venue_l2_checks),
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
                # operator sees the exception in the cron-redirected log
                # file (canonically ~/collector_health.log per the module
                # docstring's operator-install block).
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
