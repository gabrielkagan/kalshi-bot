"""silver.scripts.etl_run — nightly silver ETL entry point.

Subprocesses ``dbt run`` against the silver/ dbt project for a target UTC
date. The 5 Tier-1 silver models live under ``silver/models/``; each
materializes as a Parquet file at
``{silver_root}/<silver_table>/utc_date=<target_date>/data.parquet``.

Refactored at D3.0-fu1 (ticket 86ba0a2k8, 2026-05-18) from direct-
DuckDB ETL to a dbt-run subprocess wrapper. The 5 SQL projections that
previously lived inline (``_sql_silver_*`` functions) are now dbt model
files under ``silver/models/coinbase/`` + ``silver/models/kalshi/``.
The shared envelope projection is the ``envelope_cols`` macro at
``silver/macros/envelope_columns.sql``. The materialization story
resolved at D3.0-fu1 sandbox: ``materialized='external'`` with
``location`` parameterized via ``var('target_date')`` — DuckDB ``COPY``
overwrites the target Parquet atomically (delete+insert semantics per
plan-doc decision #5).

CLI (unchanged from D3.0):
    silver/scripts/etl_run.sh                 # processes (now_utc - 1 day)
    silver/scripts/etl_run.sh --date 2026-05-18
    silver/scripts/etl_run.sh --date 2026-05-18 --bronze-root s3://... --silver-root s3://...

Programmatic (unchanged):
    from silver.scripts import etl_run
    etl_run.run_for_date("2026-05-18", bronze_root="s3://...", silver_root="s3://...")

Bronze-chunk pre-check (SKIP-on-empty per plan-doc test #3) stays in
this wrapper rather than in the dbt model SQL: ``dbt run`` invokes
DuckDB which would crash on a missing local-fs glob, so for local
paths the wrapper omits empty sources from ``--select``. For S3 paths
the wrapper currently DEFERS to DuckDB (returns True unconditionally
from ``_has_bronze_chunks``) — DuckDB's ``read_json_auto`` on an empty
S3 prefix returns zero rows, and dbt then writes a zero-row Parquet to
the silver path. The post-run rowcount loop detects this case and (on
local-fs) deletes the zero-row file + omits it from results so the
test #3 SKIP semantics are preserved end-to-end. On S3, the zero-row
file persists — see followup 86ba0a323 (LOW; add boto3-based S3
pre-check OR post-run delete to fully preserve SKIP-on-empty on S3).
The S3 zero-row case is operator-error-only in normal nightly runs
(silver targets `now_utc - 1` day and bronze for that day is always
present by 02:00 local).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ─── Source registry ────────────────────────────────────────────────────────


# (bronze_source, bronze_channel, silver_table, dbt_model_name)
# Order: Coinbase first (4 Tier-1; level2_batch deferred to D3.1),
# Kalshi second (1 Tier-1; trade deferred to D3.3, orderbook_delta to D3.2).
# dbt_model_name MUST match the filename (without .sql) under
# silver/models/<vendor>/ — this is the string dbt's --select takes.
TIER_1_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    ("coinbase_ws", "ticker",                "coinbase_ticker",             "silver_coinbase_ticker"),
    ("coinbase_ws", "matches",               "coinbase_matches",            "silver_coinbase_matches"),
    ("coinbase_ws", "heartbeat",             "coinbase_heartbeat",          "silver_coinbase_heartbeat"),
    ("coinbase_ws", "status",                "coinbase_status",             "silver_coinbase_status"),
    ("kalshi_ws",   "market_lifecycle_v2",   "kalshi_market_lifecycle_v2",  "silver_kalshi_market_lifecycle_v2"),
)


# ─── Paths ──────────────────────────────────────────────────────────────────


SILVER_DIR = Path(__file__).resolve().parent.parent
DBT_PROJECT_DIR = SILVER_DIR
PROFILES_EXAMPLE = SILVER_DIR / "profiles.yml.example"


# ─── Bronze chunk discovery ─────────────────────────────────────────────────


def _has_bronze_chunks(bronze_root: str, source: str, channel: str, utc_date: str) -> bool:
    """Return True if at least one bronze chunk exists for the partition.

    For local fs paths: glob the directory. For S3 paths: skip the
    pre-check (DuckDB returns empty result for missing globs; test #3
    asserts SKIP-on-empty semantics via the wrapper's --select arg
    omission).
    """
    if bronze_root.startswith("s3://"):
        return True
    y, m, d = utc_date.split("-")
    base = Path(bronze_root) / source / channel
    if not base.is_dir():
        return False
    day_dir = base / f"year={y}" / f"month={m}" / f"day={d}"
    if not day_dir.is_dir():
        return False
    return any(day_dir.rglob("*.jsonl.zst"))


def _output_path_for_source(silver_root: str, silver_table: str, utc_date: str) -> Path:
    """Compute the silver Parquet output path for a source + date.

    Local-fs only — for S3, parent-dir pre-creation is a no-op and the
    DuckDB COPY writes the object directly. Used by the wrapper to
    pre-create local parent dirs before invoking dbt (DuckDB's local
    COPY does NOT auto-create parent dirs and would fail otherwise).
    """
    return Path(silver_root) / silver_table / f"utc_date={utc_date}" / "data.parquet"


# ─── dbt profiles.yml generation ────────────────────────────────────────────


_PROFILES_YML = """\
silver:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: ":memory:"
      threads: 1
      extensions:
        - httpfs
