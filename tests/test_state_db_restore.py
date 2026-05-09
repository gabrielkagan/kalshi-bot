"""Tests for scripts/state_db_restore.py — verify + restore.

Pairs with test_state_db_s3_backup.py. Tests the verify-only path
(weekly automated check) AND the restore-to-path path (manual incident
recovery), including the safety guards against overwriting live state.db.

Ticket: 86b9vd9e3.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def _make_test_db(path: Path, n_rows: int = 50) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE settled_trades (ticker TEXT PRIMARY KEY, pnl_cents INTEGER);
        CREATE TABLE evaluated_opportunities (id INTEGER PRIMARY KEY AUTOINCREMENT, edge REAL);
        CREATE TABLE rejected_opportunities (ticker TEXT PRIMARY KEY);
        """
    )
    for i in range(n_rows):
        conn.execute("INSERT INTO settled_trades VALUES (?, ?)", (f"K-{i}", i))
        conn.execute("INSERT INTO evaluated_opportunities (edge) VALUES (?)", (0.01 + i * 0.001,))
        conn.execute("INSERT INTO rejected_opportunities VALUES (?)", (f"R-{i}",))
    conn.commit()
    conn.close()


@pytest.fixture
def populated_store(tmp_path):
    """A LocalDirStore with three days of snapshots already uploaded."""
    import state_db_s3_backup as backup

    store_root = tmp_path / "store"
    store = backup.LocalDirStore(store_root)

    for n_rows, day in ((50, date(2026, 5, 7)), (60, date(2026, 5, 8)), (70, date(2026, 5, 9))):
        live = tmp_path / f"live-{day}.db"
        _make_test_db(live, n_rows=n_rows)
        backup.run_backup(
            db_path=live,
            store=store,
            today=day,
            tmp_dir=tmp_path / f"tmp-{day}",
            algorithm="zstd",
            min_free_mb=1,
        )
        live.unlink()

    return store


@pytest.fixture
def restore_module():
    import state_db_restore
    return state_db_restore


# ── fetch_latest ───────────────────────────────────────────────────────


class TestFetchLatest:
    def test_returns_lex_max_key(self, populated_store, restore_module):
        latest = restore_module.fetch_latest(populated_store)
        assert latest == "daily/state-db-2026-05-09.db.zst"

    def test_raises_on_empty_store(self, tmp_path, restore_module):
        import state_db_s3_backup as backup
        empty = backup.LocalDirStore(tmp_path / "empty")
        with pytest.raises(RuntimeError, match="no snapshots"):
            restore_module.fetch_latest(empty)


# ── integrity + row counts ─────────────────────────────────────────────


class TestIntegrityAndCounts:
    def test_integrity_check_ok_on_valid_db(self, tmp_path, restore_module):
        db = tmp_path / "test.db"
        _make_test_db(db, n_rows=10)
        # Round-1 A-M2: integrity_check now returns a list of issues
        # (empty == clean) including foreign_key_check results.
        assert restore_module.integrity_check(db) == []

    def test_row_counts_returns_expected_tables(self, tmp_path, restore_module):
        db = tmp_path / "test.db"
        _make_test_db(db, n_rows=42)
        counts = restore_module.row_counts(db)
        assert counts == {
            "settled_trades": 42,
            "evaluated_opportunities": 42,
            "rejected_opportunities": 42,
        }

    def test_row_counts_skips_missing_tables(self, tmp_path, restore_module):
        db = tmp_path / "minimal.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE settled_trades (ticker TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        counts = restore_module.row_counts(db)
        # Only settled_trades is present; the other two are skipped.
        assert counts == {"settled_trades": 0}


# ── diff_row_counts ────────────────────────────────────────────────────


