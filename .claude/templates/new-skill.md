<!--
Sprint 13 Bit 13.2 (2026-05-11) — scaffolding template for new SKILL.md.

Usage:
  1. Copy this file to .claude/skills/<skill-name>/SKILL.md
  2. Replace all <PLACEHOLDER> tokens with skill-specific values
  3. Delete the sections that don't apply (e.g. Preflight if the skill
     doesn't invoke any scripts/DB)
  4. Delete this HTML comment block before committing

Why this template exists: every operator skill in .claude/skills/
follows a consistent shape (frontmatter → When to use → Usage →
Preflight → Steps → Error Handling → IMPORTANT). Pre-Bit-13.2,
new skills were copy-pasted from a sibling and risked drift on
section ordering / Preflight pattern / heading levels. The template
ships the canonical shape so a new skill starts with the right
structure.

Cross-refs:
- Preflight pattern: see .claude/skills/references/preflight.md
  (Bit 11.1d shared checklist)
- DB-sync pattern: see .claude/skills/references/db-sync.md
- Bit 11.3 / 11.1a / 11.1c retargeted existing skills to prefer
  `make X` wrappers over direct `python3 scripts/X.py`. Follow that
  convention here.
-->
---
name: <SKILL_NAME>
description: "<ONE_SENTENCE_DESCRIPTION>. Use when: \"<EXAMPLE_USER_REQUEST_1>\", \"<EXAMPLE_USER_REQUEST_2>\", \"<EXAMPLE_USER_REQUEST_3>\""
---

# <SKILL_TITLE>

<ONE_PARAGRAPH_OVERVIEW>

## When to use
- "<EXAMPLE_USER_REQUEST_1>"
- "<EXAMPLE_USER_REQUEST_2>"
- "<EXAMPLE_USER_REQUEST_3>"

## Usage
```
/<SKILL_NAME>                # Default invocation
/<SKILL_NAME> <ARG>          # Custom argument (if applicable)
```

## Preflight

Follow `.claude/skills/references/preflight.md` substituting `<wrapper>` = `<MAKE_TARGET>` and `<X>` = `<SCRIPT_STEM>`. Verify `/tmp/state.db` exists, `make -n <MAKE_TARGET>` parses, and `scripts/<SCRIPT_STEM>.py` exists.

(Delete this section if the skill doesn't invoke any DB-querying script.)

## Steps

1. **Sync the database** (if the skill queries `/tmp/state.db`). Follow `.claude/skills/references/db-sync.md` to sync the database.

2. **Run the primary command**:
   ```bash
   make <MAKE_TARGET> 2>&1
   # wraps `python3 scripts/<SCRIPT_STEM>.py --db /tmp/state.db <DEFAULT_ARGS>` (Bit 11.3)
   ```

   For custom args:
   ```bash
   python3 scripts/<SCRIPT_STEM>.py --db /tmp/state.db <CUSTOM_ARGS> 2>&1
   ```

3. **Present the output** — show the relevant findings + add:
   - **Top N findings**: most actionable insights
   - **Data gaps**: any sections that show insufficient data
   - **Recommendations**: only if supported by statistical significance

## Error Handling

| Situation | Action |
|-----------|--------|
| `/tmp/state.db` missing | Follow `.claude/skills/references/db-sync.md` |
| `scripts/<SCRIPT_STEM>.py` missing | Check if Sprint 11 Bit 11.2 moved it under `scripts/<subdir>/`; check `make help` for the canonical target |
| Script outputs 0 rows / "No data found" | <SKILL_SPECIFIC_REMEDY> |
| Script errors with `no such table` | The DB may be from before that table was created. Tell user the system hasn't generated enough data yet. |

## IMPORTANT
- Always use `/tmp/state.db` — never query VPS state.db directly (avoids busy_timeout contention with live bot)
- Performance analysis must filter to current config regime — don't mix data from old configs with current
- <SKILL_SPECIFIC_RULE_1>
- <SKILL_SPECIFIC_RULE_2>
- If the skill reveals something alarming, proactively offer `/investigate` or the relevant follow-up skill