"""


def _write_profiles_dir(target_dir: Path) -> Path:
    """Write a minimal silver dbt profiles.yml into ``target_dir``.

    The operator-facing ``silver/profiles.yml.example`` is documentation;
    every ETL run generates its own profiles.yml in a private tmp dir so
    operator customization (e.g., ~/.dbt/profiles.yml) doesn't bleed in
    and so concurrent runs don't race on a single profiles file. The
    DuckDB target is in-memory because all 5 silver models are
    ``materialized='external'`` (catalog stores model metadata only, not
    data).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    profiles_path = target_dir / "profiles.yml"
    profiles_path.write_text(_PROFILES_YML)
    return profiles_path


# ─── dbt subprocess invocation ──────────────────────────────────────────────


def _resolve_dbt_binary() -> str:
    """Resolve the ``dbt`` binary path. Honors ``DBT_BIN`` env override
    (used by tests that may need to point at a sandbox venv's dbt) and
    otherwise falls back to whatever's on PATH.
    """
    override = os.environ.get("DBT_BIN")
    if override:
        return override
    found = shutil.which("dbt")
    if not found:
        raise RuntimeError(
            "dbt binary not on PATH. Install via `pip install -r silver/requirements.txt` "
            "(includes dbt-core + dbt-duckdb pins). For a sandbox venv, set DBT_BIN to "
            "the absolute path of the venv's dbt binary."
        )
    return found


def _run_dbt(
    target_date: str,
    bronze_root: str,
    silver_root: str,
    selects: list[str],
) -> None:
    """Subprocess ``dbt run`` with the given target_date + selects.

    Raises CalledProcessError on non-zero exit. The wrapper's caller is
    responsible for catching + logging.
    """
    if not selects:
        logger.info("silver ETL: no sources with bronze chunks for %s — SKIP", target_date)
        return

    dbt_bin = _resolve_dbt_binary()
    with tempfile.TemporaryDirectory(prefix="silver-dbt-profiles-") as profiles_dir_str:
        profiles_dir = Path(profiles_dir_str)
        _write_profiles_dir(profiles_dir)
        vars_payload = json.dumps({
            "target_date": target_date,
            "bronze_root": bronze_root,
            "silver_root": silver_root,
        })
        cmd = [
            dbt_bin, "run",
            "--project-dir", str(DBT_PROJECT_DIR),
            "--profiles-dir", str(profiles_dir),
            "--vars", vars_payload,
            "--select", *selects,
        ]
        logger.info("silver ETL dbt invocation: %s", " ".join(cmd))
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        logger.info("dbt stdout:\n%s", result.stdout)
        if result.stderr:
            logger.info("dbt stderr:\n%s", result.stderr)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, output=result.stdout, stderr=result.stderr
            )


# ─── Public entry points ────────────────────────────────────────────────────


