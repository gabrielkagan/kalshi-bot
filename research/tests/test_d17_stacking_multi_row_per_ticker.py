"""D-17 — stacking + shadow inserts produce multiple rows per ticker.

Authoritative source: bot/state.py schema migration changed the unique index
from `(ticker, filter_stage)` to `(ticker, filter_stage, side)`. Plus shadow
strategies insert under their own filter_stage alongside the live 'candidate'.

A single ticker can produce N rows:
- 'candidate' (live decision)
- '<cell_block_stage>' (cell block fired)
- '<shadow_stage>' (parallel shadow strategy)
- '(ticker, side='no')' (NO-side decision distinct from YES)

Replay aggregations MUST operate at row granularity, not ticker. GROUP BY
must include filter_stage (and sometimes side).
"""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest


@pytest.fixture
def multi_row_ticker_snapshot(tmp_path: Path) -> Path:
    """Synthetic snapshot: 1 ticker with 3 rows (candidate + block + shadow)."""
    db = tmp_path / "multi_row.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(textwrap.dedent("""
            CREATE TABLE evaluated_opportunities (
                id INTEGER PRIMARY KEY,
                evaluation_time TEXT NOT NULL,
                ticker TEXT NOT NULL,
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
        """))
        same_ticker = "KXBTC15M-26MAY051200-99"
        conn.executemany(
            """INSERT INTO evaluated_opportunities
               (evaluation_time, ticker, settled_time, market_result, side, market_price,
                position_size, product_type, filter_stage, status, counterfactual_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("2026-05-05T12:00:00.000Z", same_ticker, "2026-05-05T12:15:00.000Z", "yes", "yes", 85, 1, "15m", "candidate", "settled", 14),
                ("2026-05-05T12:00:00.000Z", same_ticker, "2026-05-05T12:15:00.000Z", "yes", "yes", 85, 0, "15m", "TM98_97_98C_2_5MIN_BLEED", "settled", 14),
                ("2026-05-05T12:00:00.000Z", same_ticker, "2026-05-05T12:15:00.000Z", "yes", "yes", 85, 0, "15m", "low_price_shadow", "settled", 14),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_d17_three_rows_share_ticker_distinct_stages(multi_row_ticker_snapshot: Path) -> None:
    """Sanity: synthetic snapshot has 3 rows for 1 ticker across 3 filter_stages."""
    conn = sqlite3.connect(f"file:{multi_row_ticker_snapshot}?mode=ro", uri=True)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM evaluated_opportunities WHERE ticker = ?",
            ("KXBTC15M-26MAY051200-99",),
        ).fetchone()[0]
        assert count == 3, f"D-17 fixture: expected 3 rows, got {count}"
        stages = sorted(row[0] for row in conn.execute(
            "SELECT filter_stage FROM evaluated_opportunities WHERE ticker = ?",
            ("KXBTC15M-26MAY051200-99",),
        ))
        assert stages == ["TM98_97_98C_2_5MIN_BLEED", "candidate", "low_price_shadow"]
    finally:
        conn.close()


def test_d17_per_stage_aggregation_does_not_collapse(multi_row_ticker_snapshot: Path) -> None:
    """SQL GROUP BY filter_stage returns 3 distinct buckets."""
    conn = sqlite3.connect(f"file:{multi_row_ticker_snapshot}?mode=ro", uri=True)
    try:
        rows = list(conn.execute(
            "SELECT filter_stage, COUNT(*) FROM evaluated_opportunities "
            "WHERE ticker = ? GROUP BY filter_stage",
            ("KXBTC15M-26MAY051200-99",),
        ))
        assert len(rows) == 3, f"D-17 per-stage GROUP BY: expected 3 buckets, got {len(rows)}"
        for stage, n in rows:
            assert n == 1, f"D-17 per-stage count: {stage} -> {n}, expected 1"
    finally:
        conn.close()


def test_d17_per_ticker_aggregation_collapses_without_filter_stage(multi_row_ticker_snapshot: Path) -> None:
    """SQL GROUP BY ticker alone collapses the 3 rows — pin this is the WRONG aggregation."""
    conn = sqlite3.connect(f"file:{multi_row_ticker_snapshot}?mode=ro", uri=True)
    try:
        rows = list(conn.execute(
            "SELECT ticker, COUNT(*) FROM evaluated_opportunities GROUP BY ticker",
        ))
        assert len(rows) == 1
        _, n = rows[0]
        assert n == 3, (
            f"D-17 collapse demo: ticker-only GROUP BY collapses {n} rows into 1 bucket. "
            f"Replay aggregations must NOT do this."
        )
    finally:
        conn.close()


def test_d17_yes_and_no_side_rows_are_distinct(tmp_path: Path) -> None:
    """A single ticker with YES + NO sides produces 2 rows."""
    db = tmp_path / "yes_no.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(textwrap.dedent("""
            CREATE TABLE evaluated_opportunities (
                id INTEGER PRIMARY KEY,
                evaluation_time TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT DEFAULT 'yes',
                market_price INTEGER,
                filter_stage TEXT DEFAULT 'candidate',
                status TEXT DEFAULT 'settled',
                counterfactual_pnl INTEGER
            );
        """))
        conn.executemany(
            """INSERT INTO evaluated_opportunities
               (evaluation_time, ticker, side, market_price, filter_stage, status,
                counterfactual_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                ("2026-05-05T12:00:00.000Z", "KXBTC15M-26MAY051200-99", "yes", 85, "candidate", "settled", 14),
                ("2026-05-05T12:00:00.000Z", "KXBTC15M-26MAY051200-99", "no",  15, "candidate", "settled", 14),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = list(conn.execute(
            "SELECT side, COUNT(*) FROM evaluated_opportunities "
            "WHERE ticker = ? GROUP BY side",
            ("KXBTC15M-26MAY051200-99",),
        ))
        assert len(rows) == 2, f"D-17 YES+NO: expected 2 buckets, got {len(rows)}"
    finally:
        conn.close()
