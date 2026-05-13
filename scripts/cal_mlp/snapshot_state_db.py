"""A.7 (Sprint A Bit 7, ticket 86b9vejrj) — content-addressed immutable
training-data snapshots of state.db.

Operator-facing CLI:

    python -m scripts.cal_mlp.snapshot_state_db take [--db state.db] [--snap-root data/cal_mlp/_snapshots]
    python -m scripts.cal_mlp.snapshot_state_db verify --sha256 <hex>
    python -m scripts.cal_mlp.snapshot_state_db list
    python -m scripts.cal_mlp.snapshot_state_db prune --keep-last <N>

Plus library functions imported by extract_data.py + run_pipeline.sh:

    take(src, snap_root) -> sha256
    verify(snap_root, sha256) -> bool
    snapshot_path_for(snap_root, sha256) -> Path  # the compressed file
    decompressed_db_path_for(snap_root, sha256, scratch_dir) -> Path

Storage layout (forever retention; no auto-prune):

    data/cal_mlp/_snapshots/
        <sha8>/
            state.db.{zst,gz}        # compressed snapshot
            snapshot_meta.json       # full sha256, size, timestamp, compression

The bundle records the FULL sha256 (not sha8) plus a path relative to
the project root. <sha8> directory is for human disambiguation only;
hash collisions on sha8 are caught by snapshot_meta.json verification.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import logging
import os
import shutil
import socket
import string
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Re-use the shared primitive (Phase 0a coordination). Bit 11.2 (2026-05-12)
# reorganized scripts/ into tier subdirs and moved `_state_db_snapshot.py`
# from `scripts/` → `scripts/ops/`; both paths are added defensively so this
# import survives any future move. P2.1.a-3 (2026-05-13, ticket 86b9wuhhr)
# surfaced the broken import.
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_OPS_DIR = SCRIPTS_DIR / 'ops'
for _p in (SCRIPTS_OPS_DIR, SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import _state_db_snapshot as _snap  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAP_ROOT = PROJECT_ROOT / 'data' / 'cal_mlp' / '_snapshots'
DEFAULT_DB = PROJECT_ROOT / 'state.db'

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Library API
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _snap_root_lock(snap_root: Path):
    """Cross-process file lock on snap_root/.lock. Serializes concurrent
    `take()` calls so two pipelines can't race on the same sha8 dir (C7)."""
    snap_root.mkdir(parents=True, exist_ok=True)
    lock_path = snap_root / '.lock'
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                logger.info("[snapshot] waiting for snap_root lock at %s", lock_path)
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                raise
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def take(src: Path, snap_root: Path) -> str:
    """Snapshot src → snap_root/<sha8>/state.db.{zst,gz} + snapshot_meta.json.

    Returns the FULL SHA-256 (64 hex chars) of the uncompressed snapshot.
    Idempotent: if a sha8 dir already contains a verified-matching snapshot,
    no work is done. Atomic: the entire <sha8>/ directory is staged under
    `_snapshots/.tmp_<sha8>_<unique>/` and `os.replace`-renamed into place,
    so a process crash leaves no half-published dir.
    """
    src = Path(src).resolve()
    snap_root = Path(snap_root).resolve()
    if not src.exists():
        raise FileNotFoundError(f"source DB not found: {src}")

    snap_root.mkdir(parents=True, exist_ok=True)
    _check_disk_space(
        src.stat().st_size, snap_root, snap_root_for_warn=snap_root
    )

    with _snap_root_lock(snap_root):
        with tempfile.TemporaryDirectory(prefix='snap_take_', dir=str(snap_root)) as tmp:
            tmp_path = Path(tmp)
            raw_dst = tmp_path / 'state.db'
            full_sha = _snap.take_snapshot(src, raw_dst)
            sha8 = full_sha[:8]
            sha8_dir = snap_root / sha8

            # Idempotence: if an existing dir is verified-matching, no-op.
            # Collision: if it exists with a DIFFERENT sha256, raise (do
            # NOT silently overwrite — would orphan prior bundles).
            existing_meta = sha8_dir / 'snapshot_meta.json'
            if existing_meta.exists():
                try:
                    meta = json.loads(existing_meta.read_text())
                except (OSError, ValueError):
                    meta = {}
                existing_sha = meta.get('sha256')
                existing_compressed = next(sha8_dir.glob('state.db.*'), None)
                if existing_sha == full_sha and existing_compressed and existing_compressed.exists():
                    logger.info(
                        "[snapshot] reusing existing %s (sha256=%s)",
                        sha8_dir, full_sha,
                    )
                    return full_sha
                if existing_sha and existing_sha != full_sha:
                    raise RuntimeError(
                        f"sha8 collision in {sha8_dir}: existing snapshot has "
                        f"sha256={existing_sha} but new snapshot has sha256={full_sha}. "
                        f"Refusing to overwrite (would orphan bundles referencing the "
                        f"existing snapshot). Move the existing dir aside manually."
                    )

            algo, level, compressed_tmp = _snap.compress(raw_dst, tmp_path / 'state.db')

            meta_payload = {
                'sha256': full_sha,
                'sha8': sha8,
                'size_bytes_uncompressed': raw_dst.stat().st_size,
                'size_bytes_compressed': compressed_tmp.stat().st_size,
                'compression': algo,
                'compression_level': level,
                'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
                'src_basename': src.name,
                'created_on_host': socket.gethostname(),
            }
            meta_tmp = tmp_path / 'snapshot_meta.json'
            meta_tmp.write_text(json.dumps(meta_payload, indent=2, sort_keys=True))

            # Stage the entire sha8 dir under a unique tmp name, then
            # os.replace it into place atomically. Survives mid-publish crash.
            stage = snap_root / f".tmp_{sha8}_{os.getpid()}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
            stage.mkdir()
            try:
                shutil.move(str(compressed_tmp), str(stage / compressed_tmp.name))
                shutil.move(str(meta_tmp), str(stage / 'snapshot_meta.json'))
                if sha8_dir.exists():
                    # Idempotence raced — clean up our stage and verify.
                    shutil.rmtree(stage)
                    return full_sha
                os.replace(str(stage), str(sha8_dir))
            except Exception:
                shutil.rmtree(stage, ignore_errors=True)
                raise

    logger.info(
        "[snapshot] took snapshot sha256=%s sha8=%s size_uncompressed=%d compression=%s",
        full_sha, sha8, meta_payload['size_bytes_uncompressed'], algo,
    )
    return full_sha


