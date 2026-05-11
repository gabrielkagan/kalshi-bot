"""Regression tests for pruned CLAUDE.md (Bit 1.4 of repo modularization).

Sprint 1 of repo modularization plan
(kb/decisions/repo-modularization-plan-may05.md), Bit 1.4
(plan lines 1328-1336).

Pins the Bit 1.4 contract:
- CLAUDE.md is ≤80 lines (the bit's hard done-when criterion).
- AGENTS.md (symlink to CLAUDE.md per Bit 1.3) inherits the prune —
  read-through line count matches. Catches a future "fix" that
  replaces the symlink with a stale copy.
- The five load-bearing structural `##` headings are present
  (Reference docs, Interaction rules, Critical rules, Anti-patterns,
  Skill routing). Catches a future agent who deletes a whole section
  while pruning further. The H1 project summary header
  (`# Kalshi Crypto Trading Bot`) is pinned separately by
  `test_required_sections_present` (the file must START with that
  line) — line-cap and AGENTS.md content equality alone don't cover
  H1 deletion (a future prune that drops the H1 + adds a blank line
  still passes both, and AGENTS.md inherits any CLAUDE.md change via
  the symlink, so byte-equality is satisfied either way).
- The "bot/_impl.py is sacred" rule line is present (literal). Sacred-file
  invariant; deleting it would be a serious regression.
- Reference-doc pointers (agent_docs/*) resolve to real files. Catches
  the case where a referenced doc is deleted but the pointer is left
  behind.
- Forward-looking pointers required by Bit 1.4 design:
  - bot/_impl.py implementation rules pointer (the breadcrumb to
    `agent_docs/bot-claude-md-draft.md`, which Sprint 2 Bit 2.2
    promotes via `git mv` to `bot/CLAUDE.md`). R1 review moved the
    draft out of the plan's literal `kb/drafts/` path because `kb/`
    is local-only by convention and would leave the breadcrumb
    pointing at a file absent on a fresh clone.
  - Two-file-mode flag pointer (Bit 1.3 closeout commitment 2 — the
    GO/NO-GO trigger to flip to separate AGENTS.md + CLAUDE.md).
- `make test-fast` recipe invokes this file (symmetry with
  `tests/test_agents_md_symlink.py` from Bit 1.3 — fast-tier pin).
"""
import os
import re
from pathlib import Path

import pytest
import bot.main_loop  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
MAKEFILE = REPO_ROOT / "Makefile"

# Bit 1.4 hard cap. Plan line 1331: "root CLAUDE.md ≤80 lines".
# Margin matters: every interaction loads CLAUDE.md, so each line is a
# tax on context. Aim for headroom under the cap (today ~78); a future
# agent who pushes back to 80 isn't strictly violating the contract,
# but the rising-line-count drift is the early signal of bloat.
MAX_LINES = 80


def _line_count(path: Path) -> int:
    """Count lines using the wc -l semantics: number of '\\n' terminators.

    A trailing newline is conventional for POSIX text files (and
    enforced by most editors). A file without a trailing newline
    reports as one less than visible lines, which is fine for a
    ≤80 cap — if anything it nudges authors to land cleanly.
    """
    return path.read_bytes().count(b"\n")


def test_claude_md_within_line_cap():
    """CLAUDE.md must be ≤80 lines.

    Pins the Bit 1.4 done-when criterion. Every interaction loads
    CLAUDE.md, so each line is context budget; the cap forces detail
    into agent_docs/ + kb/_index.md + package-level CLAUDE.md files
    where it costs zero tokens until pulled.
    """
    n = _line_count(CLAUDE_MD)
    assert n <= MAX_LINES, (
        f"CLAUDE.md is {n} lines (limit: {MAX_LINES}). Push detail to "
        f"agent_docs/, kb/_index.md, or package-level CLAUDE.md "
        f"(`bot/CLAUDE.md`, `tests/CLAUDE.md`, `scripts/CLAUDE.md`, "
        f"`ops/CLAUDE.md`, `kb/CLAUDE.md`) rather than expanding root. "
        f"Plan reference: "
        f"kb/decisions/repo-modularization-plan-may05.md line 1331."
    )