class TestDiffRowCounts:
    def test_within_tolerance_returns_empty(self, restore_module):
        # ~10K base, ~1% growth — within both 5% pct and 50 abs tolerance
        snap = {"settled_trades": 10000, "evaluated_opportunities": 100000}
        base = {"settled_trades": 10100, "evaluated_opportunities": 101000}
        assert restore_module.diff_row_counts(
            snap, base, tolerance_pct=0.05, tolerance_abs=200
        ) == []

    def test_growth_beyond_tolerance_flagged(self, restore_module):
        # Big absolute drift AND big % drift → flagged
        snap = {"settled_trades": 10000}
        base = {"settled_trades": 20000}  # +10K rows, 50% larger
        issues = restore_module.diff_row_counts(
            snap, base, tolerance_pct=0.05, tolerance_abs=50
        )
        assert len(issues) == 1
        assert issues[0][0] == "settled_trades"

    def test_snapshot_larger_than_baseline_flagged(self, restore_module):
        # Snapshot has MORE rows than live → suspicious (live shouldn't shrink)
        snap = {"settled_trades": 5000}
        base = {"settled_trades": 1000}
        issues = restore_module.diff_row_counts(
            snap, base, tolerance_pct=0.05, tolerance_abs=50
        )
        assert len(issues) == 1

    def test_missing_table_in_snapshot_flagged(self, restore_module):
        snap = {"settled_trades": 100}  # missing evaluated_opportunities
        base = {"settled_trades": 100, "evaluated_opportunities": 1000}
        issues = restore_module.diff_row_counts(
            snap, base, tolerance_pct=0.05, tolerance_abs=50
        )
        assert len(issues) == 1
        assert issues[0][0] == "evaluated_opportunities"
        assert issues[0][1] is None  # snap_n

    def test_low_volume_drift_within_abs_floor_not_flagged(self, restore_module):
        """Round-1 finding B-M5: a low-volume table where growth is 100%
        in pct terms but only 10 rows is normal noise, not corruption."""
        # 5 rows -> 15 rows is 200% growth in pct terms but only 10 rows
        # absolute — below the 50-row floor, so NOT flagged.
        snap = {"sports_shadow_log": 5}
        base = {"sports_shadow_log": 15}
        issues = restore_module.diff_row_counts(
            snap, base, tolerance_pct=0.05, tolerance_abs=50
        )
        assert issues == []

    def test_drift_above_abs_floor_but_below_pct_not_flagged(self, restore_module):
        """The two thresholds are AND, not OR — both must be exceeded."""
        # 60 rows abs (above 50 floor) but only ~0.6% pct (below 5%) → not flagged
        snap = {"evaluated_opportunities": 10000}
        base = {"evaluated_opportunities": 10060}
        issues = restore_module.diff_row_counts(
            snap, base, tolerance_pct=0.05, tolerance_abs=50
        )
        assert issues == []


# ── verify_only end-to-end ─────────────────────────────────────────────


class TestVerifyOnly:
    def test_verify_only_passes_on_healthy_snapshot(
        self, populated_store, tmp_path, restore_module
    ):
        rc = restore_module.verify_only(
            store=populated_store,
            tmp_dir=tmp_path / "verify-tmp",
            baseline_from_live=None,
            tolerance_pct=0.05, tolerance_abs=50,
            algorithm="zstd",
        )
        assert rc == 0

    def test_verify_only_with_baseline_passes(
        self, populated_store, tmp_path, restore_module
    ):
        # Snapshot has 70 rows; baseline has 71 (~1.4% growth) — within 5%.
        baseline = tmp_path / "live.db"
        _make_test_db(baseline, n_rows=71)

        rc = restore_module.verify_only(
            store=populated_store,
            tmp_dir=tmp_path / "verify-tmp",
            baseline_from_live=baseline,
            tolerance_pct=0.05, tolerance_abs=50,
            algorithm="zstd",
        )
        assert rc == 0

    def test_verify_only_fails_on_baseline_drift(
        self, populated_store, tmp_path, restore_module
    ):
        # Snapshot has 70 rows; baseline has 200 (185% growth) — well past 5%.
        baseline = tmp_path / "live.db"
        _make_test_db(baseline, n_rows=200)

        rc = restore_module.verify_only(
            store=populated_store,
            tmp_dir=tmp_path / "verify-tmp",
            baseline_from_live=baseline,
            tolerance_pct=0.05, tolerance_abs=50,
            algorithm="zstd",
        )
        assert rc == 3

    def test_verify_only_fails_on_corrupt_snapshot(
        self, populated_store, tmp_path, restore_module
    ):
        # Corrupt the latest snapshot in the store.
        latest_path = (
            tmp_path / "store" / "daily" / "state-db-2026-05-09.db.zst"
        )
        latest_path.write_bytes(b"\xff" * 1024)  # garbage

        # zstd will fail to decompress; the wrapper should bubble that up.
        # (subprocess.CalledProcessError isn't caught explicitly, so we
        # expect non-zero exit via the outer try/finally.)
        with pytest.raises(Exception):
            restore_module.verify_only(
                store=populated_store,
                tmp_dir=tmp_path / "verify-tmp",
                baseline_from_live=None,
                tolerance_pct=0.05, tolerance_abs=50,
                algorithm="zstd",
            )


