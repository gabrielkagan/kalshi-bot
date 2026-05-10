#!/usr/bin/env python3
"""state.db backup heartbeat alerter — Mac-side, no-upload-in-36h.

Closes the deferred B-M3 silent-failure gap documented in
`scripts/STATE_DB_BACKUP_SETUP.md` §10 #1. The Phase 0a backup chain
(`state_db_s3_backup.py` + systemd timer + `h4_run_with_alert.py`)
covers exit-code failures of the daily run, but NOT the case where the
timer itself is hung, disabled, or its unit file rejected:

  - `kalshi-state-db-backup.timer` disabled by an `apt upgrade` postinst
  - systemd hung after a kernel pid-namespace bug
  - unit file has a syntax error after a manual edit
  - VPS rebooted and the timer didn't re-enable
  - the entire VPS is down

In all of these, the wrapper never runs, no exit code ever fires, no
Telegram alert ever sends. The existing weekly verify
(`state_db_restore.py --verify-only`) catches it eventually via the
36h-stale check (B3-M5, see kb/decisions/auto-research-phase-0a-shipped-may09.md)
— but that's a 7-day worst-case detection window. This script closes
that to 6h.

Architectural decision: option (b) — Mac-side cron.

The ticket presented two options:
  (a) New read-only IAM on VPS — requires AWS console work, breaks
      the IAM-paranoia model where the VPS holds writer-only creds.
  (b) Mac-side cron — different deployment infra, decoupled from VPS
      health.

Picked (b) because:
  - No new IAM creation (matches the existing `kalshi-state-db-restore`
    AWS profile on the dev Mac).
  - Decoupled observability: the heartbeat survives the VPS being
    down. Option (a) would have the same outage eat both the backup
    AND the heartbeat — defeating the purpose.
  - Existing reader creds on Mac (set up in
    `STATE_DB_BACKUP_SETUP.md` §6) are sufficient. The script uses
    boto3's default credential chain so it picks up
    `~/.aws/credentials [kalshi-state-db-restore]` via
    `AWS_PROFILE=kalshi-state-db-restore` env var in the launchd plist.

Trade-off: heartbeat doesn't fire when the Mac is asleep / off. Macs
sleep aggressively; launchd schedules the job for the next wake. In
practice an operator who isn't using their Mac for 7+ days has bigger
problems than a missed heartbeat. If VPS-side observability is later
desired, file a separate ticket for option (a).

Decoupling guarantee: this script does NOT import
`state_db_s3_backup` or `state_db_restore`. If those modules have a
bug, the heartbeat still fires. Enforced by
test_main_does_not_depend_on_backup_module.

Ticket: 86b9vgjxw.
Parent: 86b9vd9e3 (Phase 0a).
KB: kb/decisions/phase0a-fu-backup-heartbeat-shipped-may10.md.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional


# 36h matches the weekly-verify B3-M5 stale-key threshold. Reasoning:
# the daily backup runs at 06:00 UTC. A 24h cadence + 12h slack covers
# a single missed run (e.g., systemd's Persistent=false catch-up edge,
# or one transient S3 outage). Two misses in a row WILL trip the alert.
DEFAULT_MAX_SNAPSHOT_AGE_HOURS = 36

DEFAULT_PREFIX = "daily/"

# Pattern from state_db_s3_backup.compute_object_key. Kept in sync via
# the test (test_parses_iso_date_zst pins the format). Vendored — NOT
# imported — to preserve the decoupling guarantee above.
_DAILY_KEY_DATE_RE = re.compile(
    r"daily/state-db-(\d{4}-\d{2}-\d{2})\.db\.(zst|gz)$"
)


# ── snapshot key parsing (vendored from state_db_restore) ──────────────


def snapshot_age_hours(key: str, now: Optional[datetime] = None) -> Optional[float]:
    """Parse `daily/state-db-YYYY-MM-DD.db.{zst,gz}` -> hours since
    06:00 UTC of that date (the schedule).

    Returns None if the key doesn't match the daily pattern — caller
    decides whether to fail or skip.

    Vendored (not imported) from `state_db_restore.snapshot_age_hours`
    to preserve heartbeat decoupling per the architectural decision above.
    The 1-line regex doesn't justify the import coupling.
    """
    if not key:
        return None
    m = _DAILY_KEY_DATE_RE.search(key)
    if not m:
        return None
    snap_date = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    # Treat the snapshot as taken at 06:00 UTC of its date (the cron).
    snap_taken = snap_date + timedelta(hours=6)
    return (now - snap_taken).total_seconds() / 3600.0


# ── core check ─────────────────────────────────────────────────────────


@dataclasses.dataclass
class HeartbeatResult:
    """status:
      - 'ok'           latest snapshot age <= max_age_hours
      - 'stale'        latest snapshot age > max_age_hours
      - 'empty'        no keys under the daily/ prefix
      - 'unparseable'  latest key doesn't match daily/state-db-* pattern
    """
    status: str
    key: Optional[str]
    age_hours: Optional[float]
    message: str


def check_latest_snapshot(
    s3_client,
    bucket: str,
    prefix: str = DEFAULT_PREFIX,
    max_age_hours: float = DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
    now: Optional[datetime] = None,
) -> HeartbeatResult:
    """List `s3://bucket/prefix`, find the lexicographically-last key,
    compute its age, classify into HeartbeatResult.

    `s3_client` is duck-typed — anything with `list_objects_v2(...)`
    returning the boto3 shape. Tests pass a MagicMock; main() passes
    a real `boto3.client('s3')`.

    Uses list_objects_v2 directly (not the paginator) because we only
    need the lex-last key. S3 returns keys in UTF-8 binary order, so
    even when truncated past 1000 keys, the last page contains the
    largest keys — but to be future-proof we follow the
    `NextContinuationToken` to the end. At ~365 keys/year this won't
    matter until ~2030; defensive anyway.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Walk all pages — defensive even though we currently fit in one.
    all_keys = []
    continuation_token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = s3_client.list_objects_v2(**kwargs)
        contents = response.get("Contents") or []
        all_keys.extend(obj["Key"] for obj in contents)
        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")
        if not continuation_token:
            break  # defensive: malformed response

    if not all_keys:
        return HeartbeatResult(
            status="empty",
            key=None,
            age_hours=None,
            message=(
                f"S3 bucket {bucket!r} has NO snapshots under prefix "
                f"{prefix!r}. Either the bucket is brand-new (first daily "
                "backup hasn't fired yet) OR every snapshot has been "
                "deleted. Check: `aws s3 ls s3://" + bucket + "/" + prefix + "`."
            ),
        )

    # S3 returns keys sorted, but mocks in tests may not — sort defensively.
    latest_key = sorted(all_keys)[-1]
    age = snapshot_age_hours(latest_key, now=now)

    if age is None:
        return HeartbeatResult(
            status="unparseable",
            key=latest_key,
            age_hours=None,
            message=(
                f"Latest key in s3://{bucket}/{prefix} is {latest_key!r}, "
                "which does not match the expected pattern "
                "'daily/state-db-YYYY-MM-DD.db.{zst,gz}'. A manual upload "
                "may have sorted after the real daily keys. Verify via "
                f"`aws s3 ls s3://{bucket}/{prefix}` and clean up."
            ),
        )

    if age > max_age_hours:
        days = age / 24.0
        return HeartbeatResult(
            status="stale",
            key=latest_key,
            age_hours=age,
            message=(
                f"state.db backup is STALE: latest snapshot {latest_key!r} "
                f"is {age:.1f}h old ({days:.1f} days) — threshold is "
                f"{max_age_hours}h. The daily backup timer on the VPS may "
                "be broken. Check on the VPS:\n"
                "  systemctl status kalshi-state-db-backup.timer\n"
                "  systemctl status kalshi-state-db-backup.service\n"
                "  journalctl -u kalshi-state-db-backup.service -n 200\n"
                f"S3 bucket: {bucket}"
            ),
        )

    return HeartbeatResult(
        status="ok",
        key=latest_key,
        age_hours=age,
        message=f"OK: latest snapshot {latest_key!r} is {age:.1f}h old (threshold {max_age_hours}h).",
    )