def test_agents_md_line_count_matches_claude_md():
    """AGENTS.md (symlink to CLAUDE.md per Bit 1.3) must read through to
    the same line count.

    Belt-and-suspenders for `tests/test_agents_md_symlink.py::
    test_agents_md_content_matches_claude_md` — that test pins
    byte-equal content; this one pins the line count specifically. If
    a future "fix" replaces the symlink with a stale text copy, the
    drift surfaces here as a count mismatch the moment CLAUDE.md is
    edited.
    """
    claude_lines = _line_count(CLAUDE_MD)
    agents_lines = _line_count(AGENTS_MD)
    assert agents_lines == claude_lines, (
        f"AGENTS.md line count ({agents_lines}) does not match "
        f"CLAUDE.md ({claude_lines}). Bit 1.3 ships AGENTS.md as a "
        f"symlink to CLAUDE.md; if these differ, the symlink was "
        f"replaced with a copy and is now drifting. Restore with: "
        f"rm AGENTS.md && ln -s CLAUDE.md AGENTS.md && git add AGENTS.md"
    )


# Required headings — match exactly as they appear in CLAUDE.md so a
# rename (e.g., "Critical rules" → "Rules") fires the test.
# Headings are detected as start-of-line matches to avoid false
# positives from quoted references in body text.
REQUIRED_SECTIONS = (
    "## Reference docs (read on demand)",
    "## Interaction rules",
    "## Critical rules",
    "## Anti-patterns",
    "## Skill routing",
)


def test_required_sections_present():
    """The five load-bearing sections + the H1 project summary must remain.

    These are the structural anchors: an agent (or doc-drift script)
    looking for "where do I find the skill routing" expects exactly
    `## Skill routing` and not `## Skills` or `## Routing`. A future
    further-prune that drops a section entirely would be caught here.
    Renaming a section without updating the test is the intended
    failure mode (forces a deliberate decision).

    H1 pin: the file must START with `# Kalshi Crypto Trading Bot\\n`.
    R9 review caught that line-cap + AGENTS.md byte-equality alone
    don't cover H1 deletion (line-cap is satisfied if the H1 line is
    replaced with a blank; AGENTS.md is a symlink, so any deletion
    propagates and byte-equality is preserved either way). This is
    the only test that would fire if a future prune accidentally
    drops the H1.
    """
    text = CLAUDE_MD.read_text()
    assert text.startswith("# Kalshi Crypto Trading Bot\n"), (
        "CLAUDE.md must START with `# Kalshi Crypto Trading Bot` "
        "as the H1 project summary header. AGENTS.md (Bit 1.3 "
        "symlink) inherits this; if a future prune drops the H1, "
        "neither the line-cap nor AGENTS.md content-equality test "
        "would catch it."
    )
    missing = [s for s in REQUIRED_SECTIONS if f"\n{s}\n" not in f"\n{text}"]
    assert not missing, (
        f"CLAUDE.md is missing required section headings: {missing}. "
        f"If you renamed a section deliberately, update REQUIRED_SECTIONS "
        f"in this test."
    )


def test_bot_py_sacred_rule_present():
    """The bot/__main__.py-is-the-entrypoint-shim sacred rule must remain
    in the Critical rules.

    Bit 9.4 (2026-05-10) shifted the framing from "bot/_impl.py is the
    body" (Bit-2.1a-era) to "bot/__main__.py is the entrypoint shim —
    sacred boundary, no logic. Logic lives in bot/<subpackage>/<module>.py."
    Post-Bit-9.3-ii, bot/__main__.py imports MainLoop directly from
    bot.main_loop (NOT from bot._impl); the prior framing is factually
    wrong and was replaced atomically.

    A regression agent that "tidies" by removing this rule line creates
    a path to accidental refactor of the canonical entrypoint. Match
    the leading bullet so a stray prose mention elsewhere doesn't
    satisfy the check.
    """
    text = CLAUDE_MD.read_text()
    assert "**`bot/__main__.py` is the entrypoint shim — sacred boundary, no logic.**" in text, (
        "CLAUDE.md is missing the canonical Bit-9.4 sacred rule "
        "(`**`bot/__main__.py` is the entrypoint shim — sacred boundary, "
        "no logic.**`). This rule is load-bearing post-Bit-9.4: it pins "
        "the post-Bit-9.3-ii reality that bot/__main__.py imports MainLoop "
        "directly from bot.main_loop (not via bot._impl), and that logic "
        "lives in bot/<subpackage>/<module>.py."
    )


