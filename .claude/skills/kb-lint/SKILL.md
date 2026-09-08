---
name: kb-lint
description: "KB health check — conflict-copy rot, broken wiki-links, index integrity, frontmatter, and dead skills across kb/ + kb-research/. Read-only, sub-second, fails closed. Use when: \"kb lint\", \"KB health check\", \"is the KB healthy?\", \"check kb links\", before a /kb-evolve cleanup."
---

# KB Lint

Read-only structural check over `kb/` + `kb-research/`. **Never mutates** —
fixing is `/kb-evolve`'s job.

## Run it

```bash
python3 .claude/skills/kb-lint/kb_lint.py                  # ranked findings
python3 .claude/skills/kb-lint/kb_lint.py --quiet          # summary only
python3 .claude/skills/kb-lint/kb_lint.py --json           # for /kb-evolve
python3 .claude/skills/kb-lint/kb_lint.py --stale-days 90  # tighten the age gate
python3 .claude/skills/kb-lint/kb_lint.py --repo PATH      # default: git toplevel
python3 .claude/skills/kb-lint/kb_lint.py --sync-event     # force batch mode (see step 5)
python3 .claude/skills/kb-lint/kb_lint.py --no-sync-event  # force it off
```

**Exit contract: 0 clean · 1 ERROR findings · 2 the lint could not run.**
Exit 2 fires when `kb/` is missing or the corpus is implausibly small (<50
files) — a wrong `--repo` must never report perfect health, because
"nothing found" and "nothing there" are different statements.

## What it checks against what

`kb/_meta/MAINTENANCE.md` lists six health checks. This tool implements 1, 2
and 6, inverts 3, and leaves 4 and 5 to `/kb-evolve` Tier 4 as judgment calls:

| # | MAINTENANCE.md check | here |
|---|---|---|
| 1 | `[[links]]` resolve | `LINK-BROKEN`, `LINK-MISPATHED` |
| 2 | articles lack `## Related` | `NO-RELATED` (curated tier only) |
| 3 | articles missing from `_index.md` | **not implemented** — inverted to `INDEX-DANGLING`; see below |
| 4 | contradictions between articles | not automatable → `/kb-evolve` Tier 4 |
| 5 | concepts lacking an article | not automatable → `/kb-evolve` Tier 4 |
| 6 | stale data | `FM-UPDATED-OLD` — *metadata age only* |

Check 6 caveat: `FM-UPDATED-OLD` measures how old the `updated:` field is. A
doc edited yesterday that quotes a wrong `MARKET_BLEND_W` passes clean. The
content check already exists — **`make doc-drift`** — and is the real
implementation of MAINTENANCE.md check 6.

## Findings

**ERROR** — navigation is actually broken.

| code | meaning |
|---|---|
| `DUPE-DIVERGENT` | conflict copy whose content differs from the original. Merge by hand. |
| `DUPE-INBOUND-LINK` | a link points *at* a copy scheduled for rename or deletion — it dies with the target. (Also emitted at WARN when the target is *not* scheduled for any change.) |
| `DUPE-CHAIN` | 2+ copies competing for one canonical name — a bulk rename would destroy all but one. |
| `DUPE-DIR` | a whole directory is a conflict copy; its files shadow the real tree and can mask broken links. |
| `LINK-BROKEN` | `[[link]]` resolves to nothing. Prints "did you mean X?" on separator drift. |
| `INDEX-DANGLING` | an `_index.md` cites a file that doesn't exist. Prints "did you mean X?" on separator drift. |
| `INDEX-MISPATHED` | an `_index.md` entry names a real file at the wrong path — fix the path, don't create a duplicate. |
| `IO-ERROR` (index only) | an `_index.md` is unreadable, so index integrity and the curated tier can't be evaluated. |
| `DUPE-STORE` | an entire `kb`/`kb-research` store is a sync conflict copy — nothing inside it is linted at all. |
| `SKILL-ROT` / `SKILL-MISSING` / `SKILL-FRONTMATTER` | a `/skill` routed in CLAUDE.md that cannot load. |


`SKILL-ROUTING-MISSING` (WARN) means CLAUDE.md is absent, or has no
`## Skill routing` section — either way **no** skill was checked — don't read a clean skills line as health.

**WARN** — drift, safe to batch. `DUPE-ORPHAN` · `DUPE-IDENTICAL` ·
`DUPE-EMPTY` · `DUPE-AMBIGUOUS` · `DUPE-NAMED` · `DUPE-CROSS-STORE` ·
`DUPE-INBOUND-LINK` (target not scheduled to change) · `LINK-MISPATHED` ·
`NO-RELATED` · `NO-FRONTMATTER` · `FM-FIELDS` · `FM-UNPARSEABLE-DATE` ·
`FM-UPDATED-OLD` · `IO-ERROR` (non-index) · `LINK-MALFORMED` ·
`MEM-STORE-MISSING`.

**A conflict copy is never classified on filename shape alone.** Shape only
proposes a canonical name; the class comes from evidence, in this order:

