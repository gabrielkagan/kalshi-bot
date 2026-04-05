# Integrate Research KB into Knowledge Base

## What This Is
10 research articles compiled from ~25 past Claude chat sessions, containing complete findings with specific data, model architectures, code examples, vendor comparisons, and implementation details. These are the analytical foundations behind many decisions already recorded in kb/.

The kb-research/ directory sits alongside kb/ in the repo.

## What You Need To Do

### Phase 1: Read both KBs completely
1. Read `kb/_index.md` and `kb/_meta/MAINTENANCE.md`
2. Read `kb-research/_index.md`
3. Read every article in both directories

### Phase 2: Integrate

For each article in `kb-research/`, decide:

**A) MERGE into existing kb/ article** — if the research is the backstory for an existing concept, decision, or failure. Add the relevant findings as a subsection (e.g., `## Research Background`) rather than keeping a duplicate.

**B) KEEP as separate reference** — if the research is standalone external knowledge that doesn't map to a single existing article. The fraud vendor analysis, the Bayesian LR model derivation, the market expansion ranking — these are reference documents.

**C) CREATE new kb/ article** — if the research reveals a concept, strategy, or decision that should be in kb/ but isn't. Example: if there's no kb/strategies/ article for the sports comeback model, create one that summarizes the current state and links to the full research.

### Phase 3: Cross-link
- Every research article should link to related kb/ articles in its Related section
- Every kb/ article that was informed by research should link to the research source
- Use format: `See [[kb-research/bot/sports-comeback-model.md]] for complete analysis`
- Update both indexes

### Phase 4: Add to CLAUDE.md

```markdown
### Research Knowledge Base
The `kb-research/` directory contains compiled findings from past research sessions. Check `kb-research/_index.md` when investigating topics that may have been researched previously.
```

### Phase 5: Contradiction check
Research articles contain findings from different time periods. Some may contradict current kb/ content because things changed. Flag any contradictions — the kb/ article (operational truth) wins, but note the historical context from the research.

### Phase 6: Report
- How many research articles merged vs kept vs spawned new kb/ articles
- Total article count across both KBs  
- Any contradictions found between research findings and current kb/ state
- Any gaps: topics referenced in research that have no kb/ coverage
