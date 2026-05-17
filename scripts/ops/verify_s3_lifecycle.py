#!/usr/bin/env python3
"""D1.7 — verify S3 lifecycle CLI for bronze-archive DEEP_ARCHIVE transition.

Ticket: 86b9zka3q (2026-05-17). Standalone operator-run CLI. Reads the
bucket's lifecycle configuration + lists `bronze/` objects older than the
threshold + asserts (a) the `bronze-archive` rule transitions to
DEEP_ARCHIVE at the expected day-count, AND (b) every old object is at
StorageClass=DEEP_ARCHIVE.

Why this exists: bronze day-zero was 2026-05-17. The first chunk cohort
transitions Standard → DEEP_ARCHIVE on 2026-06-17 per
`scripts/STATE_DB_BACKUP_SETUP.md` §2 `bronze-archive` rule (30d). Without
verification, lifecycle misconfig (rule missing OR wrong target class
OR live bucket diverged from the canonical template) would silently
leave chunks at STANDARD — ~23x DEEP_ARCHIVE cost. The CLI surfaces both
drift classes for the operator to remediate before they accumulate.

Operator run (after 2026-06-17):
    AWS_PROFILE=kalshi-state-db-restore \\
        python3 scripts/ops/verify_s3_lifecycle.py --bucket kalshi-bot-archive

Exit codes:
    0 — clean (lifecycle rule present + targets DEEP_ARCHIVE + all old objects DEEP_ARCHIVE)
    1 — drift. Triggered by ANY of:
        - `bronze-archive` lifecycle rule missing, disabled, or wrong target class
        - object older than threshold at non-DEEP_ARCHIVE StorageClass
        - get_bucket_lifecycle_configuration raised NoSuchLifecycleConfiguration
          (no rules at all on the bucket) — drift message will say
          'NoSuchLifecycle'
        - get_bucket_lifecycle_configuration raised AccessDenied /
          EndpointConnectionError / other infra failure — drift message
          will name the exception class so the operator triages IAM/network
          rather than 'rule missing'
        - list_objects_v2 raised — same infra-failure treatment
    64 — usage error (--bucket missing AND $KALSHI_BRONZE_BUCKET unset)
    65 — config error: boto3 not installed, OR boto3.Session/.client raised
         (e.g., ProfileNotFound when --aws-profile is not in ~/.aws/credentials).
         Triage AWS_PROFILE + region in ~/.aws/credentials, NOT the bucket.

`--dry-run` always exits 0; drift still surfaces on stderr.

Design notes (mirror `state_db_backup_heartbeat.py`):
  - boto3 is lazy-imported in `main()` so contract tests don't require it.
  - `verify()` takes a duck-typed `s3_client` so tests pass MagicMock.
  - No top-level botocore import — `Exception` catch covers
    NoSuchLifecycleConfiguration without coupling to botocore.exceptions.
  - Stdlib `logging` for INFO output; explicit `print(..., file=sys.stderr)`
    for drift surfacing (mirrors `collector_health_monitor.py`).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# Canonical lifecycle expectations per `scripts/STATE_DB_BACKUP_SETUP.md` §2
# `bronze-archive` rule (D1.5 template + D0.3 §8 operator decision).
EXPECTED_RULE_ID = "bronze-archive"
EXPECTED_PREFIX = "bronze/"
EXPECTED_TARGET_CLASS = "DEEP_ARCHIVE"
EXPECTED_TRANSITION_DAYS = 30

# S3 lifecycle transitions run on an asynchronous daily-batch schedule;
# AWS docs guarantee "completion within a reasonable time" but objects
# can lag the configured day-threshold by up to ~48h before the actual
# StorageClass header flips. Without this slack the script would
# false-positive on freshly-aged objects that haven't yet been picked
# up by the lifecycle batcher.
# https://docs.aws.amazon.com/AmazonS3/latest/userguide/lifecycle-transition-general-considerations.html
LIFECYCLE_TRANSITION_SLACK_DAYS = 2

DEFAULT_BUCKET = os.environ.get("KALSHI_BRONZE_BUCKET", "kalshi-bot-archive")
# Default = transition cutoff + S3 batcher slack. Operator can pass a
# larger value (e.g., --max-age-days=60) to scrutinize older cohorts only.
DEFAULT_MAX_AGE_DAYS = EXPECTED_TRANSITION_DAYS + LIFECYCLE_TRANSITION_SLACK_DAYS
DEFAULT_PREFIX = "bronze/"


logger = logging.getLogger("verify_s3_lifecycle")


# ── lifecycle-rule inspection ──────────────────────────────────────────


def find_bronze_rule(rules: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the rule with ID == EXPECTED_RULE_ID, or None if absent.

    The canonical 6-rule template uses `Filter.Prefix == "bronze/"` AND
    `ID == "bronze-archive"`. We match by ID — the operator should not
    rename it (the verify queries in `scripts/STATE_DB_BACKUP_SETUP.md` §12
    pin the canonical ID set).
    """
    for rule in rules:
        if rule.get("ID") == EXPECTED_RULE_ID:
            return rule
    return None


