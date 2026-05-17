"""D1.7 — verify_s3_lifecycle CLI contract pins (ticket 86b9zka3q, 2026-05-17).

NEW `scripts/ops/verify_s3_lifecycle.py` standalone CLI for operator
post-bronze-day-zero verification. Bronze day-zero was 2026-05-17; the
first chunk cohort transitions Standard → DEEP_ARCHIVE on 2026-06-17 per
`kb/decisions/data-corpus-architecture.md` §8 + `scripts/STATE_DB_BACKUP_SETUP.md` §2
(`bronze-archive` rule, 30d).

Without this CLI, lifecycle misconfig (missing `bronze-archive` rule, or
objects somehow stuck at STANDARD past 30d) would leave bronze chunks at
~23x the cost of DEEP_ARCHIVE — silently — until the operator manually
audited S3.

Pins:
  1. The CLI module imports without crashing AND does NOT pull boto3
     into sys.modules (lazy-import-in-main contract; verified in a
     fresh subprocess so contract-test process pollution doesn't mask).
  2. Canonical-template constants (EXPECTED_RULE_ID, EXPECTED_PREFIX,
     EXPECTED_TARGET_CLASS, EXPECTED_TRANSITION_DAYS,
     LIFECYCLE_TRANSITION_SLACK_DAYS) match
     `scripts/STATE_DB_BACKUP_SETUP.md` §2 bronze-archive rule.
  3. Exits 0 on canonical lifecycle + only DEEP_ARCHIVE old objects.
  4. Exits 1 when the `bronze-archive` rule is missing.
  5. Exits 1 when the rule exists but Filter.Prefix is wrong (operator typo).
  6. Exits 1 when the rule transitions to a non-DEEP_ARCHIVE class.
  7. Exits 1 when any bronze/ object older than the threshold is at
     STANDARD storage class.
  8. Exits 0 when old objects are already DEEP_ARCHIVE (steady state).
  9. Handles `list_objects_v2` pagination (`IsTruncated`/
     `NextContinuationToken`).
 10. `--dry-run` never exits non-zero even on drift (reports only).
 11. CLI exposes `--bucket`, `--prefix`, `--max-age-days`, `--dry-run`
     with documented defaults (max-age-days = 32, i.e., transition cutoff
     + S3 batcher slack).
 12. NoSuchLifecycleConfiguration and AccessDenied are both surfaced as
     drift (exit 1) but with DIFFERENT stderr messages so the operator
     triages "rule missing" vs "IAM broken" correctly.
 13. list_objects_v2 raising surfaces as [infra] tag, NOT a synthetic
     [object] row (the R4 implementation had this bug; fixed in R5).
 14. `--help` exits 0 via subprocess (catches top-level import crashes
     the unit tests miss).

Hermetic — mocks boto3 via `unittest.mock`. No real network calls.
Pattern mirrors `tests/integration/test_state_db_backup_heartbeat.py`.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ops" / "verify_s3_lifecycle.py"


# ── canonical lifecycle fixtures ───────────────────────────────────────


def _canonical_lifecycle_response():
    """Mirror the 6-rule canonical lifecycle from
    `scripts/STATE_DB_BACKUP_SETUP.md` §2 — what `get_bucket_lifecycle_configuration`
    returns for a correctly-configured kalshi-bot-archive bucket.
    """
    return {
        "Rules": [
            {
                "ID": "tier-to-glacier-forever",
                "Status": "Enabled",
                "Filter": {"Prefix": "daily/"},
                "Transitions": [
                    {"Days": 30, "StorageClass": "GLACIER_IR"},
                    {"Days": 90, "StorageClass": "DEEP_ARCHIVE"},
                ],
            },
            {
                "ID": "journals-archive",
                "Status": "Enabled",
                "Filter": {"Prefix": "journals/"},
                "Transitions": [{"Days": 30, "StorageClass": "DEEP_ARCHIVE"}],
            },
            {
                "ID": "market-obs-archive",
                "Status": "Enabled",
                "Filter": {"Prefix": "market_obs/"},
                "Transitions": [{"Days": 0, "StorageClass": "GLACIER_IR"}],
            },
            {
                "ID": "bronze-archive",
                "Status": "Enabled",
                "Filter": {"Prefix": "bronze/"},
                "Transitions": [{"Days": 30, "StorageClass": "DEEP_ARCHIVE"}],
            },
            {
                "ID": "silver-archive",
                "Status": "Enabled",
                "Filter": {"Prefix": "silver/"},
                "Transitions": [{"Days": 90, "StorageClass": "GLACIER_IR"}],
            },
            {
                "ID": "expire-install-probes",
                "Status": "Enabled",
                "Filter": {"Prefix": "_install_check/"},
                "Expiration": {"Days": 7},
            },
        ],
    }


def _lifecycle_missing_bronze_rule():
    """Canonical 6 minus the `bronze-archive` rule — the drift the CLI must catch."""
    resp = _canonical_lifecycle_response()
    resp["Rules"] = [r for r in resp["Rules"] if r["ID"] != "bronze-archive"]
    return resp


def _list_objects_response(objects, is_truncated=False, next_token=None):
    """Build a fake list_objects_v2 response.

    `objects` is a list of (Key, LastModified, StorageClass) tuples.
    """
    contents = [
        {"Key": k, "LastModified": d, "StorageClass": s, "Size": 100}
        for (k, d, s) in objects
    ]
    out = {"Contents": contents, "KeyCount": len(contents), "IsTruncated": is_truncated}
    if next_token:
        out["NextContinuationToken"] = next_token
    return out


# ── module-level pins ──────────────────────────────────────────────────


def test_lifecycle_constants_match_canonical_template():
    """Hard-pin the canonical lifecycle constants against
    `scripts/STATE_DB_BACKUP_SETUP.md` §2 `bronze-archive` rule shape
    (D1.5 template + D0.3 §8 operator decision).

    Drift in any of these means either the canonical template moved
    (update both this file AND the docs) OR someone changed the script
    without updating the canonical template (revert).
    """
    from scripts.ops import verify_s3_lifecycle as mod
    assert mod.EXPECTED_RULE_ID == "bronze-archive"
    assert mod.EXPECTED_PREFIX == "bronze/"
    assert mod.EXPECTED_TARGET_CLASS == "DEEP_ARCHIVE"
    assert mod.EXPECTED_TRANSITION_DAYS == 30
    # Slack must be > 0 to avoid false-positives on freshly-aged objects;
    # set to 2d per AWS lifecycle latency docs.
    assert mod.LIFECYCLE_TRANSITION_SLACK_DAYS >= 1, (
        f"LIFECYCLE_TRANSITION_SLACK_DAYS must be >=1 to absorb S3 batcher "
        f"latency; got {mod.LIFECYCLE_TRANSITION_SLACK_DAYS}"
    )


def test_script_imports_no_crash():
    """`scripts/ops/verify_s3_lifecycle.py` imports cleanly + doesn't
    pull boto3 into sys.modules.

    The script MUST lazy-import boto3 inside `main()` so that:
      - Contract tests can run without boto3 installed.
      - `python3 scripts/ops/verify_s3_lifecycle.py --help` works on a
        boto3-less machine (per state_db_backup_heartbeat.py precedent).

    Enforced via subprocess so the test starts from a CLEAN sys.modules
    (the contract-test process may itself have already imported boto3
    transitively via another test, polluting an in-process check). A
    `boto3` key in sys.modules of the fresh subprocess after import-only
    means a top-level (or any pre-main()) import pulled it in.
    """
    code = (
        "import sys; "
        "import scripts.ops.verify_s3_lifecycle as mod; "
        "assert mod is not None, 'module import returned None'; "
        # Only fail if boto3 is in the fresh subprocess's sys.modules.
        # If the dev machine has boto3 installed AND the script lazy-imports,
        # boto3 should NOT be in sys.modules after a bare module import.
        "assert 'boto3' not in sys.modules, "
        "f'boto3 leaked into sys.modules on import — must be lazy in main(); "
        "found: {[m for m in sys.modules if m.startswith(\"boto3\")]!r}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=15,
    )
    assert result.returncode == 0, (
        f"Script import failed or boto3 was eagerly imported.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_cli_exposes_documented_flags():
    """argparse exposes --bucket, --prefix, --max-age-days, --dry-run."""
    from scripts.ops.verify_s3_lifecycle import build_arg_parser
    parser = build_arg_parser()
    # Parse a representative invocation to assert the flags exist.
    args = parser.parse_args(["--bucket", "test-bucket"])
    assert args.bucket == "test-bucket"
    assert args.prefix == "bronze/"
    # Default = 30 (transition cutoff) + 2 (S3 lifecycle batcher slack).
    # A bare 30d default would false-positive on freshly-aged objects.
    assert args.max_age_days == 32, (
        f"Default --max-age-days must account for ~48h S3 lifecycle "
        f"transition latency (30 + 2); got {args.max_age_days}"
    )
    assert args.dry_run is False
    # Each flag is exposed.
    args = parser.parse_args([
        "--bucket", "b",
        "--prefix", "other/",
        "--max-age-days", "60",
        "--dry-run",
    ])
    assert args.prefix == "other/"
    assert args.max_age_days == 60
    assert args.dry_run is True


# ── lifecycle-config drift detection ───────────────────────────────────


def test_script_exits_0_on_canonical_lifecycle(capsys):
    """Canonical 6-rule lifecycle + no old objects → exit 0."""
    from scripts.ops import verify_s3_lifecycle as mod
    mock_s3 = MagicMock()
    mock_s3.get_bucket_lifecycle_configuration.return_value = _canonical_lifecycle_response()
    # Only objects newer than threshold (today).
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    mock_s3.list_objects_v2.return_value = _list_objects_response([
        ("bronze/kalshi_ws/orderbook_delta/2026-05-17/chunk-001.jsonl.zst", fresh, "STANDARD"),
    ])
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=False,
    )
    assert rc == 0


def test_script_exits_1_on_missing_deep_archive_rule(capsys):
    """`bronze-archive` rule missing from lifecycle → exit 1 with stderr drift."""
    from scripts.ops import verify_s3_lifecycle as mod
    mock_s3 = MagicMock()
    mock_s3.get_bucket_lifecycle_configuration.return_value = _lifecycle_missing_bronze_rule()
    mock_s3.list_objects_v2.return_value = _list_objects_response([])
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=False,
    )
    assert rc == 1, "Missing bronze-archive rule must exit 1"
    captured = capsys.readouterr()
    # Drift must surface on stderr explicitly per the §6 CLI contract.
    assert "bronze" in captured.err.lower(), (
        f"stderr must mention the missing bronze rule for operator triage; "
        f"got stderr={captured.err!r}"
    )


def test_script_exits_1_on_rule_with_wrong_filter_prefix(capsys):
    """`bronze-archive` rule exists with correct transitions but
    Filter.Prefix='silver/' (operator typo) → exit 1. ID alone is not
    sufficient — Filter.Prefix must also pin to 'bronze/'.
    """
    from scripts.ops import verify_s3_lifecycle as mod
    bad_lifecycle = _canonical_lifecycle_response()
    for rule in bad_lifecycle["Rules"]:
        if rule["ID"] == "bronze-archive":
            rule["Filter"] = {"Prefix": "silver/"}  # operator typo
    mock_s3 = MagicMock()
    mock_s3.get_bucket_lifecycle_configuration.return_value = bad_lifecycle
    mock_s3.list_objects_v2.return_value = _list_objects_response([])
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=False,
    )
    assert rc == 1, (
        "Rule ID='bronze-archive' but Filter.Prefix='silver/' must exit 1"
    )
    captured = capsys.readouterr()
    assert "Filter.Prefix" in captured.err or "silver/" in captured.err, (
        f"stderr must surface the prefix mismatch; got {captured.err!r}"
    )


def test_script_exits_1_on_wrong_transition_storage_class(capsys):
    """`bronze-archive` rule exists but transitions to GLACIER_IR (not DEEP_ARCHIVE)
    → exit 1."""
    from scripts.ops import verify_s3_lifecycle as mod
    bad_lifecycle = _canonical_lifecycle_response()
    for rule in bad_lifecycle["Rules"]:
        if rule["ID"] == "bronze-archive":
            rule["Transitions"] = [{"Days": 30, "StorageClass": "GLACIER_IR"}]
    mock_s3 = MagicMock()
    mock_s3.get_bucket_lifecycle_configuration.return_value = bad_lifecycle
    mock_s3.list_objects_v2.return_value = _list_objects_response([])
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=False,
    )
    assert rc == 1, "Wrong target storage class must exit 1"


# ── object-state drift detection ───────────────────────────────────────


def test_script_exits_1_on_old_object_at_standard_tier(capsys):
    """Bronze object 35d old still at STANDARD → exit 1 with stderr listing."""
    from scripts.ops import verify_s3_lifecycle as mod
    mock_s3 = MagicMock()
    mock_s3.get_bucket_lifecycle_configuration.return_value = _canonical_lifecycle_response()
    old = datetime.now(timezone.utc) - timedelta(days=35)
    mock_s3.list_objects_v2.return_value = _list_objects_response([
        ("bronze/kalshi_ws/orderbook_delta/2026-04-12/chunk-001.jsonl.zst", old, "STANDARD"),
    ])
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=False,
    )
    assert rc == 1, "Old object at STANDARD tier must exit 1"
    captured = capsys.readouterr()
    assert "STANDARD" in captured.err, (
        f"stderr must surface the offending STANDARD-tier object; got {captured.err!r}"
    )


def test_script_exits_0_when_old_objects_in_deep_archive():
    """Bronze object 35d old at DEEP_ARCHIVE → exit 0 (the expected steady state)."""
    from scripts.ops import verify_s3_lifecycle as mod
    mock_s3 = MagicMock()
    mock_s3.get_bucket_lifecycle_configuration.return_value = _canonical_lifecycle_response()
    old = datetime.now(timezone.utc) - timedelta(days=35)
    mock_s3.list_objects_v2.return_value = _list_objects_response([
        ("bronze/kalshi_ws/orderbook_delta/2026-04-12/chunk-001.jsonl.zst", old, "DEEP_ARCHIVE"),
    ])
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=False,
    )
    assert rc == 0


# ── pagination ─────────────────────────────────────────────────────────


def test_script_handles_pagination():
    """`list_objects_v2` returns 2 pages → script walks both pages."""
    from scripts.ops import verify_s3_lifecycle as mod
    mock_s3 = MagicMock()
    mock_s3.get_bucket_lifecycle_configuration.return_value = _canonical_lifecycle_response()
    old = datetime.now(timezone.utc) - timedelta(days=35)
    page1 = _list_objects_response(
        [("bronze/k/2026-04-12/chunk-001.jsonl.zst", old, "DEEP_ARCHIVE")],
        is_truncated=True,
        next_token="TOKEN-PAGE2",
    )
    page2 = _list_objects_response(
        [("bronze/k/2026-04-13/chunk-001.jsonl.zst", old, "DEEP_ARCHIVE")],
        is_truncated=False,
    )
    mock_s3.list_objects_v2.side_effect = [page1, page2]
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=False,
    )
    assert rc == 0
    # Both pages must have been requested.
    assert mock_s3.list_objects_v2.call_count == 2
    # Second call must have carried the continuation token.
    second_call_kwargs = mock_s3.list_objects_v2.call_args_list[1].kwargs
    assert second_call_kwargs.get("ContinuationToken") == "TOKEN-PAGE2"


# ── dry-run posture ────────────────────────────────────────────────────


def test_dry_run_never_exits_nonzero(capsys):
    """`--dry-run` reports drift on stderr but ALWAYS exits 0."""
    from scripts.ops import verify_s3_lifecycle as mod
    mock_s3 = MagicMock()
    # Missing rule + old STANDARD-tier object — both drifts.
    mock_s3.get_bucket_lifecycle_configuration.return_value = _lifecycle_missing_bronze_rule()
    old = datetime.now(timezone.utc) - timedelta(days=35)
    mock_s3.list_objects_v2.return_value = _list_objects_response([
        ("bronze/k/2026-04-12/chunk-001.jsonl.zst", old, "STANDARD"),
    ])
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=True,
    )
    assert rc == 0, "dry-run must exit 0 even on drift (report-only contract)"
    captured = capsys.readouterr()
    # Drift still surfaces on stderr so an operator sees it.
    assert captured.err, "dry-run must still report drift to stderr"


# ── absent-lifecycle (NoSuchLifecycleConfiguration) ────────────────────


def test_script_exits_1_on_no_lifecycle_at_all(capsys):
    """Bucket has NO lifecycle config at all → boto3 raises
    NoSuchLifecycleConfiguration → script must exit 1 (worst-case drift).
    """
    from scripts.ops import verify_s3_lifecycle as mod
    mock_s3 = MagicMock()
    # Simulate the boto3 ClientError. Class NAME must contain
    # "NoSuchLifecycle" so verify() can differentiate from AccessDenied.
    class NoSuchLifecycleConfiguration(Exception):
        pass
    mock_s3.get_bucket_lifecycle_configuration.side_effect = NoSuchLifecycleConfiguration(
        "The lifecycle configuration does not exist."
    )
    mock_s3.list_objects_v2.return_value = _list_objects_response([])
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=False,
    )
    assert rc == 1, "No lifecycle config at all must exit 1 (worst drift)"
    captured = capsys.readouterr()
    # Drift message must call out NoSuchLifecycle so operator triages
    # "no rule" not "IAM broken".
    assert "NoSuchLifecycle" in captured.err, (
        f"stderr must surface NoSuchLifecycle class explicitly; got {captured.err!r}"
    )


def test_script_exits_1_on_list_objects_failure_with_infra_tag(capsys):
    """If `list_objects_v2` raises (AccessDenied / EndpointConnectionError /
    rate-limit), the script must exit 1 and surface the failure as an
    `[infra]` entry (NOT as a synthetic `[object]` row that looks like a
    weirdly-named key — that was the R4 mistake).
    """
    from scripts.ops import verify_s3_lifecycle as mod
    mock_s3 = MagicMock()
    mock_s3.get_bucket_lifecycle_configuration.return_value = _canonical_lifecycle_response()
    class AccessDenied(Exception):
        pass
    mock_s3.list_objects_v2.side_effect = AccessDenied(
        "User does not have ListBucket permission"
    )
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=False,
    )
    assert rc == 1, "list_objects_v2 raising must exit 1 (drift posture)"
    captured = capsys.readouterr()
    # Must use the [infra] tag, NOT [object] (would mislead triage to
    # "object at wrong tier" when the real issue is IAM).
    assert "[infra]" in captured.err, (
        f"List failure must surface as [infra] tag for correct triage; "
        f"got {captured.err!r}"
    )
    # Specifically must NOT print `[object] <list_objects_v2 error: ...>`
    # which the R4 implementation did.
    assert "<list_objects_v2 error" not in captured.err, (
        f"Synthetic-key encoding leaked into output: {captured.err!r}"
    )


def test_script_exits_1_on_access_denied_distinct_from_no_lifecycle(capsys):
    """AccessDenied on get_bucket_lifecycle_configuration is an INFRA
    failure distinct from NoSuchLifecycleConfiguration — the script
    treats both as drift (exit 1) but the stderr message must
    differentiate so the operator triages IAM, not 'rule missing'.
    """
    from scripts.ops import verify_s3_lifecycle as mod
    mock_s3 = MagicMock()
    class AccessDenied(Exception):
        pass
    mock_s3.get_bucket_lifecycle_configuration.side_effect = AccessDenied(
        "User does not have GetLifecycleConfiguration permission"
    )
    mock_s3.list_objects_v2.return_value = _list_objects_response([])
    rc = mod.verify(
        s3_client=mock_s3,
        bucket="test-bucket",
        prefix="bronze/",
        max_age_days=30,
        dry_run=False,
    )
    assert rc == 1
    captured = capsys.readouterr()
    # Must mention IAM triage path, NOT "rule is missing" (the latter
    # would mislead the operator).
    assert "IAM" in captured.err or "AccessDenied" in captured.err, (
        f"stderr must point at IAM/AccessDenied for triage; got {captured.err!r}"
    )


# ── CLI smoke: `python3 scripts/ops/verify_s3_lifecycle.py --help` ─────


def test_cli_help_succeeds_via_subprocess():
    """`python3 scripts/ops/verify_s3_lifecycle.py --help` exits 0 and
    prints usage. Catches top-level import crashes the unit tests would miss.
    """
    assert SCRIPT_PATH.exists(), f"Expected script at {SCRIPT_PATH}"
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"--help should exit 0; got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "--bucket" in result.stdout
    assert "--max-age-days" in result.stdout
