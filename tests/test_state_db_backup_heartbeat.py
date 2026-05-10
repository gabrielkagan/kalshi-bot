"""Tests for scripts/state_db_backup_heartbeat.py — Phase 0a follow-up.

Closes the deferred B-M3 silent-failure gap (`STATE_DB_BACKUP_SETUP.md` §10):
the systemd timer + h4_run_with_alert wrapper covers exit-code failures
but NOT the case where the timer itself is hung / disabled / unit-file
syntax error rejected → zero invocations, zero alerts, 6 days of silence
before the weekly verify catches it.

Heartbeat design (Mac-side cron):
  - Run every 6h via launchd/cron.
  - List `daily/` prefix in S3 via boto3 (read-only creds in ~/.aws).
  - Find the lexicographically last key, parse the embedded ISO date.
  - If `now_utc - snapshot_taken_utc > 36h`, send Telegram alert.
  - Otherwise exit 0 silently (operator only hears about failure).

Why Mac-side (not VPS): chosen architecture option (b) per ticket. The
heartbeat must survive VPS being down (otherwise the alert it'd send
would be eaten by the same outage). Mac has admin AWS creds locally;
read-only IAM on the VPS would require AWS console work and break the
existing IAM-paranoia model (writer-only on VPS).

Hermetic — mocks boto3 + Telegram via urllib monkeypatch. No real
network calls. Pattern mirrors test_state_db_restore.py.

Ticket: 86b9vgjxw.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


@pytest.fixture
def heartbeat_module():
    """Import the script as a module. Lazy so collection doesn't fail
    if boto3 is unavailable (heartbeat imports it lazily inside main())."""
    import state_db_backup_heartbeat as mod
    importlib.reload(mod)  # fresh module per test (no stale module state)
    return mod


def _fake_list_objects_v2(keys_and_dates):
    """Build a fake boto3 list_objects_v2 response.

    `keys_and_dates` is a list of (key, modified_dt) tuples. Returns a
    callable matching the boto3 paginator signature.
    """
    contents = [
        {"Key": k, "LastModified": d, "Size": 100} for (k, d) in keys_and_dates
    ]
    return {"Contents": contents, "KeyCount": len(contents), "IsTruncated": False}


# ── parsing ───────────────────────────────────────────────────────────


class TestSnapshotAgeHours:
    """Parse `daily/state-db-YYYY-MM-DD.db.{zst,gz}` -> hours since 06:00 UTC.

    Vendored from state_db_restore.snapshot_age_hours; the heartbeat MUST
    be decoupled from the backup chain (per ticket: "Heartbeat must NOT
    depend on the backup timer being healthy") so we don't import it."""

    def test_parses_iso_date_zst(self, heartbeat_module):
        # Snapshot 2026-05-09 (taken at 06:00 UTC); query 2026-05-10 18:00 UTC.
        # 24h + 12h = 36h.
        now = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
        age = heartbeat_module.snapshot_age_hours(
            "daily/state-db-2026-05-09.db.zst", now=now
        )
        assert age == pytest.approx(36.0)

    def test_parses_iso_date_gz(self, heartbeat_module):
        now = datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)
        age = heartbeat_module.snapshot_age_hours(
            "daily/state-db-2026-05-09.db.gz", now=now
        )
        assert age == pytest.approx(24.0)

    def test_returns_none_for_unrecognized(self, heartbeat_module):
        assert heartbeat_module.snapshot_age_hours("daily/garbage.zst") is None
        assert heartbeat_module.snapshot_age_hours("_install_check/probe.txt") is None
        assert heartbeat_module.snapshot_age_hours("") is None


# ── core check ─────────────────────────────────────────────────────────