# Bit 2.0.5.1 of repo modularization plan
# (kb/decisions/repo-modularization-plan-may05.md, plan line 1459).
# After the Bit 2.1a systemd-mismatch incident
# (kb/failures/bit-2.1a-systemd-mismatch-may06.md), CLAUDE.md's
# documented startup chain became load-bearing prose with no
# enforcement. This pin closes that gap: the chain string must appear
# in CLAUDE.md so a future prune that quietly drops or rewords it
# breaks the test instead of silently drifting away from production.
#
# The chain reads `systemd -> ops/kalshi-bot.service -> start.sh ->
# bot/_impl.py` (in `->` arrow form here for source readability; CLAUDE.md
# itself uses the unicode arrow). Each segment in CLAUDE.md is
# wrapped in backticks (matching the existing code-reference
# convention); the test pins the literal form including backticks.
#
# Sprint progression — when this test must update:
#   - Bit 2.1a re-attempt (after Sprint 2.0.5 GO/NO-GO): the chain
#     extends to `systemd -> ops/kalshi-bot.service -> start.sh ->
#     python -m bot -> bot/__main__.py -> bot/_impl.py`. Update
#     SYSTEMD_CHAIN_LITERAL in the same atomic commit that edits
#     CLAUDE.md and start.sh — otherwise CI fails on the test
#     mismatch and forces the deliberate decision.
SYSTEMD_CHAIN_LITERAL = (
    "systemd → `ops/kalshi-bot.service` → `start.sh` → `python -m bot` → "
    "`bot/__main__.py` → `bot.main_loop.MainLoop`"
)


def test_systemd_chain_documented_in_claude_md():
    """The startup chain `systemd -> ops/kalshi-bot.service -> start.sh
    -> bot/_impl.py` must appear literally in CLAUDE.md.

    This pin exists because the Bit 2.1a incident proved that an
    unenforced architectural claim in CLAUDE.md is a future incident.
    Pre-Bit-2.0.5.1 CLAUDE.md said `systemd -> start.sh -> bot/_impl.py`
    but the on-VPS unit invoked `python3 bot/_impl.py` directly, bypassing
    start.sh entirely. The mismatch went undetected through 14+3
    adversarial review rounds because no test backed the claim.

    Bit 2.0.5.1 ships a tracked `ops/kalshi-bot.service` whose
    ExecStart calls `start.sh`; once the operator runs
    `bash ops/install.sh` on the VPS, the on-VPS unit matches the
    documented chain. Bit 2.0.5.2 then adds CI drift detection that
    diffs the on-VPS unit against `ops/kalshi-bot.service`. This test
    is the doc-side complement: it pins the chain string in CLAUDE.md
    so the documented chain cannot drift away from the unit-tracked
    chain without one of the two checks firing.

    Failure mode this catches: a future agent prunes the chain
    segment for brevity, or rewords the arrows, or replaces the
    backticks with quotes. Any such edit fails this test, forcing the
    author to either reconsider or update SYSTEMD_CHAIN_LITERAL
    deliberately. The latter case is expected at Bit 2.1a re-attempt
    (chain extends to include `python -m bot -> bot/__main__.py ->
    bot/_impl.py`).
    """
    text = CLAUDE_MD.read_text()
    assert SYSTEMD_CHAIN_LITERAL in text, (
        f"CLAUDE.md is missing the documented startup chain literal "
        f"{SYSTEMD_CHAIN_LITERAL!r}. This chain is load-bearing per "
        f"the Bit 2.1a postmortem "
        f"(kb/failures/bit-2.1a-systemd-mismatch-may06.md): the "
        f"on-VPS systemd unit must invoke this exact chain, and "
        f"CLAUDE.md must document it so adversarial reviewers and "
        f"future agents have an on-load reference. If you intend to "
        f"extend the chain (e.g., post-Bit-2.1a `python -m bot -> "
        f"bot/__main__.py -> bot/_impl.py`), update "
        f"SYSTEMD_CHAIN_LITERAL in this test in the SAME commit that "
        f"edits CLAUDE.md."
    )

    # Lesson of Bit 2.1a NOT yet internalized = pinning prose with
    # prose. The chain string is a doc-side claim; without on-disk
    # enforcement, a future "tidy" that deletes the ops/ tree while
    # leaving CLAUDE.md untouched silently passes. Pin the underlying
    # files so the test fails LOUDLY in that case (R1 review #3).
    ops_unit = REPO_ROOT / "ops" / "kalshi-bot.service"
    assert ops_unit.exists(), (
        f"CLAUDE.md documents `ops/kalshi-bot.service` as the systemd "
        f"unit source of truth, but the file does not exist at "
        f"{ops_unit}. Either restore the file or — if Bit 2.0.5.1 has "
        f"been deliberately reverted — also revert the chain edit in "
        f"CLAUDE.md and update SYSTEMD_CHAIN_LITERAL in this test."
    )
    ops_install = REPO_ROOT / "ops" / "install.sh"
    assert ops_install.exists(), (
        f"`ops/install.sh` does not exist at {ops_install}. The chain "
        f"in CLAUDE.md is meaningless without the install script that "
        f"makes the on-VPS unit match `ops/kalshi-bot.service`."
    )
    assert os.access(ops_install, os.X_OK), (
        f"{ops_install} is not executable. Fix with: chmod +x "
        f"{ops_install}. Without the +x bit, `bash ops/install.sh` "
        f"still works (bash interprets it directly), but operators "
        f"following `./ops/install.sh` muscle-memory will hit a "
        f"permission error."
    )
    # CLAUDE.md line 14 (package-level-guides bullet) lists
    # `ops/CLAUDE.md` alongside tests/ + scripts/. Pin its existence
    # too so a future delete that leaves the CLAUDE.md mention behind
    # fails the test (R2 review #3).
    ops_claude = REPO_ROOT / "ops" / "CLAUDE.md"
    assert ops_claude.exists(), (
        f"CLAUDE.md lists `ops/CLAUDE.md` as an in-dir auto-loaded "
        f"package guide, but the file does not exist at {ops_claude}. "
        f"Either restore the file or remove the `ops/CLAUDE.md` "
        f"mention from CLAUDE.md's package-level-guides bullet."
    )