# ── restore_to_path safety guards ──────────────────────────────────────


class TestRestoreSafety:
    def test_restore_to_path_writes_file(
        self, populated_store, tmp_path, restore_module
    ):
        out = tmp_path / "recovery" / "snapshot.db"
        rc = restore_module.restore_to_path(
            store=populated_store,
            dst=out,
            tmp_dir=tmp_path / "tmp",
            algorithm="zstd",
            key_override=None,
            force=False,
            allow_overwrite_live=False,
        )
        assert rc == 0
        assert out.exists()
        assert out.read_bytes()[:16] == b"SQLite format 3\x00"
        # And it has the expected rows
        conn = sqlite3.connect(str(out))
        try:
            n = conn.execute("SELECT COUNT(*) FROM settled_trades").fetchone()[0]
            assert n == 70  # latest snapshot had 70 rows
        finally:
            conn.close()

    def test_refuses_to_overwrite_live_state_db(
        self, populated_store, tmp_path, restore_module
    ):
        live = tmp_path / "state.db"
        # Don't actually create it; the heuristic is name-based.
        rc = restore_module.restore_to_path(
            store=populated_store,
            dst=live,
            tmp_dir=tmp_path / "tmp",
            algorithm="zstd",
            key_override=None,
            force=False,
            allow_overwrite_live=False,
        )
        assert rc == 4

    def test_allows_live_overwrite_with_explicit_flag(
        self, populated_store, tmp_path, restore_module
    ):
        live = tmp_path / "state.db"
        rc = restore_module.restore_to_path(
            store=populated_store,
            dst=live,
            tmp_dir=tmp_path / "tmp",
            algorithm="zstd",
            key_override=None,
            force=False,
            allow_overwrite_live=True,
        )
        assert rc == 0
        assert live.exists()

    def test_refuses_overwrite_existing_without_force(
        self, populated_store, tmp_path, restore_module
    ):
        out = tmp_path / "recovery.db"
        out.write_bytes(b"existing")
        rc = restore_module.restore_to_path(
            store=populated_store,
            dst=out,
            tmp_dir=tmp_path / "tmp",
            algorithm="zstd",
            key_override=None,
            force=False,
            allow_overwrite_live=False,
        )
        assert rc == 5
        assert out.read_bytes() == b"existing"

    def test_force_overwrites_existing(
        self, populated_store, tmp_path, restore_module
    ):
        out = tmp_path / "recovery.db"
        out.write_bytes(b"existing")
        rc = restore_module.restore_to_path(
            store=populated_store,
            dst=out,
            tmp_dir=tmp_path / "tmp",
            algorithm="zstd",
            key_override=None,
            force=True,
            allow_overwrite_live=False,
        )
        assert rc == 0
        assert out.read_bytes()[:16] == b"SQLite format 3\x00"

    def test_refuses_state_db_wal_sidecar(self, populated_store, tmp_path, restore_module):
        """Round-1 A-C3: restoring to state.db-wal would silently corrupt
        the live DB on next bot open. Refuse without --allow-overwrite-live."""
        wal = tmp_path / "state.db-wal"
        rc = restore_module.restore_to_path(
            store=populated_store,
            dst=wal,
            tmp_dir=tmp_path / "tmp",
            algorithm="zstd",
            key_override=None,
            force=False,
            allow_overwrite_live=False,
        )
        assert rc == 4

    def test_refuses_state_db_shm_sidecar(self, populated_store, tmp_path, restore_module):
        shm = tmp_path / "state.db-shm"
        rc = restore_module.restore_to_path(
            store=populated_store,
            dst=shm,
            tmp_dir=tmp_path / "tmp",
            algorithm="zstd",
            key_override=None,
            force=False,
            allow_overwrite_live=False,
        )
        assert rc == 4

    def test_refuses_when_wal_sidecar_exists_alongside_target(
        self, populated_store, tmp_path, restore_module
    ):
        """If a -wal file exists next to dst, dst is an active SQLite DB
        regardless of basename — refuse."""
        # Use a benign-looking name
        target = tmp_path / "myanalysis.db"
        # But create a -wal sidecar alongside it (simulating an active SQLite)
        (tmp_path / "myanalysis.db-wal").write_bytes(b"\x00" * 32)
        rc = restore_module.restore_to_path(
            store=populated_store,
            dst=target,
            tmp_dir=tmp_path / "tmp",
            algorithm="zstd",
            key_override=None,
            force=False,
            allow_overwrite_live=False,
        )
        assert rc == 4

    def test_refuses_overwrite_live_when_sidecars_present(
        self, populated_store, tmp_path, restore_module
    ):
        """Round-2 R2-M-allow-overwrite-skips-sidecar: even with the
        --allow-overwrite-live escape hatch, refuse if stale -wal/-shm
        exists alongside dst. The combination of restored mainfile +
        stale WAL is silent corruption (SQLite applies stale WAL frames
        atop the snapshot, garbling the recovered data)."""
        live = tmp_path / "state.db"
        # Operator stopped the bot but forgot to rm the sidecars
        (tmp_path / "state.db-wal").write_bytes(b"\x00" * 64)
        (tmp_path / "state.db-shm").write_bytes(b"\x00" * 32)

        rc = restore_module.restore_to_path(
            store=populated_store,
            dst=live,
            tmp_dir=tmp_path / "tmp",
            algorithm="zstd",
            key_override=None,
            force=True,
            allow_overwrite_live=True,
        )
        # Code 4 — refuse despite --allow-overwrite-live, force operator
        # to rm the sidecars first (proving the bot is stopped).
        assert rc == 4


