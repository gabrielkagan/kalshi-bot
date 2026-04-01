#!/usr/bin/env python3
"""One-time migration: positions and settled_trades to composite PK (ticker, strategy_group).

Run with the bot STOPPED:
    python3 scripts/migrate_composite_pk.py

Idempotent — safe to run multiple times. Checks if strategy_group column
already exists before migrating.
"""

import os
import re
import shutil
import sqlite3
import sys

DB_PATH = os.environ.get("STATE_DB_PATH", "state.db")
BACKUP_PATH = DB_PATH + ".backup_pre_migration"

# ── Strategy group CASE expression (mirrors models.strategy_to_group) ─────
STRATEGY_GROUP_CASE = """
    CASE
        WHEN strategy IN ('MAKER_PATIENT','TAKER_NOW','MAKER_AGGRESSIVE',
                          'PANIC_CAPTURE','CONFIRMATION_ADDON','DIP_ADDON')
             THEN 'main'
        WHEN strategy LIKE 'decided_%' THEN 'decided'
        WHEN strategy IS NULL OR strategy = '' THEN 'main'
        ELSE strategy
    END
""".strip()


def has_column(cur: sqlite3.Cursor, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def get_create_sql(cur: sqlite3.Cursor, table: str) -> str:
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
    row = cur.fetchone()
    if not row:
        print(f"ERROR: Table '{table}' not found in database.")
        sys.exit(1)
    return row[0]


def get_columns(cur: sqlite3.Cursor, table: str) -> list:
    cur.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def row_count(cur: sqlite3.Cursor, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def migrate_table(conn: sqlite3.Connection, table: str):
    """Migrate a table to composite PK (ticker, strategy_group)."""
    cur = conn.cursor()

    # ── Idempotency check ─────────────────────────────────────────────
    if has_column(cur, table, "strategy_group"):
        print(f"  [{table}] strategy_group column already exists — skipping migration.")
        return

    # ── Read current schema ───────────────────────────────────────────
    original_sql = get_create_sql(cur, table)
    print(f"\n  [{table}] Current schema:")
    print(f"    {original_sql}\n")

    columns = get_columns(cur, table)
    original_count = row_count(cur, table)
    print(f"  [{table}] Row count before migration: {original_count}")

    # ── Backfill NULL strategies ──────────────────────────────────────
    if "strategy" in columns:
        cur.execute(f"UPDATE {table} SET strategy='TAKER_NOW' WHERE strategy IS NULL")
        backfilled = cur.rowcount
        print(f"  [{table}] Backfilled {backfilled} NULL strategies to 'TAKER_NOW'")
    else:
        print(f"  [{table}] No 'strategy' column — strategy_group will default to 'main'")

    # ── Build new CREATE TABLE statement ──────────────────────────────
    new_table = f"{table}_new"

    # Build column defs from existing columns (excluding old PK constraint)
    # We'll construct the new schema from PRAGMA info
    cur.execute(f"PRAGMA table_info({table})")
    col_info = cur.fetchall()
    # col_info: (cid, name, type, notnull, dflt_value, pk)

    col_defs = []
    for cid, name, ctype, notnull, dflt, pk in col_info:
        parts = [name, ctype or "TEXT"]
        # Skip old PRIMARY KEY on ticker — we'll add composite PK
        if name == "ticker" and pk:
            pass  # Don't add PRIMARY KEY here
        elif notnull and not pk:
            parts.append("NOT NULL")
        if dflt is not None:
            parts.append(f"DEFAULT {dflt}")
        col_defs.append(" ".join(parts))

    # Add new columns
    col_defs.append("strategy_group TEXT NOT NULL DEFAULT 'main'")
    col_defs.append("is_stacked INTEGER DEFAULT 0")
    col_defs.append("PRIMARY KEY (ticker, strategy_group)")

    create_sql = f"CREATE TABLE {new_table} (\n    " + ",\n    ".join(col_defs) + "\n)"
    print(f"  [{table}] New schema:")
    print(f"    {create_sql}\n")

    cur.execute(create_sql)

    # ── Migrate data ──────────────────────────────────────────────────
    existing_cols = [c[1] for c in col_info]
    col_list = ", ".join(existing_cols)

    if "strategy" in existing_cols:
        insert_sql = f"""
            INSERT INTO {new_table} ({col_list}, strategy_group, is_stacked)
            SELECT {col_list}, {STRATEGY_GROUP_CASE}, 0
            FROM {table}
        """
    else:
        insert_sql = f"""
            INSERT INTO {new_table} ({col_list}, strategy_group, is_stacked)
            SELECT {col_list}, 'main', 0
            FROM {table}
        """

    cur.execute(insert_sql)
    migrated = cur.rowcount
    new_count = row_count(cur, new_table)
    print(f"  [{table}] Migrated {migrated} rows → {new_table} has {new_count} rows")

    if new_count != original_count:
        print(f"  ERROR: Row count mismatch! Original={original_count}, New={new_count}")
        print(f"  Rolling back — {new_table} NOT swapped.")
        cur.execute(f"DROP TABLE {new_table}")
        conn.rollback()
        sys.exit(1)

    # ── Swap tables ───────────────────────────────────────────────────
    cur.execute(f"DROP TABLE {table}")
    cur.execute(f"ALTER TABLE {new_table} RENAME TO {table}")
    conn.commit()
    print(f"  [{table}] Migration complete. Table swapped successfully.")


def run_verification(conn: sqlite3.Connection):
    """Verify composite PK works correctly."""
    cur = conn.cursor()
    print("\n── Verification Tests ──────────────────────────────────────\n")

    # Test 1: Two rows with same ticker, different strategy_groups
    print("  Test 1: INSERT two rows — same ticker, different strategy_groups")
    _now = "2026-01-01T00:00:00Z"
    try:
        cur.execute("""
            INSERT INTO positions (ticker, event_ticker, asset, side, count,
                avg_price_cents, total_cost_cents, opened_at, updated_at,
                status, strategy, strategy_group, is_stacked)
            VALUES ('TEST_TICKER', 'TEST_EVT', 'BTC', 'yes', 10,
                95, 950, ?, ?, 'open', 'MAKER_PATIENT', 'main', 0)
        """, (_now, _now))
        cur.execute("""
            INSERT INTO positions (ticker, event_ticker, asset, side, count,
                avg_price_cents, total_cost_cents, opened_at, updated_at,
                status, strategy, strategy_group, is_stacked)
            VALUES ('TEST_TICKER', 'TEST_EVT', 'BTC', 'yes', 50,
                96, 4800, ?, ?, 'open', 'terminal_momentum', 'terminal_momentum', 1)
        """, (_now, _now))
        cur.execute("SELECT ticker, strategy_group FROM positions WHERE ticker='TEST_TICKER'")
        rows = cur.fetchall()
        assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
        print(f"    PASS — inserted 2 rows: {rows}")
    except Exception as e:
        print(f"    FAIL — {e}")
        conn.rollback()
        return

    # Test 2: INSERT OR REPLACE with same (ticker, strategy_group)
    print("  Test 2: INSERT OR REPLACE same (ticker, strategy_group)")
    try:
        cur.execute("""
            INSERT OR REPLACE INTO positions (ticker, event_ticker, asset, side, count,
                avg_price_cents, total_cost_cents, opened_at, updated_at,
                status, strategy, strategy_group, is_stacked)
            VALUES ('TEST_TICKER', 'TEST_EVT', 'BTC', 'yes', 20,
                95, 1900, ?, ?, 'open', 'MAKER_PATIENT', 'main', 1)
        """, (_now, _now))
        cur.execute("""
            SELECT ticker, strategy_group, is_stacked FROM positions
            WHERE ticker='TEST_TICKER' AND strategy_group='main'
        """)
        row = cur.fetchone()
        assert row[2] == 1, f"Expected is_stacked=1, got {row[2]}"
        print(f"    PASS — replaced row: {row}")
    except Exception as e:
        print(f"    FAIL — {e}")
        conn.rollback()
        return

    # Cleanup test rows
    cur.execute("DELETE FROM positions WHERE ticker='TEST_TICKER'")
    conn.commit()
    print("    Cleaned up test rows.\n")

    # Final summary
    print("── Final Summary ───────────────────────────────────────────\n")
    for table in ("positions", "settled_trades"):
        count = row_count(cur, table)
        print(f"  {table}: {count} rows")

        cur.execute(f"SELECT DISTINCT strategy_group FROM {table}")
        groups = [r[0] for r in cur.fetchall()]
        print(f"  {table} strategy_groups: {groups}")

        cur.execute(f"SELECT * FROM {table} LIMIT 3")
        cols = [d[0] for d in cur.description]
        print(f"  {table} columns: {cols}")
        for row in cur.fetchall():
            print(f"    {dict(zip(cols, row))}")
        print()


def main():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    # ── Step 1: Backup ────────────────────────────────────────────────
    if not os.path.exists(BACKUP_PATH):
        shutil.copy2(DB_PATH, BACKUP_PATH)
        print(f"Backed up {DB_PATH} → {BACKUP_PATH}")
    else:
        print(f"Backup already exists at {BACKUP_PATH} — skipping backup.")

    # ── Step 2: Connect ───────────────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    print(f"Connected to {DB_PATH} (WAL mode, 30s busy timeout)\n")

    # ── Step 3: Migrate tables ────────────────────────────────────────
    print("═══ Migrating positions ═══")
    migrate_table(conn, "positions")

    print("\n═══ Migrating settled_trades ═══")
    migrate_table(conn, "settled_trades")

    # ── Step 4: Verification ──────────────────────────────────────────
    run_verification(conn)

    conn.close()
    print("Done. Migration complete.")


if __name__ == "__main__":
    main()