# Bit 2.0.5.1 R2 review #5. start.sh becomes load-bearing once the
# on-VPS systemd unit's ExecStart points at it (rather than directly
# at python3 bot/_impl.py). Without `set -e`, a silent venv-activate failure
# would fall back to system python3 with missing dependencies — exactly
# the kind of "documentation-claim-without-enforcement" smell that
# Bit 2.1a's incident reinforced. Pin the line here so a future "tidy"
# of start.sh fails the test instead of silently regressing.
START_SH = REPO_ROOT / "start.sh"


def test_start_sh_has_set_e_for_load_bearing_invocation():
    """start.sh must contain `set -eo pipefail` (or stricter).

    Once the on-VPS systemd unit's ExecStart points at start.sh
    (Bit 2.0.5.1 ships the unit; operator activates via `bash
    ops/install.sh`), start.sh is load-bearing for production startup.
    A silent failure in `source venv/bin/activate` (corrupt venv,
    missing file, partial pip install) without `set -e` falls through
    to `exec python3 bot/_impl.py` resolving to system python3 — which
    typically lacks lightgbm/scipy/scikit-learn and crashes bot/_impl.py
    at import. That confusing failure mode is what `set -e` prevents.

    The shebang must be `/bin/bash` (not `/bin/sh`) because `set -o
    pipefail` is bash-specific. This test pins both invariants.
    """
    text = START_SH.read_text()
    first_line = text.splitlines()[0] if text else ""
    assert first_line == "#!/bin/bash", (
        f"start.sh must start with `#!/bin/bash` (not {first_line!r}). "
        f"`set -o pipefail` is bash-specific; switching to /bin/sh "
        f"would silently break the safety net."
    )
    assert "set -eo pipefail" in text or "set -euo pipefail" in text, (
        f"start.sh must contain `set -eo pipefail` (or `set -euo "
        f"pipefail`). Without it, a silent venv-activate failure "
        f"falls back to system python3 — exactly the smell Bit "
        f"2.0.5.1 was designed to close. See "
        f"kb/failures/bit-2.1a-systemd-mismatch-may06.md for the "
        f"incident class this guards against."
    )