class TestSchemaDrift:
    """Round-1 finding A-M3: snapshot missing tables that exist in live
    is corruption, not normal drift."""

    def test_table_set_returns_user_tables(self, tmp_path, restore_module):
        db = tmp_path / "x.db"
        _make_test_db(db, n_rows=5)
        tables = restore_module.table_set(db)
        assert tables == {
            "settled_trades", "evaluated_opportunities", "rejected_opportunities"
        }

    def test_verify_only_flags_schema_drift(
        self, populated_store, tmp_path, restore_module
    ):
        """If live has tables snapshot doesn't, fail with code 6."""
        # Build a live DB with an EXTRA table the snapshot lacks.
        live = tmp_path / "live.db"
        _make_test_db(live, n_rows=70)
        conn = sqlite3.connect(str(live))
        conn.execute("CREATE TABLE new_shadow_table_v999 (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        rc = restore_module.verify_only(
            store=populated_store,
            tmp_dir=tmp_path / "verify-tmp",
            baseline_from_live=live,
            tolerance_pct=0.05, tolerance_abs=50,
            algorithm="zstd",
        )
        assert rc == 6


class TestStaleSnapshotDetection:
    """Round-3 B3-M5: weekly verify must catch the case where the daily
    backup hasn't fired in days (timer disabled, systemd hung, etc.).
    Partially closes the deferred B-M3 heartbeat alerter."""

    def test_snapshot_age_hours_parses_iso_date(self, restore_module):
        from datetime import datetime, timezone
        # Snapshot date 2026-05-09; query 2026-05-10 18:00 UTC
        # Snapshot taken at 2026-05-09 06:00, so age = 36h
        now = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
        age = restore_module.snapshot_age_hours(
            "daily/state-db-2026-05-09.db.zst", now=now
        )
        assert age == pytest.approx(36.0)

    def test_snapshot_age_returns_none_for_unrecognized_key(self, restore_module):
        assert restore_module.snapshot_age_hours("daily/random-name.zst") is None
        assert restore_module.snapshot_age_hours("_install_check/probe.txt") is None

    def test_verify_only_fails_on_stale_snapshot(
        self, populated_store, tmp_path, restore_module, monkeypatch
    ):
        """If the latest snapshot is older than DEFAULT_MAX_SNAPSHOT_AGE_HOURS,
        the daily backup timer is broken — fail with code 7."""
        # populated_store has snapshots from May 7-9. Pretend it's now May 20.
        from datetime import datetime, timezone
        fake_now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)

        # Patch datetime.now in the restore module
        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now
        monkeypatch.setattr(restore_module, "datetime", FakeDateTime)

        rc = restore_module.verify_only(
            store=populated_store,
            tmp_dir=tmp_path / "verify-tmp",
            baseline_from_live=None,
            tolerance_pct=0.05, tolerance_abs=50,
            algorithm="zstd",
        )
        assert rc == 7

    def test_verify_only_skips_stale_check_when_key_explicit(
        self, populated_store, tmp_path, restore_module, monkeypatch
    ):
        """If --key is passed, operator is restoring an old version on
        purpose — don't fail on staleness."""
        from datetime import datetime, timezone
        fake_now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
        class FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fake_now
        monkeypatch.setattr(restore_module, "datetime", FakeDateTime)

        rc = restore_module.verify_only(
            store=populated_store,
            tmp_dir=tmp_path / "verify-tmp",
            baseline_from_live=None,
            tolerance_pct=0.05, tolerance_abs=50,
            algorithm="zstd",
            key_override="daily/state-db-2026-05-09.db.zst",
        )
        assert rc == 0