1. **byte-identity** with a surviving original → `DUPE-IDENTICAL`
2. **zero length** → `DUPE-EMPTY` (never rename — it would shadow real content)
3. **canonical in another directory or store** → `DUPE-CROSS-STORE` (a file
   inside a conflict *directory* never qualifies — it is itself shadow)
4. **link evidence**, counted in *distinct linking documents* — one doc citing a
   copy twice is one witness, not two. Neither conflict-shaped files **nor any
   file inside a conflict directory** counts as a linker — a sync client
   duplicates whole trees, and that is how it manufactures a second
   independent-looking witness. Rot must not vouch for rot. Within this step, canonical-name
   evidence is tested first: docs linking the *canonical* name mean the original
   was lost (`DUPE-ORPHAN`); docs linking the *full* name mean the title is real
   (`DUPE-NAMED`, never rename). Beside a surviving canonical the bar is **two**
   distinct linkers, because there the competing reading is a divergence that may
   hold the only copy of something — one stray link must not silence that.
5. **batch evidence** — ≥10 conflict-shaped files across ≥3 directories is a
   sync event, not a naming choice, and members inherit that (`DUPE-ORPHAN`).
   Two counts, two jobs: `shaped_all` (every conflict-shaped stem, including
   inside a conflict directory) is the evidence; `shaped` (those same files
   minus shadow-dir copies) is the classified/rename set. The summary prints
   both (`N classified / M shaped`); JSON has `total` (= classified) plus
   `shaped_all` and `dirs_hit`. Shadow-dir files raise `DUPE-DIR` and are
   **not** in the rename set — do not bulk-`mv` them because the banner's M
   is larger than N. Batch evidence only promotes step 6 → `DUPE-ORPHAN`; it
   never overrides step 4, on either fork. Forced via `--sync-event`, the
   finding says so explicitly and `stats.conflict_copies.batch_event_forced`
   is true — a forced verdict must never print the evidence sentence of a
   detected one.
6. otherwise → `DUPE-AMBIGUOUS`, which must never be bulk-renamed. *Beside a
   surviving canonical the fallthrough is `DUPE-DIVERGENT` instead* — there the
   competing reading is content risk, not a naming question.

Steps are evaluated in this order and the code matches it. Note the threshold is
measured on the **surviving** rot, so a partial cleanup drops the count and
re-classifies the remainder as `DUPE-AMBIGUOUS` — pass `--sync-event` to carry
the verdict across an incremental fix loop.

Step 5 exists because link evidence alone is nearly useless on a real event:
only ~19% of corpus files have any inbound link, and under `decisions/` — where
rot actually accumulates — it is ~5%. Measured by replaying this repo's own 2026-09-06 rot event (221 orphans) through
the current classifier: link evidence alone recovers **2**; adding batch evidence
recovers all **221** (219 via the batch branch, 2 via link). Without step 5 the tool cannot see the rot it exists
to find. Step 5 is deliberately inert on a healthy corpus, so `/kb-evolve`
still requires a printed plan and a human read before any rename.

## Four suppressions that are load-bearing

Applied naively these checks fire on most of the corpus and get muted. Don't
"simplify" them away:

1. **Links resolve against the memory store**, not just kb/. kb docs link to
   `[[feedback_*]]` / `[[project_*]]` slugs under
   `~/.claude/projects/<slug>/memory/`; dozens of live links resolve only this
   way. If that directory is absent — a worktree, or a non-canonical `--repo` —
   the linter says so via `MEM-STORE-MISSING` rather than silently reporting
   every one of those links as broken.
2. **Code fences and inline backticks are stripped first** — `` `[[path/file.md]]` ``
   in MAINTENANCE.md is documentation, not a reference.
3. **Explicit paths are honored.** `[[concepts/foo.md]]` does *not* resolve to
   `kb/failures/foo.md`; Obsidian wouldn't either. A bare-stem fallback here
   would be a false negative inside a correctness gate — it reports
   `LINK-MISPATHED` instead.
4. **Doomed conflict copies are exempt from article rules.** Don't ask anyone to
   backfill frontmatter into a file Tier 1 is about to delete. Copies classified
   `DUPE-NAMED` / `DUPE-AMBIGUOUS` / `DUPE-CROSS-STORE` are *not* scheduled for
   deletion — they are still linted, because they may be real documents.

## Tiering follows the curation policy, not MAINTENANCE.md

`kb/_index.md` states it is a **curated** subset and that session notes under
`decisions/` `findings/` `failures/` are *intentionally* unindexed. So
index-membership and `## Related` apply only to the **curated tier**
(`concepts/`, `strategies/`, plus anything either `_index.md` cites) — roughly
90 files out of ~530, not all of them.

This contradicts MAINTENANCE.md's older "every article gets exactly one line in
`_index.md`". The curation policy is the live rule and the linter follows it.
Reconciling the two docs is a `/kb-evolve` Tier 4 judgment call.

## After a run

Route findings to `/kb-evolve`. Per CLAUDE.md, anything outside the current
task's scope gets a `/ticket`, not a KB bullet.