# Pointers in the "Reference docs" section. A pointer to a deleted
# file is a silent rot; we want loud breakage. The list is the
# minimum survivable set — a future prune that drops one of these
# should rename the test, not skip it.
REFERENCED_DOCS = (
    "agent_docs/current_state.md",
    "agent_docs/config_reference.md",
    "agent_docs/db_schema.md",
    "agent_docs/bot_layout.md",
    "agent_docs/calibration_pipeline.md",
    "kb/_index.md",
    "kb-research/_index.md",
)


@pytest.mark.parametrize("rel_path", REFERENCED_DOCS)
def test_referenced_doc_path_is_mentioned(rel_path: str):
    """Each referenced doc path must appear literally in CLAUDE.md.

    Catches the case where a future prune drops one of the seven
    on-demand reference docs that fire on agent demand. The pointer
    is the contract; deletion of the file should also delete the
    pointer (or an agent following the pointer hits a dead link).
    """
    text = CLAUDE_MD.read_text()
    assert rel_path in text, (
        f"CLAUDE.md no longer references {rel_path!r}. If the doc "
        f"was renamed/moved, update the pointer; if removed, drop "
        f"the entry from REFERENCED_DOCS in this test."
    )


@pytest.mark.parametrize("rel_path", REFERENCED_DOCS)
def test_referenced_doc_path_exists_on_disk(rel_path: str):
    """Each referenced doc path must resolve to a real file.

    `kb/_index.md` and `kb-research/_index.md` are git-tracked
    pre-rule legacy entries; on a normal `git clone` the parent
    `kb/` / `kb-research/` directories exist and the files are
    present, so the skip below does NOT fire. The skip is an edge-
    case backstop for pathological package installs (sdist/wheel
    builds that strip non-package content via MANIFEST.in
    exclusions, or out-of-tree harness scenarios) where the kb tree
    is intentionally absent. The broader local-only-by-convention
    rule applies to net-new files, not to these tracked legacy
    entries. The literal pointer presence (covered by
    `test_referenced_doc_path_is_mentioned`) is the portable
    contract that always fires.
    """
    parent = (REPO_ROOT / rel_path).parent
    if not parent.exists():
        pytest.skip(
            f"{parent} does not exist on this checkout (likely a "
            f"sdist/wheel/non-author clone without local kb/). The "
            f"literal pointer presence is covered by "
            f"test_referenced_doc_path_is_mentioned."
        )
    target = REPO_ROOT / rel_path
    assert target.exists(), (
        f"CLAUDE.md references {rel_path!r} but the file does not "
        f"exist at {target}. Either restore the doc or remove the "
        f"pointer from CLAUDE.md (and from REFERENCED_DOCS in this "
        f"test)."
    )


# Active-breadcrumb pattern: an "active pointer" is the canonical
# `see \`PATH\`` form Bit 1.4's Critical-rules breadcrumb uses. Forward-
# looking mentions ("Sprint 2 Bit 2.2 promotes this draft to
# `bot/CLAUDE.md`", "Package-level guides ... `bot/CLAUDE.md` once
# Sprint 2 Bit 2.2 ships") deliberately do NOT match — they're
# describing future state, not pointing at a file that should exist
# today. The `see \`...\`` regex captures the path INSIDE backticks
# preceded by literal "see `" so a casual mention isn't promoted to
# an active-breadcrumb claim.
_ACTIVE_BREADCRUMB_RE = re.compile(r"see `([^`]+)`")

# Subset of paths that are considered "breadcrumb candidates" for this
# test. Other `see \`...\`` references (e.g., to a postmortem) live in
# `kb/failures/` and are out of scope for the dead-link check (they're
# covered by the broader pointer-presence convention; their absence
# isn't a Bit 1.4 contract).
#
# **Maintenance contract:** when adding a new active `see \`<path>\``
# breadcrumb to CLAUDE.md (e.g., a draft for `tests/CLAUDE.md`, or a
# third package guide), add the path here so its existence is
# checked. Otherwise the addition gets silent under-coverage.
_BREADCRUMB_CANDIDATES = {
    "agent_docs/bot-claude-md-draft.md",
    "bot/CLAUDE.md",
}

