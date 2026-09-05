"""Phantom reconcile monitor — auto-runs the phantom_pnl_audit and
Telegram-alerts on material drift (ticket TBD, 2026-05-19).

Standalone CLI runs hourly via cron on the VPS. Wraps
``scripts/audit/phantom_pnl_audit.py``: invokes ``run_audit(apply=True)``
on the last 24h of settled tickers, writes phantom_corrections rows,
and dispatches Telegram alerts on material drift with day-stable
cross-process dedup so a chronic phantom doesn't generate 24 alerts.

Why this exists
---------------
Pre-this-Bit the audit was manual-only. Memory record
``feedback_use_corrected_pnl_always`` notes the local
``settled_trades`` ledger drifts from Kalshi truth via ghost fills —
2026-05-18 raw 7d -$98.92 vs corrected -$39.02 ($60 swing on one
ticker). Operator had to remember to run ``--run-id <name>`` to true
the dashboard. This monitor closes that gap.

Design
------
Three alert classes with cross-process day-stable dedup via JSON
sidecar (``DEFAULT_DEDUP_SIDECAR_PATH``, default ``./phantom_reconcile_dedup.json``).
Dedup keys incorporate a **content fingerprint** so genuinely new
state (a fresh material phantom, a worse unverified-rate decile, a
different exception class) defeats dedup — R2-M1 fix: the prior
day-only key suppressed a 15:00 UTC NEW $500 phantom because a 09:00
UTC $5 phantom had already fired and the date hadn't rolled over.

  - **summary** (R1-M1 + R2-M1): aggregated message with n_divergent
    + sum_delta_pnl + top-by-|delta_pnl| tickers. Fires once per UTC
    calendar day per (date, fingerprint) where fingerprint =
    ``n<count>_t<top_ticker>_<top_side>_b<sum_bucket_dollars>``
    (top-by-|Δpnl| ticker + count of material + |sum_Δpnl| bucketed
    to nearest $10). Stable across re-runs of the same drift state;
    fresh when count grows OR top ticker changes OR cumulative
    crosses a $10 boundary.
  - **unverified-rate** (C1): separate alert when
    ``n_unverified / n_audited >= DEFAULT_UNVERIFIED_RATE_THRESHOLD``.
    Fingerprint = decile bucket of the rate so a flake that worsens
    by ≥10pp re-fires same-day.
  - **crash** (C2 / R1-M2: catches ``Exception``, NOT
    ``BaseException`` — ``KeyboardInterrupt`` / ``SystemExit``
    propagate). Fingerprint = exception class name so a fresh
    failure class (e.g., KalshiAPIError → DiskFullError) re-fires
    same-day.

audit_run_id format (R1-M4 fix)
-------------------------------
``compute_run_id`` returns ``auto-YYYY-MM-DD`` (DAY granular, not
hour granular). 24 hourly re-runs within the same UTC day all share
one run_id, and ``phantom_pnl_audit.write_correction``'s INSERT OR
REPLACE on UNIQUE(audit_run_id, ticker, side) idempotently
overwrites the same row in place. Downstream LEFT JOIN consumers
get AT MOST one row per (ticker, side) per day — no row
multiplication.

Operator install (cron):
    # `crontab -e` (botuser); source whichever env files export
    # KALSHI_API_KEY (or KALSHI_API_KEY_ID), KALSHI_PRIVATE_KEY_PATH,
    # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. These may be split — on the
    # production VPS, TELEGRAM_* live in ~/.env while KALSHI_* live in
    # ~/kalshi-bot-repo/.env (a deliberate isolation: bot service
    # sources the repo-rooted .env via systemd EnvironmentFile; user
    # cron jobs source ~/.env for Telegram/Anthropic/Supabase). Source
    # ALL files that export the 4 required keys; later-sourced wins on
    # duplicate keys.
    7 * * * * cd /home/botuser/kalshi-bot-repo && set -a && . ~/.env && . .env && set +a && . venv/bin/activate && python3 scripts/ops/phantom_reconcile_monitor.py >> ~/phantom_reconcile.log 2>&1

Exit code: always 0 (cron health-script convention; alerts go via
Telegram, not exit code).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Dict, Iterator, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
# scripts/audit/ has no __init__.py (PEP 420 namespace package);
# inject its dirname so `import phantom_pnl_audit` resolves.
sys.path.insert(0, str(REPO_ROOT / "scripts" / "audit"))


# Defaults — overridable via env or CLI for tuning without redeploy.
DEFAULT_THRESHOLD_CENTS = 500            # $5; below this we don't alert.
DEFAULT_UNVERIFIED_RATE_THRESHOLD = 0.5  # 50%+ unverified → C1 alert.
DEFAULT_LOOKBACK_DAYS = 1                # Hourly cron → 24h re-audit window.
DEFAULT_TOP_N_TICKERS_IN_ALERT = 5
DEFAULT_DEDUP_SIDECAR_PATH = "./phantom_reconcile_dedup.json"

# Stable dedup-prefix; YYYY-MM-DD suffix is appended per call.
DEDUP_PREFIX_SUMMARY = "phantom_reconcile_summary"
DEDUP_PREFIX_UNVERIFIED = "phantom_reconcile_unverified"
DEDUP_PREFIX_CRASH = "phantom_reconcile_crash"


def compute_run_id(dt: datetime.datetime) -> str:
    """`auto-YYYY-MM-DD` — DAY-granular UTC stamp (R1-M4 fix).

    Day granularity means 24 hourly cron firings within the same UTC
    day share one run_id, and ``phantom_pnl_audit.write_correction``'s
    INSERT OR REPLACE on UNIQUE(audit_run_id, ticker, side)
    idempotently overwrites the same row in place. A fresh UTC day
    starts a fresh batch.

    Originally proposed hour-granular (`auto-YYYY-MM-DDTHH`); rejected
    in R1-M4 because a persistent phantom would have produced 24 rows
    per day per (ticker, side), and the downstream "LEFT JOIN
    phantom_corrections" consumer pattern (memory record
    `feedback_use_corrected_pnl_always`) would have multiplied
    settled_trades rows N-fold.

    The ``auto-`` prefix namespace-isolates from operator manual runs
    (e.g., ``--run-id may18``) so a manual audit on the same day
    doesn't accidentally clobber.
    """
    if dt.tzinfo is None:
        raise ValueError("compute_run_id requires a timezone-aware datetime")
    utc_dt = dt.astimezone(datetime.timezone.utc)
    return utc_dt.strftime("auto-%Y-%m-%d")


def _format_dollars(cents: int) -> str:
    """+$12.34 / -$5.00 / $0.00. Sign is mandatory on nonzero so
    direction is unambiguous; zero prints unsigned for readability
    (R1-N2)."""
    if cents == 0:
        return "$0.00"
    sign = "+" if cents > 0 else "-"
    return f"{sign}${abs(cents) / 100:.2f}"


def build_summary_alert(
    summary: Dict,
    threshold_cents: int = DEFAULT_THRESHOLD_CENTS,
    top_n: int = DEFAULT_TOP_N_TICKERS_IN_ALERT,
) -> Optional[str]:
    """Aggregate findings into a single Telegram message.

    Returns None if no finding clears ``threshold_cents`` — chronic
    sub-$5 phantom drift doesn't need to wake the operator.
    """
    material = [
        f for f in (summary.get("findings") or [])
        if abs(int(f.get("delta_pnl_cents") or 0)) >= threshold_cents
    ]
    if not material:
        return None

    ranked = sorted(material,
                    key=lambda f: abs(int(f.get("delta_pnl_cents") or 0)),
                    reverse=True)
    top = ranked[:top_n]

    n_divergent = int(summary.get("n_divergent") or 0)
    n_audited = int(summary.get("n_audited") or 0)
    sum_delta_pnl = int(summary.get("sum_delta_pnl_cents") or 0)
    sum_delta_count = int(summary.get("sum_delta_count") or 0)
    material_sum_delta_pnl = sum(
        int(f.get("delta_pnl_cents") or 0) for f in material
    )

    lines = [
        f"*PHANTOM RECONCILE ALERT* — {len(material)} material "
        f"phantom(s) of {n_divergent} divergent / {n_audited} audited "
        f"(threshold ${threshold_cents / 100:.2f}).",
        f"  Total Δpnl (material): {_format_dollars(material_sum_delta_pnl)}, "
        f"Δcount: {sum_delta_count:+d}",
        f"  All-divergent Δpnl: {_format_dollars(sum_delta_pnl)}",
        "",
        f"Top {min(top_n, len(top))} by |Δpnl|:",
    ]
    for f in top:
        lines.append(
            f"  {f.get('ticker', '?'):40s} "
            f"side={f.get('side', '?')} "
            f"local={int(f.get('local_count') or 0):>4d} "
            f"kalshi={int(f.get('kalshi_count') or 0):>4d} "
            f"Δct={int(f.get('delta_count') or 0):+d} "
            f"Δpnl={_format_dollars(int(f.get('delta_pnl_cents') or 0))}"
        )
    lines.append("")
    lines.append(
        "Written to `phantom_corrections` (one row per day per "
        "ticker+side; INSERT OR REPLACE on re-run). Dashboard PnL "
        "consumers should LEFT JOIN phantom_corrections ON "
        "(settled_trades.ticker = phantom_corrections.ticker AND "
        "settled_trades.side = phantom_corrections.side) and prefer "
        "`corrected_pnl_cents` over `local_pnl_cents`."
    )
    return "\n".join(lines)


def build_unverified_rate_alert(
    summary: Dict,
    rate_threshold: float = DEFAULT_UNVERIFIED_RATE_THRESHOLD,
) -> Optional[str]:
    """Fire a SEPARATE alert if Kalshi REST couldn't verify most audited
    tickers — closes the C1 silent-fail class.
    """
    n_audited = int(summary.get("n_audited") or 0)
    n_unverified = int(summary.get("n_unverified") or 0)
    if n_audited == 0:
        return None
    rate = n_unverified / n_audited
    if rate < rate_threshold:
        return None
    return (
        f"*PHANTOM RECONCILE VISIBILITY DEGRADED* — "
        f"Kalshi REST left {n_unverified}/{n_audited} "
        f"({rate * 100:.0f}%) (ticker, side) pairs unverified "
        f"(threshold {rate_threshold * 100:.0f}%). Phantom drift may "
        f"be accumulating silently. Check Kalshi API status + "
        f"`~/phantom_reconcile.log` for the latest run."
    )


def _load_dedup_state(sidecar_path: Path) -> Dict[str, str]:
    """Read the dedup sidecar `{dedup_key: last_fired_UTC_date}`.

    Missing / malformed file → empty state (operator may have
    deleted it intentionally to force re-alert; do not raise).
    """
    if not sidecar_path.exists():
        return {}
    try:
        with sidecar_path.open("r") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def _save_dedup_state(sidecar_path: Path, state: Dict[str, str]) -> None:
    """Atomic-replace write of the dedup sidecar."""
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
    with tmp.open("w") as fh:
        json.dump(state, fh, sort_keys=True, indent=2)
    tmp.replace(sidecar_path)


@contextlib.contextmanager
def _sidecar_lock(sidecar_path: Path) -> Iterator[None]:
    """fcntl-exclusive lock around sidecar read-modify-write (R2-N6).

    Without this, two overlapping cron firings on `_record_sent` would
    race on _load_dedup_state → mutate → _save_dedup_state and the
    later writer's atomic-replace would clobber the earlier writer's
    new key. Lock file is a sibling ``<sidecar>.lock`` so the dedup
    JSON itself stays clean (atomic-replace would replace the locked
    inode otherwise).
    """
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sidecar_path.with_suffix(sidecar_path.suffix + ".lock")
    # `O_CREAT|O_RDWR` so concurrent processes share the same lock-
    # target inode. `0o600` keeps it operator-private.
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _should_send_today(
    sidecar_path: Path, dedup_key: str, today_iso_date: str,
) -> bool:
    """R1-M1 cross-process dedup: True iff this dedup_key has NOT
    already been alerted on for today's UTC calendar date.

    The ``TelegramNotifier._dedup`` in-memory 60s window resets every
    cron tick (fresh process). Sidecar persists last-fired-date so
    24 hourly invocations within one UTC day fire AT MOST once per
    dedup_key.
    """
    state = _load_dedup_state(sidecar_path)
    return state.get(dedup_key) != today_iso_date


def _record_sent(
    sidecar_path: Path, dedup_key: str, today_iso_date: str,
) -> None:
    """R2-N6: serialize read-modify-write via fcntl.flock so overlapping
    cron firings don't drop each other's writes.
    """
    with _sidecar_lock(sidecar_path):
        state = _load_dedup_state(sidecar_path)
        state[dedup_key] = today_iso_date
        _save_dedup_state(sidecar_path, state)


def _summary_fingerprint(material: List[Dict], bucket_cents: int = 1000) -> str:
    """Stable fingerprint of material findings (R2-M1).

    Changes when ANY of: count of material findings, top-by-|Δpnl|
    ticker+side identity, total |Δpnl| crosses a $10 bucket boundary.
    The bucket-crossing rule means a small phantom inching from $4 to
    $6 won't re-fire (operator already saw the class), but a fresh
    $500 phantom landing on top WILL re-fire (sum bucket jumps from
    `b0` to `b50`).
    """
    if not material:
        return "empty"
    ranked = sorted(
        material,
        key=lambda f: abs(int(f.get("delta_pnl_cents") or 0)),
        reverse=True,
    )
    top = ranked[0]
    total_abs = sum(abs(int(f.get("delta_pnl_cents") or 0)) for f in material)
    bucket = total_abs // bucket_cents
    return (
        f"n{len(material)}"
        f"_t{top.get('ticker', '?')}"
        f"_{top.get('side', '?')}"
        f"_b{bucket}"
    )


def _unverified_fingerprint(summary: Dict) -> str:
    """Decile-bucket fingerprint for the visibility-degraded signal.

    A flake that worsens from 55% → 65% bumps the decile and re-fires;
    chronic 55% holds dedup for the calendar day.
    """
    n_audited = int(summary.get("n_audited") or 0)
    n_unverified = int(summary.get("n_unverified") or 0)
    if n_audited == 0:
        return "zero"
    rate = n_unverified / n_audited
    return f"d{int(rate * 10)}"


def _crash_fingerprint(exc: BaseException) -> str:
    """Exception-class fingerprint for the crash signal.

    A KalshiAPIError followed same-day by a DiskFullError both fire
    (different classes); two KalshiAPIErrors dedup.
    """
    return f"e{type(exc).__name__}"


def _maybe_send(
    notifier,
    message: Optional[str],
    *,
    dedup_prefix: str,
    fingerprint: str,
    today_iso_date: str,
    sidecar_path: Path,
) -> None:
    """Send synchronously via notifier respecting the (date, fingerprint)
    on-disk dedup.

    Uses ``notifier.send_sync(...)`` (R2-N1) so the sidecar record only
    lands when Telegram returned 2xx — a transient HTTP failure leaves
    the sidecar untouched and the next cron tick retries. The
    fingerprint defeats dedup when material state changes (R2-M1).

    R2-N2: when ``notifier.enabled`` is False (missing tokens), SKIP
    both the send and the sidecar record — otherwise a mid-day env fix
    would still be suppressed until tomorrow.
    """
    if message is None:
        return
    if not getattr(notifier, "enabled", True):
        return
    dedup_key = f"{dedup_prefix}_{today_iso_date}_{fingerprint}"
    if not _should_send_today(sidecar_path, dedup_key, today_iso_date):
        return
    delivered = notifier.send_sync(message, dedup_key=dedup_key)
    if delivered:
        _record_sent(sidecar_path, dedup_key, today_iso_date)


def _run_audit_safely(
    db_path: str,
    days: int,
    run_id: str,
) -> Dict:
    """Thin seam for tests to patch the entire audit invocation.

    Production callers MUST pass ``apply=True`` here; AST-pinned at
    `tests/contracts/test_phantom_reconcile_monitor.py::test_run_audit_safely_invokes_apply_true`
    so a refactor to ``apply=False`` produces alert-only-no-DB-write
    drift gets caught.
    """
    # Import lazily so unit tests can run without the audit script's
    # Kalshi-client env requirements (env-check fires at module import).
    from phantom_pnl_audit import (  # type: ignore[import-not-found]
        connect_db, load_client, run_audit,
    )

    client = load_client()
    conn = connect_db(db_path)
    try:
        return run_audit(
            conn, client,
            audit_run_id=run_id,
            days=days,
            apply=True,
            limit=None,
        )
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point. Runs the audit + dispatches alerts.

    Cron convention: ALWAYS returns 0. Alerting goes through
    Telegram, not exit code. ``Exception`` is caught + alerted;
    ``KeyboardInterrupt`` / ``SystemExit`` propagate (R1-M2 fix).
    """
    parser = argparse.ArgumentParser(
        description="Hourly phantom-fill reconcile + Telegram drift alerter.",
    )
    parser.add_argument(
        "--db", default=os.environ.get("STATE_DB_PATH", "./state.db"),
        help="Path to state.db",
    )
    parser.add_argument(
        "--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help=f"Lookback window in days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument(
        "--threshold-cents", type=int, default=DEFAULT_THRESHOLD_CENTS,
        help=f"Min |Δpnl| to alert on (default: {DEFAULT_THRESHOLD_CENTS}¢)",
    )
    parser.add_argument(
        "--dedup-sidecar",
        default=os.environ.get(
            "PHANTOM_RECONCILE_DEDUP_PATH", DEFAULT_DEDUP_SIDECAR_PATH,
        ),
        help=(
            "Path to the day-stable dedup sidecar JSON "
            f"(default: {DEFAULT_DEDUP_SIDECAR_PATH})"
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    run_id = compute_run_id(now_utc)
    today_iso = now_utc.strftime("%Y-%m-%d")
    sidecar_path = Path(args.dedup_sidecar)

    # Lazy import — sibling pattern matches collector_health_monitor.py
    # so `patch("bot.notifier.TelegramNotifier", ...)` resolves cleanly.
    from bot.notifier import TelegramNotifier
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)

    # R1-M2: catch Exception, NOT BaseException. KeyboardInterrupt and
    # SystemExit propagate so operator Ctrl-C aborts cleanly and
    # `load_client()` missing-env sys.exit(1) lands in journalctl.
    try:
        summary = _run_audit_safely(
            db_path=args.db, days=args.days, run_id=run_id,
        )
    except Exception as exc:
        tb = traceback.format_exc(limit=4)
        logging.error("phantom_reconcile auditor crashed: %s\n%s", exc, tb)
        _maybe_send(
            notifier,
            (
                f"*PHANTOM RECONCILE CRASHED* — auditor raised "
                f"`{type(exc).__name__}: {exc}` (run_id={run_id}). "
                f"Phantom corrections are NOT being written. Check "
                f"`~/phantom_reconcile.log` for full traceback."
            ),
            dedup_prefix=DEDUP_PREFIX_CRASH,
            fingerprint=_crash_fingerprint(exc),
            today_iso_date=today_iso,
            sidecar_path=sidecar_path,
        )
        return 0  # cron convention

    logging.info(
        "phantom_reconcile run_id=%s n_audited=%d n_divergent=%d "
        "n_matched=%d n_unverified=%d sum_delta_pnl=%s",
        run_id, summary.get("n_audited", 0), summary.get("n_divergent", 0),
        summary.get("n_matched", 0), summary.get("n_unverified", 0),
        _format_dollars(int(summary.get("sum_delta_pnl_cents") or 0)),
    )

    # C1 visibility alert — independent of summary alert.
    _maybe_send(
        notifier,
        build_unverified_rate_alert(summary),
        dedup_prefix=DEDUP_PREFIX_UNVERIFIED,
        fingerprint=_unverified_fingerprint(summary),
        today_iso_date=today_iso,
        sidecar_path=sidecar_path,
    )

    # M1 aggregated summary alert + R2-M1 fingerprint.
    material = [
        f for f in (summary.get("findings") or [])
        if abs(int(f.get("delta_pnl_cents") or 0)) >= args.threshold_cents
    ]
    _maybe_send(
        notifier,
        build_summary_alert(summary, threshold_cents=args.threshold_cents),
        dedup_prefix=DEDUP_PREFIX_SUMMARY,
        fingerprint=_summary_fingerprint(material),
        today_iso_date=today_iso,
        sidecar_path=sidecar_path,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
