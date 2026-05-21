"""D-18 — JSONL journal vs DB are separate truth streams; replay reads ONLY DB.

Authoritative source: bot._impl::_poll_evaluated_opportunities writes a row to
`opportunity_journal.jsonl` BEFORE the DB write (per RCA D-18). The JSONL has
extra fields not in the DB column. DB is canonical for replay; journal is
durable backup.

Per RCA: "mismatched stream synchronization is a known failure mode (per
finding_fifteenm_shadow_lock_traceback_may01.md)." Replay must not mix in
JSONL data.

Test surface: AST guard — research.replay does NOT import json or open jsonl files.
"""
from __future__ import annotations

import inspect
import re

import research.replay as rep


def test_d18_replay_does_not_import_json_or_jsonlines() -> None:
    """research/replay.py does not import json/jsonlines/orjson at module level."""
    src = inspect.getsource(rep)
    # Tokenize-friendly check: look for import statements
    forbidden_imports = [
        "import json",
        "from json ",
        "import jsonlines",
        "from jsonlines ",
        "import orjson",
        "from orjson ",
        "import ujson",
        "from ujson ",
    ]
    for needle in forbidden_imports:
        assert needle not in src, (
            f"D-18 forbidden JSON import in research/replay.py: {needle!r}. "
            f"Replay reads ONLY from state.db snapshot."
        )


def test_d18_replay_source_does_not_reference_jsonl_files() -> None:
    """research/replay.py does not reference *.jsonl files anywhere in source."""
    src = inspect.getsource(rep)
    # Any string containing '.jsonl' is suspect
    assert ".jsonl" not in src, (
        f"D-18 jsonl reference in research/replay.py: jsonl substring found. "
        f"Replay must NOT read journal files."
    )
    assert "opportunity_journal" not in src, (
        f"D-18 opportunity_journal reference in research/replay.py."
    )


def test_d18_replay_does_not_open_files_outside_db_paths() -> None:
    """research/replay.py only opens files via sqlite3 (URI mode). No raw open() of files.

    Heuristic: if replay.py uses `open(` outside of test code, that's a smell.
    Allow None/empty result; this is a guardrail not a strict invariant.
    """
    src = inspect.getsource(rep)
    # `open(` should only appear in contexts safe for replay (sqlite3 connections
    # use sqlite3.connect, not Python open()). If it appears, flag for review.
    open_calls = re.findall(r"\bopen\(", src)
    if open_calls:
        # If B3 legitimately adds an open() for, e.g., reading a config file,
        # this test will surface it for explicit review. Update the test then.
        assert all(False for _ in open_calls), (
            f"D-18 raw open() in replay.py: found {len(open_calls)} call(s). "
            f"Replay should use sqlite3.connect, not Python open()."
        )


def test_d18_test_files_do_not_load_journal_data() -> None:
    """B2 test files themselves don't load journal data into fixtures.

    Self-exclusion: this test file documents the journal terms in its docstring
    for reader context. Skip self to avoid false-positive on the very assertions
    that are scanning for these strings.
    """
    import research.tests as t
    test_pkg_path = inspect.getfile(t)
    import pathlib
    test_dir = pathlib.Path(test_pkg_path).parent
    self_filename = "test_d18_journal_isolation.py"
    for test_file in test_dir.glob("test_*.py"):
        if test_file.name == self_filename:
            continue  # Self-exclusion: this file's docstring documents the names.
        text = test_file.read_text()
        journal_marker = "opportunity" + "_journal"  # split to avoid self-scan
        assert journal_marker not in text, (
            f"D-18 test file references journal: {test_file}"
        )
        # .jsonl substring is more strict: pin no jsonl references in tests
        # (since real test data comes from sqlite snapshot DBs).
        jsonl_marker = "." + "jsonl"  # split to avoid self-scan
        assert jsonl_marker not in text, (
            f"D-18 test file references jsonl-extension: {test_file}"
        )