# Files outside CLAUDE.md that may carry pointers to the bot/_impl.py-rules
# draft path. Bit 1.4 R1+R3 introduced these cross-file pointers (in
# README.md "Repository conventions" and scripts/CLAUDE.md cell-block
# one-liner). Sprint 2 Bit 2.2 must update all three sites in the
# same commit, otherwise the cross-file pointers become dead links.
# `test_cross_file_draft_references_resolve` enforces that hand-off.
_CROSS_FILE_DRAFT_REFERENCE_SITES = (
    "README.md",
    "scripts/CLAUDE.md",
)


def test_bot_claude_md_draft_exists_on_disk():
    """Every ACTIVE breadcrumb path in CLAUDE.md must exist on disk.

    Distinct from `test_bot_py_implementation_rules_breadcrumb_present`
    (which only checks the path appears in the file). This one catches
    dead pointers — the breadcrumb file is moved/deleted but
    CLAUDE.md still references it via "see `<path>`".

    "Active breadcrumb" = the `see \\`PATH\\`` form. Forward-looking
    mentions ("once Sprint 2 ships", "promotes to") deliberately
    don't match; they describe future state and the target file isn't
    expected to exist today. The R2 review hardened the prior
    `if A and not B → check A` conditional which silently passed when
    both paths were referenced; this regex-driven version checks every
    active pointer independently.

    Sprint progression:
      - Today (Bit 1.4 → Bit 2.1): the active pointer is
        `see \\`agent_docs/bot-claude-md-draft.md\\``; that file must exist.
        `bot/CLAUDE.md` is only mentioned as forward-looking text and
        is not flagged.
      - Bit 2.2: the active pointer flips to `see \\`bot/CLAUDE.md\\``
        in the same commit that ships `bot/CLAUDE.md`; the draft is
        `git mv`d. Both `bot/CLAUDE.md` and the (now-missing) draft
        are covered correctly.
    """
    text = CLAUDE_MD.read_text()
    active_paths = _ACTIVE_BREADCRUMB_RE.findall(text)
    breadcrumb_targets = [
        REPO_ROOT / p for p in active_paths if p in _BREADCRUMB_CANDIDATES
    ]
    assert breadcrumb_targets, (
        "CLAUDE.md has no active breadcrumb (`see `<path>``) pointing "
        "at `agent_docs/bot-claude-md-draft.md` or `bot/CLAUDE.md`. "
        "The breadcrumb is the contract; if this is intentional "
        "(e.g., draft retired and bot/_impl.py rules fully reabsorbed at "
        "root), update or remove this test."
    )
    missing = [str(p) for p in breadcrumb_targets if not p.exists()]
    assert not missing, (
        f"CLAUDE.md has active breadcrumb(s) (`see `<path>``) "
        f"pointing at file(s) that are missing on disk: {missing}. "
        f"Either restore the file(s), or remove the dead `see `...`` "
        f"reference from CLAUDE.md."
    )


