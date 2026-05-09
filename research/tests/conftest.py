"""Shared fixtures for replay-engine pytest cases.

Phase 2 of the replay engine plan (kb/decisions/replay-engine-execution-plan-may09.md).
The validation anchor is the date-stamped state.db snapshot.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_DEFAULT = REPO_ROOT / "data" / "state_snapshot_20260505_2157.db"


@pytest.fixture(scope="session")
def snapshot_db_path() -> Path:
    path_env = os.environ.get("REPLAY_SNAPSHOT_DB")
    path = Path(path_env) if path_env else SNAPSHOT_DEFAULT
    if not path.exists():
        pytest.skip(f"snapshot DB not found at {path}; set REPLAY_SNAPSHOT_DB to override")
    return path


@pytest.fixture(scope="session")
def snapshot_conn(snapshot_db_path: Path):
    uri = f"file:{snapshot_db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()
