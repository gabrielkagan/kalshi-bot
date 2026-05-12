"""Pillar 4 — tdd_guard.py PreToolUse hook regression tests.

[86b9ve110] testing-foundation-sprint Pillar 4. The hook lives at
``.claude/hooks/tdd_guard.py`` and is wired into ``.claude/settings.json``
as a ``PreToolUse`` matcher on ``Edit|Write|MultiEdit``. It blocks
edits to ``bot/**/*.py`` unless the session transcript shows a prior
``tests/**`` edit OR a bypass marker is active.

Tests exercise the script as a black box: build the JSON the Claude
Code harness sends on stdin, capture exit code + stderr.

Hook contract reference:
    https://code.claude.com/docs/en/hooks
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO_ROOT / ".claude" / "hooks" / "tdd_guard.py"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"
SKILL_PATH = REPO_ROOT / ".claude" / "skills" / "test-writer" / "SKILL.md"


# ─── Helpers ────────────────────────────────────────────────────────────


def _make_transcript(tmp_path: Path, entries: Iterable[Dict[str, Any]]) -> Path:
    """Write a JSONL transcript with the supplied entries.

    Mirrors the shape Claude Code writes to ``transcript_path``:
    one JSON object per line, with ``message.content`` arrays
    containing ``tool_use`` blocks for tool invocations.
    """
    transcript = tmp_path / "transcript.jsonl"
    with transcript.open("w") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return transcript


def _tool_use_entry(
    tool_name: str,
    file_path: str,
    *,
    tool_use_id: str = "toolu_test",
) -> Dict[str, Any]:
    """A minimal assistant message with a single tool_use block."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": tool_name,
                    "input": {"file_path": file_path},
                }
            ],
        },
    }


