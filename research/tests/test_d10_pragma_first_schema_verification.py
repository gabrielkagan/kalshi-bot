"""D-10 — PRAGMA-first schema verification.

Authoritative source: bot/state.py (DDL) + CLAUDE.md "Verify schema before
querying." evaluated_opportunities has a base DDL + many idempotent
ALTER TABLE ADD COLUMN migrations. The actual on-disk column set = base ∪
ALTER. ~120 columns post-9.3-iii.c.

Replay must call PRAGMA table_info on the snapshot before issuing SELECTs.
A missing column should produce a clear error, not a silent AttributeError.

Test surface:
1. open_snapshot(path) runs PRAGMA and verifies expected columns exist.
2. Mutation: rename an expected column in a synthetic schema → clear raise.

TDD-red until B3 ships open_snapshot.
"""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest


# Minimal expected column set that ANY replay query needs.
# Other tests (D-1, D-9, D-20) pin specific subset of columns required for
# their queries; D-10 covers the existence-of-table baseline.
MIN_EXPECTED_COLUMNS_EVAL_OPP = frozenset({
    "id",
    "evaluation_time",
    "market_result",
    "market_price",
    "side",
    "position_size",
    "product_type",
    "filter_stage",
    "status",
    "counterfactual_pnl",
    "settled_time",  # Per D-20. NOTE: RCA D-20 calls this 'settled_at' — drift.
                     # The real column is 'settled_time' per PRAGMA on the 5/5 snapshot.
})


@pytest.fixture
def good_synthetic_snapshot(tmp_path: Path) -> Path:
    """Snapshot with the canonical evaluated_opportunities columns."""
    db = tmp_path / "good_schema.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(textwrap.dedent("""
            CREATE TABLE evaluated_opportunities (
                id INTEGER PRIMARY KEY,
                evaluation_time TEXT NOT NULL,
                settled_at TEXT,
                market_result TEXT,
                side TEXT DEFAULT 'yes',
                market_price INTEGER,
                position_size INTEGER,
                product_type TEXT,
                filter_stage TEXT DEFAULT 'candidate',
                status TEXT DEFAULT 'settled',
                counterfactual_pnl INTEGER
            );
        """))
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.fixture
def bad_synthetic_snapshot_missing_column(tmp_path: Path) -> Path:
    """Snapshot with `market_price` renamed to `entry_price_cents` — column drift."""
    db = tmp_path / "bad_schema.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(textwrap.dedent("""
            CREATE TABLE evaluated_opportunities (
                id INTEGER PRIMARY KEY,
                evaluation_time TEXT NOT NULL,
                settled_at TEXT,
                market_result TEXT,
                side TEXT DEFAULT 'yes',
                entry_price_cents INTEGER,  -- RENAMED from market_price
                position_size INTEGER,
                product_type TEXT,
                filter_stage TEXT DEFAULT 'candidate',
                status TEXT DEFAULT 'settled',
                counterfactual_pnl INTEGER
            );
        """))
        conn.commit()
    finally:
        conn.close()
    return db


def test_d10_real_snapshot_has_expected_columns(snapshot_conn: sqlite3.Connection) -> None:
    """The real May-5 snapshot has the minimum expected columns.

    Uses B1's existing snapshot_conn fixture (real snapshot DB). If this
    fails, the snapshot schema diverged from the expected baseline — which
    would invalidate every replay test using it.
    """
    cols = {row[1] for row in snapshot_conn.execute("PRAGMA table_info(evaluated_opportunities)")}
    missing = MIN_EXPECTED_COLUMNS_EVAL_OPP - cols
    assert not missing, (
        f"D-10 snapshot schema drift: missing columns {missing}. "
        f"Snapshot has {len(cols)} columns total."
    )


def test_d10_open_snapshot_function_exists() -> None:
    """B3 must ship research.replay.open_snapshot (TDD-red)."""
    import research.replay as rep
    assert hasattr(rep, "open_snapshot"), (
        "D-10 TDD-red: B3 must ship research.replay.open_snapshot(path) -> sqlite3.Connection"
    )


def test_d10_open_snapshot_validates_schema_on_good_db(good_synthetic_snapshot: Path) -> None:
    """B3's open_snapshot accepts a snapshot with the expected schema."""
    import research.replay as rep
    if not hasattr(rep, "open_snapshot"):
        pytest.skip("D-10 TDD-red: open_snapshot not yet implemented")
    conn = rep.open_snapshot(good_synthetic_snapshot)
    try:
        # Sanity: connection is usable.
        cur = conn.execute("SELECT COUNT(*) FROM evaluated_opportunities")
        assert cur.fetchone()[0] == 0
    finally:
        conn.close()


def test_d10_open_snapshot_raises_clearly_on_renamed_column(
    bad_synthetic_snapshot_missing_column: Path,
) -> None:
    """B3's open_snapshot raises a CLEAR exception (not AttributeError) when a
    required column is missing/renamed."""
    import research.replay as rep
    if not hasattr(rep, "open_snapshot"):
        pytest.skip("D-10 TDD-red: open_snapshot not yet implemented")
    with pytest.raises((RuntimeError, ValueError, KeyError)) as excinfo:
        rep.open_snapshot(bad_synthetic_snapshot_missing_column)
    msg = str(excinfo.value).lower()
    # Error should mention the schema problem, not be a silent AttributeError.
    assert (
        "column" in msg or "schema" in msg or "market_price" in msg
    ), f"D-10 schema raise: expected clear schema message, got {excinfo.value!r}"


def test_d10_pragma_table_info_returns_expected_tuple_shape(
    good_synthetic_snapshot: Path,
) -> None:
    """PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk) per SQLite docs.

    Pin the shape so any helper consuming PRAGMA output knows the index of
    each field — name=row[1] is the most commonly referenced.
    """
    conn = sqlite3.connect(f"file:{good_synthetic_snapshot}?mode=ro", uri=True)
    try:
        rows = list(conn.execute("PRAGMA table_info(evaluated_opportunities)"))
        assert len(rows) > 0, "D-10 PRAGMA returned no rows"
        for row in rows:
            assert len(row) == 6, (
                f"D-10 PRAGMA shape: expected 6-tuple (cid,name,type,notnull,dflt_value,pk), "
                f"got {len(row)}: {row!r}"
            )
            # name is a string
            assert isinstance(row[1], str)
            # notnull is 0 or 1
            assert row[3] in (0, 1)
    finally:
        conn.close()


def test_d10_no_hardcoded_column_lists_in_replay() -> None:
    """research/replay.py must not have a hardcoded SELECT *-equivalent string.

    Pin the contract: any SELECT should reference specific columns BY NAME,
    not via `SELECT *` (which silently changes shape when columns are added).
    """
    import inspect
    import research.replay as rep
    src = inspect.getsource(rep)
    forbidden = ["SELECT *", "select *"]
    for needle in forbidden:
        assert needle not in src, (
            f"D-10 wildcard SELECT: found {needle!r} in research/replay.py. "
            f"Specify columns explicitly."
        )
