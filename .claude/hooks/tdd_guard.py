#!/usr/bin/env python3
"""tdd_guard — PreToolUse hook enforcing test-first discipline.

[86b9ve110] Pillar 4 of testing-foundation-sprint.

Wired in ``.claude/settings.json`` as a ``PreToolUse`` matcher on
``Edit|Write|MultiEdit``. Blocks edits to ``bot/**/*.py`` unless:

  1. The session transcript shows a prior ``Edit|Write|MultiEdit``
     of any file under ``tests/`` — implying the agent wrote a test
     before touching production code.
  2. The ``KALSHI_TDD_BYPASS=1`` env var is set (per-session
     bypass for refactor sessions covered by Pillar-3 equivalence).
  3. The HEAD commit subject contains the ``[no-tdd]`` marker
     (for doc-only Bits, ``git mv`` Bits, or refactors with the
     equivalence harness as the proof).

Hook contract reference: https://code.claude.com/docs/en/hooks

Failure modes split into fail-open vs. fail-closed:

* **fail-open (exit 0)**: malformed stdin JSON, ``transcript_path``
  field set but file missing on disk, unparseable transcript lines,
  ``git`` not on PATH. These are infrastructure issues — the hook
  is a workflow nudge, not a correctness gate.
* **fail-closed (exit 2 / block)**: ``transcript_path`` field
  empty string OR field absent entirely. Per the Claude Code hook
  spec the field is mandatory; missing/empty indicates harness
  contract regression or spoofed input, and the conservative
  default is to block (with the standard agent-readable message).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional


BLOCK_MESSAGE = (
    "TDD guard: edit a test under tests/ first "
    "(try `/test-writer TARGET` to scaffold a failing test), "
    "or set KALSHI_TDD_BYPASS=1, "
    "or add [no-tdd] to the HEAD commit subject. See tests/CLAUDE.md."
)

# Tools whose tool_input can be a file edit/write under bot/.
_FILE_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}


def _read_stdin_json() -> Optional[dict]:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return None


def _normalize_path(path_str: str, cwd: Path) -> Path:
    """Resolve a (possibly relative) path against cwd without
    requiring the file to exist."""
    p = Path(path_str)
    if not p.is_absolute():
        p = cwd / p
    try:
        return p.resolve()
    except OSError:
        return p


def _is_under(path: Path, ancestor: Path) -> bool:
    """True iff ``path`` is at or below ``ancestor``. ``Path.is_relative_to``
    only landed in 3.9, but our floor is 3.9 — still, do this manually
    to keep the resolution semantics identical across the hook.
    """
    try:
        path.relative_to(ancestor)
        return True
    except ValueError:
        return False


def _bypass_via_env() -> bool:
    """Exact-match (no whitespace-strip) so the contract is unambiguous:
    only ``KALSHI_TDD_BYPASS=1`` bypasses; ``"0"``, ``"true"``, ``" 1 "``
    all gate. Documented in tests/CLAUDE.md."""
    return os.environ.get("KALSHI_TDD_BYPASS") == "1"


def _bypass_via_commit_marker(cwd: Path) -> bool:
    """Run ``git log -1 --format=%s`` in ``cwd`` and return True if the
    HEAD subject contains ``[no-tdd]``. Fail open on any git error
    (no commits yet, git missing, not a repo, etc.)."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    subject = result.stdout.strip()
    # Anchored token match — substring `in` would false-positive on
    # subjects like "Decision: discuss [no-tdd] policy" that mention
    # the marker in passing. Require [no-tdd] to be its own token,
    # delimited by start/end-of-string or whitespace/punctuation.
    return bool(re.search(r"(^|\s)\[no-tdd\](\s|$|[:.,;])", subject))


def _iter_transcript_tool_uses(transcript_path: Path) -> Iterable[dict]:
    """Yield ``tool_use`` blocks from a Claude Code transcript JSONL.

    Streamed line-by-line so a multi-MB transcript does not OOM.
    Lines that fail to parse are silently skipped (defensive — the
    hook is not the place to crash on log-format drift).
    """
    try:
        fh = transcript_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            # Sub-agent transcripts (isSidechain=True) live in their own
            # tool_use space; only the parent session's tool_uses count
            # toward the gate. Belt-and-suspenders against future inlining
            # at either the top level or inside `message`.
            if entry.get("isSidechain") is True:
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            if message.get("isSidechain") is True:
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    yield block


def _transcript_has_prior_test_edit(
    transcript_path: Path,
    cwd: Path,
) -> bool:
    """True iff the transcript contains a prior ``Edit|Write|MultiEdit``
    of any file under ``tests/`` (relative to ``cwd``)."""
    if not transcript_path.is_file():
        return False
    tests_dir = (cwd / "tests").resolve()
    for block in _iter_transcript_tool_uses(transcript_path):
        if block.get("name") not in _FILE_EDIT_TOOLS:
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        fp = tool_input.get("file_path")
        if not isinstance(fp, str) or not fp:
            continue
        candidate = _normalize_path(fp, cwd)
        if not _is_under(candidate, tests_dir):
            continue
        # Restrict to .py — snapshot YAML/CSV, REGEN.md, .DS_Store under
        # tests/ are not "tests" in the TDD sense and must not bypass
        # the gate.
        if candidate.suffix != ".py":
            continue
        return True
    return False


def _is_in_scope(file_path_str: str, cwd: Path) -> bool:
    """The hook only fires for ``bot/**/*.py``. Returns False
    (skip / fail-open) for anything else.
    """
    if not file_path_str:
        return False
    candidate = _normalize_path(file_path_str, cwd)
    if candidate.suffix != ".py":
        return False
    bot_dir = (cwd / "bot").resolve()
    return _is_under(candidate, bot_dir)


def main() -> int:
    payload = _read_stdin_json()
    if not payload:
        return 0

    if payload.get("tool_name") not in _FILE_EDIT_TOOLS:
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str):
        return 0
    cwd_str = payload.get("cwd")
    if cwd_str is not None and not isinstance(cwd_str, str):
        return 0
    cwd = Path(cwd_str or os.getcwd()).resolve()

    if not _is_in_scope(file_path, cwd):
        return 0

    if _bypass_via_env():
        return 0

    if _bypass_via_commit_marker(cwd):
        return 0

    transcript_path_str = payload.get("transcript_path")
    if transcript_path_str is not None and not isinstance(transcript_path_str, str):
        return 0
    transcript_path_str = transcript_path_str or ""
    transcript_path = Path(transcript_path_str) if transcript_path_str else None
    # transcript file missing entirely (e.g., very first tool_use of a
    # fresh session before harness flushes) → fail open. Empty string
    # is treated as a contract violation and DOES gate (block path).
    if transcript_path is not None and not transcript_path.is_file():
        return 0
    if transcript_path is not None and _transcript_has_prior_test_edit(transcript_path, cwd):
        return 0

    print(BLOCK_MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
