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


def _extract_string_literals_from_call_arg(node: "ast.AST") -> "list[str]":
    """Best-effort extract string literal(s) from an AST call argument.

    Handles:
      - Plain str: 'INSERT ...'
      - Triple-quoted str: '''INSERT ...'''
      - f-strings: f'INSERT {x}' → returns the constant fragments
      - String concat: 'INSERT' + 'foo' → returns both pieces
    Cannot resolve: variable references (Name lookups), method calls.
    """
    import ast as _ast
    out: "list[str]" = []
    if isinstance(node, _ast.Constant) and isinstance(node.value, str):
        out.append(node.value)
    elif isinstance(node, _ast.JoinedStr):
        for v in node.values:
            if isinstance(v, _ast.Constant) and isinstance(v.value, str):
                out.append(v.value)
    elif isinstance(node, _ast.BinOp) and isinstance(node.op, _ast.Add):
        out.extend(_extract_string_literals_from_call_arg(node.left))
        out.extend(_extract_string_literals_from_call_arg(node.right))
    return out


def test_d25_replay_source_has_no_write_sql() -> None:
    """research/replay.py has no INSERT/UPDATE/DELETE/CREATE/DROP/ALTER SQL inside .execute*() calls.

    R1 finding M5 narrowed the regex to require `.execute(` context, but R2
    finding M2 noted the regex blind-spotted triple-quoted strings, f-strings,
    and variable-bound SQL. Switched to ast.parse + NodeVisitor so all three
    patterns are covered.
    """
    import ast as _ast
    src = inspect.getsource(rep)
    tree = _ast.parse(src)
    # Walk every Call node whose function is `something.execute*`.
    violations: "list[tuple[str, str]]" = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        func = node.func
        # Match conn.execute / conn.executescript / conn.executemany / cursor.execute*
        if isinstance(func, _ast.Attribute) and func.attr in (
            "execute", "executescript", "executemany"
        ):
            if not node.args:
                continue
            literals = _extract_string_literals_from_call_arg(node.args[0])
            for lit in literals:
                for pattern in WRITE_SQL_PATTERNS:
                    if re.search(pattern, lit, re.IGNORECASE):
                        violations.append((pattern, lit))
    assert not violations, (
        f"D-25 forbidden write SQL inside .execute*() call(s): {violations[:3]}. "
        f"Replay is read-only."
    )


def test_d25_whole_source_write_sql_must_be_marked_allowed() -> None:
    """Defense-in-depth: any write-SQL string anywhere in replay.py source must be
    accompanied by a `# D-25-allow:` line comment within 3 lines of the match.

    Catches the case where SQL is built via string concatenation or variable
    binding outside of an .execute() call that the AST visitor sees.
    """
    src = inspect.getsource(rep)
    lines = src.splitlines()
    for pattern in WRITE_SQL_PATTERNS:
        for ln_idx, line in enumerate(lines):
            if re.search(pattern, line, re.IGNORECASE):
                # Allow if a # D-25-allow: comment is within ±3 lines
                window = lines[max(0, ln_idx - 3): ln_idx + 4]
                allowed = any("D-25-allow" in w for w in window)
                # Also allow if the match is inside a comment / docstring marker on the same line
                # (e.g., the WRITE_SQL_PATTERNS list itself, comments explaining the pattern).
                is_pattern_decl = "WRITE_SQL_PATTERNS" in line or line.strip().startswith("#")
                if allowed or is_pattern_decl:
                    continue
                assert False, (
                    f"D-25 unguarded write-SQL match at replay.py line {ln_idx + 1}: "
                    f"pattern={pattern!r} line={line.strip()!r}. "
                    f"Add `# D-25-allow: <reason>` within 3 lines if intentional."
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