def _run_hook(
    payload: Dict[str, Any],
    *,
    cwd: Optional[Path] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook script with the supplied JSON on stdin.

    Returns the CompletedProcess so tests can assert on returncode +
    stdout + stderr.
    """
    env = os.environ.copy()
    env.pop("KALSHI_TDD_BYPASS", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
        env=env,
        timeout=10,
    )


def _payload(
    *,
    tool_name: str = "Edit",
    file_path: str = "bot/_impl.py",
    transcript_path: str = "",
    cwd: Optional[Path] = None,
) -> Dict[str, Any]:
    """Build the PreToolUse JSON the harness sends on stdin."""
    return {
        "session_id": "sess-test",
        "transcript_path": transcript_path,
        "cwd": str(cwd or REPO_ROOT),
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "tool_use_id": "toolu_under_test",
    }


# ─── Existence + wiring contracts ───────────────────────────────────────


def test_hook_script_exists():
    assert HOOK_PATH.is_file(), f"Hook missing at {HOOK_PATH}"


def test_hook_script_is_python_executable_via_python3():
    """Hook must be invokable via ``python3 .claude/hooks/tdd_guard.py``;
    settings.json invokes via the python3 interpreter so the script
    does not need a shebang and need not be chmod +x. Asserting we
    can compile the source rules out a syntax error class.
    """
    import py_compile

    py_compile.compile(str(HOOK_PATH), doraise=True)


def test_settings_json_wires_tdd_guard_as_pretooluse_hook():
    """The hook is wired into .claude/settings.json under PreToolUse
    with an Edit|Write|MultiEdit matcher. Verifies the wiring
    declaratively rather than relying on Claude Code to load it."""
    data = json.loads(SETTINGS_PATH.read_text())
    pre_hooks = data.get("hooks", {}).get("PreToolUse", [])
    assert pre_hooks, "PreToolUse section missing from .claude/settings.json"

    matched = [
        entry
        for entry in pre_hooks
        if "Edit" in (entry.get("matcher") or "")
        and "Write" in (entry.get("matcher") or "")
        and "MultiEdit" in (entry.get("matcher") or "")
    ]
    assert matched, (
        "PreToolUse hook with Edit|Write|MultiEdit matcher not found in "
        ".claude/settings.json"
    )

    cmd_entries = [h for entry in matched for h in entry.get("hooks", [])]
    assert any("tdd_guard.py" in (h.get("command") or "") for h in cmd_entries), (
        "tdd_guard.py not invoked by any PreToolUse Edit|Write|MultiEdit hook"
    )


def test_settings_json_preserves_post_tool_use_ast_check():
    """Pillar 4 must NOT remove the existing PostToolUse ast-check
    hook (set up before Pillar 4 — load-bearing for catching syntax
    errors after edits land).
    """
    data = json.loads(SETTINGS_PATH.read_text())
    post_hooks = data.get("hooks", {}).get("PostToolUse", [])
    cmd_blob = " ".join(
        h.get("command", "") for entry in post_hooks for h in entry.get("hooks", [])
    )
    assert "ast.parse" in cmd_blob, (
        "PostToolUse ast-check hook was removed; Pillar 4 must preserve it."
    )


def test_test_writer_skill_exists_with_expected_frontmatter():
    """The /test-writer skill is half of Pillar 4 AC."""
    assert SKILL_PATH.is_file(), f"/test-writer SKILL.md missing at {SKILL_PATH}"
    body = SKILL_PATH.read_text()
    assert body.startswith("---\n"), "Frontmatter delimiter missing"
    assert "name: test-writer" in body
    assert "description:" in body


# ─── Hook decisions: skip cases (file outside scope) ────────────────────


def test_skip_when_file_outside_bot(tmp_path: Path):
    """Editing tests/test_foo.py never blocks (hook scope is bot/**)."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="tests/test_foo.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr


def test_skip_when_file_is_not_python(tmp_path: Path):
    """Editing bot/CLAUDE.md (non-py) never blocks."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/CLAUDE.md", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr


def test_skip_when_tool_is_bash(tmp_path: Path):
    """Defensive: matcher should filter by tool name, but the hook
    must also exit 0 on Bash (e.g., if matcher is ever broadened)."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(
        tool_name="Bash",
        file_path="bot/_impl.py",
        transcript_path=str(transcript),
    )
    payload["tool_input"] = {"command": "echo hi"}
    result = _run_hook(payload)
    assert result.returncode == 0


def test_skip_when_file_path_missing(tmp_path: Path):
    """A malformed/empty tool_input.file_path must fail open
    (we only block on confirmed bot/ edits)."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(transcript_path=str(transcript))
    payload["tool_input"] = {}
    result = _run_hook(payload)
    assert result.returncode == 0


def test_fail_open_on_malformed_stdin():
    """Garbage in stdin must not block the agent — fail open with
    exit 0 and no stderr stack trace."""
    result = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input="this is not json",
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=10,
    )
    assert result.returncode == 0


def test_fail_open_on_missing_transcript(tmp_path: Path):
    """A transcript_path that does not exist must not block — the
    hook fails open rather than penalize an infrastructure issue."""
    payload = _payload(
        file_path="bot/_impl.py",
        transcript_path=str(tmp_path / "nonexistent.jsonl"),
    )
    result = _run_hook(payload)
    assert result.returncode == 0


# ─── Hook decisions: block cases ────────────────────────────────────────


def test_block_when_no_test_edited_this_session(tmp_path: Path):
    """The canonical TDD-violation: edit bot/_impl.py with an empty
    transcript → block."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 2, (
        f"Expected block (exit 2), got {result.returncode}. "
        f"stderr={result.stderr!r}"
    )
    assert result.stderr.strip(), "Block message must be non-empty"


def test_block_message_is_single_line(tmp_path: Path):
    """Block messages displayed to the agent must be a single line —
    ticket AC: 'Hook output is agent-readable (one-line message; no
    stack trace noise)'.
    """
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    msg = result.stderr.rstrip("\n")
    assert "\n" not in msg, f"Block message must be one line; got {msg!r}"


def test_block_message_mentions_bypass(tmp_path: Path):
    """Agent-readable means the message tells the agent how to
    proceed: write a test, or use a bypass marker."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    msg = result.stderr.lower()
    assert "test" in msg
    assert "bypass" in msg or "no-tdd" in msg or "kalshi_tdd_bypass" in msg


def test_block_only_fires_for_bot_py_files(tmp_path: Path):
    """Edits inside bot/engines/ are also in scope."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(
        file_path="bot/engines/calibration.py", transcript_path=str(transcript)
    )
    result = _run_hook(payload)
    assert result.returncode == 2


def test_block_with_absolute_path_under_bot(tmp_path: Path):
    """The harness sends absolute paths; the hook must recognize
    bot/_impl.py as in-scope regardless of relative-vs-absolute."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(
        file_path=str(REPO_ROOT / "bot" / "_impl.py"),
        transcript_path=str(transcript),
    )
    result = _run_hook(payload)
    assert result.returncode == 2


# ─── Hook decisions: allow because of prior test edit ──────────────────


def test_allow_when_test_edited_earlier_in_session(tmp_path: Path):
    """The canonical TDD pattern: agent edits a test, then edits bot/."""
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Edit", "tests/integration/test_calibration_engine.py")],
    )
    payload = _payload(
        file_path="bot/engines/calibration.py", transcript_path=str(transcript)
    )
    result = _run_hook(payload)
    assert result.returncode == 0, result.stderr


def test_allow_when_test_written_earlier_in_session(tmp_path: Path):
    """A new test file scaffolded with Write counts."""
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Write", "tests/integration/test_calibration_engine.py")],
    )
    payload = _payload(
        file_path="bot/engines/calibration.py", transcript_path=str(transcript)
    )
    result = _run_hook(payload)
    assert result.returncode == 0


def test_allow_when_test_multiedited_earlier_in_session(tmp_path: Path):
    """MultiEdit on tests counts."""
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("MultiEdit", "tests/integration/test_calibration_engine.py")],
    )
    payload = _payload(
        file_path="bot/engines/calibration.py", transcript_path=str(transcript)
    )
    result = _run_hook(payload)
    assert result.returncode == 0


def test_allow_when_equivalence_test_edited(tmp_path: Path):
    """tests/equivalence/ counts as tests."""
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Edit", "tests/equivalence/test_calibration_engine.py")],
    )
    payload = _payload(
        file_path="bot/engines/calibration.py", transcript_path=str(transcript)
    )
    result = _run_hook(payload)
    assert result.returncode == 0


def test_allow_when_conftest_edited(tmp_path: Path):
    """conftest.py under tests/ is a test-infrastructure file and
    should count (Bit 6.3 will need to extend equivalence/conftest.py
    to inject a frozen calibrator)."""
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Edit", "tests/equivalence/conftest.py")],
    )
    payload = _payload(
        file_path="bot/engines/calibration.py", transcript_path=str(transcript)
    )
    result = _run_hook(payload)
    assert result.returncode == 0


def test_test_edit_path_must_be_under_tests_dir(tmp_path: Path):
    """An edit to ``bot/tests_helper.py`` must NOT count as a test
    edit — only ``tests/**`` files do."""
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Edit", "bot/tests_helper.py")],
    )
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 2, (
        "Edit to a bot/ file with 'test' in the name must not count as "
        f"a tests/ edit. stderr={result.stderr!r}"
    )


def test_allow_with_absolute_test_path_in_transcript(tmp_path: Path):
    """Real Claude Code transcripts use absolute paths — verify the
    hook canonicalizes correctly."""
    abs_test_path = str(REPO_ROOT / "tests" / "test_calibration_engine.py")
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Edit", abs_test_path)],
    )
    payload = _payload(
        file_path=str(REPO_ROOT / "bot" / "engines" / "calibration.py"),
        transcript_path=str(transcript),
    )
    result = _run_hook(payload)
    assert result.returncode == 0


# ─── Bypass markers ─────────────────────────────────────────────────────


def test_allow_when_kalshi_tdd_bypass_env_set(tmp_path: Path):
    """KALSHI_TDD_BYPASS=1 is the per-session bypass marker."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload, extra_env={"KALSHI_TDD_BYPASS": "1"})
    assert result.returncode == 0


def test_allow_when_no_tdd_marker_in_HEAD_commit_subject(tmp_path: Path):
    """[no-tdd] in the HEAD commit subject bypasses the hook for
    doc-only Bits + git-mv Bits + refactors with equivalence proof.
    Uses an isolated git repo so we don't rely on the live HEAD."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "[no-tdd] doc-only sweep"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    bot_dir = repo / "bot"
    bot_dir.mkdir()
    (bot_dir / "_impl.py").write_text("x = 1\n")
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(
        file_path=str(bot_dir / "_impl.py"),
        transcript_path=str(transcript),
        cwd=repo,
    )
    result = _run_hook(payload, cwd=repo)
    assert result.returncode == 0, (
        f"[no-tdd] HEAD subject should bypass hook. stderr={result.stderr!r}"
    )


def test_block_when_no_tdd_NOT_in_HEAD_commit_subject(tmp_path: Path):
    """Negative: a commit message without [no-tdd] does NOT bypass."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "regular commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    bot_dir = repo / "bot"
    bot_dir.mkdir()
    (bot_dir / "_impl.py").write_text("x = 1\n")
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(
        file_path=str(bot_dir / "_impl.py"),
        transcript_path=str(transcript),
        cwd=repo,
    )
    result = _run_hook(payload, cwd=repo)
    assert result.returncode == 2, (
        f"Plain commit must NOT bypass; got returncode={result.returncode}"
    )


def test_no_tdd_marker_anywhere_in_subject_bypasses(tmp_path: Path):
    """The marker [no-tdd] is recognized regardless of position
    within the subject line — bracketed convention, not strict-prefix."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "Bit 4.X README sweep [no-tdd] [86b9ve110]"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(
        file_path=str(repo / "bot" / "_impl.py"),
        transcript_path=str(transcript),
        cwd=repo,
    )
    (repo / "bot").mkdir()
    (repo / "bot" / "_impl.py").write_text("x = 1\n")
    result = _run_hook(payload, cwd=repo)
    assert result.returncode == 0


# ─── Mac-first demonstration cases (AC: fires on synthetic edit, ──────
#     stays quiet on legitimate edit) ─────────────────────────────────


def test_demo_fires_on_synthetic_edit(tmp_path: Path):
    """AC demonstration #1 — a fresh session edit to bot/_impl.py
    with no test work fires the block."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 2
    assert "test" in result.stderr.lower()


def test_demo_stays_quiet_on_legitimate_edit(tmp_path: Path):
    """AC demonstration #2 — a session that wrote a test first and
    is now editing bot/ stays quiet (exit 0, no message)."""
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Write", "tests/test_new_thing.py")],
    )
    payload = _payload(file_path="bot/new_thing.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 0
    assert result.stderr.strip() == ""


# ─── Drift guards — pin behavior that must not regress ────────────────


def test_hook_runs_under_two_seconds(tmp_path: Path):
    """Hooks block tool execution; >2s would be felt as agent lag.
    With a stateless transcript scan + small synthetic transcript,
    the hook must be fast."""
    import time

    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Edit", f"tests/test_{i}.py") for i in range(50)],
    )
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    start = time.monotonic()
    result = _run_hook(payload)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"hook too slow: {elapsed:.2f}s"
    assert result.returncode == 0


def test_hook_does_not_print_to_stdout_when_blocking(tmp_path: Path):
    """Per Claude Code hooks docs: 'Exit code 2: Ignores stdout/JSON.
    stderr is fed to Claude.' Writing to stdout on block is a smell —
    pin the contract."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 2
    assert result.stdout == "", f"unexpected stdout: {result.stdout!r}"


def test_hook_handles_large_transcript_without_OOM(tmp_path: Path):
    """Agent transcripts can run thousands of turns — the hook
    must scan efficiently without loading the whole file at once."""
    entries = []
    for i in range(2000):
        entries.append(
            _tool_use_entry("Bash", f"placeholder_{i}", tool_use_id=f"t{i}")
        )
    entries.append(_tool_use_entry("Write", "tests/test_big.py"))
    transcript = _make_transcript(tmp_path, entries)
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 0


# ─── R1 findings — additional adversarial coverage ─────────────────────


@pytest.mark.parametrize("val", ["", "0", "false", "False", "no", "true", "yes", "01", " 1 "])
def test_kalshi_tdd_bypass_env_only_bypasses_on_exact_one(tmp_path: Path, val: str):
    """R1 MAJOR #2 — Negative semantics for KALSHI_TDD_BYPASS pinned.
    Only the exact value ``"1"`` (after strip) bypasses; anything else
    must still gate. A future refactor to ``bool(env_var)`` or
    ``.lower() in {"true","1"}`` would silently expand the bypass
    surface; this test forces a deliberate change to the contract."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload, extra_env={"KALSHI_TDD_BYPASS": val})
    assert result.returncode == 2, (
        f"KALSHI_TDD_BYPASS={val!r} must NOT bypass; got returncode={result.returncode}"
    )


def test_skips_malformed_transcript_line_and_finds_subsequent_test_edit(tmp_path: Path):
    """R1 MINOR #2 — A corrupted JSONL line must not abort the scan.
    Hook must keep iterating and recognize a downstream legitimate
    test edit. Pins the documented log-format-drift defense."""
    transcript = tmp_path / "transcript.jsonl"
    valid_entry = json.dumps(_tool_use_entry("Edit", "tests/test_thing.py"))
    with transcript.open("w") as fh:
        fh.write("{not valid json\n")
        fh.write("\n")  # blank line
        fh.write("garbage that does not parse {\n")
        fh.write(valid_entry + "\n")
    payload = _payload(file_path="bot/thing.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 0, (
        f"Hook must skip malformed lines and find the valid test edit. "
        f"stderr={result.stderr!r}"
    )


def test_allow_with_realistic_edit_input_fields(tmp_path: Path):
    """R1 MINOR #3 — Real Edit tool_uses have ``old_string``,
    ``new_string``, ``replace_all`` alongside ``file_path``. Pin
    that the hook tolerates the realistic shape (it only reads
    ``file_path``, but a future "validate the input shape" change
    must not break this contract).
    """
    realistic_input = {
        "file_path": "tests/test_thing.py",
        "old_string": "x = 1",
        "new_string": "x = 2",
        "replace_all": False,
    }
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "Edit", "input": realistic_input}],
        },
    }
    transcript = _make_transcript(tmp_path, [entry])
    payload = _payload(file_path="bot/thing.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 0


def test_sidechain_test_edit_does_not_count(tmp_path: Path):
    """R1 MINOR #1 — Sub-agent transcripts (isSidechain=True) edit
    tool_uses must NOT count toward the parent gate. Defensive
    against future inlining of sidechain entries into the parent
    transcript."""
    sidechain_entry = {
        "type": "assistant",
        "isSidechain": True,
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Edit",
                 "input": {"file_path": "tests/test_thing.py"}},
            ],
        },
    }
    transcript = _make_transcript(tmp_path, [sidechain_entry])
    payload = _payload(file_path="bot/thing.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 2, (
        "isSidechain test edit must not count toward parent gate. "
        f"stderr={result.stderr!r}"
    )


def test_block_path_runs_under_two_seconds_on_large_no_match_transcript(tmp_path: Path):
    """R1 MINOR #6 — Performance pin for the *block* path (no test
    edit anywhere in a large transcript). The earlier test_runs_under_two_seconds
    short-circuits on the first tests/ Edit; this one forces the
    full scan."""
    import time

    entries = []
    for i in range(5000):
        entries.append(_tool_use_entry("Bash", f"placeholder_{i}", tool_use_id=f"t{i}"))
    transcript = _make_transcript(tmp_path, entries)
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    start = time.monotonic()
    result = _run_hook(payload)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"hook too slow on full no-match scan: {elapsed:.2f}s"
    assert result.returncode == 2


def test_repo_root_conftest_does_not_count_as_test_edit(tmp_path: Path):
    """R1 MINOR #5 — Only files under ``tests/`` count. The
    repo-root ``conftest.py`` (sibling of bot/, tests/, etc.) is
    test infrastructure but not under tests/ — it must not bypass
    the gate. Documented in tests/CLAUDE.md so an agent who hits
    the block knows why their conftest edit didn't help.
    """
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Edit", "conftest.py")],
    )
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 2


def test_block_message_mentions_test_writer_skill(tmp_path: Path):
    """R1 MINOR #4 — Block message points the agent to the
    /test-writer skill so they don't have to dig through tests/CLAUDE.md
    to find the helper."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 2
    assert "/test-writer" in result.stderr or "test-writer" in result.stderr.lower()


def test_block_when_multiedit_target_is_bot_py(tmp_path: Path):
    """R1 MINOR — explicit MultiEdit-on-bot/ block. The matcher
    covers MultiEdit, but sealing it with a positive test guards
    against a future regression that drops MultiEdit from the
    matcher or from _FILE_EDIT_TOOLS."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(
        tool_name="MultiEdit",
        file_path="bot/_impl.py",
        transcript_path=str(transcript),
    )
    result = _run_hook(payload)
    assert result.returncode == 2



# ─── R2 findings — additional adversarial coverage ─────────────────────


@pytest.mark.parametrize("bad_input", [
    "string-not-dict",
    42,
    ["list"],
    None,
    {"file_path": [1]},
    {"file_path": 42},
    {"file_path": None},
])
def test_fail_open_on_malformed_tool_input(bad_input, tmp_path: Path):
    """R2 CRITICAL #1 — non-dict ``tool_input`` and non-string
    ``tool_input.file_path`` must fail open (exit 0) without a
    stack trace. The hook docstring promises this fail-open
    contract; a future BulkEdit-style tool with a different
    payload shape must not crash the hook.
    """
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    payload["tool_input"] = bad_input
    result = _run_hook(payload)
    assert result.returncode == 0, (
        f"malformed tool_input {bad_input!r} must fail open. "
        f"stderr={result.stderr!r}"
    )
    assert "Traceback" not in result.stderr, (
        f"hook leaked a traceback for {bad_input!r}: {result.stderr}"
    )


def test_no_tdd_marker_must_be_anchored_token(tmp_path: Path):
    """R2 MAJOR #2 — substring match on ``[no-tdd]`` would false-positive
    on subjects that mention the marker in passing (e.g., a meta-discussion
    decision-log commit). Anchored token match required."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m",
         "Decision: discuss[no-tdd]policy in next standup"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    bot_dir = repo / "bot"
    bot_dir.mkdir()
    (bot_dir / "_impl.py").write_text("x = 1\n")
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(
        file_path=str(bot_dir / "_impl.py"),
        transcript_path=str(transcript),
        cwd=repo,
    )
    result = _run_hook(payload, cwd=repo)
    assert result.returncode == 2, (
        "[no-tdd] embedded inside other text without whitespace separators "
        "must not bypass; got rc={}".format(result.returncode)
    )


def test_no_tdd_with_punctuation_separator_bypasses(tmp_path: Path):
    """R2 MAJOR #2 — Sister test to the anchored check: legitimate
    bracketed marker followed by a colon (``[no-tdd]: doc-only``) is
    a recognized anchored form."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m",
         "[no-tdd]: README sweep"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "bot").mkdir()
    (repo / "bot" / "_impl.py").write_text("x = 1\n")
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(
        file_path=str(repo / "bot" / "_impl.py"),
        transcript_path=str(transcript),
        cwd=repo,
    )
    result = _run_hook(payload, cwd=repo)
    assert result.returncode == 0


def test_block_when_transcript_path_is_empty_string(tmp_path: Path):
    """R2 MAJOR #3 — transcript_path is mandatory in the Claude Code
    PreToolUse JSON schema. An empty string indicates contract
    violation (or spoofed/fuzzed input). The conservative call is
    to block with the standard message rather than silently grant
    bypass on every bot/ edit.
    """
    payload = _payload(file_path="bot/_impl.py", transcript_path="")
    result = _run_hook(payload)
    assert result.returncode == 2, (
        "empty transcript_path must NOT silently bypass — that would let "
        "any harness regression unlock all bot/ edits. "
        f"stderr={result.stderr!r}"
    )


def test_non_py_file_under_tests_does_not_count(tmp_path: Path):
    """R2 MINOR #1 — Files under tests/ that are not .py (e.g.,
    REGEN.md, snapshot YAML/CSV, .DS_Store) must NOT count as
    "test edits". Editing a Markdown runbook is not a test."""
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Edit", "tests/REGEN.md")],
    )
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 2, (
        "tests/REGEN.md is not a .py test file and must not bypass"
    )


def test_snapshot_yaml_does_not_count(tmp_path: Path):
    """R2 MINOR #1 — Pillar-3 equivalence snapshot files (YAML/CSV
    under tests/equivalence/) are explicitly human-review-only.
    Editing one must not bypass the gate."""
    transcript = _make_transcript(
        tmp_path,
        [_tool_use_entry("Edit",
                         "tests/equivalence/test_volatility_engine/snap.yml")],
    )
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 2


def test_message_nested_isSidechain_also_excluded(tmp_path: Path):
    """R2 MINOR #2 — Belt-and-suspenders: if Anthropic moves the
    isSidechain flag from top-level to nested under ``message``,
    the filter still works. Today's transcripts have it at the top
    level; this is forward-looking."""
    nested_sidechain_entry = {
        "type": "assistant",
        # NOTE: isSidechain is NOT at top level; only inside message.
        "message": {
            "role": "assistant",
            "isSidechain": True,
            "content": [
                {"type": "tool_use", "id": "t1", "name": "Edit",
                 "input": {"file_path": "tests/test_thing.py"}},
            ],
        },
    }
    transcript = _make_transcript(tmp_path, [nested_sidechain_entry])
    payload = _payload(file_path="bot/thing.py", transcript_path=str(transcript))
    result = _run_hook(payload)
    assert result.returncode == 2, (
        "nested isSidechain must also exclude. stderr={!r}".format(result.stderr)
    )


def test_settings_json_matcher_is_strict_edit_write_multiedit(tmp_path: Path):
    """R2 MINOR #4 — substring match (``"Edit" in matcher``) admits
    false-positive matchers like ``"NotEdit"``. Strict set-equality
    against the documented contract."""
    data = json.loads(SETTINGS_PATH.read_text())
    pre_hooks = data.get("hooks", {}).get("PreToolUse", [])
    matchers = [entry.get("matcher", "") for entry in pre_hooks]
    expected = {"Edit", "Write", "MultiEdit"}
    matched = [m for m in matchers if set(m.split("|")) >= expected]
    assert matched, (
        f"PreToolUse matcher must include all of {expected} as |-tokens; "
        f"got {matchers!r}"
    )



# ─── R3 findings — additional adversarial coverage ─────────────────────


def test_subagent_transcript_does_not_inherit_parent_test_edits(tmp_path: Path):
    """R3 MINOR #2 — Each Claude Code session (parent + each Task
    subagent) has its own transcript_path. A parent's test edit
    does NOT satisfy the hook for a subagent. The subagent must
    write its own test, or the parent must bypass.

    This pins the cross-session isolation of the gate.
    """
    # Construct a "subagent-shaped" transcript: empty (no test edits
    # in the subagent's own session). The parent's test edits live
    # in a different file the subagent never reads.
    subagent_transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(subagent_transcript))
    result = _run_hook(payload)
    assert result.returncode == 2, (
        "subagent transcript without its own test edits must block. "
        f"stderr={result.stderr!r}"
    )


def test_payload_cwd_takes_precedence_over_process_cwd(tmp_path: Path):
    """R3 MINOR #3 — The hook reads ``cwd`` from the JSON payload (the
    documented contract), not from ``os.getcwd()``. If the harness
    sends a payload from a subagent's working dir but the hook process
    happens to be running in the parent dir, the hook must trust the
    payload's cwd."""
    fake_repo = tmp_path / "fake_repo"
    fake_repo.mkdir()
    (fake_repo / "bot").mkdir()
    transcript = _make_transcript(tmp_path, [])
    # Run the hook with process-cwd = REPO_ROOT (real repo) but
    # payload-cwd = fake_repo. The in-scope check should resolve
    # the file path against payload-cwd.
    payload = _payload(
        file_path="bot/_impl.py",
        transcript_path=str(transcript),
        cwd=fake_repo,
    )
    result = _run_hook(payload, cwd=REPO_ROOT)
    # bot/_impl.py resolved against fake_repo IS under fake_repo/bot,
    # so the file is in scope. With no test edit and no bypass, block.
    assert result.returncode == 2, (
        "payload cwd should be authoritative for in-scope checks; "
        f"stderr={result.stderr!r}"
    )


def test_skill_file_references_are_live(tmp_path: Path):
    """R3 MINOR #1 — SKILL.md should not link to non-existent files
    (other than the planned closeout doc). Specifically, the parent
    ticket reference should be a live URL, and the closeout doc
    reference must be marked "planned" so a fresh maintainer doesn't
    chase a dead link."""
    body = SKILL_PATH.read_text()
    # Closeout doc reference should be hedged with "planned" until
    # the closeout doc actually lands.
    if "kb/decisions/testing-foundation-pillar-4-shipped" in body:
        assert "planned" in body.lower(), (
            "Closeout doc reference must be marked 'planned' until "
            "the doc exists at that path."
        )


def test_tests_claude_md_failure_modes_table_documents_empty_transcript_blocks(tmp_path: Path):
    """R3 MAJOR #1 — tests/CLAUDE.md must reflect post-R2 contract:
    empty transcript_path BLOCKS (was incorrectly listed under fail-
    open in the pre-R3 doc)."""
    docs = (REPO_ROOT / "tests" / "CLAUDE.md").read_text()
    # The doc must distinguish empty-string (block) from missing-file (fail-open).
    assert "empty string" in docs and "block" in docs, (
        "tests/CLAUDE.md failure modes must explicitly say empty "
        "transcript_path BLOCKS"
    )
    # And it must document the sub-agent (Task) cross-session isolation.
    assert "Sub-agent" in docs or "sub-agent" in docs.lower() or "subagent" in docs.lower(), (
        "tests/CLAUDE.md must document subagent cross-session isolation"
    )



# ─── R4 findings — additional adversarial coverage ─────────────────────


@pytest.mark.parametrize("bad_value", [42, ["list"], {"dict": 1}, 3.14])
def test_fail_open_on_non_string_transcript_path(bad_value, tmp_path: Path):
    """R4 MINOR #1 — non-string ``transcript_path`` must fail open
    rather than crash with a TypeError. Mirror of the R2 CRITICAL #1
    isinstance defense for tool_input."""
    payload = _payload(file_path="bot/_impl.py", transcript_path="ignored")
    payload["transcript_path"] = bad_value
    result = _run_hook(payload)
    assert result.returncode == 0, (
        f"non-string transcript_path {bad_value!r} must fail open. "
        f"stderr={result.stderr!r}"
    )
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("bad_value", [42, ["list"], {"dict": 1}, 3.14])
def test_fail_open_on_non_string_cwd(bad_value, tmp_path: Path):
    """R4 MINOR #1 — non-string ``cwd`` must fail open."""
    transcript = _make_transcript(tmp_path, [])
    payload = _payload(file_path="bot/_impl.py", transcript_path=str(transcript))
    payload["cwd"] = bad_value
    result = _run_hook(payload)
    assert result.returncode == 0, (
        f"non-string cwd {bad_value!r} must fail open. "
        f"stderr={result.stderr!r}"
    )
    assert "Traceback" not in result.stderr


def test_tests_claude_md_documents_py_only_test_edit_gate():
    """R4 MAJOR #1 — tests/CLAUDE.md must say only ``.py`` files
    under tests/ count. The R2 fix tightened the gate; R4 caught
    the doc never said so."""
    docs = (REPO_ROOT / "tests" / "CLAUDE.md").read_text()
    assert ".py" in docs, "tests/CLAUDE.md must reference .py restriction"
    # Verify the doc covers the non-counts: snapshot YAML/CSV and REGEN.md.
    assert "snapshot" in docs.lower() or "REGEN.md" in docs, (
        "tests/CLAUDE.md should give an example of what does NOT count"
    )
