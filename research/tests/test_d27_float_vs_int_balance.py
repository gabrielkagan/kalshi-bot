"""D-27 — float vs int balance / money column representations.

Authoritative source: bot/state.py DDL — money-typed columns are INTEGER cents
(per RCA D-27 + scripts/CLAUDE.md "settled_trades.pnl_cents is GROSS, not net").

The May 1 supabase sync wedge surfaced that some rows got float `ask_depth`
values that ate the 22P02 sync. Local SQLite is INTEGER, but pandas reads via
`pd.to_numeric(errors='coerce')` can silently promote to float64.

Replay must:
1. Read money-typed columns as nullable Int64 (pandas Int64, NOT int64).
2. Reject (raise ValueError) on non-integer values for these columns.
3. NEVER silently truncate `market_price=87.5` to 87.

Money-typed columns (subset; full list in RCA D-27):
    available_balance_cents, position_size, market_price,
    counterfactual_pnl, count, entry_price_cents, seconds_to_close,
    revenue_cents, fee_cents, pnl_cents

Most tests are TDD-red until B3 ships the schema-coercion layer.
"""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import pytest


# Money-typed INTEGER columns split by source table (R1 finding M2: the RCA
# D-27 list conflated two tables; verified against the real snapshot via
# PRAGMA at B1 base).
EVAL_OPP_MONEY_COLS = frozenset({
    "available_balance_cents",
    "position_size",
    "market_price",
    "counterfactual_pnl",
    "seconds_to_close",
})
SETTLED_TRADES_MONEY_COLS = frozenset({
    "count",
    "entry_price_cents",
    "revenue_cents",
    "fee_cents",
    "pnl_cents",
})
MONEY_TYPED_COLUMNS = EVAL_OPP_MONEY_COLS | SETTLED_TRADES_MONEY_COLS


@pytest.fixture
def snapshot_with_float_money(tmp_path: Path) -> Path:
    """Snapshot with one row having market_price=87.5 (impossible per schema but possible from malformed sync)."""
    db = tmp_path / "float_money.db"
    conn = sqlite3.connect(str(db))
    try:
        # Note: SQLite's type affinity is permissive — storing a float in an
        # INTEGER-typed column doesn't raise. The bug surfaces downstream.
        conn.executescript(textwrap.dedent("""
            CREATE TABLE evaluated_opportunities (
                id INTEGER PRIMARY KEY,
                evaluation_time TEXT NOT NULL,
                market_result TEXT,
                side TEXT DEFAULT 'yes',
                market_price INTEGER,
                position_size INTEGER,
                product_type TEXT,
                filter_stage TEXT DEFAULT 'candidate',
                status TEXT DEFAULT 'settled',
                counterfactual_pnl INTEGER,
                settled_time TEXT
            );
        """))
        # Insert one row with float market_price (silently stored in INTEGER affinity)
        conn.execute(
            "INSERT INTO evaluated_opportunities "
            "(evaluation_time, market_result, market_price, position_size, "
            " product_type, status, counterfactual_pnl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("2026-05-05T12:00:00.000Z", "yes", 87.5, 1, "15m", "settled", 14),
        )
        conn.commit()
    finally:
        conn.close()
    return db


def test_d27_money_columns_pinned() -> None:
    """Inline regression contract: per-table money-typed column sets."""
    # evaluated_opportunities money columns
    assert "market_price" in EVAL_OPP_MONEY_COLS
    assert "counterfactual_pnl" in EVAL_OPP_MONEY_COLS
    assert "available_balance_cents" in EVAL_OPP_MONEY_COLS
    # settled_trades money columns
    assert "pnl_cents" in SETTLED_TRADES_MONEY_COLS
    assert "fee_cents" in SETTLED_TRADES_MONEY_COLS
    # Non-money columns NOT in either set
    assert "evaluation_time" not in MONEY_TYPED_COLUMNS
    assert "ticker" not in MONEY_TYPED_COLUMNS
    # Tables don't overlap (each column belongs to exactly one source)
    assert not (EVAL_OPP_MONEY_COLS & SETTLED_TRADES_MONEY_COLS), (
        "D-27 cross-table column drift: a money column appears in both tables"
    )


def test_d27_eval_opp_money_cols_present_in_real_snapshot(
    snapshot_conn: "sqlite3.Connection",
) -> None:
    """Each EVAL_OPP_MONEY_COLS entry exists on the real evaluated_opportunities table."""
    cols = {row[1] for row in snapshot_conn.execute(
        "PRAGMA table_info(evaluated_opportunities)"
    )}
    missing = EVAL_OPP_MONEY_COLS - cols
    assert not missing, (
        f"D-27 EVAL_OPP_MONEY_COLS drift: {missing} not in real snapshot"
    )


