#!/usr/bin/env python3
"""fetch_training_data_from_s3 — single-command S3 pull for cal_mlp retraining.

Ticket: 86b9zkn60 (2026-05-17).

Pulls four training-data sources from S3 into a local target directory in
one shot, emitting a `MANIFEST.txt` so the operator can detect drift on
later re-fetches:

  1. state.db daily backup (`daily/state-db-YYYY-MM-DD.db.zst`) — restored
     to <target>/state.db. Reuses `state_db_restore.restore_to_path` for
     decompress + integrity_check semantic parity.
  2. market_observations Parquet+zstd (`market_obs/YYYY-MM-DD.parquet.zst`)
     — copied verbatim to <target>/market_obs/.
  3. Journal archives (`journals/<name>_YYYY-MM-DD.jsonl.{zst,gz}`) —
     copied verbatim to <target>/journals/.
  4. Bronze Kalshi WS chunks
     (`bronze/kalshi_ws/<channel>/year=Y/month=M/day=D/hour=H/conn=X/...`)
     — copied to <target>/bronze/kalshi_ws/... preserving the partition
     structure.

Underlying primitive: `rclone copy --checksum --immutable` for the three
flat-prefix sources + the bronze partition tree. `--checksum` skips files
already present + ETag-matched; `--immutable` causes a non-zero exit if a
remote file changed content under us (drift signal). Same primitive
discipline as `scripts/ops/journal_archives_s3_sync.py`.

Output structure:
    <target>/
      state.db                   # restored from daily backup nearest --from-date
      market_obs/                # Parquet files in date range
      journals/                  # zstd archives in date range
      bronze/kalshi_ws/...       # bronze partitions in date range
      MANIFEST.txt               # SHA256 + S3 paths + dates + sizes

Behavior:
  - Date-range filtering per source (no fetch-everything).
  - --dry-run lists what WOULD be fetched + sizes WITHOUT pulling.
  - --sources subset (e.g., state_db,journals) skips unnamed sources.
  - Idempotent re-fetch via rclone --checksum --immutable.
  - MANIFEST.txt records source S3 path, local path, SHA256, size, mtime.
  - state.db: routes through `state_db_restore.restore_to_path` to reuse
    its decompress + integrity-check semantics.

Exit codes:
  0 — all named sources succeeded (or --dry-run regardless of result)
  1 — partial failure (some named source(s) succeeded, some failed)
  2 — full failure (no named source succeeded)
 64 — usage error (bad --from-date / --to-date / unknown source name /
       --target-dir missing)

This is a one-shot operator CLI, NOT a cron job. No flock guard (the
operator owns the singleton invariant). No Telegram alerting (manual run).
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import logging
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional


DEFAULT_RCLONE_REMOTE = "s3prod"
DEFAULT_BUCKET = os.environ.get("S3_BACKUP_BUCKET", "kalshi-bot-archive")
ALL_SOURCES = ("state_db", "market_obs", "journals", "bronze")
DEFAULT_SOURCES_CSV = ",".join(ALL_SOURCES)
DEFAULT_BRONZE_CHANNELS = ("orderbook_delta", "trade")

logger = logging.getLogger("fetch_training_data_from_s3")


# ── data shapes ────────────────────────────────────────────────────────


@dataclasses.dataclass
class FetchedFile:
    """One file pulled to disk. Manifest entry."""
    source_path: str          # e.g., "s3prod:kalshi-bot-archive/daily/state-db-2026-05-01.db.zst"
    local_path: Path          # absolute path on the local filesystem
    size_bytes: int           # post-decompress for state.db, post-copy otherwise


@dataclasses.dataclass
class FetchResult:
    """Per-source outcome. Tracked by the orchestrator for the exit-code
    decision (full / partial / clean)."""
    source: str               # "state_db" | "market_obs" | "journals" | "bronze"
    files: List[FetchedFile]  # files added on this run (may be empty on no-op)
    skipped: bool             # True if rclone said "nothing to transfer"
    error: Optional[str]      # exception string if the fetch raised; None on success


# ── rclone shim ────────────────────────────────────────────────────────


def _run_rclone(cmd: List[str], dry_run: bool = False) -> subprocess.CompletedProcess:
    """Wrap subprocess.run with a rclone-not-found friendly error.

    Mirrors `scripts/ops/state_db_s3_backup.py:_run_rclone` so the same
    "install rclone" guidance fires uniformly across the ops scripts.

    `dry_run` is captured here so callers can branch on it before
    constructing argv; the actual --dry-run flag is added in
    `build_rclone_copy_argv` below (so contract tests can assert flag
    shape without running rclone).
    """
    try:
        return subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        if "rclone" in str(e):
            raise RuntimeError(
                "rclone binary not found in PATH. Install via: "
                "curl https://rclone.org/install.sh | sudo bash"
            ) from e
        raise


def build_rclone_copy_argv(
    src_remote: str,
    dst_local: Path,
    include: Optional[List[str]] = None,
    dry_run: bool = False,
) -> List[str]:
    """Assemble the rclone `copy` argv with the canonical idempotency
    flags. Mirrors `journal_archives_s3_sync.build_rclone_argv`.

    `include` is a list of `--include` glob patterns (used for date-range
    filtering by filename prefix on market_obs/ and journals/). Bronze
    uses partition-path filtering instead (passes a more specific
    `src_remote`).
    """
    cmd = [
        "rclone", "copy",
        "--checksum",         # idempotent re-fetch via ETag
        "--immutable",        # drift signal: non-zero if remote content changed
        "--s3-no-check-bucket",
        "--retries", "3",
        "--low-level-retries", "10",
    ]
    if include:
        for pat in include:
            cmd.extend(["--include", pat])
    if dry_run:
        cmd.append("--dry-run")
    cmd.extend([src_remote, str(dst_local)])
    return cmd


# ── date helpers ───────────────────────────────────────────────────────


def parse_iso_date(s: str) -> date:
    """Parse YYYY-MM-DD with a clear error on garbage input.

    Mirrors `date.fromisoformat` but wraps the ValueError in a CLI-
    friendly message so the operator sees `bad date '2026-99-99'` rather
    than `month must be in 1..12`.
    """
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise ValueError(
            f"invalid --from-date / --to-date value {s!r}: expected YYYY-MM-DD "
            f"({e})"
        ) from e


def date_range_inclusive(start: date, end: date) -> List[date]:
    """Inclusive list of dates from start to end. Empty if start > end."""
    if end < start:
        return []
    out = []
    d = start
    while d <= end:
        out.append(d)
        d = d + timedelta(days=1)
    return out


# ── per-source fetchers ────────────────────────────────────────────────


def fetch_state_db(
    target_dir: Path,
    from_date: date,
    to_date: date,
    remote: str,
    bucket: str,
    dry_run: bool,
) -> FetchResult:
    """Restore state.db to <target>/state.db from the daily backup nearest
    --from-date (per ticket spec).

    Selection rule: latest snapshot whose date is <= from_date, falling
    back to the earliest snapshot > from_date if no older snapshot
    exists. Operator can override by passing a window whose --from-date
    is the snapshot they actually want (the daily snapshot key carries
    the date verbatim, so `--from-date 2026-05-01` → state-db-2026-05-01.db.zst
    when available).

    NOTE on state.db corpus completeness: state.db is cumulative, so any
    single snapshot covers everything-up-to its snapshot date — picking
    a snapshot anchored at --from-date means rows settled BETWEEN
    --from-date and --to-date are absent from the restored DB. For
    cal_mlp retraining the journals + market_obs corpus provides the
    per-row training signal; state.db is fetched for the auxiliary
    tables (cohort_attribution, weekly bleed, etc.) and the operator
    is expected to widen --from-date or re-run with a later
    --from-date if they need a more recent DB snapshot.

    Routes through state_db_restore.restore_to_path for decompress +
    integrity-check semantic parity with the canonical restore path.
    """
    files: List[FetchedFile] = []
    try:
        # Lazy-import sibling so contract tests can mock fetch_state_db
        # without state_db_restore in scope. Mutate sys.path only if
        # the parent dir isn't already on it (R1-M5: don't permanently
        # leak script-dir into the process sys.path on every call).
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        import state_db_s3_backup as backup_mod
        import state_db_restore as restore_mod

        store = backup_mod.S3RcloneStore(remote=remote, bucket=bucket)
        keys = store.list("daily/")
        # Filter to date-bearing keys at-or-before from_date.
        matching = []
        for k in keys:
            # daily/state-db-YYYY-MM-DD.db.{zst,gz}
            m = restore_mod._DAILY_KEY_DATE_RE.search(k)
            if not m:
                continue
            key_date = date.fromisoformat(m.group(1))
            if key_date <= from_date:
                matching.append((key_date, k))
        if not matching:
            # No snapshot at-or-before from_date; fall back to earliest > from_date.
            future = []
            for k in keys:
                m = restore_mod._DAILY_KEY_DATE_RE.search(k)
                if not m:
                    continue
                key_date = date.fromisoformat(m.group(1))
                future.append((key_date, k))
            if not future:
                raise RuntimeError(
                    f"no state.db daily snapshot found in s3://{bucket}/daily/"
                )
            future.sort(key=lambda kv: kv[0])
            chosen_date, chosen_key = future[0]
        else:
            matching.sort(key=lambda kv: kv[0])
            chosen_date, chosen_key = matching[-1]  # latest at-or-before

        dst = target_dir / "state.db"
        if dry_run:
            logger.info(
                "[dry-run] state_db: would restore s3://%s/%s -> %s",
                bucket, chosen_key, dst,
            )
            return FetchResult(source="state_db", files=[], skipped=True, error=None)

        # R1-C2 (RCA): restore_to_path REFUSES to write to any path whose
        # basename is in `_LIVE_SQLITE_BASENAMES` (= {state.db, state.db-wal,
        # ...}) unless `allow_overwrite_live=True` is passed AND no sidecar
        # files exist alongside dst. Our target is a research/retraining
        # corpus dir, NOT the live VPS DB — pass allow_overwrite_live=True
        # so the canonical-named `state.db` restore actually proceeds.
        # The inner guard ALSO checks `kalshi-bot.service` is inactive when
        # the dst path contains "kalshi-bot-repo"; our target dirs (e.g.,
        # /tmp/retraining_corpus, ~/research/cal_mlp_v2_corpus) never
        # contain that substring, so the systemctl-active check is a
        # no-op for this caller. If the operator deliberately passes
        # --target-dir inside kalshi-bot-repo on the VPS, restore_to_path
        # will (correctly) refuse — we want that fail-safe behavior.
        with tempfile.TemporaryDirectory(prefix="fetch_training_state_db_") as tmp:
            rc = restore_mod.restore_to_path(
                store=store,
                dst=dst,
                tmp_dir=Path(tmp),
                algorithm=backup_mod.DEFAULT_ALGORITHM,
                key_override=chosen_key,
                force=True,                 # overwrite stale local restore
                allow_overwrite_live=True,  # research/retraining target, NOT live VPS
            )
        if rc != 0:
            raise RuntimeError(
                f"state_db_restore.restore_to_path exit={rc} "
                f"(dst={dst}); see scripts/ops/state_db_restore.py for "
                f"exit-code meanings"
            )
        files.append(FetchedFile(
            source_path=f"{remote}:{bucket}/{chosen_key}",
            local_path=dst.resolve(),
            size_bytes=dst.stat().st_size,
        ))
        return FetchResult(source="state_db", files=files, skipped=False, error=None)
    except Exception as exc:  # noqa: BLE001 — surface to orchestrator
        logger.warning("state_db fetch failed: %s", exc)
        return FetchResult(source="state_db", files=[], skipped=False, error=str(exc))


def _date_includes_for(from_date: date, to_date: date) -> List[str]:
    """Build a list of `--include` patterns matching YYYY-MM-DD substrings
    for each date in the inclusive range.

    rclone's `--include` is glob-based against the file path relative to
    the source root. Both market_obs/<date>.parquet.zst and journals/
    <name>_<date>.jsonl.zst happen to embed the date as a substring; the
    `*<date>*` pattern catches both without the caller having to know
    the exact filename shape.
    """
    return [f"*{d.isoformat()}*" for d in date_range_inclusive(from_date, to_date)]


def _scan_local_added(
    src_dir: Path,
    before_files: set,
) -> List[Path]:
    """Return list of files under `src_dir` that were not in `before_files`.

    Used post-rclone to enumerate which files actually landed (rclone's
    stdout transfer summary varies by version; walking the dir is the
    portable contract).
    """
    after = {p.resolve() for p in src_dir.rglob("*") if p.is_file()}
    return sorted(after - before_files)


def _fetch_flat_prefix(
    source_name: str,
    prefix: str,
    target_subdir: Path,
    from_date: date,
    to_date: date,
    remote: str,
    bucket: str,
    dry_run: bool,
) -> FetchResult:
    """Shared flat-prefix fetcher for market_obs/ and journals/.

    Both layouts are flat (no partition dirs) with date-bearing filenames,
    so the same code path serves both — the only differences are the
    source name and S3 prefix.
    """
    try:
        target_subdir.mkdir(parents=True, exist_ok=True)
        before_files = {p.resolve() for p in target_subdir.rglob("*") if p.is_file()}
        src_remote = f"{remote}:{bucket}/{prefix}"
        includes = _date_includes_for(from_date, to_date)
        cmd = build_rclone_copy_argv(
            src_remote=src_remote,
            dst_local=target_subdir,
            include=includes,
            dry_run=dry_run,
        )
        cp = _run_rclone(cmd)
        if cp.returncode != 0:
            raise RuntimeError(
                f"rclone copy exit={cp.returncode}: {cp.stderr.strip()}"
            )
        if dry_run:
            logger.info("[dry-run] %s: %s", source_name, cp.stdout.strip())
            return FetchResult(source=source_name, files=[], skipped=True, error=None)
        added_paths = _scan_local_added(target_subdir, before_files)
        added = [
            FetchedFile(
                source_path=f"{remote}:{bucket}/{prefix}{p.name}",
                local_path=p,
                size_bytes=p.stat().st_size,
            )
            for p in added_paths
        ]
        # "skipped" = rclone ran but nothing new landed (idempotent re-fetch).
        return FetchResult(
            source=source_name,
            files=added,
            skipped=(len(added) == 0),
            error=None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s fetch failed: %s", source_name, exc)
        return FetchResult(source=source_name, files=[], skipped=False, error=str(exc))


def fetch_market_obs(
    target_dir: Path,
    from_date: date,
    to_date: date,
    remote: str,
    bucket: str,
    dry_run: bool,
) -> FetchResult:
    """Copy market_obs/<date>.parquet.zst files in [from_date, to_date]."""
    return _fetch_flat_prefix(
        source_name="market_obs",
        prefix="market_obs/",
        target_subdir=target_dir / "market_obs",
        from_date=from_date,
        to_date=to_date,
        remote=remote,
        bucket=bucket,
        dry_run=dry_run,
    )


def fetch_journals(
    target_dir: Path,
    from_date: date,
    to_date: date,
    remote: str,
    bucket: str,
    dry_run: bool,
) -> FetchResult:
    """Copy journals/<name>_<date>.jsonl.{zst,gz} files in [from_date, to_date]."""
    return _fetch_flat_prefix(
        source_name="journals",
        prefix="journals/",
        target_subdir=target_dir / "journals",
        from_date=from_date,
        to_date=to_date,
        remote=remote,
        bucket=bucket,
        dry_run=dry_run,
    )


def _bronze_date_partition_includes(from_date: date, to_date: date) -> List[str]:
    """Build `--include` patterns matching Hive partition paths for each
    date in the inclusive range.

    Bronze chunk paths are `<channel>/year=YYYY/month=MM/day=DD/hour=HH/conn=X/<file>`
    relative to the channel-root rclone source. The pattern
    `year=YYYY/month=MM/day=DD/**` matches every hour+conn under that date.
    """
    return [
        f"year={d.year:04d}/month={d.month:02d}/day={d.day:02d}/**"
        for d in date_range_inclusive(from_date, to_date)
    ]


def fetch_bronze(
    target_dir: Path,
    from_date: date,
    to_date: date,
    remote: str,
    bucket: str,
    dry_run: bool,
    channels: Optional[List[str]] = None,
) -> FetchResult:
    """Copy bronze/kalshi_ws/<channel>/year=Y/month=M/day=D/... partitions
    in [from_date, to_date].

    R2-M2 (perf): issues ONE rclone copy per channel with date-bearing
    `--include` patterns (NOT one rclone per (channel, date) pair).
    Cuts a 30-day × 2-channel fetch from ~60 subprocesses to 2, saving
    ~60s of TCP+process overhead. Channels default to
    (orderbook_delta, trade) per `collector/CLAUDE.md`.
    """
    channels = channels or list(DEFAULT_BRONZE_CHANNELS)
    target_subdir = target_dir / "bronze" / "kalshi_ws"
    all_added: List[FetchedFile] = []
    any_succeeded = False
    last_error: Optional[str] = None
    try:
        target_subdir.mkdir(parents=True, exist_ok=True)
        date_includes = _bronze_date_partition_includes(from_date, to_date)
        for channel in channels:
            channel_dst = target_subdir / channel
            channel_dst.mkdir(parents=True, exist_ok=True)
            before_files = {
                p.resolve() for p in channel_dst.rglob("*") if p.is_file()
            }
            channel_prefix = f"bronze/kalshi_ws/{channel}/"
            src_remote = f"{remote}:{bucket}/{channel_prefix}"
            cmd = build_rclone_copy_argv(
                src_remote=src_remote,
                dst_local=channel_dst,
                include=date_includes,
                dry_run=dry_run,
            )
            cp = _run_rclone(cmd)
            if cp.returncode != 0:
                # Channel-root prefix may legitimately not exist (collector
                # never ran that channel). Treat as soft-skip but log.
                last_error = (
                    f"channel={channel}: exit={cp.returncode} {cp.stderr.strip()}"
                )
                logger.info("bronze channel skip: %s", last_error)
                continue
            any_succeeded = True
            if dry_run:
                continue
            added_paths = _scan_local_added(channel_dst, before_files)
            for p in added_paths:
                rel = p.relative_to(channel_dst).as_posix()
                all_added.append(FetchedFile(
                    source_path=f"{remote}:{bucket}/{channel_prefix}{rel}",
                    local_path=p,
                    size_bytes=p.stat().st_size,
                ))
        if dry_run:
            return FetchResult(source="bronze", files=[], skipped=True, error=None)
        # Bronze is "ok" if at least one channel issued a successful rclone —
        # fully-empty result is reported as skipped, not error.
        if not any_succeeded and last_error:
            return FetchResult(
                source="bronze",
                files=[],
                skipped=False,
                error=f"all channels failed; last: {last_error}",
            )
        return FetchResult(
            source="bronze",
            files=all_added,
            skipped=(len(all_added) == 0),
            error=None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("bronze fetch failed: %s", exc)
        return FetchResult(source="bronze", files=[], skipped=False, error=str(exc))


# ── manifest ───────────────────────────────────────────────────────────


def _sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    """Stream-hash a file. 1 MiB chunks balance memory + I/O syscall count."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(manifest_path: Path, results: List[FetchResult]) -> None:
    """Write MANIFEST.txt with per-file source_path / local_path / sha256
    / size_bytes / mtime records.

    Format is line-oriented, space-separated values with explicit field
    tags so a later `diff` shows the field that drifted. Header carries
    the generation timestamp + per-source skipped/error status so a
    no-op re-fetch still produces a self-documenting manifest.
    """
    lines = []
    lines.append(f"# fetch_training_data_from_s3 manifest")
    lines.append(f"# generated_at_utc={datetime.now(timezone.utc).isoformat()}")
    lines.append("# field order: source_path local_path sha256 size_bytes mtime")
    for res in results:
        if res.error:
            lines.append(
                f"# source={res.source} status=error error={res.error!r}"
            )
            continue
        if res.skipped and not res.files:
            lines.append(
                f"# source={res.source} status=skipped (0 new files; rclone reports no transfers)"
            )
            continue
        lines.append(f"# source={res.source} status=ok files={len(res.files)}")
        for f in res.files:
            try:
                sha = _sha256_of(f.local_path)
            except OSError as e:
                sha = f"<sha256-error: {e}>"
            try:
                mtime = datetime.fromtimestamp(
                    f.local_path.stat().st_mtime, tz=timezone.utc,
                ).isoformat()
            except OSError as e:
                mtime = f"<mtime-error: {e}>"
            lines.append(
                f"source_path={f.source_path} "
                f"local_path={f.local_path} "
                f"sha256={sha} "
                f"size_bytes={f.size_bytes} "
                f"mtime={mtime}"
            )
    manifest_path.write_text("\n".join(lines) + "\n")


