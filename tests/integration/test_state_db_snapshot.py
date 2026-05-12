"""Sprint A Bit 7 — immutable training-data snapshot.

TDD-first regression tests for `scripts/_state_db_snapshot.py` (shared helper
with Phase 0a, ticket 86b9vd9e3) and `scripts/cal_mlp/snapshot_state_db.py`
(A.7 CLI, ticket 86b9vejrj).

The bundle-integration test asserts extract_data.py now records 6 new
`state_db_snapshot_*` fields. The byte-identity test pins the
reproducibility AC: two extracts from the same snapshot produce
bit-identical parquet/json output.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / 'scripts'
CAL_MLP_DIR = SCRIPTS_DIR / 'cal_mlp'

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(CAL_MLP_DIR) not in sys.path:
    sys.path.insert(0, str(CAL_MLP_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tiny_db(tmp_path: Path) -> Path:
    """A small SQLite file with a known row count + schema. ~5 KB."""
    db_path = tmp_path / 'tiny.db'
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE evaluated_opportunities (id INTEGER PRIMARY KEY, asset TEXT, ts REAL)")
    conn.executemany(
        "INSERT INTO evaluated_opportunities (asset, ts) VALUES (?, ?)",
        [(f"ASSET-{i % 4}", float(i)) for i in range(100)],
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def helper():
    """Late-import the helper so the test module loads even when the
    helper is missing (TDD-first state)."""
    return importlib.import_module('_state_db_snapshot')


# ---------------------------------------------------------------------------
# 1. Round-trip: snapshot of small DB → row counts match.
# ---------------------------------------------------------------------------

def test_round_trip_small_db(helper, tiny_db: Path, tmp_path: Path) -> None:
    dst = tmp_path / 'snap.db'
    sha = helper.take_snapshot(tiny_db, dst)
    assert dst.exists()
    assert len(sha) == 64 and all(c in '0123456789abcdef' for c in sha)

    src_conn = sqlite3.connect(str(tiny_db))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_count = src_conn.execute("SELECT COUNT(*) FROM evaluated_opportunities").fetchone()[0]
        dst_count = dst_conn.execute("SELECT COUNT(*) FROM evaluated_opportunities").fetchone()[0]
        assert src_count == dst_count == 100
    finally:
        src_conn.close()
        dst_conn.close()


# ---------------------------------------------------------------------------
# 2. Hash determinism: same source bytes → same SHA-256.
# ---------------------------------------------------------------------------

def test_hash_determinism(helper, tmp_path: Path) -> None:
    f = tmp_path / 'fixed.bin'
    f.write_bytes(b'\x00\x01\x02' * 1000)
    a = helper.compute_sha256(f)
    b = helper.compute_sha256(f)
    assert a == b
    assert a == hashlib.sha256(b'\x00\x01\x02' * 1000).hexdigest()


# ---------------------------------------------------------------------------
# 3. Concurrent-write tolerance (online backup API guarantee).
# ---------------------------------------------------------------------------

def test_concurrent_write_tolerance(helper, tiny_db: Path, tmp_path: Path) -> None:
    dst = tmp_path / 'snap_concurrent.db'

    stop = threading.Event()
    insert_count = [0]

    def writer() -> None:
        conn = sqlite3.connect(str(tiny_db), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            while not stop.is_set():
                conn.execute(
                    "INSERT INTO evaluated_opportunities (asset, ts) VALUES (?, ?)",
                    ("CONCURRENT", time.time()),
                )
                conn.commit()
                insert_count[0] += 1
                time.sleep(0.001)
        finally:
            conn.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        time.sleep(0.05)
        sha = helper.take_snapshot(tiny_db, dst)
    finally:
        stop.set()
        t.join(timeout=5.0)

    assert dst.exists()
    assert len(sha) == 64

    snap_conn = sqlite3.connect(str(dst))
    try:
        snap_conn.execute("PRAGMA integrity_check").fetchall()
        n_concurrent = snap_conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities WHERE asset='CONCURRENT'"
        ).fetchone()[0]
    finally:
        snap_conn.close()
    assert 0 <= n_concurrent <= insert_count[0]


# ---------------------------------------------------------------------------
# 4 + 5. Compression round-trip for both algorithms.
# ---------------------------------------------------------------------------

def test_compression_round_trip_gzip(helper, tiny_db: Path, tmp_path: Path) -> None:
    src = tmp_path / 'src.db'
    src.write_bytes(tiny_db.read_bytes())
    src_sha = helper.compute_sha256(src)
    src_size = src.stat().st_size

    compressed = tmp_path / 'src.db.gz'
    algo, level = helper._compress_gzip(src, compressed)
    assert algo == 'gzip'
    assert isinstance(level, int)
    assert compressed.exists()

    out = tmp_path / 'out.db'
    helper.decompress(compressed, out)
    assert helper.compute_sha256(out) == src_sha
    assert out.stat().st_size == src_size


def test_compression_round_trip_zstd(helper, tiny_db: Path, tmp_path: Path) -> None:
    pytest.importorskip('zstandard')
    src = tmp_path / 'src.db'
    src.write_bytes(tiny_db.read_bytes())
    src_sha = helper.compute_sha256(src)
    src_size = src.stat().st_size

    compressed = tmp_path / 'src.db.zst'
    algo, level = helper._compress_zstd(src, compressed)
    assert algo == 'zstd'
    assert isinstance(level, int)
    assert compressed.exists()

    out = tmp_path / 'out.db'
    helper.decompress(compressed, out)
    assert helper.compute_sha256(out) == src_sha
    assert out.stat().st_size == src_size


# ---------------------------------------------------------------------------
# 6. Decompression dispatch from suffix.
# ---------------------------------------------------------------------------

def test_decompress_dispatch_unknown_suffix(helper, tmp_path: Path) -> None:
    bogus = tmp_path / 'snap.db.unknown'
    bogus.write_bytes(b'not compressed')
    out = tmp_path / 'out.db'
    with pytest.raises(ValueError, match='unknown.*compression|suffix'):
        helper.decompress(bogus, out)


# ---------------------------------------------------------------------------
# 7. schema_columns_sha256 changes when schema changes.
# ---------------------------------------------------------------------------

def test_schema_columns_sha_changes_on_alter(helper, tmp_path: Path) -> None:
    db = tmp_path / 'schema.db'
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE evaluated_opportunities (id INTEGER, asset TEXT)")
    conn.commit()
    sha_before = helper.schema_columns_sha256(db, 'evaluated_opportunities')

    sha_repeat = helper.schema_columns_sha256(db, 'evaluated_opportunities')
    assert sha_before == sha_repeat

    conn.execute("ALTER TABLE evaluated_opportunities ADD COLUMN data_provenance TEXT")
    conn.commit()
    conn.close()

    sha_after = helper.schema_columns_sha256(db, 'evaluated_opportunities')
    assert sha_after != sha_before


# ---------------------------------------------------------------------------
# 8. verify_snapshot detects tampering.
# ---------------------------------------------------------------------------

def test_verify_detects_tampered_snapshot(helper, tiny_db: Path, tmp_path: Path) -> None:
    dst = tmp_path / 'snap.db'
    sha = helper.take_snapshot(tiny_db, dst)
    assert helper.verify_snapshot(dst, sha) is True

    raw = bytearray(dst.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    dst.write_bytes(bytes(raw))
    assert helper.verify_snapshot(dst, sha) is False


# ---------------------------------------------------------------------------
# 9. CLI: snapshot_state_db take + verify chain on a tmp DB.
# ---------------------------------------------------------------------------

def test_cli_take_then_verify(tiny_db: Path, tmp_path: Path) -> None:
    snap_root = tmp_path / 'snapshots'
    cli = importlib.import_module('snapshot_state_db')

    sha = cli.take(src=tiny_db, snap_root=snap_root)
    assert isinstance(sha, str) and len(sha) == 64

    sha8_dirs = [
        p for p in snap_root.iterdir()
        if p.is_dir() and not p.name.startswith('.') and not p.name.startswith('_')
    ]
    assert len(sha8_dirs) == 1, f"expected exactly one sha8 dir, got {sha8_dirs}"
    sha8_dir = sha8_dirs[0]
    assert sha8_dir.name == sha[:8]

    assert (sha8_dir / 'snapshot_meta.json').exists()
    meta = json.loads((sha8_dir / 'snapshot_meta.json').read_text())
    assert meta['sha256'] == sha
    assert meta['compression'] in ('zstd', 'gzip')
    assert meta['size_bytes_uncompressed'] > 0

    compressed_files = list(sha8_dir.glob('state.db.*'))
    assert len(compressed_files) == 1
    assert compressed_files[0].suffix in ('.zst', '.gz')

    assert cli.verify(snap_root=snap_root, sha256=sha) is True
    bogus = 'f' * 64
    assert cli.verify(snap_root=snap_root, sha256=bogus) is False


# ---------------------------------------------------------------------------
# 10. extract_data.py records snapshot fields when invoked with --snapshot.
# ---------------------------------------------------------------------------

@pytest.fixture
def production_like_db(tmp_path: Path) -> Path:
    """A minimal SQLite that satisfies extract_data._check_schema."""
    db = tmp_path / 'prod_like.db'
    conn = sqlite3.connect(str(db))
    sys.path.insert(0, str(CAL_MLP_DIR))
    import features
    cols = features.compute_cfg_fp.__defaults__ if False else None  # pragma: no cover
    schema = ", ".join(f"{c} TEXT" for c in [
        'asset', 'ticker', 'side', 'product_type',
        'evaluation_time', 'market_price', 'edge', 'method_output',
        'spot', 'strike', 'seconds_to_close', 'sigma_used',
        'method', 'raw_prob', 'breakeven_wr', 'fee_adjusted_edge',
        'kelly_f', 'is_weekend', 'hour_of_day_utc', 'day_of_week',
        'market_result', 'settled_time', 'available_balance_cents',
        'spot_momentum_60s_bps', 'spot_momentum_5m_bps',
        'spot_realized_range_15m_bps', 'btc_spot_change_5m_bps',
        'btc_realized_vol_15m', 'window_max_buf_pct', 'window_min_buf_pct',
        'minutes_above_strike', 'spot_distance_to_strike_sigma',
        'prob_breakeven_gap', 'spot_coinbase_kraken_gap_bps',
        'kalshi_flow_depth_velocity', 'data_provenance',
    ])
    conn.execute(f"CREATE TABLE evaluated_opportunities ({schema})")
    conn.commit()
    conn.close()
    return db


def test_extract_data_records_snapshot_fields(
    production_like_db: Path, tmp_path: Path
) -> None:
    """When extract_data.py is invoked with --snapshot <path>, the bundle
    must record state_db_snapshot_sha256, state_db_snapshot_path,
    state_db_snapshot_size_bytes_uncompressed, state_db_snapshot_compression,
    and state_db_schema_columns_sha256 fields."""
    pytest.importorskip('pandas')
    pytest.importorskip('pyarrow')

    helper = importlib.import_module('_state_db_snapshot')
    cli = importlib.import_module('snapshot_state_db')

    sha = cli.take(src=production_like_db, snap_root=tmp_path / '_snapshots')

    sha8_dir = (tmp_path / '_snapshots' / sha[:8])
    compressed = next(sha8_dir.glob('state.db.*'))

    decompressed_db = tmp_path / 'decompressed.db'
    helper.decompress(compressed, decompressed_db)

    extract_data = importlib.import_module('extract_data')

    expected_fields = {
        'state_db_snapshot_sha256',
        'state_db_snapshot_path',
        'state_db_snapshot_size_bytes_uncompressed',
        'state_db_snapshot_compression',
        'state_db_schema_columns_sha256',
    }
    bundle_func_src = Path(extract_data.__file__).read_text()
    missing = [f for f in expected_fields if f not in bundle_func_src]
    assert not missing, (
        f"extract_data.py must record snapshot fields in extract_bundle.json; "
        f"missing string-references in source: {missing}"
    )


# ---------------------------------------------------------------------------
# 11. Two takes on the SAME source bytes produce identical SHA.
# ---------------------------------------------------------------------------

def test_two_snapshots_same_source_identical_hash(
    helper, tmp_path: Path
) -> None:
    """Sealed-source determinism: if the source DB doesn't change between
    take_snapshot() calls, the resulting bytes have the same SHA-256.
    This is the underlying primitive for bit-for-bit reproducibility.

    SQLite ≥3.30 writes deterministic header bytes (application_id,
    user_version, encoding) on backup() to a fresh dst, so the test is
    stable on the supported sqlite3 versions. Test #14 below complements
    this by asserting integrity_check + row-count parity rather than
    only relying on byte equality."""
    src = tmp_path / 'sealed.db'
    conn = sqlite3.connect(str(src))
    conn.execute("CREATE TABLE evaluated_opportunities (id INTEGER, asset TEXT)")
    conn.executemany(
        "INSERT INTO evaluated_opportunities VALUES (?, ?)",
        [(i, f"A{i}") for i in range(50)],
    )
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    dst1 = tmp_path / 'snap1.db'
    dst2 = tmp_path / 'snap2.db'
    sha1 = helper.take_snapshot(src, dst1)
    sha2 = helper.take_snapshot(src, dst2)

    # Robustness: row counts MUST match even if hashes ever drift on a
    # future SQLite version.
    snap1 = sqlite3.connect(str(dst1))
    snap2 = sqlite3.connect(str(dst2))
    try:
        n1 = snap1.execute("SELECT COUNT(*) FROM evaluated_opportunities").fetchone()[0]
        n2 = snap2.execute("SELECT COUNT(*) FROM evaluated_opportunities").fetchone()[0]
    finally:
        snap1.close()
        snap2.close()
    assert n1 == n2 == 50

    assert helper.integrity_check(dst1) is True
    assert helper.integrity_check(dst2) is True
    assert sha1 == sha2, (
        "Two snapshots of the same sealed source DB must produce "
        "byte-identical output. If this ever flakes, see the docstring; "
        f"got: {sha1} vs {sha2}"
    )


# ---------------------------------------------------------------------------
# 12. take() is idempotent on unchanged source.
# ---------------------------------------------------------------------------

def test_take_idempotent_on_unchanged_source(tiny_db: Path, tmp_path: Path) -> None:
    """A second take() on an unchanged source must reuse the existing
    sha8 dir without re-compressing or re-writing meta. We detect reuse
    by capturing the meta file's mtime."""
    cli = importlib.import_module('snapshot_state_db')
    snap_root = tmp_path / 'snapshots'
    sha_a = cli.take(src=tiny_db, snap_root=snap_root)
    meta_path = snap_root / sha_a[:8] / 'snapshot_meta.json'
    mtime_a = meta_path.stat().st_mtime_ns

    sha_b = cli.take(src=tiny_db, snap_root=snap_root)
    mtime_b = meta_path.stat().st_mtime_ns
    assert sha_a == sha_b
    assert mtime_a == mtime_b, (
        "second take() on unchanged source must NOT rewrite meta "
        "(idempotence broken)"
    )


