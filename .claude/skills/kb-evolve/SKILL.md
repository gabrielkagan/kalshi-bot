---
name: kb-evolve
description: "KB structural maintenance — clear conflict-copy rot, repair links, reconcile the index, backfill frontmatter, retire superseded docs. Mutates kb/; confirm before bulk moves. Use when: \"kb evolve\", \"clean up the KB\", \"fix the kb lint findings\", \"KB maintenance\", after /kb-lint reports ERRORs."
---

# KB Evolve

The fix pass for what `/kb-lint` finds. This one **mutates** `kb/`, so it runs
in tiers: reversible things first, judgment calls last, bulk moves only on
explicit confirmation.

## Always start from a lint run

```bash
python3 .claude/skills/kb-lint/kb_lint.py --json > /tmp/kb.json
```

Exit 2 means the lint could not run — **stop**, do not proceed to mutate on an
empty finding list. Check `$?`: on exit 2 with `--json`, stdout is empty and
`/tmp/kb.json` is a 0-byte file.

**If `MEM-STORE-MISSING` appears, do not run Tier 2.** kb docs link memory-store
slugs, and without that store every one of those links reports as `LINK-BROKEN`.
Re-run with `--repo` pointing at the primary checkout (not a worktree, not `.`)
before believing any link finding.

## Before any bulk move — four preconditions

`kb/` is local-only by convention and conflict copies are untracked, so **a
bad delete here is unrecoverable**. There is no git safety net. Establish all
four, in this order:

1. **Back up.** `tar czf <scratch>/kb_backup_$(date -u +%Y%m%dT%H%M%SZ).tgz kb kb-research .claude/skills`
2. **Check each DESTINATION, not the tree.** Part of the KB *is* tracked
   (check with `git ls-files -- kb kb-research | wc -l`; it is not zero), so "the KB is untracked" is false and a whole-tree test can
   never pass. The blast radius is one rename landing on a tracked-but-deleted
   path, so the guard is per-destination, inside the move loop:
   ```bash
   # run from the repo root; $dest repo-relative
   git ls-files --error-unmatch -- "$dest" >/dev/null 2>&1 && echo "TRACKED: $dest"
   ```
   Tracked destinations are recoverable via git; untracked ones are not. That
   distinction — not a blanket "no safety net" — is what decides how careful
   to be with a given file.
3. **Never clobber.** Every destination must be absent and unique across the
   batch. Use `mv -n`, and assert uniqueness before the first move — two
   sources mapping to one destination silently destroys one.
4. **Print the full plan and have a human read it.** Dry-run first. The
   classifier below is a heuristic, and 200+ unrecoverable renames deserve one
   pair of eyes.

## Tier 1 — conflict-copy rot

macOS/iCloud sync leaves `<name> 2.md`, `<name> 3.md`, `<name> copy.md`,
`<name> (2).md` — always
with a **space** before the discriminator, never an underscore (an underscore
rule would eat ordinary snake_case and date ranges).
**Root cause is the repo living in iCloud** — this tier treats the symptom and
the rot will regenerate until the repo is moved out.

The **`[SYNC EVENT detected]`** banner means the evidence bar was met —
≥10 conflict-shaped files (`shaped_all`, including inside conflict directories)
across ≥3 directories — so `DUPE-ORPHAN` is reliable and bulk work is
reasonable *after* a human reads the plan. The printed `N classified / M shaped`
splits that: **N is the rename-candidate set**, M is the evidence count.
Shadow-dir copies sit in M and raise `DUPE-DIR`; they are **not** in N and
must not be bulk-renamed (the directory merge subsumes them). (With
`--no-sync-event` the banner still shows but every `DUPE-ORPHAN` *the batch*
would have produced is suppressed; orphans backed by link evidence still
fire.) Without that banner, treat every no-canonical copy as hand-confirm; a
lone `x 2.md` is far more likely a real title.

**Fix the whole batch in one pass, or pass `--sync-event` on the re-runs.** The
threshold counts *surviving* rot, so clearing part of a batch drops it below the
bar and the tool will then tell you the remainder — the same files you just
confirmed — must never be bulk-renamed. `DUPE-NAMED` is never promoted by batch
evidence on either fork, so files with real inbound links stay protected.

`--sync-event` bypasses the only quantitative guard on an unrecoverable rename,
so it is for continuing a batch you have already eyeballed — never for reaching a
verdict on a fresh corpus. A forced run says so in the finding and sets `batch_event_forced` in the JSON,
and the finding states whether the bar was MET or NOT met on `shaped_all` —
`batch_event_detected` carries the same fact. Forcing while the bar is NOT met
means you are asserting the event from outside the data; the plan deserves the
same scrutiny as an un-bannered run.

Act by finding code, not by filename pattern:

- **`DUPE-IDENTICAL`** — byte-identical to the canonical. Delete the copy,
  re-verifying identity at fix time (`cmp -s`); never trust a stale lint JSON.
- **`DUPE-ORPHAN`** — no canonical, and either other docs link the canonical name
  (positive evidence the original was lost) **or** the file is part of a detected
  sync batch. The batch path is the common one: replaying the
  2026-09-06 event, it produced **219 of 221** (the other 2 had link evidence). Likely `mv` — still confirm by hand.
- **`DUPE-AMBIGUOUS`** — no canonical and no link evidence either way. **Filename
  shape alone cannot prove a sync artifact**: `roadmap-phase 2.md` is a
  legitimate title, and the same rule would strip the range off
  `config_changes_mar21_27`. Never bulk-rename this class.