# ── orchestration ──────────────────────────────────────────────────────


def run(
    from_date: date,
    to_date: date,
    target_dir: Path,
    sources: List[str],
    bronze_channels: Optional[List[str]],
    remote: str,
    bucket: str,
    dry_run: bool,
) -> int:
    """Run the named source fetches sequentially + write MANIFEST.txt.

    Returns the documented exit code: 0=clean, 1=partial, 2=full failure.
    Dry-run always returns 0 (report-only contract; mirrors verify_s3_lifecycle).
    """
    target_dir = target_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    # Orchestrator-level catch: each per-source fetcher already wraps
    # its internal exceptions in FetchResult(error=...), but a defense-
    # in-depth catch here means a programming bug (e.g., unmocked
    # subprocess call in tests, OR a partial mock that raises rather
    # than returning a FetchResult) still produces a structured result
    # so the exit-code decision (partial vs full failure) stays sound.
    results: List[FetchResult] = []
    for name in sources:
        try:
            if name == "state_db":
                r = fetch_state_db(target_dir, from_date, to_date, remote, bucket, dry_run)
            elif name == "market_obs":
                r = fetch_market_obs(target_dir, from_date, to_date, remote, bucket, dry_run)
            elif name == "journals":
                r = fetch_journals(target_dir, from_date, to_date, remote, bucket, dry_run)
            elif name == "bronze":
                r = fetch_bronze(
                    target_dir, from_date, to_date, remote, bucket, dry_run,
                    channels=bronze_channels,
                )
            else:
                # Should have been caught at CLI parse; defense-in-depth.
                r = FetchResult(source=name, files=[], skipped=False, error=f"unknown source {name!r}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("source=%s raised unexpectedly: %s", name, exc)
            r = FetchResult(source=name, files=[], skipped=False, error=str(exc))
        results.append(r)
        if r.error:
            logger.warning("source=%s FAILED: %s", name, r.error)
        elif r.skipped:
            logger.info("source=%s SKIPPED (no new files / dry-run)", name)
        else:
            logger.info("source=%s OK (%d files)", name, len(r.files))

    # Write the manifest before the exit-code decision so even on full
    # failure the operator has a record of what was attempted.
    manifest_path = target_dir / "MANIFEST.txt"
    try:
        write_manifest(manifest_path, results)
    except OSError as e:
        logger.warning("could not write MANIFEST.txt: %s", e)

    if dry_run:
        return 0

    # Successes = sources that returned without error (skipped counts as success).
    successes = sum(1 for r in results if r.error is None)
    failures = sum(1 for r in results if r.error is not None)
    total = len(results)
    if failures == 0:
        return 0
    if successes == 0:
        return 2
    return 1


# ── CLI ────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    """Exposed for the contract test so we don't have to spin up subprocess
    just to assert flag shape. Mirrors `verify_s3_lifecycle.build_arg_parser`.
    """
    p = argparse.ArgumentParser(
        prog="fetch_training_data_from_s3.py",
        description=(
            "Pull cal_mlp retraining corpus from S3 (state.db daily backup + "
            "market_obs Parquet + journal archives + bronze WS chunks) into "
            "a local target dir. Emits MANIFEST.txt for drift detection."
        ),
    )
    p.add_argument("--from-date", required=True,
                   help="ISO date (YYYY-MM-DD) — start of the inclusive window.")
    p.add_argument("--to-date", required=True,
                   help="ISO date (YYYY-MM-DD) — end of the inclusive window.")
    p.add_argument("--target-dir", required=True, type=Path,
                   help="local dir to populate (created if missing).")
    p.add_argument("--sources", default=DEFAULT_SOURCES_CSV,
                   help=(
                       f"comma-separated subset of {{{','.join(ALL_SOURCES)}}}. "
                       f"Default: all four."
                   ))
    p.add_argument("--bronze-channels", default=None,
                   help=(
                       "comma-separated Kalshi WS channels for bronze fetch "
                       "(default: orderbook_delta,trade)."
                   ))
    p.add_argument("--dry-run", action="store_true",
                   help="list what would be fetched + sizes without pulling.")
    p.add_argument("--rclone-remote", default=DEFAULT_RCLONE_REMOTE,
                   help=f"rclone remote name (default: {DEFAULT_RCLONE_REMOTE}).")
    p.add_argument("--bucket", default=DEFAULT_BUCKET,
                   help=f"S3 bucket (default: $S3_BACKUP_BUCKET or {DEFAULT_BUCKET!r}).")
    return p


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    # Validate dates with operator-friendly error messages.
    try:
        from_date = parse_iso_date(args.from_date)
        to_date = parse_iso_date(args.to_date)
    except ValueError as e:
        print(f"fetch_training_data_from_s3: FAIL {e}", file=sys.stderr)
        return 64  # EX_USAGE

    if to_date < from_date:
        print(
            f"fetch_training_data_from_s3: FAIL --to-date ({to_date}) is "
            f"before --from-date ({from_date})",
            file=sys.stderr,
        )
        return 64

    # Validate sources subset.
    raw_sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in raw_sources if s not in ALL_SOURCES]
    if unknown:
        print(
            f"fetch_training_data_from_s3: FAIL unknown --sources entries: "
            f"{unknown!r} (valid: {list(ALL_SOURCES)})",
            file=sys.stderr,
        )
        return 64
    if not raw_sources:
        print(
            "fetch_training_data_from_s3: FAIL --sources resolved to empty list",
            file=sys.stderr,
        )
        return 64

    bronze_channels: Optional[List[str]] = None
    if args.bronze_channels:
        bronze_channels = [s.strip() for s in args.bronze_channels.split(",") if s.strip()]

    if not args.bucket:
        print(
            "fetch_training_data_from_s3: FAIL --bucket not given and "
            "S3_BACKUP_BUCKET not set",
            file=sys.stderr,
        )
        return 64

    return run(
        from_date=from_date,
        to_date=to_date,
        target_dir=args.target_dir,
        sources=raw_sources,
        bronze_channels=bronze_channels,
        remote=args.rclone_remote,
        bucket=args.bucket,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