def test_bot_py_implementation_rules_breadcrumb_present():
    """The breadcrumb to `agent_docs/bot-claude-md-draft.md` must remain.

    Bit 1.4 design: bot/_impl.py-specific implementation rules
    (torch threading + `_thread_env` ordering, cal_mlp four-site
    lock-step, cell-block string literals, SQLite WAL pragmas, etc.)
    were moved out of root CLAUDE.md and staged at
    `agent_docs/bot-claude-md-draft.md` (R1 review fix: the original
    plan path `kb/drafts/` is local-only-by-convention and would
    leave the breadcrumb pointing at a file absent on a fresh
    clone). Sprint 2 Bit 2.2 promotes the draft to `bot/CLAUDE.md`.
    Until that ships, the breadcrumb at root is the only on-load
    reminder in the Critical-rules section that those rules exist —
    deleting it strands the rules.

    Two pinned elements:
      1. The literal label `**bot/_impl.py implementation rules**` (the
         Critical-rules bullet prefix). Stable across the
         draft → `bot/CLAUDE.md` transition.
      2. A pointer (today: `agent_docs/bot-claude-md-draft.md`;
         after Bit 2.2: `bot/CLAUDE.md`). Either form satisfies.

    Pinning the LABEL specifically (rather than just the path)
    closes the R1 bypass: without the label check, the OR-with
    `bot/CLAUDE.md` was satisfied by the unrelated reference-docs
    line that mentions `bot/CLAUDE.md` "once Sprint 2 Bit 2.2
    ships", and the actual breadcrumb could be deleted silently.
    """
    text = CLAUDE_MD.read_text()
    # Bit 9.3-iii.c (2026-05-11): bot/_impl.py was DELETED. The "implementation
    # rules" breadcrumb was renamed to "bot/ implementation rules" to reflect
    # the cross-cutting scope post-deletion (rules apply to bot/scanner,
    # bot/executor, bot/main_loop, bot/state, etc.).
    assert "**`bot/` implementation rules**" in text, (
        "CLAUDE.md is missing the ``**`bot/` implementation rules**`` "
        "Critical-rules bullet label. This is the load-bearing "
        "breadcrumb that points operators at the bot/ implementation rules "
        "(torch threading, cal_mlp four-site lock-step, cell-block "
        "filter_stage values, SQLite WAL pragmas, etc.). Without "
        "this label at root, citations across the codebase that "
        "reference 'CLAUDE.md' for these rules become wrong-by-pointer."
    )
    assert (
        "agent_docs/bot-claude-md-draft.md" in text
        or "bot/CLAUDE.md" in text
    ), (
        "CLAUDE.md is missing the bot/_impl.py-implementation-rules path "
        "pointer. Until Sprint 2 Bit 2.2 ships `bot/CLAUDE.md`, the "
        "breadcrumb must point at `agent_docs/bot-claude-md-draft.md`. "
        "After Bit 2.2 ships, the pointer should reference "
        "`bot/CLAUDE.md` instead. (The label assertion above is the "
        "primary contract; this is the secondary check.)"
    )


def test_two_file_mode_flag_present():
    """The Bit 1.3 closeout-commitment-2 forward-looking flag must remain.

    Bit 1.3 closeout (`kb/decisions/bit-1.3-agents-md-shipped-may06.md`
    commitment 2) committed to a forward-looking flag in CLAUDE.md
    that names the GO/NO-GO trigger for flipping AGENTS.md from
    symlink to a separate portable file. Without this flag, a future
    agent considering option 2 has no on-load reminder of the
    decision criteria and re-relitigates the choice.

    The flag has two checked elements:
      1. Mention of `AGENTS.md` (the surface affected).
      2. Pointer to the Bit 1.3 closeout doc (the criteria source).
    """
    text = CLAUDE_MD.read_text()
    assert "AGENTS.md" in text, (
        "CLAUDE.md is missing the AGENTS.md mention required by the "
        "two-file-mode flag (Bit 1.3 closeout commitment 2)."
    )
    assert "bit-1.3-agents-md-shipped-may06.md" in text, (
        "CLAUDE.md is missing the pointer to "
        "kb/decisions/bit-1.3-agents-md-shipped-may06.md required "
        "by the two-file-mode flag (Bit 1.3 closeout commitment 2). "
        "The pointer is the on-load reminder of the GO/NO-GO trigger."
    )


