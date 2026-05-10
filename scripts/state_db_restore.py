#!/usr/bin/env python3
"""state.db restore + verify from S3 (or any BackupStore).

Companion to state_db_s3_backup.py. Two modes:

1. `--verify-only` (default for the weekly automated check):
   - Pull the latest snapshot to a tmp dir.
   - Decompress.
   - Run PRAGMA integrity_check; expect "ok".
   - If `--baseline-from-live` given, compare row counts to the live DB
     within `--tolerance` (defaults to ±5%; allows for normal growth
     between snapshot and live).
   - Telegram alert via h4_run_with_alert.py if any check fails.
   - Restored DB is left in tmp dir for inspection; not promoted.

2. `--to PATH` (manual incident recovery):
   - Pull a specific (or latest) snapshot to PATH.
   - Refuses to overwrite without `--force`.
   - Refuses to write to live state.db (path heuristic) without
     `--allow-overwrite-live`.
   - Operator decides what to do with the restored file (replace live
     after stopping the bot, etc. — runbook spells it out).

Phase 0a per kb/decisions/autoresearch-design-may05.md.
Ticket: 86b9vd9e3.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Reuse module from sibling script.
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import state_db_s3_backup as backup  # noqa: E402


# Tables central to the autoresearch + dashboard PnL pipeline. The
# row-count + aggregate parity check uses these as the explicit
# AC-coverage table set. Schema-drift outside this set is caught by
# the full schema diff (table_set_diff) rather than this list.
CORE_TABLES = (
    "settled_trades",
    "evaluated_opportunities",
    "rejected_opportunities",
)

# Tolerance default: max(50 absolute rows, 5% pct). Round-1 finding B-M5:
# percent-only tolerance is too noisy for low-volume tables (settled_trades
# may grow by 1-2 rows/hr; sports_shadow_log may go days between rows).
# absolute-floor prevents false alerts on small tables; pct prevents
# false-negatives on big tables that genuinely diverge.
DEFAULT_TOLERANCE_PCT = 0.05
DEFAULT_TOLERANCE_ABS = 50


def _connect(db_path: Path) -> sqlite3.Connection:
    """sqlite3.connect with the bot's standard pragmas. scripts/CLAUDE.md
    requires journal_mode=WAL + busy_timeout=10000 on every connect()
    that touches state.db (Round-1 MN7)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


_DAILY_KEY_DATE_RE = re.compile(r"daily/state-db-(\d{4}-\d{2}-\d{2})\.db\.(zst|gz)$")
# Maximum age of the latest snapshot before we treat it as a stale signal.
# Round-3 finding B3-M5: weekly verify must catch the case where the
# daily backup hasn't fired in days (timer disabled, systemd hung, etc.).
# 36h is the same threshold the deferred heartbeat alerter targets and
# allows for a single missed daily without false-positive (24h cadence
# + 12h slack covers the Persistent=false catch-up edge cases).
DEFAULT_MAX_SNAPSHOT_AGE_HOURS = 36


def fetch_latest(store: backup.BackupStore, prefix: str = "daily/") -> str:
    """Return the lexicographically last key under `prefix`. Raises if
    the store is empty."""
    keys = store.list(prefix)
    if not keys:
        raise RuntimeError(f"no snapshots found in store under {prefix!r}")
    return keys[-1]  # ISO-8601 sorts lexicographically


def snapshot_age_hours(key: str, now: Optional[datetime] = None) -> Optional[float]:
    """Parse `daily/state-db-YYYY-MM-DD.db.{zst,gz}` -> hours since UTC date.

    Returns None if the key doesn't match the daily pattern (custom
    install-probe keys, manual uploads, etc.) so the caller can decide
    whether to fail or skip. Comparison is end-of-snapshot-day vs `now`
    so a snapshot uploaded at 06:00 UTC on day D returns ~0 hours when
    queried at 18:00 UTC on day D — we treat the date as the snapshot
    period, not a single timestamp.
    """
    m = _DAILY_KEY_DATE_RE.search(key)
    if not m:
        return None
    snap_date = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    # Treat the snapshot as taken at 06:00 UTC of its date (the schedule).
    snap_taken = snap_date + timedelta(hours=6)
    return (now - snap_taken).total_seconds() / 3600.0