def verify(snap_root: Path, sha256: str) -> bool:
    """True iff a snapshot with the given sha256 exists in snap_root and
    decompresses to bytes matching that sha256."""
    snap_root = Path(snap_root)
    sha8 = sha256[:8]
    sha8_dir = snap_root / sha8
    if not sha8_dir.is_dir():
        return False
    meta_path = sha8_dir / 'snapshot_meta.json'
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return False
    if meta.get('sha256') != sha256:
        return False

    compressed = next(sha8_dir.glob('state.db.*'), None)
    if compressed is None or not compressed.exists():
        return False

    with tempfile.TemporaryDirectory(prefix='snap_verify_') as tmp:
        out = Path(tmp) / 'state.db'
        try:
            _snap.decompress(compressed, out)
        except (RuntimeError, ValueError):
            return False
        return _snap.verify_snapshot(out, sha256)


def snapshot_path_for(snap_root: Path, sha256: str) -> Optional[Path]:
    """Return the compressed file path for a snapshot, or None if not present."""
    sha8_dir = Path(snap_root) / sha256[:8]
    if not sha8_dir.is_dir():
        return None
    return next(sha8_dir.glob('state.db.*'), None)


def decompress_for_extract(
    snap_root: Path, sha256: str, scratch_dir: Path
) -> Path:
    """Decompress a snapshot to scratch_dir/<sha8>.db and return the path.
    Raises FileNotFoundError if the snapshot is missing, or RuntimeError on
    sha mismatch (covers compressed-file corruption + sha8 misroute)."""
    compressed = snapshot_path_for(snap_root, sha256)
    if compressed is None:
        raise FileNotFoundError(
            f"snapshot {sha256[:8]} not found under {snap_root}"
        )
    scratch_dir = Path(scratch_dir)
    # Ensure scratch_dir exists BEFORE the disk-space check so
    # `shutil.disk_usage` doesn't raise FileNotFoundError (which the
    # checker would have swallowed).
    scratch_dir.mkdir(parents=True, exist_ok=True)

    sha8_dir = Path(snap_root) / sha256[:8]
    meta_path = sha8_dir / 'snapshot_meta.json'
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            uncompressed_size = int(meta.get('size_bytes_uncompressed') or 0)
            if uncompressed_size > 0:
                _check_disk_space(uncompressed_size, scratch_dir, snap_root_for_warn=Path(snap_root))
        except (OSError, ValueError, TypeError):
            pass

    out = scratch_dir / f"{sha256[:8]}.db"
    if out.exists():
        if _snap.verify_snapshot(out, sha256):
            return out
        out.unlink()

    # Atomic publish to defend against concurrent decompress_for_extract
    # calls on the same sha (M2): write to a per-pid tmp, verify post-
    # decompress, then os.replace into final position. Concurrent readers
    # then see either the prior content or the fully-verified new content,
    # never a torn intermediate file.
    tmp_out = scratch_dir / f"{sha256[:8]}.db.tmp-{os.getpid()}"
    if tmp_out.exists():
        tmp_out.unlink()
    try:
        _snap.decompress(compressed, tmp_out)
        if not _snap.verify_snapshot(tmp_out, sha256):
            raise RuntimeError(
                f"snapshot {sha256[:8]} sha256 mismatch after decompress; "
                f"file may be corrupted at {compressed}"
            )
        os.replace(str(tmp_out), str(out))
    except Exception:
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass
        raise
    return out


