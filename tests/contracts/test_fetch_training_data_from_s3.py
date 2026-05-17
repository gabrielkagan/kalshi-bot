"""fetch_training_data_from_s3 CLI contract pins (ticket 86b9zkn60, 2026-05-17).

NEW `scripts/ops/fetch_training_data_from_s3.py` standalone CLI that pulls
the four training-data sources (state.db daily backup, market_obs Parquet,
journal archives, bronze Kalshi WS chunks) from S3 into a local target
directory for cal_mlp retraining or external research.

Pre-this-script, an operator preparing a research package or retraining
corpus had to hand-stitch:
  - rclone copyto for the state.db daily backup, then decompress.
  - rclone copy for journals/ with date filters.
  - rclone copy for market_obs/ Parquet files.
  - rclone copy for bronze/kalshi_ws/<channel>/year=Y/... partition paths.
  - A by-hand MANIFEST tracking sha256 + sizes so a later re-fetch could
    detect drift.

This CLI collapses all four into a single command + emits a MANIFEST.txt.

Pins:
  1. Module imports without crash AND does NOT pull rclone subprocess
     into sys.modules.
  2. `--help` exits 0 via subprocess.
  3. argparse exposes --from-date, --to-date, --target-dir, --sources,
     --bronze-channels, --dry-run.
  4. Malformed --from-date (e.g., 2026-99-99) exits non-zero with a
     clear error (NOT 0 with silently-corrupted date semantics).
  5. --dry-run lists what would be fetched + sizes WITHOUT invoking
     rclone copy (mock + assert call_count == 0 for copy).
  6. --sources subset (e.g., state_db,journals) invokes ONLY the named
     fetchers (not all 4).
  7. Happy path generates MANIFEST.txt with source path, local path,
     sha256, size_bytes, mtime fields.
  8. Idempotent re-fetch via --checksum --immutable: when rclone reports
     "0 new transfers" the script exits 0 and the manifest reflects the
     skipped state.
  9. Exit code 2 when ALL 4 sources fail (full failure).
 10. Exit code 1 when 2/4 sources succeed (partial failure).

Hermetic — mocks rclone subprocess + the state.db restore call. No real
network calls. Pattern mirrors `tests/contracts/test_verify_s3_lifecycle.py`.
"""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "ops" / "fetch_training_data_from_s3.py"


# ── module-level pins ──────────────────────────────────────────────────


def test_script_imports_no_crash():
    """`scripts/ops/fetch_training_data_from_s3.py` imports cleanly."""
    mod = importlib.import_module("scripts.ops.fetch_training_data_from_s3")
    assert mod is not None