def integrity_check(db_path: Path) -> List[str]:
    """Run full integrity_check + foreign_key_check. Returns list of
    issues (empty list == clean).

    Round-1 finding A-M2: default `PRAGMA integrity_check` truncates at
    100 errors. `integrity_check(0)` is unlimited. Also adds
    `foreign_key_check` which the default pragma omits.
    """
    issues: List[str] = []
    conn = _connect(db_path)
    try:
        # 0 = unlimited error count.
        rows = conn.execute("PRAGMA integrity_check(0)").fetchall()
        for (msg,) in rows:
            if msg != "ok":
                issues.append(f"integrity_check: {msg}")
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        for r in fk_rows:
            issues.append(f"foreign_key_check: {r}")
    finally:
        conn.close()
    return issues


def table_set(db_path: Path) -> set:
    """Return set of all user-created tables (excludes sqlite_*)."""
    conn = _connect(db_path)
    try:
        return {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        conn.close()


def row_counts(db_path: Path, tables=CORE_TABLES) -> Dict[str, int]:
    """Return {table: count}. Skips tables that don't exist."""
    conn = _connect(db_path)
    try:
        present = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        out = {}
        for t in tables:
            if t in present:
                out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    """Return set of column names for `table` (empty if missing)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {r[1] for r in rows}


def summary_aggregates(db_path: Path) -> Dict[str, Optional[int]]:
    """Return aggregate statistics that mirror the dashboard's PnL totals.

    Round-1 finding B-C2: AC #3 of the ticket says "verified restore
    produces identical row counts to live + REPRODUCES DASHBOARD TOTALS."
    Dashboard aggregates `SUM(pnl_cents - COALESCE(fee_cents, 0))` per
    `scripts/CLAUDE.md` "settled_trades.pnl_cents is GROSS, not net" rule.
    Row-count parity alone is not sufficient AC coverage — settlement
    can update fee_cents on existing rows, leaving counts identical
    while net-PnL diverges.

    Schema-defensive: if a table or column is absent (legacy DB,
    in-progress migration, test fixture), the corresponding aggregate
    is omitted from the result rather than raising.
    """
    out: Dict[str, Optional[int]] = {}
    conn = _connect(db_path)
    try:
        present = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "settled_trades" in present:
            cols = _table_columns(conn, "settled_trades")
            out["settled_trades_count"] = conn.execute(
                "SELECT COUNT(*) FROM settled_trades"
            ).fetchone()[0]
            if "pnl_cents" in cols:
                # Mirror dashboard_snapshot.py: net PnL excludes fees via
                # `SUM(pnl_cents - COALESCE(fee_cents, 0))` IF fee_cents
                # exists; otherwise just SUM(pnl_cents).
                if "fee_cents" in cols:
                    expr = "pnl_cents - COALESCE(fee_cents, 0)"
                else:
                    expr = "pnl_cents"
                out["settled_trades_net_pnl_cents"] = conn.execute(
                    f"SELECT COALESCE(SUM({expr}), 0) FROM settled_trades"
                ).fetchone()[0]
        if "evaluated_opportunities" in present:
            out["evaluated_opportunities_count"] = conn.execute(
                "SELECT COUNT(*) FROM evaluated_opportunities"
            ).fetchone()[0]
        if "rejected_opportunities" in present:
            out["rejected_opportunities_count"] = conn.execute(
                "SELECT COUNT(*) FROM rejected_opportunities"
            ).fetchone()[0]
        return out
    finally:
        conn.close()


def diff_row_counts(
    snapshot_counts: Dict[str, int],
    baseline_counts: Dict[str, int],
    tolerance_pct: float = DEFAULT_TOLERANCE_PCT,
    tolerance_abs: int = DEFAULT_TOLERANCE_ABS,
) -> list:
    """Return list of (table, snapshot_n, baseline_n, abs_diff, pct_diff)
    for tables whose drift exceeds BOTH `tolerance_abs` AND `tolerance_pct`.

    Round-1 finding B-M5: the original percent-only tolerance was too
    noisy for low-volume tables. We now flag only when BOTH thresholds
    are exceeded — i.e., abs(diff) > 50 rows AND > 5% of baseline.

    pct_diff convention: (baseline - snapshot) / max(1, baseline). A
    positive value means baseline grew since snapshot (expected — bot
    keeps writing between snapshot and verify). A negative value means
    snapshot has MORE rows than baseline (suspicious — schema truncation,
    wrong baseline DB, or restore-from-stale-snapshot).
    """
    issues = []
    for t in baseline_counts:
        snap_n = snapshot_counts.get(t)
        base_n = baseline_counts[t]
        if snap_n is None:
            issues.append((t, None, base_n, None, None))
            continue
        abs_diff = base_n - snap_n
        pct = abs_diff / max(1, base_n)
        if abs(abs_diff) > tolerance_abs and abs(pct) > tolerance_pct:
            issues.append((t, snap_n, base_n, abs_diff, pct))
    return issues


def verify_only(
    store: backup.BackupStore,
    tmp_dir: Path,
    baseline_from_live: Optional[Path],
    tolerance_pct: float,
    tolerance_abs: int,
    algorithm: str,
    key_override: Optional[str] = None,
    now: Optional[datetime] = None,
) -> int:
    """Returns 0 on pass, non-zero on fail (caller exits with this).

    Failure exit codes:
        2 = integrity_check / foreign_key_check found issues
        3 = row-count drift > tolerance OR aggregate (PnL) drift
        6 = schema-set drift (table missing in snapshot)
    """
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    key = key_override or fetch_latest(store)
    print(f"state_db_restore: latest key = {key}")

    # Round-3 B3-M5: stale-snapshot detection. If the latest daily key
    # is more than DEFAULT_MAX_SNAPSHOT_AGE_HOURS old, the nightly
    # backup hasn't been firing — partially closes the deferred
    # heartbeat-alerter (B-M3). Skip if --key was passed explicitly
    # (operator restoring an old version on purpose).
    if key_override is None:
        age = snapshot_age_hours(key, now=now)
        if age is not None and age > DEFAULT_MAX_SNAPSHOT_AGE_HOURS:
            print(
                f"state_db_restore: FAIL latest snapshot {key!r} is "
                f"{age:.1f}h old (>{DEFAULT_MAX_SNAPSHOT_AGE_HOURS}h threshold). "
                "The daily backup timer may be broken — "
                "check `systemctl status kalshi-state-db-backup.timer` "
                "and recent journalctl logs.",
                file=sys.stderr,
            )
            return 7

    ext = key.rsplit(".", 1)[-1]
    if ext not in ("zst", "gz"):
        print(f"state_db_restore: FAIL unrecognized key extension {ext!r}",
              file=sys.stderr)
        return 1
    # Auto-correct algorithm to match the key (Round-1 m2).
    expected_algorithm = "zstd" if ext == "zst" else "gzip"
    if algorithm != expected_algorithm:
        print(
            f"state_db_restore: NOTE --algorithm={algorithm} but key has "
            f".{ext} extension; using {expected_algorithm}",
            file=sys.stderr,
        )
        algorithm = expected_algorithm

    compressed = tmp_dir / Path(key).name
    decompressed = tmp_dir / Path(key).name.rsplit(f".{ext}", 1)[0]

    try:
        store.get(key, compressed)
        print(f"state_db_restore: downloaded {compressed.stat().st_size:,} bytes")
        backup.decompress(compressed, decompressed, algorithm=algorithm)
        print(f"state_db_restore: decompressed -> {decompressed}")

        # Round-1 A-M2: full integrity + foreign-key check.
        issues = integrity_check(decompressed)
        if issues:
            print(
                f"state_db_restore: FAIL {len(issues)} integrity issue(s):",
                file=sys.stderr,
            )
            for msg in issues[:20]:
                print(f"  {msg}", file=sys.stderr)
            return 2
        print("state_db_restore: integrity_check + foreign_key_check = ok")

        snap_counts = row_counts(decompressed)
        snap_aggs = summary_aggregates(decompressed)
        print(f"state_db_restore: snapshot row counts: {snap_counts}")
        print(f"state_db_restore: snapshot aggregates: {snap_aggs}")

        if baseline_from_live:
            baseline_path = Path(baseline_from_live)
            base_counts = row_counts(baseline_path)
            base_aggs = summary_aggregates(baseline_path)
            print(f"state_db_restore: live row counts:     {base_counts}")
            print(f"state_db_restore: live aggregates:     {base_aggs}")

            # Round-1 A-M3: schema-set parity. Tables present in live but
            # absent in snapshot are corruption, not normal drift.
            snap_tables = table_set(decompressed)
            base_tables = table_set(baseline_path)
            missing_in_snap = base_tables - snap_tables
            if missing_in_snap:
                print(
                    f"state_db_restore: FAIL snapshot is missing tables that "
                    f"exist in live: {sorted(missing_in_snap)}",
                    file=sys.stderr,
                )
                return 6

            # Row-count diff (now with absolute floor + percent).
            issues = diff_row_counts(
                snap_counts, base_counts,
                tolerance_pct=tolerance_pct,
                tolerance_abs=tolerance_abs,
            )
            if issues:
                print(
                    f"state_db_restore: FAIL row-count drift > "
                    f"max({tolerance_abs} rows, {tolerance_pct:.0%}):",
                    file=sys.stderr,
                )
                for t, snap_n, base_n, abs_diff, pct in issues:
                    if snap_n is None:
                        print(f"  {t}: missing from snapshot (baseline has {base_n})",
                              file=sys.stderr)
                    else:
                        print(
                            f"  {t}: snapshot={snap_n} baseline={base_n} "
                            f"drift={abs_diff:+d} ({pct:+.1%})",
                            file=sys.stderr,
                        )
                return 3
            print(
                f"state_db_restore: row counts within "
                f"max({tolerance_abs}, {tolerance_pct:.0%}) tolerance"
            )

            # Aggregate (PnL) parity. Same tolerance shape.
            agg_issues = []
            for k, base_v in base_aggs.items():
                if base_v is None:
                    continue
                snap_v = snap_aggs.get(k)
                if snap_v is None:
                    agg_issues.append(f"{k}: missing from snapshot (baseline={base_v})")
                    continue
                abs_diff = base_v - snap_v
                pct = abs_diff / max(1, abs(base_v))
                if abs(abs_diff) > tolerance_abs and abs(pct) > tolerance_pct:
                    agg_issues.append(
                        f"{k}: snapshot={snap_v} baseline={base_v} drift={abs_diff:+d} ({pct:+.1%})"
                    )
            if agg_issues:
                print(
                    f"state_db_restore: FAIL aggregate drift > "
                    f"max({tolerance_abs}, {tolerance_pct:.0%}):",
                    file=sys.stderr,
                )
                for msg in agg_issues:
                    print(f"  {msg}", file=sys.stderr)
                return 3
            print("state_db_restore: aggregate (net PnL etc.) within tolerance")
        else:
            print("state_db_restore: no --baseline-from-live; skipping row-count parity")

        return 0
    finally:
        # Keep the decompressed file for operator inspection; only delete
        # the compressed copy. Caller can pass --keep-all to preserve both.
        try:
            if compressed.exists():
                compressed.unlink()
        except OSError:
            pass


# Filenames that look like an active SQLite WAL DB. Round-1 finding A-C3:
# the original guard only refused basename == "state.db", which would let
# operators silently overwrite the WAL or SHM sidecar — which on next bot
# open either fails to start or silently applies garbage to the main DB.
_LIVE_SQLITE_BASENAMES = {
    "state.db",
    "state.db-wal",
    "state.db-shm",
    "state.db-journal",
}


def restore_to_path(
    store: backup.BackupStore,
    dst: Path,
    tmp_dir: Path,
    algorithm: str,
    key_override: Optional[str],
    force: bool,
    allow_overwrite_live: bool,
) -> int:
    dst = Path(dst).resolve()

    # Round-1 A-C3 expanded guard: refuse any of the SQLite mainfile or
    # sidecar names. Also refuse if a sidecar exists alongside `dst` —
    # that's a strong indicator dst IS an active SQLite database
    # regardless of basename.
    looks_like_live = dst.name in _LIVE_SQLITE_BASENAMES
    sidecar_exists = any(
        (dst.parent / f"{dst.name}{suffix}").exists()
        for suffix in ("-wal", "-shm", "-journal")
    )
    if (looks_like_live or sidecar_exists) and not allow_overwrite_live:
        why = "name matches live SQLite mainfile/sidecar" if looks_like_live \
              else "WAL/SHM sidecar exists alongside it"
        print(
            f"state_db_restore: refusing to write to {dst} ({why}); "
            "pass --allow-overwrite-live AND stop the bot first "
            "(systemctl stop kalshi-bot.service)",
            file=sys.stderr,
        )
        return 4

    # Round-2 R2-M-allow-overwrite-skips-sidecar: even with the
    # --allow-overwrite-live escape hatch, refuse if a stale -wal/-shm/-journal
    # exists alongside dst. A restored mainfile + stale WAL is silent
    # corruption: SQLite either fails to open OR applies the stale WAL
    # frames atop the restored snapshot, garbling the recovered data.
    # Operator must `rm` the sidecars (proves bot is stopped) first.
    if allow_overwrite_live and sidecar_exists:
        stale_files = [
            str(dst.parent / f"{dst.name}{suffix}")
            for suffix in ("-wal", "-shm", "-journal")
            if (dst.parent / f"{dst.name}{suffix}").exists()
        ]
        print(
            f"state_db_restore: refusing --allow-overwrite-live: stale SQLite "
            f"sidecars exist alongside {dst}:",
            file=sys.stderr,
        )
        for f in stale_files:
            print(f"  {f}", file=sys.stderr)
        print(
            "Stop the bot (systemctl stop kalshi-bot.service) AND "
            "rm the sidecar files first; then re-run with --allow-overwrite-live.",
            file=sys.stderr,
        )
        return 4

    # Round-3 lens-A R3 m3: race window — operator stops bot, deletes
    # sidecars, then bot is restarted (systemd Restart=on-failure or
    # manual restart by colleague), then operator runs --allow-overwrite-live.
    # Fresh bot has no sidecars yet so the prior check passes; restore
    # then unlinks the live mainfile while the bot has it open
    # (orphan-inode write loss). Defense: actively check the bot
    # service is inactive. Only fires if the dst path looks like the
    # production bot repo (heuristic — operator restoring to /tmp on
    # the Mac doesn't trip this).
    if allow_overwrite_live and "kalshi-bot-repo" in str(dst):
        try:
            cp = subprocess.run(
                ["systemctl", "is-active", "--quiet", "kalshi-bot.service"],
                capture_output=True, timeout=5,
            )
            if cp.returncode == 0:
                print(
                    "state_db_restore: refusing --allow-overwrite-live: "
                    "kalshi-bot.service is currently ACTIVE. Stop it first "
                    "(`sudo systemctl stop kalshi-bot.service`), then re-run.",
                    file=sys.stderr,
                )
                return 4
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # systemctl missing (Mac) or hung — proceed with the prior
            # checks as best-effort. Don't fail closed here because that
            # would block legitimate Mac/non-systemd recoveries.
            pass
    if dst.exists() and not force:
        print(
            f"state_db_restore: destination exists: {dst} (pass --force to overwrite)",
            file=sys.stderr,
        )
        return 5

    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    key = key_override or fetch_latest(store)
    print(f"state_db_restore: restoring key {key} -> {dst}")

    ext = key.rsplit(".", 1)[-1]
    if ext not in ("zst", "gz"):
        print(f"state_db_restore: FAIL unrecognized key extension {ext!r}",
              file=sys.stderr)
        return 1
    expected_algorithm = "zstd" if ext == "zst" else "gzip"
    if algorithm != expected_algorithm:
        algorithm = expected_algorithm  # silent autocorrect on restore-to-path
    compressed = tmp_dir / Path(key).name

    try:
        store.get(key, compressed)
        if dst.exists() and force:
            dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        backup.decompress(compressed, dst, algorithm=algorithm)
        print(f"state_db_restore: wrote {dst.stat().st_size:,} bytes -> {dst}")

        issues = integrity_check(dst)
        if issues:
            print(
                f"state_db_restore: WARNING {len(issues)} integrity issue(s):",
                file=sys.stderr,
            )
            for msg in issues[:20]:
                print(f"  {msg}", file=sys.stderr)
            return 2
        print("state_db_restore: integrity_check + foreign_key_check = ok")
        return 0
    finally:
        try:
            if compressed.exists():
                compressed.unlink()
        except OSError:
            pass


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--store", choices=("s3", "local"), default="s3")
    p.add_argument("--bucket", default=None,
                   help="S3 bucket (default: $S3_BACKUP_BUCKET)")
    p.add_argument("--rclone-remote", default=backup.DEFAULT_RCLONE_REMOTE)
    p.add_argument("--local-root", default=None, type=Path,
                   help="root dir for --store=local")
    p.add_argument("--algorithm", choices=("zstd", "gzip"),
                   default=backup.DEFAULT_ALGORITHM)
    p.add_argument("--tmp-dir", default=None, type=Path,
                   help="working dir (default: mkdtemp)")
    p.add_argument("--key", default=None,
                   help="restore a specific key (default: latest under daily/)")

    # Verify-only mode (default):
    p.add_argument("--verify-only", action="store_true", default=True,
                   help="verify the latest snapshot and exit (default)")
    p.add_argument("--baseline-from-live", default=None, type=Path,
                   help="path to live state.db for row-count + aggregate parity check")
    p.add_argument("--tolerance-pct", type=float, default=DEFAULT_TOLERANCE_PCT,
                   help=f"percent tolerance (default: {DEFAULT_TOLERANCE_PCT:.0%}). "
                        "Drift must exceed BOTH --tolerance-pct AND --tolerance-abs to fail.")
    p.add_argument("--tolerance-abs", type=int, default=DEFAULT_TOLERANCE_ABS,
                   help=f"absolute-row tolerance (default: {DEFAULT_TOLERANCE_ABS}). "
                        "Floor that prevents low-volume tables from false-alerting.")

    # Restore-to-path mode:
    p.add_argument("--to", default=None, type=Path,
                   help="restore to PATH (disables --verify-only)")
    p.add_argument("--force", action="store_true",
                   help="overwrite --to destination if it exists")
    p.add_argument("--allow-overwrite-live", action="store_true",
                   help="allow --to to write to a path named 'state.db' "
                        "(MUST stop the bot first)")

    args = p.parse_args(argv)

    # If --to was given, switch out of verify-only mode.
    if args.to is not None:
        args.verify_only = False

    # Resolve store. Reuse parent module's logic so semantics match.
    if args.store == "s3":
        bucket = args.bucket or os.environ.get("S3_BACKUP_BUCKET", "").strip()
        if not bucket:
            print("FAIL: --bucket not given and S3_BACKUP_BUCKET not set",
                  file=sys.stderr)
            return 1
        store = backup.S3RcloneStore(remote=args.rclone_remote, bucket=bucket)
    else:
        if not args.local_root:
            print("FAIL: --local-root required with --store=local", file=sys.stderr)
            return 1
        store = backup.LocalDirStore(args.local_root)

    tmp_dir = args.tmp_dir or Path(tempfile.mkdtemp(prefix="state_db_restore_"))

    try:
        if args.verify_only:
            return verify_only(
                store=store,
                tmp_dir=tmp_dir,
                baseline_from_live=args.baseline_from_live,
                tolerance_pct=args.tolerance_pct,
                tolerance_abs=args.tolerance_abs,
                algorithm=args.algorithm,
                key_override=args.key,
            )
        else:
            return restore_to_path(
                store=store,
                dst=args.to,
                tmp_dir=tmp_dir,
                algorithm=args.algorithm,
                key_override=args.key,
                force=args.force,
                allow_overwrite_live=args.allow_overwrite_live,
            )
    except Exception as e:
        print(f"state_db_restore: FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