# ── alerting ───────────────────────────────────────────────────────────


def send_telegram_alert(message: str) -> bool:
    """Post `message` to Telegram. Returns True on success.

    Mirrors `h4_run_with_alert.send_telegram_alert` and
    `doc_drift_check.send_telegram`: stdlib `urllib.request` (no
    `requests` dep), graceful skip when env creds missing, swallow
    network errors so alert failure doesn't compound the underlying
    issue.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print(
            "backup_heartbeat: TELEGRAM_BOT_TOKEN/CHAT_ID not set; skipping alert",
            file=sys.stderr,
        )
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat_id,
        # Telegram caps at 4096 chars; truncate defensively.
        "text": message[:4096],
    }).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as e:  # noqa: BLE001 — alert path must not raise
        print(f"backup_heartbeat: telegram POST failed: {e}", file=sys.stderr)
        return False


# ── orchestration ──────────────────────────────────────────────────────


def run_heartbeat(
    s3_client,
    bucket: str,
    prefix: str = DEFAULT_PREFIX,
    max_age_hours: float = DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
    now: Optional[datetime] = None,
) -> int:
    """Returns 0 on healthy ('ok'), non-zero on any alert-firing state.

    Exit codes (so a wrapping cron-on-failure mailer can differentiate):
      0 — ok
      1 — stale (latest snapshot too old)
      2 — empty (no snapshots under prefix)
      3 — unparseable (latest key doesn't match pattern)

    Side effects: prints status to stdout; on non-zero, fires
    `send_telegram_alert`. Alert failures are non-fatal (env var
    missing OR network error) — the return code still reflects the
    snapshot health.
    """
    result = check_latest_snapshot(
        s3_client=s3_client,
        bucket=bucket,
        prefix=prefix,
        max_age_hours=max_age_hours,
        now=now,
    )

    # Always print the result line for the cron log.
    print(
        f"backup_heartbeat: status={result.status} "
        f"key={result.key!r} age_hours={result.age_hours} "
        f"bucket={bucket!r}"
    )

    if result.status == "ok":
        return 0

    # Alert. Use a slightly more compact message for the actual Telegram
    # body so it fits in one notification.
    send_telegram_alert(f"*state.db backup heartbeat*\n\n{result.message}")
    return {"stale": 1, "empty": 2, "unparseable": 3}.get(result.status, 1)


# ── CLI ────────────────────────────────────────────────────────────────


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Mac-side heartbeat: alert if state.db backup hasn't uploaded in 36h.",
    )
    p.add_argument(
        "--bucket", default=os.environ.get("S3_BACKUP_BUCKET", "").strip() or None,
        help="S3 bucket name (default: $S3_BACKUP_BUCKET).",
    )
    p.add_argument(
        "--prefix", default=DEFAULT_PREFIX,
        help=f"S3 prefix to scan (default: {DEFAULT_PREFIX!r}).",
    )
    p.add_argument(
        "--max-age-hours", type=float, default=DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
        help=f"Stale threshold in hours (default: {DEFAULT_MAX_SNAPSHOT_AGE_HOURS}).",
    )
    p.add_argument(
        "--region", default=os.environ.get("S3_BACKUP_REGION", "us-east-1"),
        help="AWS region (default: $S3_BACKUP_REGION or us-east-1).",
    )
    p.add_argument(
        "--aws-profile", default=os.environ.get("AWS_PROFILE", "kalshi-state-db-restore"),
        help="AWS named profile (default: $AWS_PROFILE or 'kalshi-state-db-restore').",
    )
    args = p.parse_args(argv)

    if not args.bucket:
        print(
            "backup_heartbeat: FAIL --bucket not given and S3_BACKUP_BUCKET not set",
            file=sys.stderr,
        )
        return 64  # EX_USAGE

    # Lazy import so unit tests that monkeypatch a mock s3_client don't
    # require boto3 installed. Production install path: `pip3 install
    # boto3` (or pipx, or system Python). See operator install steps in
    # scripts/STATE_DB_BACKUP_SETUP.md §11.
    try:
        import boto3  # type: ignore
    except ImportError:
        print(
            "backup_heartbeat: FAIL boto3 not installed. "
            "Install with: pip3 install --user boto3",
            file=sys.stderr,
        )
        return 65  # EX_DATAERR — config problem, not the bot's

    session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
    s3_client = session.client("s3")

    return run_heartbeat(
        s3_client=s3_client,
        bucket=args.bucket,
        prefix=args.prefix,
        max_age_hours=args.max_age_hours,
    )


if __name__ == "__main__":
    sys.exit(main())
