"""SHARED helper — A.7 (86b9vejrj) + Phase 0a (86b9vd9e3) both consume.

A.7 = scripts/cal_mlp/snapshot_state_db.py (extract-time, local, content-addressed).
Phase 0a = scripts/state_db_s3_backup.py (operational, nightly, S3-bound).

Both take an online SQLite backup of state.db, SHA-256 it, and compress it.
This module factors that shared primitive.

Coordination contract pinned in:
  kb/decisions/sprint-a-bit-7-plan-may09.md (this session, 86b9vejrj)
  .claude/worktrees/86b9vd9e3-state-db-s3-backup/kb/decisions/auto-research-phase-0a-plan-may09.md

Do not change a function signature without updating both consumers atomically.
Stdlib-only by deliberate choice (zstandard is optional, runtime-checked).
"""
from __future__ import annotations

import gzip
import hashlib
import shutil
import sqlite3
from pathlib import Path
from typing import Tuple


_DEFAULT_GZIP_LEVEL = 6
_DEFAULT_ZSTD_LEVEL = 10
_HASH_CHUNK_SIZE = 1024 * 1024


def take_snapshot(src_path: Path, dst_path: Path) -> str:
    """Online backup of src_path → dst_path. Returns SHA-256 hex of dst.

    Uses sqlite3.Connection.backup() — safe under concurrent writes
    (per-page shared lock, holds only briefly). The result is a
    transactionally consistent SQLite file. dst_path's parent must exist.
    """
    src_path = Path(src_path)
    dst_path = Path(dst_path)
    if not src_path.exists():
        raise FileNotFoundError(f"source DB not found: {src_path}")
    if not dst_path.parent.exists():
        raise FileNotFoundError(
            f"destination parent must exist: {dst_path.parent}"
        )

    src = sqlite3.connect(_ro_uri(src_path), uri=True)
    dst = sqlite3.connect(str(dst_path))
    try:
        src.execute("PRAGMA busy_timeout=10000")
        dst.execute("PRAGMA busy_timeout=10000")
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    # `.backup()` clones the source's pages, including the header that
    # records `journal_mode`. State.db runs WAL → the snapshot's header
    # also says WAL. Subsequent RO opens via `?mode=ro` then fail with
    # "unable to open database file" because URI mode=ro forbids
    # creating the `-wal`/`-shm` sidecars SQLite would otherwise auto-
    # create. Flip the snapshot back to DELETE journal_mode so it
    # opens cleanly RO without sidecars. This is a one-line follow-up
    # write on the snapshot file, after which the file is sealed.
    flip = sqlite3.connect(str(dst_path))
    try:
        flip.execute("PRAGMA busy_timeout=10000")
        flip.execute("PRAGMA journal_mode=DELETE")
        flip.commit()
    finally:
        flip.close()
    # Sweep any -wal / -shm / -journal leftovers from the flip transaction.
    for ext in ('-wal', '-shm', '-journal'):
        sidecar = Path(str(dst_path) + ext)
        if sidecar.exists():
            try:
                sidecar.unlink()
            except OSError:
                pass

    return compute_sha256(dst_path)


