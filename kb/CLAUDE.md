# kb/ — knowledge-base conventions

Agent-facing operational rules for working in `kb/`. Layout, frontmatter,
writing format, and health-check protocol live in
`kb/_meta/MAINTENANCE.md` (source of truth). Read `kb/_index.md` first
for navigation.

## Local-only by convention

KB files (`kb/`, `kb-research/`) are local-only — don't `git add` new
entries. Existing tracked entries are pre-rule legacy (e.g., the 71
files committed before the convention took effect) or agent-guide
infrastructure (`kb/_index.md`, `kb/_meta/MAINTENANCE.md`, this file).

If you want to track a new kb file, raise it as a separate Bit instead
of bypassing the rule (see kb-in-git TODO below).

## kb/ vs kb-research/

- `kb/` — operational. What the bot does now and why.
- `kb-research/` — analytical. Research that informed those decisions
  (model comparisons, data analysis, vendor evaluations). Point-in-time
  snapshots.

## TODO (deferred)

- kb-in-git (track `kb/` in git instead of local-only): paused
  2026-05-08.
- `/kb-lint`, `/kb-ingest`, `/kb-evolve` skills: referenced in root
  `CLAUDE.md` skill-routing table; `.claude/skills/kb-lint/`,
  `.claude/skills/kb-ingest/`, `.claude/skills/kb-evolve/` directories
  (and their `SKILL.md` files) not yet created.
- Autoresearch (replay-engine track) future hook-up:
  `kb/{findings,decisions,failures,concepts,strategies}/` are candidate
  inputs.