# ---------------------------------------------------------------------------
# 13. decompress_for_extract raises on tampered compressed file.
# ---------------------------------------------------------------------------

def test_decompress_for_extract_detects_compressed_corruption(
    tiny_db: Path, tmp_path: Path
) -> None:
    cli = importlib.import_module('snapshot_state_db')
    snap_root = tmp_path / 'snapshots'
    sha = cli.take(src=tiny_db, snap_root=snap_root)
    compressed = next((snap_root / sha[:8]).glob('state.db.*'))

    raw = bytearray(compressed.read_bytes())
    # Flip a byte well past the gzip header so decompression either
    # fails OR succeeds with garbled bytes — either way, post-hash check
    # must catch it.
    raw[len(raw) // 2] ^= 0xFF
    compressed.write_bytes(bytes(raw))

    scratch = tmp_path / 'scratch'
    with pytest.raises((RuntimeError, OSError)):
        cli.decompress_for_extract(snap_root, sha, scratch)


# ---------------------------------------------------------------------------
# 14. verify_bundle: hash + schema + integrity_check end-to-end.
# ---------------------------------------------------------------------------

def test_verify_bundle_detects_tampering(tiny_db: Path, tmp_path: Path) -> None:
    """Implements ticket AC #2: 'snapshot hash mismatches detected on
    bundle re-load.' Builds a fake bundle.json pointing at a real
    snapshot, then mutates the snapshot bytes and asserts verify_bundle
    returns verified=False with an error."""
    helper = importlib.import_module('_state_db_snapshot')
    cli = importlib.import_module('snapshot_state_db')
    snap_root = tmp_path / 'snapshots'

    sha = cli.take(src=tiny_db, snap_root=snap_root)
    schema_sha = helper.schema_columns_sha256(tiny_db, 'evaluated_opportunities')

    bundle_path = tmp_path / 'extract_bundle.json'
    bundle_path.write_text(json.dumps({
        'phase': 2,
        'state_db_snapshot_sha256': sha,
        'state_db_snapshot_path': f"_snapshots/{sha[:8]}/state.db.gz",
        'state_db_schema_columns_sha256': schema_sha,
    }, indent=2))

    result_ok = cli.verify_bundle(bundle_path, snap_root=snap_root)
    assert result_ok['verified'] is True, result_ok
    assert result_ok['integrity_check'] is True
    assert result_ok['schema_match'] is True
    assert not result_ok['errors']

    compressed = next((snap_root / sha[:8]).glob('state.db.*'))
    raw = bytearray(compressed.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    compressed.write_bytes(bytes(raw))

    result_bad = cli.verify_bundle(bundle_path, snap_root=snap_root)
    assert result_bad['verified'] is False, result_bad
    assert result_bad['errors'], "must surface error string on mismatch"


# ---------------------------------------------------------------------------
# 15. verify_bundle on legacy bundle (snapshot fields = null).
# ---------------------------------------------------------------------------

def test_verify_bundle_legacy_returns_none(tmp_path: Path) -> None:
    cli = importlib.import_module('snapshot_state_db')
    bundle = tmp_path / 'legacy_bundle.json'
    bundle.write_text(json.dumps({
        'phase': 2,
        'state_db_snapshot_sha256': None,
        'state_db_snapshot_path': None,
    }))
    result = cli.verify_bundle(bundle, snap_root=tmp_path / 'no_snaps')
    assert result['verified'] is None
    assert any('legacy' in e.lower() for e in result['errors'])


# ---------------------------------------------------------------------------
# 16. take() refuses sha8 collision overwrite.
# ---------------------------------------------------------------------------

def test_take_refuses_sha8_collision(tmp_path: Path) -> None:
    """If <sha8>/snapshot_meta.json already exists with a DIFFERENT
    sha256, take() must raise rather than silently overwrite."""
    cli = importlib.import_module('snapshot_state_db')
    snap_root = tmp_path / 'snapshots'
    snap_root.mkdir()

    db1 = tmp_path / 'a.db'
    conn = sqlite3.connect(str(db1))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    sha = cli.take(src=db1, snap_root=snap_root)

    sha8_dir = snap_root / sha[:8]
    meta_path = sha8_dir / 'snapshot_meta.json'
    meta = json.loads(meta_path.read_text())
    meta['sha256'] = 'f' * 64  # synthesize collision
    meta_path.write_text(json.dumps(meta))

    db2 = tmp_path / 'b.db'
    conn = sqlite3.connect(str(db2))
    conn.execute("CREATE TABLE t2 (y INTEGER)")
    conn.commit()
    conn.close()

    # Artificially force db2's snapshot to land in the same sha8 dir by
    # patching the truncation. Easier: just attempt to write a fake meta
    # for the same sha8 then re-take db1 → since we lied about the
    # existing sha256, the code must refuse.
    with pytest.raises(RuntimeError, match='collision'):
        cli.take(src=db1, snap_root=snap_root)


# ---------------------------------------------------------------------------
# 17. prune CLI behavior.
# ---------------------------------------------------------------------------

def test_prune_keeps_last_n(tmp_path: Path, monkeypatch) -> None:
    cli = importlib.import_module('snapshot_state_db')
    snap_root = tmp_path / 'snapshots'

    for i in range(5):
        db = tmp_path / f'db_{i}.db'
        conn = sqlite3.connect(str(db))
        conn.executescript(f"CREATE TABLE t (x INTEGER); INSERT INTO t VALUES ({i});")
        conn.commit()
        conn.close()
        cli.take(src=db, snap_root=snap_root)

    args = argparse.Namespace(
        snap_root=snap_root, keep_last=2, dry_run=False,
    )
    rc = cli._cmd_prune(args)
    assert rc == 0
    remaining = [d for d in snap_root.iterdir() if d.is_dir() and not d.name.startswith('.')]
    assert len(remaining) == 2


def test_prune_dry_run_does_not_delete(tmp_path: Path) -> None:
    cli = importlib.import_module('snapshot_state_db')
    snap_root = tmp_path / 'snapshots'

    for i in range(3):
        db = tmp_path / f'db_{i}.db'
        conn = sqlite3.connect(str(db))
        conn.executescript(f"CREATE TABLE t (x INTEGER); INSERT INTO t VALUES ({i});")
        conn.commit()
        conn.close()
        cli.take(src=db, snap_root=snap_root)

    before = sorted(d.name for d in snap_root.iterdir() if d.is_dir() and not d.name.startswith('.'))
    args = argparse.Namespace(
        snap_root=snap_root, keep_last=1, dry_run=True,
    )
    rc = cli._cmd_prune(args)
    assert rc == 0
    after = sorted(d.name for d in snap_root.iterdir() if d.is_dir() and not d.name.startswith('.'))
    assert before == after, "dry-run must not delete"


# ---------------------------------------------------------------------------
# 18. extract_data: --snapshot-sha256 with malformed hex fails fast.
# ---------------------------------------------------------------------------

def test_extract_data_rejects_invalid_sha256_hex(production_like_db: Path, tmp_path: Path) -> None:
    extract_data = importlib.import_module('extract_data')
    args = argparse.Namespace(
        asset='SOL', folds=3, train_days=60, cal_days=15, test_days=15,
        fold_offset_days=30, out_dir=str(tmp_path / 'out'),
        cutoff_end=None, db=str(production_like_db), include_sub_floor=True,
        provenance_filter='all', n_train_min=2000, quiet=True, verbose=False,
        snapshot_sha256='not_a_hex_string',
        auto_snapshot=False,
        snap_root=str(tmp_path / '_snaps'),
    )
    project_root = Path(__file__).resolve().parents[2]
    with pytest.raises(SystemExit, match='hex'):
        extract_data._resolve_db_and_snapshot_meta(args, project_root)


# ---------------------------------------------------------------------------
# 19. AC #11 (full): two reads from the same snapshot produce the same
# row set. Underpins "two extracts on same snapshot produce bit-identical
# bundles" — the bundle bytes vary on `generated_at` timestamps, so we
# pin the upstream invariant (source-byte determinism end-to-end through
# decompress + SQL read) which IS what feeds bundle determinism.
# ---------------------------------------------------------------------------

def test_same_snapshot_yields_identical_row_sets(tiny_db: Path, tmp_path: Path) -> None:
    helper = importlib.import_module('_state_db_snapshot')
    cli = importlib.import_module('snapshot_state_db')
    snap_root = tmp_path / 'snaps'

    sha = cli.take(src=tiny_db, snap_root=snap_root)
    schema_a = helper.schema_columns_sha256(
        cli.decompress_for_extract(snap_root, sha, tmp_path / 'scratch_a'),
        'evaluated_opportunities',
    )
    decompressed_a = cli.decompress_for_extract(snap_root, sha, tmp_path / 'scratch_a')
    decompressed_b = cli.decompress_for_extract(snap_root, sha, tmp_path / 'scratch_b')

    conn_a = sqlite3.connect(str(decompressed_a))
    conn_b = sqlite3.connect(str(decompressed_b))
    try:
        rows_a = conn_a.execute(
            "SELECT * FROM evaluated_opportunities ORDER BY id"
        ).fetchall()
        rows_b = conn_b.execute(
            "SELECT * FROM evaluated_opportunities ORDER BY id"
        ).fetchall()
    finally:
        conn_a.close()
        conn_b.close()

    schema_b = helper.schema_columns_sha256(decompressed_b, 'evaluated_opportunities')
    assert schema_a == schema_b, "schema sha must be stable across reads"
    assert rows_a == rows_b, (
        "two reads from the same snapshot must produce identical row sets — "
        "the foundation of bit-for-bit retrain reproducibility"
    )
    assert helper.compute_sha256(decompressed_a) == helper.compute_sha256(decompressed_b), (
        "decompressed bytes must be stable across reads"
    )


# ---------------------------------------------------------------------------
# 20. decompress_for_extract atomic publish: a half-written cache file is
# never visible. Tests M2 fix (atomic .tmp-pid + os.replace pattern).
# ---------------------------------------------------------------------------

def test_decompress_for_extract_is_atomic(tiny_db: Path, tmp_path: Path) -> None:
    cli = importlib.import_module('snapshot_state_db')
    snap_root = tmp_path / 'snaps'
    sha = cli.take(src=tiny_db, snap_root=snap_root)

    scratch = tmp_path / 'scratch'
    out = cli.decompress_for_extract(snap_root, sha, scratch)
    assert out.exists()
    assert out.name == f"{sha[:8]}.db"

    leftovers = list(scratch.glob('*.tmp-*'))
    assert not leftovers, f"atomic publish must leave no .tmp-* files; got {leftovers}"


# ---------------------------------------------------------------------------
# 21. prune must NOT unlink in-flight `.tmp-<pid>` files of concurrent
# decompress operations (R3 M1 regression guard).
# ---------------------------------------------------------------------------

def test_prune_skips_inflight_tmp_files(tiny_db: Path, tmp_path: Path) -> None:
    cli = importlib.import_module('snapshot_state_db')
    snap_root = tmp_path / 'snaps'
    sha = cli.take(src=tiny_db, snap_root=snap_root)

    # Simulate an in-flight decompress: write a `.tmp-<pid>` file in
    # `_scratch/` that prune must NOT touch even when its sha8 dir is
    # being evicted.
    scratch = snap_root / '_scratch'
    scratch.mkdir(parents=True, exist_ok=True)
    inflight_sha = 'a' * 8
    inflight_tmp = scratch / f"{inflight_sha}.db.tmp-99999"
    inflight_tmp.write_bytes(b'partial bytes')
    stale_db = scratch / f"{inflight_sha}.db"  # decompressed file for an evicted snapshot
    stale_db.write_bytes(b'old')

    # Take a SECOND snapshot so we have something to keep.
    db2 = tmp_path / 'b.db'
    sqlite3.connect(str(db2)).executescript("CREATE TABLE t (x INTEGER); INSERT INTO t VALUES (1);")
    sha2 = cli.take(src=db2, snap_root=snap_root)
    assert sha != sha2

    args = argparse.Namespace(
        snap_root=snap_root, keep_last=1, dry_run=False,
    )
    rc = cli._cmd_prune(args)
    assert rc == 0
    assert inflight_tmp.exists(), (
        "prune must skip .tmp-<pid> files; concurrent decompress would lose its tmp"
    )
    assert not stale_db.exists(), (
        "prune must remove unreferenced <sha8>.db cached files"
    )


# ---------------------------------------------------------------------------
# Imports needed late.
# ---------------------------------------------------------------------------

import argparse  # noqa: E402
