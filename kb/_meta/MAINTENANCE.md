# Knowledge Base Maintenance Guide

This file tells you (Claude Code) how to maintain the `kb/` directory.

## Structure
```
kb/
├── _index.md          # Master index. Read FIRST on any deep query.
├── concepts/          # How things work and why
├── strategies/        # One file per strategy, lifecycle tracked
├── failures/          # Post-mortems, bug catalogs
├── decisions/         # Decision records (when a choice was made and why)
└── _meta/             # This file and other meta-docs
```

## Rules

### After Every Significant Session
1. Identify what was learned, fixed, or decided
2. Update or create the relevant article(s)
3. Update `_index.md` with any new articles and revised summaries
4. Add/update cross-links (`## Related` section at bottom of each article)

### Frontmatter Convention
Every kb/ and kb-research/ article should have YAML frontmatter at the top:
```yaml
---
status: active | resolved | superseded | pending | killed
updated: YYYY-MM-DD
tags: [tag1, tag2]
---
```
- **Failures** also include: `severity: critical | major | minor`
- **Decisions** also include: `date: YYYY-MM-DD` and `status: decided | pending | superseded`
- **Strategies** use status: `active` (live trading), `shadow` (observation only), `killed` (disabled)
- This enables Dataview queries in Obsidian. See `kb/_meta/dashboard.md` for example queries.

### Marp Slides
When asked for slides or presentations, produce Marp-formatted markdown (with `marp: true` in frontmatter and `---` slide separators). Save to kb/ or kb-research/ as appropriate. These render as slide decks in Obsidian with the Marp plugin.

### Writing Articles
- Start with `## Summary` — one paragraph max
- Use `## Related` at the bottom linking to other articles
- Include concrete data: dates, numbers, thresholds, win rates
- Distinguish facts from hypotheses clearly
- For failures: always include Symptom, Root Cause, Fix, Lessons

### Creating Decision Records
When a significant choice is made (e.g., shadow vs kill a strategy, change a parameter):
```markdown
# Decision: [Title]
Date: YYYY-MM-DD
Status: Decided | Pending | Superseded

## Context
Why this decision came up.

## Options Considered
1. Option A — tradeoffs
2. Option B — tradeoffs

## Decision
What was chosen and why.

## Consequences
What changed as a result.
```

### Index Maintenance
- Every article gets exactly one line in `_index.md`
- Format: `- [[path/file.md]] - One-sentence summary.`
- Keep summaries under 20 words
- Group by section (Concepts, Strategies, Failures, Decisions)

### Health Checks (Run Periodically)
When asked to do a KB health check:
1. Check all `[[links]]` resolve to real files
2. Flag articles with no `## Related` section
3. Flag articles not listed in `_index.md`
4. Look for contradictions between articles
5. Identify concepts mentioned but lacking their own article
6. Check for stale data (strategies or parameters that have changed)

---

## Research Knowledge Base (kb-research/)

### What Goes Where
- **kb/** = operational (what the bot does now and why). Updated as the bot changes.
- **kb-research/** = analytical (the research that informed those decisions — model comparisons, data analysis, vendor evaluations, deep dives). Point-in-time snapshots.

### When to Create a Research Article
After any deep research session (ML evaluation, market analysis, vendor comparison, strategy investigation) where the findings are substantial enough to reference later. Not every session — only ones with real analytical output (tables, numbers, model architectures, code examples).

### Research Article Format
Same as kb/ (Summary section, Related section, concrete data). Additionally:
- Include `Source:` with chat links and dates at the top
- Include specific data (tables, numbers, code) — not just summaries
- Add `## Related (KB operational articles)` linking to kb/ articles it informed

### Cross-Linking Rule
- Every kb/ decision or concept informed by research should link to the research article: `See kb-research/path/to/article.md for complete analysis`
- Every research article should link to the kb/ articles it informed

### Staleness
Research articles are point-in-time snapshots. They don't need updating when the bot changes — that's what kb/ is for. If a research article's conclusions have been completely superseded, note at the top: `## Status: Superseded by [decision/event]`