def check_lifecycle_drift(
    lifecycle_response: Optional[Dict[str, Any]],
    fetch_error: Optional[str] = None,
) -> List[str]:
    """Return a list of drift descriptions; empty list = clean.

    Drift classes detected:
      - Lifecycle response is None (caller hit NoSuchLifecycleConfiguration
        OR AccessDenied OR a network failure — the caller passes the
        exception class name in `fetch_error` so the operator can
        differentiate "rule missing" from "IAM broken").
      - `bronze-archive` rule missing.
      - `bronze-archive` rule disabled (Status != "Enabled").
      - `bronze-archive` rule has no DEEP_ARCHIVE transition at 30d.
    """
    drifts: List[str] = []
    if lifecycle_response is None:
        # Differentiate NoSuchLifecycleConfiguration (expected drift) from
        # AccessDenied / EndpointConnectionError (infra failure) — same
        # exit code (1, drift) but distinct triage paths surfaced in the
        # message body.
        if fetch_error and "NoSuchLifecycle" in fetch_error:
            cause = (
                "GetBucketLifecycleConfiguration returned "
                "NoSuchLifecycleConfiguration — the bucket has NO lifecycle "
                "rules at all. Bronze chunks will NEVER tier to DEEP_ARCHIVE."
            )
        elif fetch_error:
            cause = (
                f"GetBucketLifecycleConfiguration raised {fetch_error}. "
                "Cannot determine if lifecycle is healthy — treating as drift. "
                "Triage IAM (reader needs s3:GetLifecycleConfiguration) and "
                "network before assuming the rule is missing."
            )
        else:
            cause = (
                "Lifecycle response is None for unknown reasons (caller did "
                "not pass fetch_error)."
            )
        drifts.append(
            f"{cause} Remediation: re-run scripts/STATE_DB_BACKUP_SETUP.md §2."
        )
        return drifts

    rules = lifecycle_response.get("Rules") or []
    rule = find_bronze_rule(rules)
    if rule is None:
        present_ids = sorted(r.get("ID", "<no-id>") for r in rules)
        drifts.append(
            f"lifecycle is missing the {EXPECTED_RULE_ID!r} rule for prefix "
            f"{EXPECTED_PREFIX!r}. Present rules: {present_ids}. "
            f"See scripts/STATE_DB_BACKUP_SETUP.md §2."
        )
        return drifts

    if rule.get("Status") != "Enabled":
        drifts.append(
            f"{EXPECTED_RULE_ID!r} rule exists but Status={rule.get('Status')!r} "
            f"(must be 'Enabled')."
        )

    # Defense-in-depth: ID alone could be carried by an operator-typo
    # rule pointed at the wrong prefix (e.g., Filter.Prefix='silver/').
    # The canonical template ties ID 'bronze-archive' to
    # Filter.Prefix='bronze/'; a divergence is operator drift.
    rule_filter = rule.get("Filter") or {}
    actual_prefix = rule_filter.get("Prefix")
    # boto3 may nest the Filter under {"And": {"Prefix": ..., ...}} when
    # tags or object-size predicates are added. The canonical template
    # has neither, so the bare Filter.Prefix path is the only expected
    # shape. We surface drift on any divergence.
    if actual_prefix != EXPECTED_PREFIX:
        drifts.append(
            f"{EXPECTED_RULE_ID!r} rule Filter.Prefix={actual_prefix!r} "
            f"(expected {EXPECTED_PREFIX!r}). Rule targets the wrong "
            f"object prefix — bronze chunks under {EXPECTED_PREFIX!r} "
            f"will NOT be tiered."
        )

    transitions = rule.get("Transitions") or []
    matching = [
        t for t in transitions
        if t.get("StorageClass") == EXPECTED_TARGET_CLASS
        and t.get("Days") == EXPECTED_TRANSITION_DAYS
    ]
    if not matching:
        drifts.append(
            f"{EXPECTED_RULE_ID!r} rule does NOT transition to "
            f"{EXPECTED_TARGET_CLASS} at day {EXPECTED_TRANSITION_DAYS}. "
            f"Actual Transitions={transitions!r}."
        )
    return drifts