def verify_bundle(bundle_path: Path, snap_root: Optional[Path] = None) -> dict:
    """Verify that a bundle's recorded snapshot is still byte-intact.

    Returns a dict with `sha256`, `verified`, `schema_match`,
    `integrity_check`, plus an `errors` list. `verified` is True iff the
    snapshot decompresses to bytes whose SHA-256 matches what the bundle
    recorded. If the bundle's `state_db_snapshot_sha256` is null (legacy
    pre-A.7 run), returns `verified: None` with a note. Implements ticket
    AC #2: "snapshot hash mismatches detected on bundle re-load."
    """
    bundle_path = Path(bundle_path)
    bundle = json.loads(bundle_path.read_text())
    sha256 = bundle.get('state_db_snapshot_sha256')
    out = {
        'bundle_path': str(bundle_path),
        'sha256': sha256,
        'verified': None,
        'schema_match': None,
        'integrity_check': None,
        'errors': [],
    }
    if sha256 is None:
        out['errors'].append(
            "bundle has no state_db_snapshot_sha256 (legacy run; not byte-reproducible)"
        )
        return out

    if snap_root is None:
        snap_root = PROJECT_ROOT / 'data' / 'cal_mlp' / '_snapshots'
    snap_root = Path(snap_root)

    try:
        with tempfile.TemporaryDirectory(prefix='snap_verify_bundle_') as tmp:
            decompressed = decompress_for_extract(snap_root, sha256, Path(tmp))
            out['verified'] = True

            recorded_schema_sha = bundle.get('state_db_schema_columns_sha256')
            if recorded_schema_sha:
                actual_schema_sha = _snap.schema_columns_sha256(
                    decompressed, 'evaluated_opportunities'
                )
                out['schema_match'] = (recorded_schema_sha == actual_schema_sha)
                if not out['schema_match']:
                    out['errors'].append(
                        f"schema drift: bundle recorded {recorded_schema_sha[:16]}, "
                        f"snapshot has {actual_schema_sha[:16]}"
                    )

            out['integrity_check'] = _snap.integrity_check(decompressed)
            if not out['integrity_check']:
                out['errors'].append("PRAGMA integrity_check failed on snapshot")
    except FileNotFoundError as e:
        out['verified'] = False
        out['errors'].append(f"snapshot file missing: {e}")
    except RuntimeError as e:
        out['verified'] = False
        out['errors'].append(str(e))
    except (OSError, ValueError, EOFError) as e:
        # Catches gzip.BadGzipFile (subclasses OSError), zstd decompression
        # errors, and truncated-file EOFs. All represent compressed-file
        # corruption from the bundle's perspective.
        out['verified'] = False
        out['errors'].append(f"snapshot decompression failed: {type(e).__name__}: {e}")

    # Final coherence: verified=True with errors is a confusing surface
    # (schema drift / integrity failure surfaces both). Demote to False
    # so JSON `verified` always tracks CLI exit-code semantics.
    if out['verified'] is True and out['errors']:
        out['verified'] = False

    return out


