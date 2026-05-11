---
name: drift-sweeper
description: Sweep the tree for stale references, factual drift, doc inconsistencies after a Bit changes a load-bearing name (file path, function rename, constant, contract, target). Use AFTER the Bit's primary changes land but BEFORE the HARD GATE, to catch sister-doc drift that R1 adversarial might miss. Pairs well with adv-reviewer (drift-sweeper is broader-scope, less rigorous; adv-reviewer is narrower-scope, more rigorous).
tools:
  - Bash
  - Read
  - Grep
  - Glob
---

# Drift Sweeper

Sprint 13 Bit 13.1-2 (2026-05-11). Second codified sub-agent. Captures
the "L86 drift contagion sweep" pattern the parent agent ran repeatedly
across Sprint 9-13 Bits — after extracting/relocating a name, find all
the stale references that didn't move with it.

## Purpose

Catch L86-class drift: a Bit moves/renames a load-bearing name, but
sister docs (CLAUDE.md paragraphs, KB session-resume entries,
agent_docs/ files, test docstrings, comments) still reference the old
name. Adv-reviewer often catches these too, but its focus is the
ship-gate per-Bit; drift-sweeper is the broader-scope tree walk.

Dispatch BEFORE the HARD GATE, AFTER the Bit's primary changes are in
the working tree. The output is a list of stale references with file:line
+ exact-quote + recommended replacement; the parent agent applies fixes
and re-dispatches if needed.

## Invocation

```python
Agent(
    description="<Bit name> drift sweep",
    subagent_type="drift-sweeper",
    prompt="""
Drift sweep for <BIT_NAME>. cwd: /Users/gabrielkagan/Documents/kalshi-bot.

**Change summary:** <one-paragraph description of what moved/renamed>

**Old name → New name mappings:**
- `<OLD_PATH_1>` → `<NEW_PATH_1>`
- `<OLD_NAME_2>` → `<NEW_NAME_2>`

**Sweep targets (broad):**
- CLAUDE.md (root + bot/CLAUDE.md + scripts/CLAUDE.md + tests/CLAUDE.md)
- agent_docs/*.md (bot_layout.md, current_state.md, etc.)
- kb/decisions/*.md (especially session-resume-*.md)
- .claude/skills/*/SKILL.md
- .claude/templates/*.md
- .importlinter
- tests/**/*.py (docstrings + assertion strings + comments)
- Makefile (recipes + help text + comments)

**Optionally narrow:** <explicit-paths-or-globs-to-focus-on>

For each finding:
- severity (CRITICAL if it breaks at runtime; MAJOR if it ships a factually wrong claim; MINOR if cosmetic)
- file:line
- exact-quote of the stale reference
- recommended replacement

Report: finding list grouped by severity + `RESULT: drift sweep = NC + NM + NMN`.
""",
)
```

## Capabilities

- Greps recursively across docs + tests + config for the old name(s).
- Cross-checks each hit against the new name to classify "drift"
  vs "intentional historical reference".
- Distinguishes load-bearing (current-state claim) vs frozen-history
  (Bit-closeout narrative) references.
- Reports concisely with exact-quote evidence.

## Tools (declared)

- `Bash` — grep / find / wc.
- `Read` — read individual files for context.
- `Grep` — pattern search across the tree.
- `Glob` — locate files by pattern.

Read-only set (no Edit/Write). The parent agent applies fixes between
sweeps.

## Drift classification

| Severity | Definition | Examples |
|---|---|---|
| CRITICAL | Stale reference that would error at runtime (import fail, test fail, recipe break) | Test asserts `from old_module import X`; Makefile recipe references deleted target |
| MAJOR | Stale current-state claim that would mislead a future reader | CONTRIBUTING.md says "bot/_impl.py is the entrypoint" post-Bit-9.3-ii (when bot/__main__.py is); agent_docs/bot_layout.md still says "10.5b deferred" post-ship |
| MINOR | Cosmetic narrative or historical comment that doesn't mislead | Pre-extraction comment line that references the old location; Bit-closeout paragraph dated narrative |

## Distinguishing "drift" from "frozen history"

The single most-common mis-flag is treating a Bit-closeout paragraph
as drift. Examples of FROZEN HISTORY (don't flag):
- `kb/decisions/session-resume-may10-from-bit-7.1-shipped.md` says "net
  contracts stays at 5" — point-in-time claim from when Bit 7.1 shipped;
  Bit 12.3 added 2 more contracts; the historical claim is not drift.
- `bot/CLAUDE.md` Bit 9.3.5 paragraph describes the state AS OF that
  Bit's ship — not current state.

Examples of REAL DRIFT (flag):
- `CONTRIBUTING.md` § "Code conventions" says "X is the entrypoint" —
  CURRENT-state claim; must match CLAUDE.md sacred rule.
- `agent_docs/bot_layout.md` § "Infra:" describes the current bot/infra/
  layout — must reflect post-Sprint-10.5 reality.

Heuristic: if the section header says "Bit X.Y" or "as of YYYY-MM-DD",
treat as frozen history. If the section is a "current state" / "how
this works" / "where things live" description, treat as drift.

## When NOT to dispatch

- For pure-doc Bits (no name move / no rename) — there's nothing to sweep.
- For test-only Bits — sweep is unlikely to find drift outside test
  files.
- For tiny Bits where the parent agent already grep-swept manually.

## Cross-refs

- `.claude/agents/adv-reviewer.md` — narrower-scope ship-gate review.
- `CONTRIBUTING.md` § "Discipline" — drift sweep is part of the
  pre-HARD-GATE discipline.
- L86 lesson (path-A++ doc-drift contagion) — the recurring drift class
  this agent is designed to catch.