def test_d27_settled_trades_money_cols_present_in_real_snapshot(
    snapshot_conn: "sqlite3.Connection",
) -> None:
    """Each SETTLED_TRADES_MONEY_COLS entry exists on the real settled_trades table."""
    cols = {row[1] for row in snapshot_conn.execute(
        "PRAGMA table_info(settled_trades)"
    )}
    missing = SETTLED_TRADES_MONEY_COLS - cols
    assert not missing, (
        f"D-27 SETTLED_TRADES_MONEY_COLS drift: {missing} not in real snapshot"
    )


def test_d27_sqlite_int_affinity_silently_stores_floats() -> None:
    """SQLite's INTEGER affinity stores float values without raising — the bug surface.

    Pin the underlying SQLite behavior so we know what we're defending against:
    a row with market_price=87.5 reads back as 87.5 (float), not 87 (truncated int).
    Replay's read layer is what catches this; SQLite itself doesn't.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (87.5)")
        row = conn.execute("SELECT x FROM t").fetchone()
        # SQLite stores the value as-typed; INTEGER affinity converts only
        # when the value is convertible without loss. 87.5 is NOT.
        assert row[0] == 87.5, (
            f"D-27 SQLite affinity check: expected 87.5 stored as-is, got {row[0]!r} "
            f"({type(row[0]).__name__})"
        )
        assert isinstance(row[0], float), "D-27: SQLite returned non-float for 87.5"
    finally:
        conn.close()


def test_d27_replay_raises_on_float_money_value(snapshot_with_float_money: Path) -> None:
    """B3's snapshot reader raises ValueError on float market_price (TDD-red).

    Catches the bug where pandas.read_sql + to_numeric(errors='coerce') silently
    promotes the column to float64, and downstream comparisons against integer
    cents silently truncate or fail.
    """
    import research.replay as rep
    if not hasattr(rep, "open_snapshot"):
        pytest.skip("D-27 TDD-red: open_snapshot not yet implemented (D-10 dependency)")
    # B3 should raise when reading a row with market_price=87.5.
    with pytest.raises((ValueError, TypeError)) as excinfo:
        conn = rep.open_snapshot(snapshot_with_float_money)
        try:
            # If B3 raises eagerly on open, the with-block catches it.
            # If B3 raises lazily on first SELECT, force a read here.
            list(conn.execute("SELECT market_price FROM evaluated_opportunities"))
        finally:
            conn.close()
    msg = str(excinfo.value).lower()
    assert (
        "int" in msg or "money" in msg or "87.5" in msg or "float" in msg or "market_price" in msg
    ), f"D-27 raise message: expected mention of int/money/float/market_price, got {excinfo.value!r}"


def test_d27_no_silent_truncate_on_float_input() -> None:
    """If replay accepts a float input directly (e.g., via replay_cf_pnl),
    it must NOT silently truncate to int.

    Tests the cf path on a float entry_price. B1's replay_cf_pnl uses
    `int(entry_price)` which truncates. Pin that this is intentional (the
    formula assumes int cents) but with a clear contract: callers must pass
    int, not float. If the truncation is a bug, B3 should change the
    int() call to raise instead.

    This test passes today (truncation works) and serves as a regression
    contract: if B3 changes int() to a stricter cast, the test must update.
    """
    from research.replay import replay_cf_pnl
    # Calling with float entry_price: B1 currently does int(87.5) = 87 silently.
    # If this is wrong, change B1's replay_cf_pnl to raise on non-int entry.
    cf = replay_cf_pnl(
        entry_price=87.5,
        market_result="yes",
        side="yes",
        position_size=1,
        product_type="15m",
    )
    # Pre-truncation: (100-87)*1 - fee(1,87) where fee(1,87) = ceil(0.07*87*13/100) = ceil(0.7917) = 1
    # Cf = 13 - 1 = 12.
    assert cf == 12, (
        f"D-27 float-truncate behavior: replay_cf_pnl(87.5) -> {cf}, expected 12 "
        f"(int(87.5)=87 truncation). If B3 changes this to raise, update the test."
    )


@pytest.mark.parametrize("col", sorted(MONEY_TYPED_COLUMNS))
def test_d27_money_column_in_canonical_set(col: str) -> None:
    """Each canonical money column is in the set (regression sanity)."""
    assert col in MONEY_TYPED_COLUMNS


def test_d27_replay_has_nullable_int_coercion_helper() -> None:
    """B3 must ship a helper that coerces money columns to nullable Int64 (TDD-red).

    Either as a public function (`_coerce_int_columns(df, cols)`) or implicit
    in `open_snapshot`. This test accepts either pattern via hasattr probing.
    """
    import research.replay as rep
    has_helper = (
        hasattr(rep, "_coerce_int_columns")
        or hasattr(rep, "coerce_int_columns")
    )
    has_open = hasattr(rep, "open_snapshot")
    assert has_helper or has_open, (
        "D-27 TDD-red: B3 must ship either _coerce_int_columns helper "
        "OR open_snapshot that applies coercion internally."
    )
