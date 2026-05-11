<!--
Sprint 13 Bit 13.2-rest (2026-05-11) — template for adding a new audit
or alpha-research script under scripts/.

Usage:
  1. Copy this file's structure to scripts/<audit_name>.py
  2. Replace <PLACEHOLDER> tokens with audit-specific values
  3. Delete this HTML comment block + the # TEMPLATE COMMENT lines
  4. If wrapping in a Makefile target, add to Makefile + tests/test_makefile.py
     (Bit 11.3 / Bit 13.2 narrow-skill template precedent)
  5. If user-facing as a `/skill`, copy .claude/templates/new-skill.md
     and reference this audit script

Cross-refs:
- scripts/CLAUDE.md § "Conventions" — regime filter, Kelly-sized PnL,
  Wilson CI on win rates, schema verification before queries
- scripts/CLAUDE.md § "audit_runner.sh" — aggregate runner pattern
- scripts/CLAUDE.md § "DB connections" — WAL + busy_timeout=10000
-->

#!/usr/bin/env python3
"""<ONE_SENTENCE_DESCRIPTION>.

<ONE_PARAGRAPH_OVERVIEW>

Usage:
    python3 scripts/<AUDIT_NAME>.py --db /tmp/state.db <DEFAULT_ARGS>
    python3 scripts/<AUDIT_NAME>.py --db /tmp/state.db --regime auto
    python3 scripts/<AUDIT_NAME>.py --db /tmp/state.db --since 2026-04-01

Per scripts/CLAUDE.md conventions:
- Regime-filter every analysis (--regime auto detects last config-relevant
  git commit).
- Use Kelly-sized PnL (never flat 1-contract).
- Wilson CI on win rates when n<200.
- Verify schema before querying (PRAGMA table_info / SELECT DISTINCT).
- pnl_cents is GROSS, not net — use SUM(pnl_cents - COALESCE(fee_cents, 0)).
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from typing import Iterable, List, Tuple


def _detect_regime_cutoff(db_path: str) -> str:
    """Find the last commit that changed regime-relevant files.

    Per scripts/CLAUDE.md: use git log --diff-filter on bot/constants.py
    + bot/main_loop.py + bot/scanner/__init__.py + market_config.py to
    detect the last regime change. (Bit 9.3-iii.c deleted bot/_impl.py.)
    Returns ISO datetime string for use as --since filter.
    """
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%cI",
             "--diff-filter=M",
             "bot/constants.py", "bot/main_loop.py",
             "bot/scanner/__init__.py", "market_config.py"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return out or "2026-01-01T00:00:00Z"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "2026-01-01T00:00:00Z"


def _connect(db_path: str) -> sqlite3.Connection:
    """Open DB with the canonical pragmas (WAL + busy_timeout).

    Per scripts/CLAUDE.md "DB connections": every new sqlite3.connect()
    must include both PRAGMA journal_mode=WAL and PRAGMA busy_timeout=10000.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _verify_schema(conn: sqlite3.Connection, table: str, columns: Iterable[str]) -> None:
    """Fail fast if the table or any required column is missing.

    Per scripts/CLAUDE.md: verify schema before querying. Catches DB
    snapshots from before a column was added.
    """
    cur = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if not existing:
        sys.exit(f"FATAL: table {table!r} does not exist in DB.")
    missing = [c for c in columns if c not in existing]
    if missing:
        sys.exit(f"FATAL: table {table!r} missing columns: {missing}")


def main() -> int:
    parser = argparse.ArgumentParser(description="<AUDIT_DESCRIPTION>")
    parser.add_argument("--db", default="/tmp/state.db", help="SQLite DB path")
    parser.add_argument("--since", default=None, help="ISO date filter (e.g., 2026-04-01)")
    parser.add_argument("--regime", default=None, choices=[None, "auto"],
                        help="Auto-detect regime cutoff from git history")
    parser.add_argument("--verbose", action="store_true", help="Extra detail")
    args = parser.parse_args()

    cutoff = args.since
    if args.regime == "auto":
        cutoff = _detect_regime_cutoff(args.db)
        if args.verbose:
            print(f"[<AUDIT_NAME>] regime auto cutoff: {cutoff}")

    conn = _connect(args.db)
    _verify_schema(conn, "<TABLE_NAME>", ["<REQUIRED_COLUMN_1>", "<REQUIRED_COLUMN_2>"])

    # ─────────────────────────────────────────────────────────────
    # Main analysis
    # ─────────────────────────────────────────────────────────────

    # <YOUR_QUERY_LOGIC_HERE>

    return 0


if __name__ == "__main__":
    sys.exit(main())
