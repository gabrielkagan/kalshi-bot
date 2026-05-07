"""Tests for scripts/stamp_data_provenance.py — Phase G-6 one-time stamp.

Coverage:
- Stamps 'backfill_60s_inputs' on pre-Phase-F-3 rows with G-2/G-4 populated
- Stamps 'live_ws' on post-Phase-F-3 rows
- Pre-deploy rows that LACK G-2/G-4 inputs are LEFT NULL (never live-captured
  with full Phase F surface, never backfilled — out of v2 training scope)
- Idempotent — re-runs no-op
- Dry-run reports correct counts without writing
- Skips non-15m rows
- Refuses to run if column missing
- Microsecond-bearing timestamps at the cutoff boundary classify correctly
  (regression for round-1 review #4 lexical comparison bug)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import stamp_data_provenance as stamp_mod  # noqa: E402


@pytest.fixture
def db_with_column(tmp_path: Path) -> Path:
    """Build a state.db-shaped DB with the data_provenance column added."""
    p = tmp_path / "state.db"
    conn = sqlite3.connect(p)
    conn.execute(
        """CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY,
            product_type TEXT,
            evaluation_time TEXT,
            btc_spot_at_decision REAL,
            time_above_strike_seconds REAL,
            data_provenance TEXT
        )"""
    )
    rows = [
        # 1: pre-cutoff, has btc_spot → backfill_60s_inputs
        (1, '15m', '2026-04-15T10:00:00.000000Z', 78400.0, 5.0, None),
        # 2: pre-cutoff, has time_above (not btc) → backfill_60s_inputs
        (2, '15m', '2026-04-20T11:00:00.000000Z', None, 12.0, None),
        # 3: pre-cutoff, NEITHER populated → leave NULL (no G-2/G-4 capture)
        (3, '15m', '2026-04-22T12:00:00.000000Z', None, None, None),
        # 4: post-cutoff, has btc_spot → live_ws
        (4, '15m', '2026-05-02T20:30:00.000000Z', 78500.0, 7.0, None),
        # 5: post-cutoff, no inputs (rejection row?) → live_ws
        (5, '15m', '2026-05-02T21:00:00.000000Z', None, None, None),
        # 6: hourly row with everything populated → leave NULL (non-15m)
        (6, 'hourly', '2026-04-15T10:00:00.000000Z', 78400.0, 5.0, None),
        # 7: pre-cutoff 15m, ALREADY stamped (previous run) → skip
        (7, '15m', '2026-04-15T10:00:00.000000Z', 78400.0, 5.0,
         'backfill_60s_inputs'),
        # 8: REGRESSION for #4 — microseconds AFTER cutoff in same second.
        #    bot/_impl.py writes '2026-05-02T19:53:19.500000Z' style strings.
        #    Naive cutoff '2026-05-02T19:53:19Z' would lexically compare
        #    AFTER this string ('.' < 'Z') → classify as pre-cutoff (WRONG).
        #    With microsecond-bearing cutoff this row stamps live_ws.
        (8, '15m', '2026-05-02T19:53:19.500000Z', 78600.0, 9.0, None),
        # 9: REGRESSION for #4 — microseconds BEFORE cutoff in same second.
        #    Should classify as pre-cutoff (backfill_60s_inputs since has
        #    btc_spot).
        (9, '15m', '2026-05-02T19:53:18.999999Z', 78600.0, 9.0, None),
    ]
    conn.executemany(
        "INSERT INTO evaluated_opportunities VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return p


def test_stamp_categorizes_correctly(db_with_column):
    conn = sqlite3.connect(db_with_column)
    metrics = stamp_mod.stamp(conn, sleep_ms=0)

    rows = dict(conn.execute(
        "SELECT id, data_provenance FROM evaluated_opportunities ORDER BY id"
    ).fetchall())

    assert rows[1] == "backfill_60s_inputs"  # pre-cutoff + btc_spot
    assert rows[2] == "backfill_60s_inputs"  # pre-cutoff + time_above
    assert rows[3] is None                    # pre-cutoff + neither (uncaptured)
    assert rows[4] == "live_ws"               # post-cutoff
    assert rows[5] == "live_ws"               # post-cutoff + null inputs
    assert rows[6] is None                    # hourly — left alone
    assert rows[7] == "backfill_60s_inputs"   # already stamped — preserved
    assert rows[8] == "live_ws"               # post-cutoff (microsecond)
    assert rows[9] == "backfill_60s_inputs"   # pre-cutoff (microsecond)

    assert metrics["backfill_60s_inputs"] == 3  # ids 1, 2, 9
    assert metrics["live_ws"] == 3              # ids 4, 5, 8
    assert metrics["examined"] == 6


def test_stamp_idempotent(db_with_column):
    """Second run must be a no-op AND must not perturb existing values."""
    conn = sqlite3.connect(db_with_column)
    first = stamp_mod.stamp(conn, sleep_ms=0)

    before = dict(conn.execute(
        "SELECT id, data_provenance FROM evaluated_opportunities"
    ).fetchall())

    second = stamp_mod.stamp(conn, sleep_ms=0)

    after = dict(conn.execute(
        "SELECT id, data_provenance FROM evaluated_opportunities"
    ).fetchall())

    assert second["backfill_60s_inputs"] == 0
    assert second["live_ws"] == 0
    assert second["examined"] == 0
    assert first["examined"] > 0
    # Per-row equality — strengthens the idempotency check (round 1 #17).
    assert before == after


def test_dry_run_does_not_write(db_with_column):
    conn = sqlite3.connect(db_with_column)
    metrics = stamp_mod.stamp(conn, sleep_ms=0, dry_run=True)

    # Count what was actually written — should be the pre-existing
    # 'backfill_60s_inputs' on id=7 only.
    n_stamped = conn.execute(
        "SELECT COUNT(*) FROM evaluated_opportunities "
        "WHERE data_provenance IS NOT NULL"
    ).fetchone()[0]
    assert n_stamped == 1  # only the pre-existing id=7

    # But metrics should report what WOULD be stamped.
    assert metrics["backfill_60s_inputs"] == 3
    assert metrics["live_ws"] == 3
    assert metrics["leave_null_pre_phase_f"] == 1  # id=3


def test_main_aborts_if_column_missing(tmp_path):
    """If data_provenance column doesn't exist (migration not run), main
    must return non-zero and not corrupt anything."""
    p = tmp_path / "no_col.db"
    conn = sqlite3.connect(p)
    conn.execute(
        """CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY, product_type TEXT,
            evaluation_time TEXT, btc_spot_at_decision REAL,
            time_above_strike_seconds REAL
        )"""
    )
    conn.commit()
    conn.close()

    rc = stamp_mod.main(["--db", str(p)])
    assert rc != 0


def test_phase_f3_cutoff_carries_microseconds():
    """Regression for round-1 review #4: cutoff string MUST carry
    microseconds so lexical comparison with bot/_impl.py timestamps
    ('%Y-%m-%dT%H:%M:%S.%fZ') is correct."""
    assert stamp_mod.PHASE_F3_DEPLOY_ISO == "2026-05-02T19:53:19.000000Z"
    # Sanity: '.000000Z' is lexically less than '.500000Z' — so a
    # microsecond-bearing post-cutoff timestamp compares correctly.
    assert stamp_mod.PHASE_F3_DEPLOY_ISO < "2026-05-02T19:53:19.500000Z"
    # Sanity demonstrating the bug we're guarding against: a naive
    # bare-second cutoff '...:19Z' would sort GREATER than any
    # microsecond-bearing timestamp in the same second (because '.' < 'Z'
    # in ASCII). With microseconds in the cutoff, a sub-second-late
    # timestamp sorts correctly POST-cutoff.
    assert "2026-05-02T19:53:19.000000Z" < "2026-05-02T19:53:19Z"
    assert "2026-05-02T19:53:19.500000Z" > stamp_mod.PHASE_F3_DEPLOY_ISO


def test_stamp_preserves_existing_values(db_with_column):
    """Pre-existing 'backfill_60s_inputs' on id=7 must survive a fresh run."""
    conn = sqlite3.connect(db_with_column)
    stamp_mod.stamp(conn, sleep_ms=0)

    val = conn.execute(
        "SELECT data_provenance FROM evaluated_opportunities WHERE id=7"
    ).fetchone()[0]
    assert val == "backfill_60s_inputs"


def test_stamp_excludes_hourly_rows(db_with_column):
    """data_provenance is meaningful only for the cal_mlp 15m pipeline.
    Hourly / weather / SPX rows should be left NULL."""
    conn = sqlite3.connect(db_with_column)
    stamp_mod.stamp(conn, sleep_ms=0)

    val = conn.execute(
        "SELECT data_provenance FROM evaluated_opportunities WHERE id=6"
    ).fetchone()[0]
    assert val is None


def test_pre_phase_f_uncaptured_rows_left_null(db_with_column):
    """A pre-cutoff 15m row with NEITHER btc_spot_at_decision nor
    time_above_strike_seconds populated has no Phase F inputs at all
    (live or backfilled). v2 training filters it out via NaN check.
    Stamp script leaves it NULL rather than lying with 'live_ws'."""
    conn = sqlite3.connect(db_with_column)
    stamp_mod.stamp(conn, sleep_ms=0)

    val = conn.execute(
        "SELECT data_provenance FROM evaluated_opportunities WHERE id=3"
    ).fetchone()[0]
    assert val is None


def test_upsert_coalesce_preserves_backfill_stamp():
    """Round-2 #11 regression: bot/_impl.py UPSERT must use COALESCE so an
    existing backfill stamp survives a live-bot UPSERT (which always
    passes the default 'live_ws'). Without COALESCE, every re-evaluation
    would overwrite the stamp and silently re-label backfilled rows."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            filter_stage TEXT,
            side TEXT,
            data_provenance TEXT,
            UNIQUE(ticker, filter_stage, side)
        )"""
    )
    upsert = """INSERT INTO evaluated_opportunities
        (ticker, filter_stage, side, data_provenance)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticker, filter_stage, side) DO UPDATE SET
            data_provenance=COALESCE(
                evaluated_opportunities.data_provenance,
                excluded.data_provenance)"""

    # Backfill-stamped row survives live UPSERT.
    conn.execute(upsert, ('X', 'candidate', 'yes', 'backfill_60s_inputs'))
    conn.execute(upsert, ('X', 'candidate', 'yes', 'live_ws'))
    val = conn.execute(
        "SELECT data_provenance FROM evaluated_opportunities WHERE ticker='X'"
    ).fetchone()[0]
    assert val == 'backfill_60s_inputs'

    # Live row stays live across UPSERTs.
    conn.execute(upsert, ('Y', 'candidate', 'yes', 'live_ws'))
    conn.execute(upsert, ('Y', 'candidate', 'yes', 'live_ws'))
    val = conn.execute(
        "SELECT data_provenance FROM evaluated_opportunities WHERE ticker='Y'"
    ).fetchone()[0]
    assert val == 'live_ws'

    # Pre-G-6 NULL row gets stamped on first UPSERT.
    conn.execute(
        "INSERT INTO evaluated_opportunities "
        "(ticker, filter_stage, side, data_provenance) VALUES (?, ?, ?, NULL)",
        ('Z', 'candidate', 'yes'),
    )
    conn.execute(upsert, ('Z', 'candidate', 'yes', 'live_ws'))
    val = conn.execute(
        "SELECT data_provenance FROM evaluated_opportunities WHERE ticker='Z'"
    ).fetchone()[0]
    assert val == 'live_ws'


def test_stamp_handles_batched_updates(tmp_path: Path):
    """Stamp must complete correctly even when batch_size < total rows
    (rowid-windowed loop). Builds 50 rows, runs with batch_size=10."""
    p = tmp_path / "batched.db"
    conn = sqlite3.connect(p)
    conn.execute(
        """CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY,
            product_type TEXT,
            evaluation_time TEXT,
            btc_spot_at_decision REAL,
            time_above_strike_seconds REAL,
            data_provenance TEXT
        )"""
    )
    rows = []
    for i in range(50):
        # Half pre-cutoff with btc → backfill_60s_inputs, half post-cutoff → live_ws
        ts = '2026-04-15T10:00:00.000000Z' if i < 25 else '2026-05-03T01:00:00.000000Z'
        btc = 78400.0  # always populated so the pre-cutoff predicate fires
        rows.append((i + 1, '15m', ts, btc, None, None))
    conn.executemany(
        "INSERT INTO evaluated_opportunities VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()

    metrics = stamp_mod.stamp(conn, batch_size=10, sleep_ms=0)
    assert metrics["backfill_60s_inputs"] == 25
    assert metrics["live_ws"] == 25
    assert metrics["examined"] == 50