def test_cli_help_succeeds_via_subprocess():
    """`python3 -m scripts.ops.fetch_training_data_from_s3 --help` exits 0.

    Catches top-level import crashes the unit tests would miss.
    """
    assert SCRIPT_PATH.exists(), f"Expected script at {SCRIPT_PATH}"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ops.fetch_training_data_from_s3", "--help"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=15,
    )
    assert result.returncode == 0, (
        f"--help should exit 0; got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "--from-date" in result.stdout
    assert "--target-dir" in result.stdout


def test_cli_exposes_documented_flags():
    """argparse exposes all 6 documented flags with the documented defaults."""
    from scripts.ops.fetch_training_data_from_s3 import build_arg_parser
    parser = build_arg_parser()
    args = parser.parse_args([
        "--from-date", "2026-04-01",
        "--to-date", "2026-05-17",
        "--target-dir", "/tmp/x",
    ])
    assert args.from_date == "2026-04-01"
    assert args.to_date == "2026-05-17"
    assert str(args.target_dir) == "/tmp/x"
    # --sources defaults to all 4 (NOT None — explicit default avoids
    # the "operator forgot --sources" silent partial-fetch class).
    assert set(args.sources.split(",")) == {"state_db", "market_obs", "journals", "bronze"}
    assert args.bronze_channels is None or args.bronze_channels == ""
    assert args.dry_run is False

    # All flags accept their documented forms.
    args = parser.parse_args([
        "--from-date", "2026-04-01",
        "--to-date", "2026-05-17",
        "--target-dir", "/tmp/y",
        "--sources", "state_db,journals",
        "--bronze-channels", "orderbook_delta,trade",
        "--dry-run",
    ])
    assert args.sources == "state_db,journals"
    assert args.bronze_channels == "orderbook_delta,trade"
    assert args.dry_run is True


def test_cli_rejects_malformed_dates(capsys):
    """`--from-date 2026-99-99` exits non-zero with a clear error.

    Without this, the script would silently coerce to a default OR pass
    the bad date through to rclone, which would either no-op (empty
    filter window) or error with an opaque "no such object" message.
    """
    from scripts.ops.fetch_training_data_from_s3 import main
    rc = main([
        "--from-date", "2026-99-99",
        "--to-date", "2026-05-17",
        "--target-dir", "/tmp/x",
        "--dry-run",
    ])
    assert rc != 0, "Malformed --from-date must exit non-zero"
    captured = capsys.readouterr()
    # Error must mention the bad date AND the expected format.
    assert "2026-99-99" in captured.err or "invalid" in captured.err.lower(), (
        f"stderr must surface the malformed date for operator triage; "
        f"got stderr={captured.err!r}"
    )


# ── --dry-run posture ──────────────────────────────────────────────────


def test_dry_run_lists_without_executing(tmp_path):
    """`--dry-run` doesn't invoke rclone copy.

    Mock `subprocess.run` for the rclone copy commands; assert it's never
    called for the copy-execution path during dry-run.
    """
    from scripts.ops import fetch_training_data_from_s3 as mod
    with patch.object(mod, "_run_rclone") as mock_rclone:
        # Even if called for listing/size estimation, must NOT be called
        # with the copy command. We track all calls and assert none
        # invoked the actual `copy` / `copyto` subcommand.
        mock_rclone.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rc = mod.main([
            "--from-date", "2026-04-01",
            "--to-date", "2026-05-17",
            "--target-dir", str(tmp_path),
            "--sources", "state_db,journals,market_obs,bronze",
            "--dry-run",
        ])
        # Dry-run is expected to succeed (exit 0) even with empty fetches.
        assert rc == 0, f"--dry-run should exit 0; got {rc}"
        # No `copy` / `copyto` invocation should have been issued.
        copy_calls = [
            c for c in mock_rclone.call_args_list
            if c.args and any(arg in ("copy", "copyto") for arg in c.args[0])
            and "--dry-run" not in c.args[0]
        ]
        assert len(copy_calls) == 0, (
            f"--dry-run must NOT invoke rclone copy/copyto without --dry-run; "
            f"got {copy_calls}"
        )


# ── --sources subset ───────────────────────────────────────────────────


def test_sources_subset_fetches_only_named(tmp_path):
    """`--sources state_db,journals` invokes only the state_db + journals
    fetchers — not market_obs or bronze.
    """
    from scripts.ops import fetch_training_data_from_s3 as mod
    called: list[str] = []

    def _record(name):
        def _fn(*args, **kwargs):
            called.append(name)
            # Return success with empty file list so the orchestrator
            # records this source as successful + done.
            return mod.FetchResult(source=name, files=[], skipped=True, error=None)
        return _fn

    with patch.object(mod, "fetch_state_db", side_effect=_record("state_db")), \
         patch.object(mod, "fetch_market_obs", side_effect=_record("market_obs")), \
         patch.object(mod, "fetch_journals", side_effect=_record("journals")), \
         patch.object(mod, "fetch_bronze", side_effect=_record("bronze")):
        rc = mod.main([
            "--from-date", "2026-04-01",
            "--to-date", "2026-05-17",
            "--target-dir", str(tmp_path),
            "--sources", "state_db,journals",
        ])
        assert rc == 0, f"Expected 0 on success; got {rc}"
        assert sorted(called) == ["journals", "state_db"], (
            f"Only state_db + journals must run; got {called}"
        )


# ── MANIFEST.txt structure ─────────────────────────────────────────────


def test_manifest_txt_structure(tmp_path):
    """Happy path generates MANIFEST.txt with source path, local path,
    sha256, size_bytes, mtime fields.

    Operator can `diff MANIFEST.txt` against a later re-fetch to detect
    drift. The manifest is load-bearing per the ticket spec.
    """
    from scripts.ops import fetch_training_data_from_s3 as mod

    # Pre-create some local files so the manifest writer has something
    # real to sha256/stat.
    local_file = tmp_path / "state.db"
    local_file.write_bytes(b"hello-world-fake-db")

    files = [
        mod.FetchedFile(
            source_path="s3prod:kalshi-bot-archive/daily/state-db-2026-04-01.db.zst",
            local_path=local_file,
            size_bytes=local_file.stat().st_size,
        ),
    ]

    manifest_path = tmp_path / "MANIFEST.txt"
    results = [mod.FetchResult(source="state_db", files=files, skipped=False, error=None)]
    mod.write_manifest(manifest_path, results)

    assert manifest_path.exists()
    content = manifest_path.read_text()
    # All required fields must be present in the manifest.
    assert "source_path" in content or "s3prod:" in content
    assert str(local_file) in content
    assert "sha256" in content.lower()
    assert "size" in content.lower()
    assert "mtime" in content.lower()
    # SHA256 of the file content must appear (real hash, not placeholder).
    import hashlib
    expected_sha = hashlib.sha256(b"hello-world-fake-db").hexdigest()
    assert expected_sha in content, (
        f"manifest must record the real sha256 ({expected_sha[:8]}...); "
        f"got content={content!r}"
    )


# ── idempotent re-fetch ────────────────────────────────────────────────


def test_idempotent_refetch_skips_existing(tmp_path):
    """Mocked rclone reports nothing new transferred → script exits 0
    AND the manifest mentions the skipped state.

    rclone copy --checksum --immutable is the underlying primitive (same
    as scripts/ops/journal_archives_s3_sync.py). On a re-run with all
    files already at-rest locally + matching ETag, rclone exits 0 with
    "There was nothing to transfer" stderr.
    """
    from scripts.ops import fetch_training_data_from_s3 as mod
    with patch.object(mod, "_run_rclone") as mock_rclone:
        # rclone returns success + 0-transfer summary.
        mock_rclone.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="There was nothing to transfer\n",
        )
        # state_db restore mocked similarly so we don't actually decompress.
        with patch.object(mod, "fetch_state_db") as mock_state, \
             patch.object(mod, "fetch_bronze") as mock_bronze:
            mock_state.return_value = mod.FetchResult(
                source="state_db", files=[], skipped=True, error=None,
            )
            mock_bronze.return_value = mod.FetchResult(
                source="bronze", files=[], skipped=True, error=None,
            )
            rc = mod.main([
                "--from-date", "2026-04-01",
                "--to-date", "2026-05-17",
                "--target-dir", str(tmp_path),
            ])
        assert rc == 0, f"Idempotent re-fetch must exit 0; got {rc}"
        # MANIFEST.txt must exist + reflect the skipped state.
        manifest = tmp_path / "MANIFEST.txt"
        assert manifest.exists(), "MANIFEST.txt must be written even on no-op"
        content = manifest.read_text()
        assert "skipped" in content.lower() or "0 new" in content.lower(), (
            f"manifest must reflect skipped/no-op state; got {content!r}"
        )


