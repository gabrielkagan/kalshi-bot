---
name: kb-ingest
description: "KB capture session findings — file a session's findings, decisions, and failures into kb/ following MAINTENANCE.md. Use when: \"kb ingest\", \"file this finding\", \"capture session notes\", \"write this up in the KB\", \"KB capture session findings\"."
---

# KB Ingest

Turn a session's findings into real `kb/` articles. This is the capture path;
`/kb-lint` checks the result, `/kb-evolve` repairs rot. Do not invent a fourth.

## When to use

- End of a session that produced a finding, decision, or failure
- User says "file this", "write this up", "capture the session"
- A followup is already ticketed and still needs a KB home

Skip if the artifact already exists and only needs a link — edit in place.

## Destination

| Kind | Directory | Status values |
|---|---|---|
| What we learned | `kb/findings/` | `active` |
| A choice that was made | `kb/decisions/` | `decided` / `pending` / `superseded` |
| Something that broke | `kb/failures/` | `resolved` / `active`, plus `severity:` |
| How it works now | `kb/concepts/` or `kb/strategies/` | see MAINTENANCE.md |

Prefer one article. Split only when two artifacts have different lifetimes
(a failure vs the decision that followed). Session-resume docs go in
`kb/decisions/session-resume-<date>-<slug>.md` and are **not** indexed.

Frontmatter, `## Summary`, `## Related`, and the failure sections (Symptom /
Root Cause / Fix / Lessons) are specified in `kb/_meta/MAINTENANCE.md`. Do not
restate them here. Failures need `severity:`; decisions need `date:`.

## Hard rules

1. **Do not add `_index.md` entries.** Session notes under `decisions/`
   `findings/` `failures/` are intentionally unindexed (`kb/_index.md`
   curation policy). Indexing is a curation decision, not a completeness chore.
2. **Do not `git add` new `kb/` files.** Local-only by convention (`kb/CLAUDE.md`).
3. **Memory slugs are underscored.** Memory files are `feedback_foo_bar.md`.
   `[[feedback-foo-bar]]` is a dead Obsidian link. Use `[[feedback_foo_bar]]`.
4. **`## Related` must be real.** Link the articles this one depends on or
   supersedes. Empty or manufactured related-blocks fail `/kb-lint` (`NO-RELATED`
   on the curated tier) and fail the human later.
5. **Do not auto-create ClickUp tickets from ingest.** Followups still go
   through `/ticket`. This skill writes the KB article; it does not substitute
   for a ticket ID.

## Steps

1. Name the file. Dated slug, no spaces: `kb/<dir>/<slug>-<monDD>.md`.
   If a close-enough article exists, update it instead of minting a twin.
2. Write it. YAML frontmatter first, then `## Summary` (one paragraph), then
   the body, then `## Related`. Concrete data: dates, ticket IDs, numbers.
   Distinguish facts from hypotheses.
3. Re-read the file you just wrote. Concurrent sessions write `kb/` too.
4. Lint:
   ```bash
   python3 .claude/skills/kb-lint/kb_lint.py --quiet
   ```
   The new article must not introduce `ERROR` findings (`LINK-BROKEN`,
   `SKILL-MISSING`, `DUPE-*`, `INDEX-DANGLING`, `FM-FIELDS` on curated docs).
   If it does, fix the article, do not suppress the lint.
5. Tell the user the path. If a followup is still unticketed, stop and run
   `/ticket` — a KB bullet is not a tracker.

## Anti-patterns

- Auto-adding the article to `_index.md` "so it is findable"
- Hyphenating memory-store wiki-links
- Copying a previous session-resume and leaving its `updated:` / ticket IDs
- Filing a finding that is actually a failure (no Symptom / Root Cause / Fix)
- `git add kb/` to "keep it safe" — that bypasses the local-only convention