- **`DUPE-EMPTY`** — 0-byte copy. Delete it; renaming would shadow real content
  with an empty file.
- **`DUPE-NAMED`** — link evidence says the title is real. **Do not rename or
  merge**, even when it sits next to a same-prefix neighbour.
- **`DUPE-CROSS-STORE`** — the canonical exists in another directory or store.
  Compare first; do not rename into place.
- **`DUPE-STORE`** — a whole store (`kb 2/`) is a conflict copy. Nothing in it is
  linted; merge it into the canonical store and re-run before trusting any result.
- **`DUPE-CHAIN`** / **`DUPE-DIR`** — resolve by hand. A bulk rule destroys data
  here. Everything inside a `DUPE-DIR` is shadow: it is doomed with the merge,
  it never counts as link evidence for anything else, **and it does count
  toward batch evidence** (`shaped_all`) so a directory-duplicating sync event
  still trips the banner. Do not treat those shadow files as the rename set.
- **`DUPE-DIVERGENT`** — both exist and differ. **Never auto-resolve.** `diff`,
  merge into the canonical, then delete the copy.

### Re-point inbound links in the same pass

**`DUPE-INBOUND-LINK` at ERROR severity must be fixed as part of the rename, not
after it.** A link pointing at `[[x 2.md]]` breaks the instant `x 2.md` becomes
`x.md` — this happened during the first run of this tier and had to be repaired.
Tier 1 is rename-and-rewrite-referrers, never a bare `mv`. (The same code at WARN
severity means the target is not a rename candidate; leave those alone.)

## Tier 2 — links and index

- `LINK-BROKEN`: the linter prints "did you mean X?" when the only problem is
  separator drift. The common case is hyphenated links to memory slugs, which
  are underscored (`[[feedback-foo-bar]]` → `feedback_foo_bar.md`). Prefer
  re-pointing over deleting.
- `LINK-MISPATHED`: the file exists but the path is wrong. Fix the path.
- `INDEX-DANGLING`: create the file or drop the entry. Keep
  `tests/integration/test_kb_index_links_resolve.py` green.
- `INDEX-MISPATHED`: the file exists at a different path. **Correct the entry's
  path — do not create a second copy of a doc that already exists.**
- `IO-ERROR` on an `_index.md`: fix permissions and re-run before trusting any
  index or curated-tier finding; both are empty while it is unreadable.
- Adding to `_index.md` is a **curation decision**, not a completeness chore.
  One line, `- [[path/file.md]] - summary under 20 words`, under the right
  section, and bump that section's `(N)`.

## Tier 2b — the tool's own health

These aren't KB content problems; they're reasons to distrust the rest of the run.

- `SKILL-ROT` — a `/skill` directory with no loadable `SKILL.md`: either shadowed
  by a conflict copy (`mv '<skill>/SKILL 2.md' '<skill>/SKILL.md'`) or missing
  entirely. The skill is silently dead until fixed. `/ticket` `/pickup`
  `/test-writer` are checked even though they are not in the routing table
  (they live in Critical / Interaction rules; this is how `/ticket` and
  `/pickup` were lost).
- `SKILL-MISSING` — routed in CLAUDE.md with no directory. Create it, or drop the
  routing row so the table stops lying.
- `SKILL-FRONTMATTER` — `SKILL.md` present but `name:` unparseable, so it won't load.
- `SKILL-ROUTING-MISSING` / `IO-ERROR` on CLAUDE.md — CLAUDE.md is missing,
  unreadable, or has lost its `## Skill routing` heading, so no skill was checked
  at all this run. Fix before trusting a clean skills line.
- `LINK-MALFORMED` — an empty `[[ ]]`. Remove it or fill in the target.

## Tier 3 — article hygiene (curated tier only)

`NO-FRONTMATTER` / `FM-FIELDS` — backfill `status` / `updated` / `tags`, plus
`severity` on failures and `date` on decisions. Set `updated` from the file's
real mtime or its dated content — never today's date by reflex, never a
guessed wall-clock stamp.

`FM-UNPARSEABLE-DATE` — `status: active` with an `updated:` that isn't a date.
Fix the value; an unparseable date reads as "fresh" to any age check.

`NO-RELATED` — add genuine cross-links. Don't manufacture links to clear a check.

## Tier 4 — judgment (never automate)

Surface these as proposals with evidence; let the user decide.

- **Contradictions.** Live one: MAINTENANCE.md says every article gets an
  `_index.md` line; `_index.md`'s own curation policy says session notes are
  intentionally unindexed. Pick one and make both docs agree.
- **`FM-UPDATED-OLD`** — re-verify against live config, then keep, update, or
  mark `superseded`. For actual content drift run **`make doc-drift`**.
- **Superseded docs** — mark `status: superseded` with a pointer to the
  successor. Prefer retiring in place over deleting; history is the point.
- **Missing concepts** — ideas referenced repeatedly with no article.

## Guardrails

- Bulk moves and deletes need explicit user confirmation with counts shown first.
- Divergent content is merged by a human. Losing the only copy of a finding is
  the one unrecoverable outcome here.
- Prefer quarantine (move to a scratch dir) over `rm`. It costs nothing.
- Don't `git add` new `kb/` files — local-only by convention (`kb/CLAUDE.md`).
- Other sessions write `kb/` concurrently. Re-read before editing, and expect
  the corpus to change under a long run.
- Finish with a lint re-run; file anything out of scope via `/ticket`.