@pytest.mark.parametrize("rel_path", _CROSS_FILE_DRAFT_REFERENCE_SITES)
def test_cross_file_draft_references_resolve(rel_path: str):
    """Every literal candidate path mention in a cross-file site must
    resolve to a real file on disk.

    Bit 1.4 introduced two cross-file pointers when the draft was
    moved out of `kb/drafts/` (R1 review fix): one in
    `README.md` "Repository conventions" and one in `scripts/CLAUDE.md`
    cell-block one-liner. Sprint 2 Bit 2.2 must update both in the
    same commit when it `git mv`s the draft to `bot/CLAUDE.md`.

    Policy (R10 review hardened): if any literal substring from
    `_BREADCRUMB_CANDIDATES` appears in a cross-file site, its
    target file must exist. The earlier "active-path-only" check
    silently passed both directions of partial migration (today: a
    forward-looking `bot/CLAUDE.md` mention without the file
    existing; post-Bit-2.2: a stale `agent_docs/...` mention after
    the file is `git mv`d). Tightened to require resolution either
    way.

    Implication for cross-file prose: forward-looking text must use
    indirection (e.g., "the `bot/` package's runtime CLAUDE.md"),
    not the literal `bot/CLAUDE.md` substring, until the file
    exists. Sprint 2 Bit 2.2 then updates the prose to use the
    literal in the same commit that creates the file. This is the
    R3 hand-off contract enforced strictly.

    Sprint progression:
      - Today (Bit 1.4 → Bit 2.1): `agent_docs/bot-claude-md-draft.md`
        is the only candidate substring that may appear in
        cross-files; it must resolve.
      - Bit 2.2 commit: in the SAME commit, (a) `git mv`
        `agent_docs/bot-claude-md-draft.md` → `bot/CLAUDE.md`,
        (b) drop the `agent_docs/...` literal from every cross-file,
        (c) reintroduce the `bot/CLAUDE.md` literal in those files.
        After the commit lands, `agent_docs/bot-claude-md-draft.md`
        no longer exists, so any remaining literal mention of it in
        cross-files fails this test — the cleanup must happen in the
        same commit.
      - Post-Bit-2.2: `bot/CLAUDE.md` is the only candidate in
        cross-files; the draft path is gone; resolution still holds.
    """
    site_path = REPO_ROOT / rel_path
    if not site_path.exists():
        pytest.fail(
            f"{rel_path} is missing — required by Bit 1.4 cross-file "
            f"pointer contract. Either restore the file or drop "
            f"{rel_path!r} from _CROSS_FILE_DRAFT_REFERENCE_SITES "
            f"in this test."
        )
    site_text = site_path.read_text()
    for candidate in _BREADCRUMB_CANDIDATES:
        if candidate not in site_text:
            continue
        target = REPO_ROOT / candidate
        assert target.exists(), (
            f"{rel_path} contains the literal substring "
            f"{candidate!r} but the target file does not exist at "
            f"{target}. Two legitimate fixes: (a) if this is "
            f"forward-looking prose about a file that doesn't exist "
            f"yet, rewrite using indirection (e.g., 'the `bot/` "
            f"package's runtime CLAUDE.md') so the literal "
            f"substring doesn't appear; or (b) if this is a stale "
            f"reference left over from a partial migration, update "
            f"or remove it. The strict policy is: any literal "
            f"candidate path in a cross-file must resolve."
        )


def test_test_unit_tier_invokes_claude_md_size_test():
    """`make test-unit` (Pillar 5 rename of test-fast) must run
    `tests/test_claude_md_size.py`.

    Symmetric with
    `tests/test_agents_md_symlink.py::test_test_unit_tier_invokes_agents_md_test`
    (the same pin pattern Bit 1.3 introduced; Pillar 5 evolved both).
    Bit 1.4's invariants (line cap, structural sections, breadcrumb
    pointers) belong in the dev-tooling fast-tier alongside the other
    invariant suites.

    Pillar 5 (86b9ve11y) renamed test-fast → test-unit and moved the
    file list into a `UNIT_FILES` Make variable. The file may appear
    in any of: test-unit recipe, test-fast recipe (legacy alias), or
    UNIT_FILES variable body — accept all three forms.
    """
    text = MAKEFILE.read_text()
    folded = re.sub(r"\\\n", " ", text)
    fragments = []
    for target in ("test-unit", "test-fast"):
        m = re.search(
            rf"^{target}:[^\n]*\n((?:\t.*\n?)+)",
            folded,
            re.MULTILINE,
        )
        if m:
            fragments.append(m.group(1))
    var_match = re.search(
        r"^UNIT_FILES\s*[:?]?=\s*([^\n]+)$",
        folded,
        re.MULTILINE,
    )
    if var_match:
        fragments.append(var_match.group(1))
    assert fragments, (
        "Makefile has neither a `test-unit:` nor a `test-fast:` target "
        "with a recipe body, and no `UNIT_FILES` variable. Likely an "
        "unrelated regression — check tests/test_makefile.py."
    )
    combined = "\n".join(fragments)
    assert "tests/test_claude_md_size.py" in combined, (
        f"Unit tier (test-unit/test-fast/UNIT_FILES) doesn't invoke "
        f"tests/test_claude_md_size.py (Bit 1.4). Combined surface: "
        f"{combined!r}"
    )