def compute_sha256(path: Path, chunk_size: int = _HASH_CHUNK_SIZE) -> str:
    """Streaming SHA-256 of a file. Constant RAM regardless of file size."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b''):
            h.update(chunk)
    return h.hexdigest()


def verify_snapshot(path: Path, expected_sha256: str) -> bool:
    """True iff compute_sha256(path) == expected_sha256."""
    if not Path(path).exists():
        return False
    return compute_sha256(path) == expected_sha256


def compress(src_path: Path, dst_path_no_suffix: Path, level: int = None) -> Tuple[str, int, Path]:
    """Compress src_path → dst_path_no_suffix.{zst,gz}.

    Prefers zstandard if importable; falls back to stdlib gzip otherwise.
    Returns (algo, level, final_dst_path). dst_path_no_suffix is treated
    as a base path; the helper picks the suffix based on the chosen algo.
    Note: gzip embeds a timestamp in the header by default, so two gzips
    of the same bytes are NOT byte-identical (the SHA-256 invariant is
    on uncompressed bytes for that reason).
    """
    src_path = Path(src_path)
    base = Path(dst_path_no_suffix)
    try:
        import zstandard  # noqa: F401
        dst = base.with_suffix(base.suffix + '.zst') if base.suffix else base.with_name(base.name + '.zst')
        algo, lvl = _compress_zstd(src_path, dst, level=level if level is not None else _DEFAULT_ZSTD_LEVEL)
        return algo, lvl, dst
    except ImportError:
        dst = base.with_suffix(base.suffix + '.gz') if base.suffix else base.with_name(base.name + '.gz')
        algo, lvl = _compress_gzip(src_path, dst, level=level if level is not None else _DEFAULT_GZIP_LEVEL)
        return algo, lvl, dst


def _compress_gzip(src_path: Path, dst_path: Path, level: int = _DEFAULT_GZIP_LEVEL) -> Tuple[str, int]:
    """gzip-compress src→dst. Stdlib only. mtime=0 to avoid timestamp drift
    polluting the compressed-file bytes (the sha256 invariant is on the
    UNCOMPRESSED form, but downstream tools may still hash the compressed
    file for their own purposes)."""
    with open(src_path, 'rb') as fin:
        with gzip.GzipFile(filename='', fileobj=open(str(dst_path), 'wb'), mode='wb',
                           compresslevel=level, mtime=0) as fout:
            shutil.copyfileobj(fin, fout, length=_HASH_CHUNK_SIZE)
    return 'gzip', level


def _compress_zstd(src_path: Path, dst_path: Path, level: int = _DEFAULT_ZSTD_LEVEL) -> Tuple[str, int]:
    """zstd-compress src→dst. Requires zstandard."""
    import zstandard
    cctx = zstandard.ZstdCompressor(level=level)
    with open(src_path, 'rb') as fin, open(dst_path, 'wb') as fout:
        cctx.copy_stream(fin, fout, read_size=_HASH_CHUNK_SIZE, write_size=_HASH_CHUNK_SIZE)
    return 'zstd', level


def decompress(src_path: Path, dst_path: Path) -> None:
    """Decompress src_path → dst_path. Algorithm dispatched on suffix.

    Supported: .zst (zstandard required), .gz (stdlib).
    """
    src_path = Path(src_path)
    dst_path = Path(dst_path)
    suffix = src_path.suffix.lower()
    if suffix == '.zst':
        try:
            import zstandard
        except ImportError as e:
            raise RuntimeError(
                f"snapshot {src_path} is zstd-compressed but zstandard is "
                f"not importable; install with `pip install zstandard`."
            ) from e
        dctx = zstandard.ZstdDecompressor()
        with open(src_path, 'rb') as fin, open(dst_path, 'wb') as fout:
            dctx.copy_stream(fin, fout, read_size=_HASH_CHUNK_SIZE, write_size=_HASH_CHUNK_SIZE)
    elif suffix == '.gz':
        with gzip.open(str(src_path), 'rb') as fin, open(dst_path, 'wb') as fout:
            shutil.copyfileobj(fin, fout, length=_HASH_CHUNK_SIZE)
    else:
        raise ValueError(
            f"unknown compression suffix {suffix!r} on {src_path}; "
            f"expected .zst or .gz"
        )


def _ro_uri(db_path: Path) -> str:
    """URL-encode a path into a SQLite RO URI so spaces and odd chars don't
    get parsed as URI params. Mirrors `extract_data._open_ro_conn`."""
    from urllib.parse import quote
    return f"file:{quote(str(Path(db_path).resolve()), safe='/')}?mode=ro"


def integrity_check(db_path: Path) -> bool:
    """Run PRAGMA integrity_check; return True iff result is exactly ['ok'].

    Phase 0a (operational backup, ticket 86b9vd9e3) calls this on every
    snapshot before upload to surface silent corruption pre-S3.
    """
    conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return len(rows) == 1 and rows[0][0] == 'ok'


def schema_columns_sha256(db_path: Path, table: str) -> str:
    """SHA-256 of the sorted column-name list from PRAGMA table_info(table).

    Used to detect schema drift between snapshot creation and re-extract.
    """
    conn = sqlite3.connect(_ro_uri(db_path), uri=True)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    finally:
        conn.close()
    cols = sorted(r[1] for r in rows)
    payload = "\n".join(cols).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()