# ---------------------------------------------------------------------------
# Disk-space safety
# ---------------------------------------------------------------------------

def _check_disk_space(
    src_size: int, target_dir: Path, snap_root_for_warn: Optional[Path] = None
) -> None:
    """Abort if `target_dir`'s filesystem has < 2x src_size free.

    `snap_root_for_warn` is OPTIONAL and triggers a warning if the
    snapshot tree has grown past 5 GB. The two thresholds are separate:
    `target_dir` is where the next file lands (snap_root for take(),
    scratch_dir for decompress); `snap_root_for_warn` is the long-lived
    tree we monitor for "should you archive off-Mac yet?" guidance.
    """
    usage = shutil.disk_usage(str(target_dir))
    if usage.free < 2 * src_size:
        raise RuntimeError(
            f"insufficient disk space at {target_dir}: free={usage.free} bytes, "
            f"need ≥{2 * src_size} bytes (2x source). Run "
            f"`python -m scripts.cal_mlp.snapshot_state_db prune --keep-last N` "
            f"or free disk."
        )
    if snap_root_for_warn is not None and snap_root_for_warn.exists():
        # Skip dotfiles AND any path under a dotted parent (`.tmp_*`,
        # `.lock`, etc.) — those are crashed-stage / lock files, not
        # active retained snapshots.
        total = sum(
            p.stat().st_size for p in snap_root_for_warn.rglob('*')
            if p.is_file() and not any(part.startswith('.') for part in p.parts)
        )
        if total > 5 * 1024 * 1024 * 1024:
            logger.warning(
                "[snapshot] %s is %.2f GB; consider archiving old snapshots off-Mac",
                snap_root_for_warn, total / (1024 ** 3),
            )


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------

def _list_snapshots(snap_root: Path) -> list:
    if not snap_root.is_dir():
        return []
    out = []
    for sha8_dir in sorted(snap_root.iterdir()):
        if not sha8_dir.is_dir():
            continue
        meta_path = sha8_dir / 'snapshot_meta.json'
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                out.append(meta)
            except (OSError, ValueError):
                continue
    return out


def _cmd_take(args: argparse.Namespace) -> int:
    sha = take(src=args.db, snap_root=args.snap_root)
    print(json.dumps({'sha256': sha, 'sha8': sha[:8]}, indent=2))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    if args.bundle is not None:
        result = verify_bundle(args.bundle, args.snap_root)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result['verified'] is True and not result['errors'] else 1
    if not args.sha256:
        raise SystemExit("verify requires --sha256 <hex> or --bundle <path>")
    if not _is_valid_sha256(args.sha256):
        raise SystemExit(f"invalid sha256: must be 64 lowercase hex chars; got {args.sha256!r}")
    ok = verify(snap_root=args.snap_root, sha256=args.sha256)
    print(json.dumps({'sha256': args.sha256, 'verified': ok}))
    return 0 if ok else 1