class TestSummaryAggregates:
    """Round-1 B-C2: AC #3 ("reproduces dashboard totals") requires PnL
    aggregate parity, not just row counts."""

    def test_summary_aggregates_includes_net_pnl(self, tmp_path, restore_module):
        # Build a DB with explicit fee_cents so we can verify the
        # SUM(pnl_cents - COALESCE(fee_cents, 0)) formula
        db = tmp_path / "x.db"
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE settled_trades (
                ticker TEXT PRIMARY KEY,
                pnl_cents INTEGER,
                fee_cents INTEGER
            );
            CREATE TABLE evaluated_opportunities (id INTEGER PRIMARY KEY);
            CREATE TABLE rejected_opportunities (ticker TEXT PRIMARY KEY);
        """)
        # 10 rows × 100 pnl - 5 fee each = (100-5)*10 = 950
        for i in range(10):
            conn.execute("INSERT INTO settled_trades VALUES (?, ?, ?)",
                         (f"K-{i}", 100, 5))
        conn.commit(); conn.close()

        agg = restore_module.summary_aggregates(db)
        assert agg["settled_trades_count"] == 10
        assert agg["settled_trades_net_pnl_cents"] == 950

    def test_summary_aggregates_handles_null_fee_cents(self, tmp_path, restore_module):
        """Per scripts/CLAUDE.md, NULL fee_cents must NOT silently drop
        rows. COALESCE(fee_cents, 0) is the correct treatment."""
        db = tmp_path / "x.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE settled_trades (ticker TEXT PRIMARY KEY,
                pnl_cents INTEGER, fee_cents INTEGER);
        """)
        conn.execute("INSERT INTO settled_trades VALUES ('A', 100, NULL)")
        conn.execute("INSERT INTO settled_trades VALUES ('B', 200, 10)")
        conn.commit(); conn.close()

        agg = restore_module.summary_aggregates(db)
        # NULL row contributes pnl=100; non-NULL contributes 200-10=190 → 290
        assert agg["settled_trades_net_pnl_cents"] == 290
        assert agg["settled_trades_count"] == 2

    def test_verify_only_flags_pnl_drift(self, populated_store, tmp_path, restore_module):
        """If live has dramatically different net PnL, verify-only fails."""
        # Build a live DB with VERY different PnL (and same row count to
        # isolate the aggregate signal from the count signal).
        # Snapshot from populated_store has 70 rows with pnl_cents=i*10.
        # Live: same 70 rows but with pnl_cents 1000x larger.
        live = tmp_path / "live.db"
        conn = sqlite3.connect(str(live))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE settled_trades (ticker TEXT PRIMARY KEY, pnl_cents INTEGER);
            CREATE TABLE evaluated_opportunities (id INTEGER PRIMARY KEY AUTOINCREMENT, edge REAL);
            CREATE TABLE rejected_opportunities (ticker TEXT PRIMARY KEY);
        """)
        # 70 rows but with ENORMOUSLY different PnL (10000x snapshot's)
        for i in range(70):
            conn.execute("INSERT INTO settled_trades VALUES (?, ?)", (f"K-{i}", i * 100000))
            conn.execute("INSERT INTO evaluated_opportunities (edge) VALUES (?)", (0.01,))
            conn.execute("INSERT INTO rejected_opportunities VALUES (?)", (f"R-{i}",))
        conn.commit(); conn.close()

        rc = restore_module.verify_only(
            store=populated_store,
            tmp_dir=tmp_path / "verify-tmp",
            baseline_from_live=live,
            tolerance_pct=0.05, tolerance_abs=50,
            algorithm="zstd",
        )
        # Row counts will pass (70 == 70). Aggregate must fail (3).
        assert rc == 3
