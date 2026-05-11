# Skill Preflight Checklist

Shared preflight reference for skills that invoke scripts against
`/tmp/state.db`. Per Sprint 11 Bit 11.1c/11.1d (master plan §Bit 11.1:
"Add Preflight section that exits with error if path missing").

## Generic checklist

Before running ANY audit/alpha skill, verify all three:

1. **DB exists + fresh enough.**
   ```bash
   [ -f /tmp/state.db ] || { echo "PREFLIGHT FAIL: /tmp/state.db missing. Run .claude/skills/references/db-sync.md first."; exit 1; }
   ```
   Optionally check freshness — if `/tmp/state.db` mtime is >6h old,
   warn the operator that audit results may be stale.

2. **Makefile target parses** (when invoking via `make <wrapper>`):
   ```bash
   make -n <wrapper> >/dev/null 2>&1 || { echo "PREFLIGHT FAIL: 'make <wrapper>' not in Makefile. Did Bit 11.3 rename the target?"; exit 1; }
   ```

3. **Fallback script exists** (when direct invocation is needed, e.g.,
   custom args):
   ```bash
   [ -f scripts/<X>.py ] || { echo "PREFLIGHT FAIL: scripts/<X>.py missing. Did Sprint 11 Bit 11.2 reorg move it?"; exit 1; }
   ```

## On failure

Don't silently continue. Surface the missing path to the operator with
the remedy from the error message. Operator workflow:

- DB missing → follow `.claude/skills/references/db-sync.md` to
  re-sync from VPS.
- Makefile target missing → run `make help` to see the current target
  list; the wrapper may have been renamed since this SKILL.md was
  written.
- Script missing → check `scripts/` and its subdirs (Sprint 11 Bit
  11.2 reorg may have moved the file).

## Skill-specific substitutions

Each consuming SKILL.md substitutes `<wrapper>` and `<X>` with its own
values. Example (data-health):

| Placeholder | data-health value |
|---|---|
| `<wrapper>` | `data-health` |
| `<X>` | `data_health_monitor` |

The dispatch tables in each skill's `## Steps` section enumerate the
substitutions. The /audit skill has the canonical multi-row example.

## Why a shared reference

Sprint 11 Bit 11.1d (2026-05-11) factored this checklist out so 7+
SKILL.md files (audit + data-health + alpha-audit + 15m-alpha +
no-side + shadow + status) don't duplicate the preflight prose.
Mirrors the existing `.claude/skills/references/db-sync.md` pattern.

A future SKILL.md adding a new audit skill should add its `##
Preflight` section by reference: "Follow
`.claude/skills/references/preflight.md` substituting `<wrapper>` =
`<target>` and `<X>` = `<script-stem>`."
