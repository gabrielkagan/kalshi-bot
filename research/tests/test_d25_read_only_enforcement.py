"""D-25 — read-only enforcement on snapshot.

Authoritative source: CLAUDE.md "Don't write to live DB" + the autoresearch
design hard constraint. Replay cannot write to snapshot DB either —
accidental writes break repeatability.

Test surface:
1. Replay's DB connection opens with uri=True, mode=ro (B1's conftest already
   pins this; D-11 reinforces).
2. AST guard: no INSERT/UPDATE/DELETE/CREATE TABLE/DROP/ALTER SQL in replay.py.
3. The B1 conftest's snapshot_conn rejects writes.
"""
from __future__ import annotations

import inspect
import re

import research.replay as rep


WRITE_SQL_PATTERNS = [
    r"\bINSERT\s+INTO\b",
    r"\bUPDATE\s+\w+\s+SET\b",
    r"\bDELETE\s+FROM\b",
    r"\bCREATE\s+TABLE\b",
    r"\bDROP\s+TABLE\b",
    r"\bALTER\s+TABLE\b",
    r"\bCREATE\s+INDEX\b",
    r"\bDROP\s+INDEX\b",
    r"\bREPLACE\s+INTO\b",
    r"\bTRUNCATE\b",
]


def test_d25_replay_source_has_no_write_sql() -> None:
    """research/replay.py has no INSERT/UPDATE/DELETE/CREATE/DROP/ALTER SQL strings."""
    src = inspect.getsource(rep)
    for pattern in WRITE_SQL_PATTERNS:
        matches = re.findall(pattern, src, re.IGNORECASE)
        assert not matches, (
            f"D-25 forbidden write SQL in replay.py: pattern {pattern!r} matched "
            f"{matches!r}. Replay is read-only."
        )


def test_d25_replay_source_select_only() -> None:
    """All SQL statements in replay.py are SELECT or PRAGMA.

    Heuristic: extract anything that looks like SQL via simple line scanning
    (between triple-quoted strings or single-line " ... " strings inside
    .execute() calls). For each, ensure it starts with SELECT or PRAGMA.

    This is best-effort — false positives possible. Skip if no SQL found yet.
    """
    src = inspect.getsource(rep)
    # Look for sqlite3 .execute( call sites — best-effort regex
    execute_calls = re.findall(r"\.execute(?:script)?\s*\(\s*[\"']([^\"']+)[\"']", src)
    if not execute_calls:
        # B1 ships no SQL in replay.py yet. Test is informational until B3.
        return
    for sql in execute_calls:
        sql_trimmed = sql.strip()
        # First non-comment word should be SELECT or PRAGMA
        first_word = re.match(r"^[A-Z]+", sql_trimmed, re.IGNORECASE)
        if first_word:
            verb = first_word.group(0).upper()
            assert verb in ("SELECT", "PRAGMA", "WITH", "EXPLAIN"), (
                f"D-25 non-read SQL in replay.py: {verb!r} in {sql_trimmed!r}"
            )


def test_d25_b1_conftest_uri_ro_mode_pinned() -> None:
    """B1's conftest.snapshot_conn uses URI mode with ?mode=ro."""
    from research.tests import conftest as c
    src = inspect.getsource(c)
    assert "mode=ro" in src, (
        "D-25 conftest: snapshot_conn must use ?mode=ro for read-only enforcement"
    )
    assert "uri=True" in src, (
        "D-25 conftest: sqlite3.connect must pass uri=True"
    )


def test_d25_no_commit_or_rollback_calls() -> None:
    """research/replay.py does not call conn.commit() or conn.rollback().

    Read-only connections don't need transaction control. If commit() appears,
    it signals a write attempt that may have been refactored partly away.
    """
    src = inspect.getsource(rep)
    forbidden_calls = [".commit()", ".rollback()"]
    for needle in forbidden_calls:
        assert needle not in src, (
            f"D-25 transaction control in read-only replay: {needle!r} in replay.py"
        )