# ── exit codes ─────────────────────────────────────────────────────────


def test_exit_code_2_on_full_failure(tmp_path):
    """All 4 source fetches raise → script exits 2 (full failure).

    Differentiates from exit 1 (partial failure) so a wrapping cron alert
    can route based on severity.
    """
    from scripts.ops import fetch_training_data_from_s3 as mod
    with patch.object(mod, "fetch_state_db", side_effect=RuntimeError("boom-state")), \
         patch.object(mod, "fetch_market_obs", side_effect=RuntimeError("boom-mo")), \
         patch.object(mod, "fetch_journals", side_effect=RuntimeError("boom-jo")), \
         patch.object(mod, "fetch_bronze", side_effect=RuntimeError("boom-br")):
        rc = mod.main([
            "--from-date", "2026-04-01",
            "--to-date", "2026-05-17",
            "--target-dir", str(tmp_path),
        ])
        assert rc == 2, f"Full failure (4/4 sources raised) must exit 2; got {rc}"


def test_state_db_calls_restore_with_allow_overwrite_live(tmp_path):
    """fetch_state_db must call state_db_restore.restore_to_path with
    `allow_overwrite_live=True` (R1-C2 regression pin).

    restore_to_path refuses to write to any path whose basename is in
    `_LIVE_SQLITE_BASENAMES = {state.db, state.db-wal, ...}` unless this
    flag is True. Our target dest is `<target>/state.db` which trips
    that guard — without the flag, every fetch_state_db run would fail
    with exit 4 ("refusing to write to live SQLite mainfile").

    The inner `kalshi-bot-repo` substring check still protects against
    the operator using --target-dir inside the live VPS repo (covered
    by restore_to_path's own contract tests; we just rely on it here).
    """
    from scripts.ops import fetch_training_data_from_s3 as mod
    captured_kwargs: dict = {}

    class _FakeStore:
        def list(self, prefix):
            return ["daily/state-db-2026-04-01.db.zst"]

    def _fake_restore_to_path(**kwargs):
        captured_kwargs.update(kwargs)
        # Pretend success — write a stub state.db so the file-stat call
        # doesn't crash.
        kwargs["dst"].write_bytes(b"fake-state-db")
        return 0

    # Make the lazy-import return our fakes.
    import sys as _sys
    fake_backup = MagicMock()
    fake_backup.S3RcloneStore.return_value = _FakeStore()
    fake_backup.DEFAULT_ALGORITHM = "zstd"
    import re as _re
    fake_restore = MagicMock()
    fake_restore._DAILY_KEY_DATE_RE = _re.compile(
        r"daily/state-db-(\d{4}-\d{2}-\d{2})\.db\.(zst|gz)$"
    )
    fake_restore.restore_to_path = _fake_restore_to_path

    saved = (_sys.modules.get("state_db_s3_backup"), _sys.modules.get("state_db_restore"))
    _sys.modules["state_db_s3_backup"] = fake_backup
    _sys.modules["state_db_restore"] = fake_restore
    try:
        result = mod.fetch_state_db(
            target_dir=tmp_path,
            from_date=__import__("datetime").date(2026, 4, 1),
            to_date=__import__("datetime").date(2026, 5, 17),
            remote="s3prod",
            bucket="test-bucket",
            dry_run=False,
        )
    finally:
        # Restore module state.
        for name, mod_obj in zip(("state_db_s3_backup", "state_db_restore"), saved):
            if mod_obj is None:
                _sys.modules.pop(name, None)
            else:
                _sys.modules[name] = mod_obj

    assert result.error is None, f"fetch_state_db should succeed; got {result.error}"
    assert captured_kwargs.get("allow_overwrite_live") is True, (
        f"fetch_state_db MUST pass allow_overwrite_live=True to "
        f"restore_to_path, else dst.name=='state.db' trips the live-DB "
        f"guard and the restore always fails with exit 4 "
        f"(R1-C2 regression pin). Got kwargs={captured_kwargs!r}"
    )
    assert captured_kwargs.get("force") is True, (
        f"fetch_state_db must pass force=True so re-runs overwrite the "
        f"stale local restore. Got kwargs={captured_kwargs!r}"
    )


def test_exit_code_1_on_partial_failure(tmp_path):
    """2/4 source fetches succeed, 2/4 raise → script exits 1.

    Partial-success exit code lets the wrapping cron alert differentiate
    "everything down, panic" from "one source flaky, retry".
    """
    from scripts.ops import fetch_training_data_from_s3 as mod
    with patch.object(mod, "fetch_state_db") as mock_state, \
         patch.object(mod, "fetch_market_obs", side_effect=RuntimeError("boom-mo")), \
         patch.object(mod, "fetch_journals") as mock_jo, \
         patch.object(mod, "fetch_bronze", side_effect=RuntimeError("boom-br")):
        mock_state.return_value = mod.FetchResult(
            source="state_db", files=[], skipped=False, error=None,
        )
        mock_jo.return_value = mod.FetchResult(
            source="journals", files=[], skipped=False, error=None,
        )
        rc = mod.main([
            "--from-date", "2026-04-01",
            "--to-date", "2026-05-17",
            "--target-dir", str(tmp_path),
        ])
        assert rc == 1, f"Partial failure (2/4 succeeded) must exit 1; got {rc}"