# ── object-state inspection ────────────────────────────────────────────


def _iter_old_objects(
    s3_client,
    bucket: str,
    prefix: str,
    max_age_days: int,
    now: Optional[datetime] = None,
):
    """Yield (Key, LastModified, StorageClass) for objects under `prefix`
    whose age exceeds `max_age_days`.

    Walks `list_objects_v2` pagination via `NextContinuationToken`.
    Uses tz-aware comparison (LastModified from boto3 is tz-aware).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    age_cutoff_seconds = max_age_days * 86400

    continuation_token: Optional[str] = None
    while True:
        kwargs: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = s3_client.list_objects_v2(**kwargs)
        contents = response.get("Contents") or []
        for obj in contents:
            last_modified = obj.get("LastModified")
            if last_modified is None:
                continue
            # If naive (test fixture using naive datetimes), treat as UTC.
            if last_modified.tzinfo is None:
                last_modified = last_modified.replace(tzinfo=timezone.utc)
            age_seconds = (now - last_modified).total_seconds()
            if age_seconds > age_cutoff_seconds:
                yield (
                    obj.get("Key", ""),
                    last_modified,
                    obj.get("StorageClass", "STANDARD"),
                )
        if not response.get("IsTruncated"):
            return
        continuation_token = response.get("NextContinuationToken")
        if not continuation_token:
            # Defensive: malformed response (IsTruncated=True but no
            # NextContinuationToken). Surface so the operator knows
            # pagination was cut short — otherwise we'd silently
            # under-count and miss object drift on later pages.
            logger.warning(
                "list_objects_v2 returned IsTruncated=True but no "
                "NextContinuationToken — pagination cut short; object "
                "drift on subsequent pages will NOT be checked."
            )
            return


def check_object_drift(
    s3_client,
    bucket: str,
    prefix: str,
    max_age_days: int,
    now: Optional[datetime] = None,
) -> List[Tuple[str, str]]:
    """Return list of (key, storage_class) for old objects NOT at
    DEEP_ARCHIVE. Empty list = clean.

    `list_objects_v2` returns `StorageClass` directly for most classes;
    DEEP_ARCHIVE objects appear with `StorageClass="DEEP_ARCHIVE"`.
    Objects originally PUT at the default (Standard) appear as
    `StorageClass="STANDARD"` until the lifecycle transition fires.
    """
    offenders: List[Tuple[str, str]] = []
    for key, _last_modified, storage_class in _iter_old_objects(
        s3_client=s3_client,
        bucket=bucket,
        prefix=prefix,
        max_age_days=max_age_days,
        now=now,
    ):
        if storage_class != EXPECTED_TARGET_CLASS:
            offenders.append((key, storage_class))
    return offenders


# ── orchestration ──────────────────────────────────────────────────────


def verify(
    s3_client,
    bucket: str,
    prefix: str = DEFAULT_PREFIX,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> int:
    """Run lifecycle + object drift checks against `s3_client`.

    Returns:
      0 — clean (or dry_run=True regardless of drift)
      1 — drift detected (only when dry_run=False)
    """
    # Lifecycle config — wrap in catch-all so NoSuchLifecycleConfiguration
    # surfaces as drift, not crash. Same posture as
    # state_db_backup_heartbeat.run_heartbeat for boto3 ClientError class.
    lifecycle_response: Optional[Dict[str, Any]]
    fetch_error: Optional[str] = None
    try:
        lifecycle_response = s3_client.get_bucket_lifecycle_configuration(Bucket=bucket)
    except Exception as exc:  # noqa: BLE001 — boto3 ClientError catch-all
        # NoSuchLifecycleConfiguration is the expected "no rules at all"
        # signal; AccessDenied/EndpointConnectionError/etc are infra
        # failures. Capture the exception class name so the drift report
        # can differentiate the two for the operator. We don't import
        # botocore (decoupling — same posture as state_db_backup_heartbeat).
        fetch_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "get_bucket_lifecycle_configuration raised %s",
            fetch_error,
        )
        lifecycle_response = None

    lifecycle_drifts = check_lifecycle_drift(lifecycle_response, fetch_error=fetch_error)

    # Object-state check — only meaningful if we can list. If list itself
    # raises (e.g., AccessDenied), surface as a separate `infra_failures`
    # category so the operator triages IAM/network, not "object at wrong
    # storage class". Synthetic-offender encoding (the R4 approach) was
    # misleading: it printed `[object] <list error>` which the operator
    # could mistake for a literal key.
    object_drifts: List[Tuple[str, str]] = []
    infra_failures: List[str] = []
    try:
        object_drifts = check_object_drift(
            s3_client=s3_client,
            bucket=bucket,
            prefix=prefix,
            max_age_days=max_age_days,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 — boto3 ClientError catch-all
        infra_msg = (
            f"list_objects_v2 raised {type(exc).__name__}: {exc}. Could not "
            f"check object storage classes. Triage IAM "
            f"(s3:ListBucket on {bucket!r}) and network before assuming "
            f"objects are correctly tiered."
        )
        logger.warning(infra_msg)
        infra_failures.append(infra_msg)

    # Report.
    total_drifts = len(lifecycle_drifts) + len(object_drifts) + len(infra_failures)
    if total_drifts == 0:
        logger.info(
            "OK: bucket=%s prefix=%s lifecycle rule %r targets %s @ %dd; "
            "no STANDARD-tier objects older than %dd.",
            bucket, prefix, EXPECTED_RULE_ID, EXPECTED_TARGET_CLASS,
            EXPECTED_TRANSITION_DAYS, max_age_days,
        )
        return 0

    # Surface drift on stderr explicitly (logger.warning could be routed
    # to stdout depending on handler config; stderr is the documented
    # CLI contract for drift visibility).
    print(
        f"verify_s3_lifecycle: DRIFT detected on bucket={bucket!r} "
        f"prefix={prefix!r} ({len(lifecycle_drifts)} lifecycle drift(s), "
        f"{len(object_drifts)} object drift(s), "
        f"{len(infra_failures)} infra failure(s))",
        file=sys.stderr,
    )
    for drift in lifecycle_drifts:
        print(f"  [lifecycle] {drift}", file=sys.stderr)
    for failure in infra_failures:
        print(f"  [infra] {failure}", file=sys.stderr)
    # Cap object drift listing so a fully-untiered bucket doesn't dump
    # 100k lines to the operator. Show first 20 + a count summary.
    for key, storage_class in object_drifts[:20]:
        print(
            f"  [object] {key} StorageClass={storage_class} (expected {EXPECTED_TARGET_CLASS})",
            file=sys.stderr,
        )
    if len(object_drifts) > 20:
        print(
            f"  [object] ... ({len(object_drifts) - 20} more offenders truncated)",
            file=sys.stderr,
        )

    if dry_run:
        print(
            "verify_s3_lifecycle: --dry-run set, exiting 0 despite drift",
            file=sys.stderr,
        )
        return 0
    return 1


# ── CLI ────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    """Exposed for the contract test so we don't have to spin up subprocess
    just to assert flag shape."""
    p = argparse.ArgumentParser(
        prog="verify_s3_lifecycle.py",
        description=(
            "Verify S3 lifecycle config tiers bronze/ chunks to DEEP_ARCHIVE "
            "after 30d, and that all old objects are at DEEP_ARCHIVE. Exits 1 "
            "on drift, 0 clean. See scripts/STATE_DB_BACKUP_SETUP.md §2."
        ),
    )
    p.add_argument(
        "--bucket",
        default=DEFAULT_BUCKET,
        help=(
            f"S3 bucket name (default: $KALSHI_BRONZE_BUCKET or "
            f"{DEFAULT_BUCKET!r})."
        ),
    )
    p.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=(
            f"S3 prefix to scan for object-state drift (default: {DEFAULT_PREFIX!r}). "
            f"NOTE: this flag varies the object-listing prefix only — the "
            f"lifecycle-rule presence check is hard-pinned to the canonical "
            f"{EXPECTED_RULE_ID!r} rule for {EXPECTED_PREFIX!r} per "
            f"STATE_DB_BACKUP_SETUP.md §2. Pass --prefix silver/ at your "
            f"own risk; the lifecycle check will still look for "
            f"{EXPECTED_RULE_ID!r}."
        ),
    )
    p.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_AGE_DAYS,
        help=(
            f"Age threshold in days; objects older than this are expected "
            f"at DEEP_ARCHIVE (default: {DEFAULT_MAX_AGE_DAYS})."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Report drift on stderr but always exit 0. Useful for cron-style "
            "polling where you don't want a non-zero exit to mail-spool the "
            "operator on every transient list_objects_v2 hiccup."
        ),
    )
    p.add_argument(
        "--aws-profile",
        default=os.environ.get("AWS_PROFILE", "kalshi-state-db-restore"),
        help=(
            "AWS named profile (default: $AWS_PROFILE or "
            "'kalshi-state-db-restore' to match the existing reader profile "
            "from STATE_DB_BACKUP_SETUP.md §6)."
        ),
    )
    p.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
        help="AWS region (default: $AWS_REGION or 'us-east-1').",
    )
    return p


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    if not args.bucket:
        print(
            "verify_s3_lifecycle: FAIL --bucket not given and KALSHI_BRONZE_BUCKET not set",
            file=sys.stderr,
        )
        return 64  # EX_USAGE

    # Lazy import — keeps contract tests boto3-free and matches the pattern
    # in state_db_backup_heartbeat.main.
    try:
        import boto3  # type: ignore
    except ImportError:
        print(
            "verify_s3_lifecycle: FAIL boto3 not installed. "
            "Install with: pip3 install --user boto3",
            file=sys.stderr,
        )
        return 65  # EX_DATAERR — config problem

    try:
        session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
        s3_client = session.client("s3")
    except Exception as exc:  # noqa: BLE001 — surface session-construct failures
        # Distinct from "drift" (rc=1) — this is heartbeat-infra failure
        # BEFORE we could check anything. Triage AWS_PROFILE in
        # ~/.aws/credentials, not the bucket lifecycle. Same posture as
        # state_db_backup_heartbeat.main session-construct catch.
        print(
            f"verify_s3_lifecycle: FAIL could not construct boto3 session "
            f"(profile={args.aws_profile!r} region={args.region!r}): "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 65  # EX_DATAERR — config problem, not drift

    return verify(
        s3_client=s3_client,
        bucket=args.bucket,
        prefix=args.prefix,
        max_age_days=args.max_age_days,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