class TestCheckLatestSnapshot:
    """Pure-function check given a (mocked) s3_client.

    Returns (status, key, age_hours, message). status is one of:
      - 'ok' (latest snapshot <= 36h)
      - 'stale' (latest snapshot > 36h)
      - 'empty' (no daily/ keys at all)
      - 'unparseable' (latest key doesn't match daily pattern — manual upload,
        install probe, etc.)
    """

    def test_fresh_snapshot_returns_ok(self, heartbeat_module):
        s3 = MagicMock()
        # Latest snapshot was yesterday (~24h old at query time).
        s3.list_objects_v2.return_value = _fake_list_objects_v2([
            ("daily/state-db-2026-05-08.db.zst", datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)),
            ("daily/state-db-2026-05-09.db.zst", datetime(2026, 5, 9, 6, 0, tzinfo=timezone.utc)),
        ])
        now = datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)  # 24h after latest
        result = heartbeat_module.check_latest_snapshot(
            s3, bucket="kalshi-test", now=now,
        )
        assert result.status == "ok"
        assert result.key == "daily/state-db-2026-05-09.db.zst"
        assert result.age_hours == pytest.approx(24.0)

    def test_stale_snapshot_48h_returns_stale(self, heartbeat_module):
        """The headline acceptance test: simulate a 48h-old snapshot
        (timer hung 2 days), verify check_latest_snapshot reports stale."""
        s3 = MagicMock()
        # Latest key is 2026-05-08; query at 2026-05-10 06:00 = 48h.
        s3.list_objects_v2.return_value = _fake_list_objects_v2([
            ("daily/state-db-2026-05-08.db.zst", datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)),
        ])
        now = datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)
        result = heartbeat_module.check_latest_snapshot(
            s3, bucket="kalshi-test", now=now,
        )
        assert result.status == "stale"
        assert result.key == "daily/state-db-2026-05-08.db.zst"
        assert result.age_hours == pytest.approx(48.0)
        assert "stale" in result.message.lower() or "old" in result.message.lower()

    def test_just_under_36h_threshold_returns_ok(self, heartbeat_module):
        """Boundary: 35h59m must be 'ok', not 'stale'."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = _fake_list_objects_v2([
            ("daily/state-db-2026-05-09.db.zst", datetime(2026, 5, 9, 6, 0, tzinfo=timezone.utc)),
        ])
        # 06:00 + 35:59 = 17:59 the next day
        now = datetime(2026, 5, 10, 17, 59, tzinfo=timezone.utc)
        result = heartbeat_module.check_latest_snapshot(
            s3, bucket="kalshi-test", now=now,
        )
        assert result.status == "ok"

    def test_just_over_36h_threshold_returns_stale(self, heartbeat_module):
        """Boundary: 36h01m must be 'stale'."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = _fake_list_objects_v2([
            ("daily/state-db-2026-05-09.db.zst", datetime(2026, 5, 9, 6, 0, tzinfo=timezone.utc)),
        ])
        now = datetime(2026, 5, 10, 18, 1, tzinfo=timezone.utc)
        result = heartbeat_module.check_latest_snapshot(
            s3, bucket="kalshi-test", now=now,
        )
        assert result.status == "stale"

    def test_empty_bucket_returns_empty(self, heartbeat_module):
        """Brand-new bucket / catastrophic deletion — no snapshots at all."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"Contents": [], "KeyCount": 0, "IsTruncated": False}
        now = datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)
        result = heartbeat_module.check_latest_snapshot(
            s3, bucket="kalshi-test", now=now,
        )
        assert result.status == "empty"
        assert result.key is None
        assert result.age_hours is None

    def test_list_response_without_contents_key_returns_empty(self, heartbeat_module):
        """boto3 omits 'Contents' entirely when the prefix is empty.
        Don't KeyError — treat as empty."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"KeyCount": 0, "IsTruncated": False}
        now = datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)
        result = heartbeat_module.check_latest_snapshot(
            s3, bucket="kalshi-test", now=now,
        )
        assert result.status == "empty"

    def test_unparseable_latest_returns_unparseable(self, heartbeat_module):
        """If the lexicographically-last key doesn't match the daily/state-db-*
        pattern, we can't compute age. Treat as 'unparseable' so the alert
        message tells the operator to investigate manually rather than
        silently returning ok."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = _fake_list_objects_v2([
            ("daily/manual-upload-debug.db.zst", datetime(2026, 5, 9, 6, 0, tzinfo=timezone.utc)),
        ])
        now = datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)
        result = heartbeat_module.check_latest_snapshot(
            s3, bucket="kalshi-test", now=now,
        )
        assert result.status == "unparseable"

    def test_pagination_uses_lexicographic_last(self, heartbeat_module):
        """The script uses a paginator (S3 pages at 1000 keys). After 90d
        of daily snapshots there are 90 keys — well under one page — but
        the implementation MUST use the paginator so it doesn't silently
        break at the 1000-key boundary in year 3.

        We don't enforce paginator-vs-list_objects_v2 in tests (impl detail);
        we just verify the lex-last key wins, which both APIs guarantee
        when the response is sorted (S3 list responses are key-sorted).
        """
        s3 = MagicMock()
        # Intentionally unsorted in the mock response; the impl must sort
        # OR use the documented S3 ordering. Either is fine.
        s3.list_objects_v2.return_value = _fake_list_objects_v2([
            ("daily/state-db-2026-05-07.db.zst", datetime(2026, 5, 7, 6, 0, tzinfo=timezone.utc)),
            ("daily/state-db-2026-05-09.db.zst", datetime(2026, 5, 9, 6, 0, tzinfo=timezone.utc)),
            ("daily/state-db-2026-05-08.db.zst", datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)),
        ])
        now = datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc)
        result = heartbeat_module.check_latest_snapshot(
            s3, bucket="kalshi-test", now=now,
        )
        assert result.key == "daily/state-db-2026-05-09.db.zst"


# ── alerting ───────────────────────────────────────────────────────────


class TestTelegramAlert:
    """The alert helper must mirror h4_run_with_alert / doc_drift_check:
    urllib.request.urlopen (stdlib), no `requests` dep, fail silently if
    env creds absent."""

    def test_alert_sends_via_urllib(self, heartbeat_module, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append((req.full_url, req.data, timeout))
            response = MagicMock()
            response.__enter__ = MagicMock(return_value=response)
            response.__exit__ = MagicMock(return_value=False)
            return response

        monkeypatch.setattr(
            heartbeat_module.urllib.request, "urlopen", fake_urlopen,
        )
        ok = heartbeat_module.send_telegram_alert("stale snapshot test message")
        assert ok is True
        assert len(calls) == 1
        url, data, timeout = calls[0]
        assert "api.telegram.org" in url
        assert "fake-token" in url
        assert b"stale snapshot test message" in data
        assert b"fake-chat" in data

    def test_alert_skips_when_creds_missing(self, heartbeat_module, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        # No urlopen mock — would explode if called.
        ok = heartbeat_module.send_telegram_alert("would-not-send")
        assert ok is False

    def test_alert_returns_false_on_network_error(self, heartbeat_module, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "fake-chat")

        def boom(req, timeout=None):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(
            heartbeat_module.urllib.request, "urlopen", boom,
        )
        # MUST NOT raise — alert failure must not compound the situation.
        ok = heartbeat_module.send_telegram_alert("test")
        assert ok is False


# ── main() integration ────────────────────────────────────────────────


class TestMain:
    """End-to-end: argparse → boto3 mock → check_latest_snapshot →
    alert-or-silent. The headline AC test ('48h-old key triggers alert')
    lives here."""

    def test_main_fresh_snapshot_no_alert(self, heartbeat_module, monkeypatch):
        """24h-old snapshot — exit 0, no Telegram POST."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = _fake_list_objects_v2([
            ("daily/state-db-2026-05-09.db.zst", datetime(2026, 5, 9, 6, 0, tzinfo=timezone.utc)),
        ])

        alerts = []
        monkeypatch.setattr(
            heartbeat_module, "send_telegram_alert",
            lambda msg: alerts.append(msg) or True,
        )

        # Inject the s3 client + frozen `now` via the testable entrypoint.
        rc = heartbeat_module.run_heartbeat(
            s3_client=s3,
            bucket="kalshi-test",
            now=datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc),
        )
        assert rc == 0
        assert alerts == []

    def test_main_stale_48h_fires_alert(self, heartbeat_module, monkeypatch):
        """THE AC — simulated 48h-old key (mock boto3 list_objects),
        verify alert fires."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = _fake_list_objects_v2([
            ("daily/state-db-2026-05-08.db.zst", datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)),
        ])

        alerts = []
        monkeypatch.setattr(
            heartbeat_module, "send_telegram_alert",
            lambda msg: alerts.append(msg) or True,
        )

        rc = heartbeat_module.run_heartbeat(
            s3_client=s3,
            bucket="kalshi-test",
            now=datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc),
        )
        assert rc != 0  # non-zero so cron sees the failure
        assert len(alerts) == 1
        msg = alerts[0]
        # Operator-actionable: must include the age + the latest key + the bucket.
        assert "48" in msg or "2.0 day" in msg.lower() or "stale" in msg.lower()
        assert "2026-05-08" in msg
        assert "kalshi-test" in msg

    def test_main_empty_bucket_fires_alert(self, heartbeat_module, monkeypatch):
        """Empty bucket — also a failure mode. Brand-new bucket or
        catastrophic deletion."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = {"Contents": [], "KeyCount": 0}

        alerts = []
        monkeypatch.setattr(
            heartbeat_module, "send_telegram_alert",
            lambda msg: alerts.append(msg) or True,
        )

        rc = heartbeat_module.run_heartbeat(
            s3_client=s3,
            bucket="kalshi-test",
            now=datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc),
        )
        assert rc != 0
        assert len(alerts) == 1
        assert "empty" in alerts[0].lower() or "no snapshots" in alerts[0].lower()

    def test_main_unparseable_latest_fires_alert(self, heartbeat_module, monkeypatch):
        """The lex-last key doesn't match the pattern. Could mean someone
        uploaded a manual debug snapshot that sorts AFTER all daily keys
        (e.g., 'daily/zzz-debug.zst'). Operator should know."""
        s3 = MagicMock()
        s3.list_objects_v2.return_value = _fake_list_objects_v2([
            ("daily/state-db-2026-05-09.db.zst", datetime(2026, 5, 9, 6, 0, tzinfo=timezone.utc)),
            ("daily/zzz-debug-upload.zst", datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)),
        ])

        alerts = []
        monkeypatch.setattr(
            heartbeat_module, "send_telegram_alert",
            lambda msg: alerts.append(msg) or True,
        )

        rc = heartbeat_module.run_heartbeat(
            s3_client=s3,
            bucket="kalshi-test",
            now=datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc),
        )
        assert rc != 0
        assert len(alerts) == 1

    # ── R1 C1: S3 exception handling ────────────────────────────────

    def test_main_s3_credentials_error_fires_infra_alert(self, heartbeat_module, monkeypatch):
        """R1 C1: simulate creds expired / missing.

        Pre-fix: list_objects_v2 raises NoCredentialsError → propagates
        out of run_heartbeat → LaunchAgent silently dies, only the
        traceback in ~/Library/Logs/...err.log. Operator never paged.

        Post-fix: caught + alerted + rc=4. Message prefix MUST identify
        this as a heartbeat-INFRA error so the operator triages AWS
        creds (Mac) rather than the VPS systemd timer.
        """
        s3 = MagicMock()

        # Use generic Exception subclasses to avoid coupling tests to
        # botocore (the production code catches Exception, so any
        # subclass exercises the same path). NoCredentialsError shape:
        class _FakeNoCredentialsError(Exception):
            """Stand-in for botocore.exceptions.NoCredentialsError."""

        s3.list_objects_v2.side_effect = _FakeNoCredentialsError(
            "Unable to locate credentials"
        )

        alerts = []
        monkeypatch.setattr(
            heartbeat_module, "send_telegram_alert",
            lambda msg: alerts.append(msg) or True,
        )

        rc = heartbeat_module.run_heartbeat(
            s3_client=s3,
            bucket="kalshi-test",
            now=datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc),
        )
        # rc=4 specifically — distinct from 1/2/3 (which map to
        # backup-staleness modes). 4 means heartbeat-infra failure.
        assert rc == 4, f"expected rc=4 (heartbeat-infra), got {rc}"
        assert len(alerts) == 1, f"expected 1 alert, got {len(alerts)}"
        msg = alerts[0]
        # The message MUST identify the failure class so the operator
        # knows where to triage (Mac AWS creds vs VPS systemd).
        assert "HEARTBEAT-INFRA" in msg or "heartbeat-infra" in msg.lower()
        # The exception class name MUST appear (so operator knows it's
        # a creds problem, not a network problem).
        assert "FakeNoCredentialsError" in msg or "NoCredentialsError" in msg
        # The exception message (the value) MUST appear so operator
        # doesn't have to ssh into the LaunchAgent log.
        assert "Unable to locate credentials" in msg
        # Bucket name MUST appear so multi-environment operators know
        # which heartbeat fired.
        assert "kalshi-test" in msg

    def test_main_s3_endpoint_connection_error_fires_infra_alert(
        self, heartbeat_module, monkeypatch,
    ):
        """R1 C1: simulate transient network outage (Mac WiFi dropped,
        DNS resolver hung, S3 region down). The heartbeat must still
        fire a paging alert with rc=4 — the operator decides whether
        to retry or escalate.

        Distinct test from the credentials case because the operator
        triage is different (network vs creds), and we want the
        regression to flag BOTH paths if a future refactor narrows the
        catch clause.
        """
        s3 = MagicMock()

        class _FakeEndpointConnectionError(Exception):
            """Stand-in for botocore.exceptions.EndpointConnectionError."""

        s3.list_objects_v2.side_effect = _FakeEndpointConnectionError(
            'Could not connect to the endpoint URL: "https://s3.amazonaws.com/"'
        )

        alerts = []
        monkeypatch.setattr(
            heartbeat_module, "send_telegram_alert",
            lambda msg: alerts.append(msg) or True,
        )

        rc = heartbeat_module.run_heartbeat(
            s3_client=s3,
            bucket="kalshi-test",
            now=datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc),
        )
        assert rc == 4
        assert len(alerts) == 1
        msg = alerts[0]
        assert "HEARTBEAT-INFRA" in msg or "heartbeat-infra" in msg.lower()
        assert "EndpointConnectionError" in msg or "FakeEndpointConnectionError" in msg
        assert "Could not connect to the endpoint" in msg

    def test_main_s3_generic_exception_does_not_crash(
        self, heartbeat_module, monkeypatch,
    ):
        """R1 C1: catch-all backstop. ANY exception out of
        list_objects_v2 must result in rc=4 + an alert, never a
        propagating exception. This is the contract that closes the
        silent-LaunchAgent-crash failure mode.

        Generic Exception, not a botocore subclass, to verify the
        catch is broad enough (matches the implementation's `except
        Exception` clause).
        """
        s3 = MagicMock()
        s3.list_objects_v2.side_effect = RuntimeError("anything at all")

        alerts = []
        monkeypatch.setattr(
            heartbeat_module, "send_telegram_alert",
            lambda msg: alerts.append(msg) or True,
        )

        # MUST NOT raise.
        rc = heartbeat_module.run_heartbeat(
            s3_client=s3,
            bucket="kalshi-test",
            now=datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc),
        )
        assert rc == 4
        assert len(alerts) == 1
        assert "anything at all" in alerts[0]
        # Sanity: the heartbeat-infra prefix is consistent across all
        # three C1 tests so the operator's Telegram filter rule
        # (e.g., starred messages matching "HEARTBEAT-INFRA") works.
        assert "HEARTBEAT-INFRA" in alerts[0]

    def test_main_does_not_depend_on_backup_module(self, heartbeat_module):
        """Decoupling constraint per ticket: 'Heartbeat must NOT depend
        on the backup timer being healthy (decoupled — only depends on
        S3 + Telegram).' Enforced by NOT importing state_db_s3_backup
        or state_db_restore.

        If a future maintainer adds such an import, this test fails
        and forces them to either add it as an explicit dep (and update
        the KB / ticket scope) or pick a different abstraction.

        AST-level (not substring) so doc-string mentions of the sibling
        modules don't trip the check.
        """
        import ast
        src = (SCRIPT_DIR / "state_db_backup_heartbeat.py").read_text()
        tree = ast.parse(src)
        forbidden = {"state_db_s3_backup", "state_db_restore"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden, (
                        f"heartbeat must not import {alias.name!r} "
                        "(see decoupling guarantee in script docstring)"
                    )
            elif isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden, (
                    f"heartbeat must not import from {node.module!r} "
                    "(see decoupling guarantee in script docstring)"
                )