def _is_valid_sha256(s: str) -> bool:
    return isinstance(s, str) and len(s) == 64 and all(c in string.hexdigits.lower() for c in s)


def _cmd_list(args: argparse.Namespace) -> int:
    items = _list_snapshots(args.snap_root)
    print(json.dumps(items, indent=2, sort_keys=True))
    return 0


def _cmd_prune(args: argparse.Namespace) -> int:
    # Hold the snap_root lock for the duration of prune so we don't race
    # against concurrent take() / decompress_for_extract() runs (R3 M1).
    with _snap_root_lock(Path(args.snap_root)):
        items = _list_snapshots(args.snap_root)
        items.sort(key=lambda m: m.get('created_at', ''), reverse=True)
        keep = items[:args.keep_last]
        drop = items[args.keep_last:]
        for meta in drop:
            sha8 = meta.get('sha8') or meta.get('sha256', '')[:8]
            if not sha8:
                continue
            target = args.snap_root / sha8
            if args.dry_run:
                print(f"[dry-run] would remove {target}")
            else:
                shutil.rmtree(target, ignore_errors=True)
                print(f"removed {target}")

        # Sweep `_scratch/` of decompressed files no longer referenced.
        # Crucial: skip in-flight `.tmp-<pid>` files of concurrent
        # decompress operations — unlinking them mid-write breaks the
        # `os.replace` step in decompress_for_extract.
        scratch = args.snap_root / '_scratch'
        if scratch.is_dir():
            kept_sha8 = {m.get('sha8') for m in keep if m.get('sha8')}
            for f in scratch.iterdir():
                if not f.is_file():
                    continue
                if '.tmp-' in f.name:
                    continue  # in-flight decompress; never touch
                if not f.name.endswith('.db'):
                    continue
                sha8 = f.stem  # `<sha8>.db` → `<sha8>`
                if sha8 not in kept_sha8:
                    if args.dry_run:
                        print(f"[dry-run] would remove scratch {f}")
                    else:
                        try:
                            f.unlink()
                            print(f"removed scratch {f}")
                        except OSError:
                            pass

    print(json.dumps(
        {'kept': len(keep), 'dropped': 0 if args.dry_run else len(drop), 'dry_run': args.dry_run},
        indent=2,
    ))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description='Content-addressed immutable training-data snapshots.',
    )
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_take = sub.add_parser('take', help='Snapshot state.db into _snapshots/')
    p_take.add_argument('--db', type=Path, default=DEFAULT_DB)
    p_take.add_argument('--snap-root', type=Path, default=DEFAULT_SNAP_ROOT)
    p_take.set_defaults(func=_cmd_take)

    p_verify = sub.add_parser('verify', help='Verify a snapshot by sha256 or bundle')
    grp = p_verify.add_mutually_exclusive_group(required=True)
    grp.add_argument('--sha256', help='Verify a single snapshot by SHA-256 hex')
    grp.add_argument('--bundle', type=Path, help='Verify the snapshot a bundle.json references')
    p_verify.add_argument('--snap-root', type=Path, default=DEFAULT_SNAP_ROOT)
    p_verify.set_defaults(func=_cmd_verify)

    p_list = sub.add_parser('list', help='List all snapshots')
    p_list.add_argument('--snap-root', type=Path, default=DEFAULT_SNAP_ROOT)
    p_list.set_defaults(func=_cmd_list)

    p_prune = sub.add_parser('prune', help='Remove all but the last N snapshots')
    p_prune.add_argument('--keep-last', type=int, required=True)
    p_prune.add_argument('--snap-root', type=Path, default=DEFAULT_SNAP_ROOT)
    p_prune.add_argument('--dry-run', action='store_true')
    p_prune.set_defaults(func=_cmd_prune)

    return ap


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