def run_for_date(
    target_date: str,
    bronze_root: str,
    silver_root: str,
) -> dict[str, int]:
    """Process one UTC date end-to-end for all 5 Tier-1 silver sources.

    Returns a dict ``{silver_table: rowcount}`` for the run. Sources
    without bronze chunks are omitted from the dict (per plan-doc test
    #3 SKIP semantics) — equivalent to rowcount=0 in the prior direct
    API but expressed as absence rather than zero.
    """
    logger.info(
        "silver ETL run start: utc_date=%s bronze_root=%s silver_root=%s",
        target_date, bronze_root, silver_root,
    )

    # Per-source bronze-chunk pre-check + parent-dir pre-create.
    selects: list[str] = []
    expected_outputs: dict[str, Path] = {}
    for source, channel, silver_table, dbt_model in TIER_1_SOURCES:
        if not _has_bronze_chunks(bronze_root, source, channel, target_date):
            logger.info(
                "silver:%s utc_date=%s — no bronze chunks; SKIP",
                silver_table, target_date,
            )
            continue
        selects.append(dbt_model)
        out_path = _output_path_for_source(silver_root, silver_table, target_date)
        expected_outputs[silver_table] = out_path
        # Pre-create the parent dir for local-fs writes; S3 paths are
        # no-ops here (mkdir on s3:// path would fail; we skip).
        if not silver_root.startswith("s3://"):
            out_path.parent.mkdir(parents=True, exist_ok=True)

    _run_dbt(target_date, bronze_root, silver_root, selects)

    # Rowcount query per source (post-run via DuckDB on the written Parquet).
    # Two cleanup classes here:
    #  - dbt partial failure: dbt-run exits 0 but a specific model was
    #    skipped → expected output file doesn't exist. On local-fs we
    #    can detect this via `out_path.exists()`; on S3 the rowcount
    #    query itself raises if the object is missing. Either way, log
    #    + raise — partial-failure is a real bug to surface, not silence.
    #  - zero-row write (S3 only, edge case): dbt wrote a zero-row Parquet
    #    because read_json_auto returned no rows. The rowcount loop
    #    detects rows==0 and, on local-fs, unlinks the file + omits
    #    from results (preserves test #3 SKIP-on-empty semantics). On S3
    #    we leave the file (no boto3 dep at D3.0-fu1; see 86ba0a323
    #    followup).
    import duckdb
    conn = duckdb.connect(":memory:")
    on_s3 = bronze_root.startswith("s3://") or silver_root.startswith("s3://")
    if on_s3:
        conn.execute("INSTALL httpfs; LOAD httpfs;")

    results: dict[str, int] = {}
    for silver_table, out_path in expected_outputs.items():
        # dbt partial-failure detection (local-fs only — S3 paths can't
        # use pathlib.Path.exists(); fall through to the read_parquet
        # which raises a clear DuckDB error if the S3 object is missing).
        if not silver_root.startswith("s3://") and not out_path.exists():
            logger.error(
                "silver: dbt produced no output for %s utc_date=%s "
                "(expected %s); dbt likely partially failed — check the "
                "dbt stdout above for `ERROR`/`SKIP` lines on this model.",
                silver_table, target_date, out_path,
            )
            raise RuntimeError(
                f"silver dbt run did not produce expected output {out_path} "
                f"for {silver_table} utc_date={target_date}"
            )
        try:
            count = conn.execute(
                f"SELECT COUNT(*) FROM read_parquet('{out_path}')"
            ).fetchone()[0]
        except Exception:
            logger.exception(
                "silver: rowcount query failed for %s utc_date=%s path=%s",
                silver_table, target_date, out_path,
            )
            raise

        if count == 0:
            # SKIP-on-empty cleanup: bronze pre-check said the partition
            # exists but actually had zero rows (or — on S3 — the pre-check
            # deferred and bronze was empty). Delete the zero-row file on
            # local-fs to preserve test #3 semantics; on S3 leave it (see
            # docstring + 86ba0a323 followup).
            if not silver_root.startswith("s3://"):
                try:
                    out_path.unlink()
                    logger.info(
                        "silver:%s utc_date=%s rows=0 — deleted zero-row "
                        "Parquet at %s (SKIP-on-empty)",
                        silver_table, target_date, out_path,
                    )
                except OSError:
                    logger.warning(
                        "silver:%s utc_date=%s rows=0 — failed to unlink "
                        "zero-row Parquet at %s (continuing)",
                        silver_table, target_date, out_path,
                    )
            else:
                logger.info(
                    "silver:%s utc_date=%s rows=0 (S3 zero-row Parquet "
                    "persisted at %s; see 86ba0a323 followup)",
                    silver_table, target_date, out_path,
                )
            continue

        results[silver_table] = int(count)
        logger.info(
            "silver:%s utc_date=%s rows=%d path=%s",
            silver_table, target_date, count, out_path,
        )

    logger.info("silver ETL run end: utc_date=%s totals=%s", target_date, results)
    return results


def _target_date_default() -> str:
    """Default target date = (now_utc - 1 day).date()."""
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=1)).date().isoformat()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--date",
        default=None,
        help="UTC date to process (YYYY-MM-DD); default = (now_utc - 1 day)",
    )
    parser.add_argument(
        "--bronze-root",
        default="s3://kalshi-bot-archive/bronze",
        help="bronze root (s3://... or local path); default S3 production",
    )
    parser.add_argument(
        "--silver-root",
        default="s3://kalshi-bot-archive/silver/v1",
        help="silver root (s3://... or local path); default S3 production",
    )
    parser.add_argument("--verbose", action="store_true", help="DEBUG-level logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    target = args.date or _target_date_default()
    run_for_date(target, args.bronze_root, args.silver_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
