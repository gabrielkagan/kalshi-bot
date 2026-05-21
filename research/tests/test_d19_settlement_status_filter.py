"""D-19 — replay filters status='settled' explicitly.

Authoritative source: bot._impl::MainLoop::_poll_evaluated_opportunities
batches settlements in chunks of 50 (`_SETTLEMENT_BATCH_SIZE = 50`). A
snapshot taken mid-settlement may have rows with `status='pending'` even
though their `market_result` is fetchable.

D-1's per-row identity test already filters `status='settled'`. D-19 adds
the explicit NEGATIVE test: rows with `status='pending'` are excluded from
any cf-aggregation function output.

Test surface:
1. Synthetic snapshot mixing settled + pending rows.
2. Pending rows have NULL counterfactual_pnl.
3. Replay aggregations filter status='settled'.
"""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def mixed_status_snapshot(tmp_path: Path) -> Path:
    """Snapshot with settled rows + pending rows (cf_pnl IS NULL on pending)."""
    db = tmp_path / "mixed_status.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(textwrap.dedent("""
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
                status TEXT,
                counterfactual_pnl INTEGER
            );
        """))
        conn.executemany(
            """INSERT INTO evaluated_opportunities
               (evaluation_time, settled_time, market_result, market_price, position_size,
                product_type, filter_stage, status, counterfactual_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                # 2 settled rows
                ("2026-05-05T12:00:00.000Z", "2026-05-05T12:15:00.000Z", "yes", 85, 1, "15m", "candidate", "settled", 14),
                ("2026-05-05T12:01:00.000Z", "2026-05-05T12:15:00.000Z", "no",  85, 1, "15m", "candidate", "settled", -86),
                # 2 pending rows (NULL cf_pnl, NULL market_result, NULL settled_time)
                ("2026-05-05T12:02:00.000Z", None, None, 85, 1, "15m", "candidate", "pending", None),
                ("2026-05-05T12:03:00.000Z", None, None, 85, 1, "15m", "candidate", "pending", None),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_d19_pending_rows_have_null_cf(mixed_status_snapshot: Path) -> None:
    """Pending rows in the fixture have NULL cf_pnl (matches live behavior)."""
    conn = sqlite3.connect(f"file:{mixed_status_snapshot}?mode=ro", uri=True)
    try:
        rows = list(conn.execute(
            "SELECT status, counterfactual_pnl FROM evaluated_opportunities ORDER BY id"
        ))
        # 2 settled with non-NULL cf
        assert rows[0] == ("settled", 14)
        assert rows[1] == ("settled", -86)
        # 2 pending with NULL cf
        assert rows[2] == ("pending", None)
        assert rows[3] == ("pending", None)
    finally:
        conn.close()


def test_d19_cf_sum_filter_settled_only(mixed_status_snapshot: Path) -> None:
    """SUM(cf_pnl) filtered to status='settled' = 14 + (-86) = -72.

    Pin the canonical filter pattern. Without the WHERE clause, SUM ignores
    NULLs but the COUNT changes — distorting per-row averages.
    """
    conn = sqlite3.connect(f"file:{mixed_status_snapshot}?mode=ro", uri=True)
    try:
        # With status='settled' filter
        total_settled = conn.execute(
            "SELECT SUM(counterfactual_pnl) FROM evaluated_opportunities "
            "WHERE status = 'settled'"
        ).fetchone()[0]
        assert total_settled == -72, f"D-19 settled-only SUM: expected -72, got {total_settled}"
        # Without filter — still -72 because NULLs ignored by SUM
        total_unfiltered = conn.execute(
            "SELECT SUM(counterfactual_pnl) FROM evaluated_opportunities"
        ).fetchone()[0]
        assert total_unfiltered == -72, f"D-19 unfiltered SUM: {total_unfiltered}"
        # But COUNT differs: settled=2 vs unfiltered=4
        n_settled = conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities WHERE status = 'settled'"
        ).fetchone()[0]
        n_all = conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities"
        ).fetchone()[0]
        assert (n_settled, n_all) == (2, 4), f"D-19 row counts: settled={n_settled}, all={n_all}"
        # Average cf is per-row; mixing pending dilutes it.
        # Settled-only avg: -72/2 = -36.0
        # Unfiltered "avg" via COUNT(*) (wrong semantics): -72/4 = -18.0
        # Pin the divergence to show why the filter matters
        assert -72 / n_settled == -36.0
        assert -72 / n_all == -18.0


    finally:
        conn.close()


def test_d19_replay_aggregations_use_settled_filter() -> None:
    """AST heuristic: any SQL aggregation in replay.py that touches counterfactual_pnl
    is paired with a status filter in the same statement.

    Strict regex: requires the matched string to look like real SQL (SELECT...FROM
    pattern), reducing false positives on docstring prose. Best-effort.
    """
    import inspect
    import re
    import research.replay as rep
    src = inspect.getsource(rep)
    # Match strings that look like SQL queries (have both SELECT and FROM) AND
    # contain an aggregation function AND mention counterfactual_pnl.
    sql_string_pattern = re.compile(
        r"[\"'](?P<sql>"
        r"[^\"']*\bSELECT\b[^\"']*\bFROM\b[^\"']*"
        r"\b(?:SUM|AVG|COUNT)\s*\([^\"']*counterfactual_pnl[^\"']*"
        r")[\"']",
        re.IGNORECASE,
    )
    # Also catch SUM(counterfactual_pnl) anywhere in SELECT context
    sql_string_pattern2 = re.compile(
        r"[\"'](?P<sql>"
        r"[^\"']*\bSELECT\b[^\"']*"
        r"\b(?:SUM|AVG)\s*\(\s*counterfactual_pnl\s*\)"
        r"[^\"']*"
        r")[\"']",
        re.IGNORECASE,
    )
    matches = list(sql_string_pattern.finditer(src)) + list(sql_string_pattern2.finditer(src))
    for m in matches:
        sql = m.group("sql")
        # Strict: must be a real SQL aggregation, must include status filter
        assert "status" in sql.lower(), (
            f"D-19 aggregation without status filter: {sql!r}. "
            f"Add WHERE status='settled' to scope to settled rows."
        )


def test_d19_pending_rows_excluded_from_per_row_iteration(mixed_status_snapshot: Path) -> None:
    """The canonical SELECT for per-row replay iteration filters status='settled'."""
    conn = sqlite3.connect(f"file:{mixed_status_snapshot}?mode=ro", uri=True)
    try:
        rows = list(conn.execute(
            "SELECT id, counterfactual_pnl FROM evaluated_opportunities "
            "WHERE status = 'settled'"
        ))
        assert len(rows) == 2, f"D-19 settled iteration: expected 2 rows, got {len(rows)}"
        # Both rows have non-NULL cf
        for _id, cf in rows:
            assert cf is not None, f"D-19 settled row id={_id} has NULL cf"
    finally:
        conn.close()
