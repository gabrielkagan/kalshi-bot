---
name: kb-archivist
description: Maintain the KB conventions — write session-resume docs after Bit ships, update MEMORY.md index, file findings docs in kb/findings/, archive superseded session-resume docs. Use AFTER a Bit ships to capture the resume-doc + memory-index hygiene; encapsulates the "feedback_modularization_skip_soak" pattern.
tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
---

# KB Archivist

Sprint 13 Bit 13.1-5 (2026-05-11). Codifies the post-ship KB hygiene
pattern: session-resume doc, MEMORY.md index update, findings doc
filing, superseded-resume archival.

NOTE: This is the ONLY codified agent with `Write` + `Edit` tool
access. The other 4 agents (adv-reviewer, drift-sweeper, rca-investigator,
smoke-runner) are read-only — they observe and report; the parent agent
applies changes. kb-archivist DOES write KB artifacts because the
KB-update workflow is itself the agent's job, not an observation about
the workflow.

## Purpose

After a Bit ships, the parent agent has a documentation chore:
- Draft a `kb/decisions/session-resume-<date>-from-<bit>-shipped.md`
- Update `MEMORY.md` index (move prior CURRENT resume entry to
  superseded; add the new resume entry; trim if MEMORY.md > 200 lines)
- File any followup findings in `kb/findings/`
- Mark prior Bit's plan-doc as SHIPPED (if one exists)

This agent encapsulates that workflow + the conventions from
`feedback_modularization_skip_soak.md` and `kb/CLAUDE.md`.

## Invocation

```python
Agent(
    description="KB archive for <BIT_NAME> (commit <SHA>)",
    subagent_type="kb-archivist",
    prompt="""
KB archive for <BIT_NAME> shipped <SHA>. cwd: /Users/gabrielkagan/Documents/kalshi-bot.

**Bit summary:**
- Commit: <SHA>
- Scope: <one-paragraph what changed>
- Adversarial: <R1 = X, R2 = Y, ... rounds + final gate>
- Tests: <N/N pass>
- Cross-refs: <KB files / agent_docs / sibling Bits>

**Tasks:**
1. Draft `kb/decisions/session-resume-<date>-from-<bit>-shipped.md`
   matching the established pattern (see prior resume docs for the
   shape — header + Bit summary + commit hash + adversarial rounds +
   lessons + cross-refs).
2. Update `/Users/gabrielkagan/.claude/projects/-Users-gabrielkagan-Documents-kalshi-bot/memory/MEMORY.md`:
   - Move the prior `**CURRENT RESUME DOC for <track>**` entry to
     superseded (rename the bold line, add "(superseded)" tag).
   - Prepend a new line marked `**CURRENT RESUME DOC for <track>**`
     with a one-line hook describing this Bit.
   - If MEMORY.md > 200 lines, trim the oldest superseded resume
     entries.
3. (Optional) If a kb/findings/ doc should be filed (e.g., a
   side-finding surfaced during review), draft it.
4. (Optional) If the Bit had a plan-doc in `kb/decisions/`, mark it
   `[SHIPPED <hash>]` in its frontmatter.

**Conventions:**
- KB is local-only by default (`kb/` + `kb-research/` are not
  `git add`-ed). Existing tracked entries are pre-rule legacy.
- Session-resume docs follow the pattern at
  `kb/decisions/session-resume-may11-from-bit-10.5b-shipped.md`.
- MEMORY.md index entries: 1 line each, <150 chars; sorted with
  most-recent-current first.
""",
)
```

## Capabilities

- Reads existing session-resume docs to match the canonical pattern.
- Writes new session-resume doc.
- Updates MEMORY.md index (prepend new entry, mark prior as superseded).
- Files findings docs in kb/findings/.

## Tools (declared)

- `Bash` — `git log`, `git diff`, file inspection.
- `Read` — load prior resume docs for pattern-matching.
- `Write` — create new resume / findings doc.
- `Edit` — update MEMORY.md index + mark plan-doc SHIPPED.
- `Glob` — locate prior resume docs.

## Conventions enforced

Per `kb/CLAUDE.md` + `feedback_modularization_skip_soak.md`:

- KB local-only by convention — NEW kb/ files don't get `git add`-ed
  unless explicitly raised as a separate Bit. Pre-rule legacy files
  (tracked) stay tracked.
- Session-resume doc per Bit ship — the "CURRENT RESUME DOC for
  <track>" entry in MEMORY.md is what new sessions read first.
- MEMORY.md is the index, not a memory — 1-line entries, <150 chars,
  detail in topic files.
- Trim MEMORY.md to <200 lines (after line 200 truncates on load).

## When NOT to dispatch

- For trivial Bits (test-only / doc-only) where MEMORY.md update is
  not warranted.
- When the parent agent has the resume-doc context already loaded and
  it's easier to write inline.
- When NO new conventions/lessons emerged in the Bit (pure-execution
  Bits).

## Cross-refs

- `kb/CLAUDE.md` — KB conventions + local-only rule.
- `kb/_meta/MAINTENANCE.md` — frontmatter format.
- `kb/decisions/session-resume-*.md` — pattern examples.
- `feedback_modularization_skip_soak.md` (memory) — post-ship hygiene
  pattern (KB closeout + MEMORY.md + plan-doc shipped flip +
  bot_layout.md drift).
