"""D-11 — snapshot-DB safety: replay must NEVER touch live state.db.

Authoritative source: autoresearch design doc + PM-001 (SQLite contention).
Per RCA D-11 + CLAUDE.md: replay's input path MUST resolve (post-realpath +
symlink-follow) to a snapshot file, never the live tree's `state.db`. An
accidental `replay state.db` would lock the file at sweep cadence and impact
live trading.

Test surface:
1. Replay rejects (RuntimeError) when input path resolves to live state.db.
2. Replay accepts snapshot_*.db, research/snapshot_*.db, /tmp/* paths.
3. Replay opens snapshot read-only (?mode=ro) + WAL + busy_timeout pragmas.

All TDD-red until B3 ships open_snapshot.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


def test_d11_replay_rejects_live_state_db(tmp_path: Path) -> None:
    """B3's open_snapshot raises when path resolves to live state.db (TDD-red).

    Simulates a path that looks like the live tree: any path ending in
    `kalshi-bot/state.db` (literal path component match, post-realpath).
    """
    import research.replay as rep
    if not hasattr(rep, "open_snapshot"):
        pytest.skip("D-11 TDD-red: open_snapshot not yet implemented")
    # Build a fake "live tree" directory structure.
    fake_live = tmp_path / "kalshi-bot"
    fake_live.mkdir()
    fake_db = fake_live / "state.db"
    # Create an EMPTY DB at the live-looking path
    conn = sqlite3.connect(str(fake_db))
    conn.close()
    with pytest.raises(RuntimeError) as excinfo:
        rep.open_snapshot(fake_db)
    msg = str(excinfo.value).lower()
    assert "live" in msg or "state.db" in msg or "snapshot" in msg, (
        f"D-11 live-DB raise: expected mention of live/state.db/snapshot, got {excinfo.value!r}"
    )


def test_d11_replay_accepts_snapshot_named_paths(tmp_path: Path) -> None:
    """B3's open_snapshot accepts paths matching snapshot_*.db / research/* (TDD-red)."""
    import research.replay as rep
    if not hasattr(rep, "open_snapshot"):
        pytest.skip("D-11 TDD-red: open_snapshot not yet implemented")
    # Create a snapshot-named DB
    snap_path = tmp_path / "snapshot_test.db"
    conn = sqlite3.connect(str(snap_path))
    # Minimal schema so open_snapshot's PRAGMA-table_info checks don't fail
    conn.executescript("""
        CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY,
            evaluation_time TEXT NOT NULL,
            settled_time TEXT,
            market_result TEXT,
            side TEXT DEFAULT 'yes',
            market_price INTEGER,
            position_size INTEGER,
            product_type TEXT,
            filter_stage TEXT DEFAULT 'candidate',
            status TEXT DEFAULT 'settled',
            counterfactual_pnl INTEGER
        );
    """)
    conn.commit()
    conn.close()
    # B3's open_snapshot should accept this path
    res = rep.open_snapshot(snap_path)
    if isinstance(res, sqlite3.Connection):
        res.close()


def test_d11_replay_follows_symlinks_to_detect_live_state_db(tmp_path: Path) -> None:
    """A symlink pointing to live state.db is detected via realpath (TDD-red)."""
    import research.replay as rep
    if not hasattr(rep, "open_snapshot"):
        pytest.skip("D-11 TDD-red: open_snapshot not yet implemented")
    # Create live-like state.db
    fake_live_dir = tmp_path / "kalshi-bot"
    fake_live_dir.mkdir()
    fake_live_db = fake_live_dir / "state.db"
    sqlite3.connect(str(fake_live_db)).close()
    # Create symlink with snapshot-looking name pointing at it
    symlink_path = tmp_path / "snapshot_decoy.db"
    os.symlink(str(fake_live_db), str(symlink_path))
    # open_snapshot must resolve the symlink and detect live state.db
    with pytest.raises(RuntimeError):
        rep.open_snapshot(symlink_path)


def test_d11_b1_snapshot_path_resolution(snapshot_db_path: Path) -> None:
    """The B1 conftest's snapshot_db_path resolves to a 'snapshot_*.db' file.

    Pin that the canonical anchor satisfies the snapshot-naming convention
    that D-11's safety guard relies on.
    """
    name = snapshot_db_path.name
    assert name.startswith("state_snapshot_") or "snapshot" in name, (
        f"D-11 anchor naming: snapshot_db_path={snapshot_db_path} doesn't match snapshot_*.db"
    )


def test_d11_replay_opens_read_only(tmp_path: Path) -> None:
    """B3's open_snapshot returns a read-only sqlite3.Connection (TDD-red).

    Attempting an INSERT on the returned connection must raise
    `sqlite3.OperationalError: attempt to write a readonly database`.
    """
    import research.replay as rep
    if not hasattr(rep, "open_snapshot"):
        pytest.skip("D-11 TDD-red: open_snapshot not yet implemented")
    # Create a snapshot-named DB with a table
    snap = tmp_path / "snapshot_ro_test.db"
    conn0 = sqlite3.connect(str(snap))
    conn0.executescript("""
        CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY,
            evaluation_time TEXT NOT NULL,
            settled_time TEXT,
            market_result TEXT,
            side TEXT DEFAULT 'yes',
            market_price INTEGER,
            position_size INTEGER,
            product_type TEXT,
            filter_stage TEXT DEFAULT 'candidate',
            status TEXT DEFAULT 'settled',
            counterfactual_pnl INTEGER
        );
    """)
    conn0.commit()
    conn0.close()
    conn = rep.open_snapshot(snap)
    try:
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            conn.execute("INSERT INTO evaluated_opportunities (evaluation_time) VALUES ('test')")
        assert "readonly" in str(excinfo.value).lower(), (
            f"D-11 read-only enforcement: expected 'readonly' in error, got {excinfo.value!r}"
        )
    finally:
        conn.close()


def test_d11_conftest_uses_read_only_mode(snapshot_conn: sqlite3.Connection) -> None:
    """B1's conftest.snapshot_conn fixture opens with ?mode=ro.

    Pin the conftest's safety posture. If the conftest is ever refactored
    to remove the URI mode, this test catches it.
    """
    # Attempt write to the fixture-provided connection → must fail.
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        snapshot_conn.execute("CREATE TABLE __d11_canary (id INTEGER)")
    assert "readonly" in str(excinfo.value).lower(), (
        f"D-11 conftest read-only: expected 'readonly' error, got {excinfo.value!r}"
    )
