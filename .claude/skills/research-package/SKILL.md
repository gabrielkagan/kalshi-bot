---
name: research-package
description: "Compile self-contained data package for external researcher — raw data, code architecture, config history, constraints, specific questions. Use when: \"build a research package\", \"prepare data for the researcher\", \"data dump for analysis\", \"package up data for external review\""
---

# Generate Research Data Package

Compile a self-contained research data package for the external researcher/analyst. The package should contain ALL data and context needed — the researcher should need nothing else.

## When to use
- "Build a research package for hourly"
- "Prepare data for the researcher"
- "I need a data dump for analysis"
- "Package up the weather data for external review"
- Any request to compile data + context for someone who doesn't have codebase access

## Usage
```
/research-package hourly
/research-package weather
/research-package <topic>
```

## Steps

1. **Determine the topic** from the argument. Common topics:
   - `hourly` — hourly crypto shadow mode optimization
   - `weather` — weather ensemble model tuning
   - `spx` — SPX hourly shadow analysis
   - `sports` — sports comeback shadow analysis
   - `calibration` — calibration pipeline tuning
   - `15m` — 15M live trading optimization
   - Custom topic: ask user what specific question they want answered

2. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database. Always sync fresh for research packages — stale data produces a stale package.

3. **Gather raw data** — query `/tmp/state.db` for the target system:
   - **Settlement data**: all settled trades/evaluations with full columns
   - **Pipeline data**: filter_stage distribution, rejection reasons
   - **Calibration data**: predicted vs actual by bucket
   - **Config sensitivity**: performance at different parameter values
   - **Time series**: day-over-day performance trend

   Format as CSV sections with headers.

4. **Extract relevant code architecture from bot.py**:
   - All constants for the target system (with line numbers)
   - The scan loop section that handles this product_type
   - Key functions: probability computation, edge calculation, sizing
   - Filter stages and what triggers each one
   - Show actual code snippets (10-30 lines) for critical decision points

5. **Include current config and regime history**:
   - Current config values (all relevant constants)
   - When each was last changed (from git log)
   - What the previous values were
   - Why they were changed (from commit messages)

6. **Include constraints and context**:
   - Owner risk appetite and constraints (from CLAUDE.md)
   - What NOT to change (e.g., OBSERVATION_MODE, MIN_ENTRY_PRICE)
   - Fee structure (maker/taker formulas)
   - Known issues and active investigations
   - Related research briefs (list files in research/)

7. **State the problem clearly**:
   - What specific question should the researcher answer?
   - What data supports or contradicts current approach?
   - What are the candidate changes being considered?
   - What would success look like? (quantitative criteria)

8. **Compile into file**:
   ```
   researcher_data_package_<topic>.txt
   ```

9. **Verify completeness** (checklist):
   - Can someone with NO access to the codebase understand the full context?
   - Are all numbers traceable to raw data?
   - Are constraints and risk appetite clearly stated?
   - Is the problem statement specific enough to produce actionable output?
   - Does the raw data include enough columns for the researcher to do their own analysis?

## Package Format

```
# RESEARCH PACKAGE: <topic>
# Generated: YYYY-MM-DD HH:MM UTC
# DB snapshot: YYYY-MM-DD HH:MM UTC (N rows in scope)

## PROBLEM STATEMENT
[3-5 sentences: what question needs answering, why it matters, what we've
tried so far, what kind of answer we want (config change, new strategy,
kill/keep decision)]

## CURRENT CONFIG
| Constant | Value | Last Changed | Previous | Why Changed |
|----------|-------|-------------|----------|-------------|
| HOURLY_OBSERVATION_ONLY | True | Feb 28 | False | -$97.47 overnight disaster |
| HOURLY_TEMPERATURE_T | 1.45 | Mar 1 | 1.0 | Overconfidence correction |
| ... | ... | ... | ... | ... |

## RAW DATA

### Settled Evaluations (N rows)
ticker,asset,filter_stage,evaluation_time,market_price,calibrated_prob,edge,...
KXBTCD-...,BTC,observation_trade,2026-03-05T14:23:00,82,0.891,0.032,...
[all rows, no truncation]

### Rejection Breakdown
filter_stage,count,pct
insufficient_edge,2810,66.4
price_out_of_range,890,21.0
...

### Calibration Buckets
prob_bucket,count,predicted_avg,actual_wr,gap_pp
0.50-0.60,45,0.553,0.511,-4.2
0.60-0.70,78,0.648,0.628,-2.0
...

## CODE ARCHITECTURE
[Key code snippets with line numbers, showing exactly how probabilities
are computed, how edge is calculated, how filters work]

### Probability computation (bot.py:5300-5340)
```python
[actual code]
```

### Edge filter (bot.py:7200-7240)
```python
[actual code]
```

## REGIME HISTORY
| Date | Change | Commit | Impact |
|------|--------|--------|--------|
| Mar 3 | MIN_EDGE_BY_PRICE halved | abc123 | More trades at lower edge |
| Feb 28 | Hourly reverted to obs | def456 | -$97.47 overnight |
| ... | ... | ... | ... |

## CONSTRAINTS
- OBSERVATION_MODE must remain True until all promotion criteria are met
- Owner risk appetite: max 15% bankroll per hourly trade (vs 25% for 15M)
- Fee structure: taker = ceil(0.07 × C × P × (1-P)), maker = $0
- Must NOT change: [list specific untouchable params]
- MUST use: Kelly sizing, Wilson CIs, regime filtering

## ANALYSIS SO FAR
[What analysis has been done, what it showed, what questions remain]
- Ran hourly_alpha_research.py: found BTC-only P>=70c marginal alpha
- Grid search: 600+ configs, best is B-grade (fails time stability)
- CalEngine shadow Brier: 0.085 (dramatically better than live 0.198)

## SPECIFIC QUESTIONS
1. Should we enable hourly CalEngine? Shadow Brier is 0.085 vs live 0.198.
   What are the risks of overfit given only 500 observations?
2. Is BTC-only the right approach, or should we try per-asset temperature?
3. What edge threshold schedule should hourly use? (Currently flat 0.25%)
```

## Error Handling

| Situation | Action |
|-----------|--------|
| Topic is ambiguous or custom | Ask the user: "What specific question should the researcher answer?" Don't compile a vague package. |
| Raw data query returns >10,000 rows | Include all rows — the researcher needs complete data. But note the row count in the header so they know the scale. |
| A column the researcher needs is mostly NULL | Include it anyway with a note: "Column X is 45% NULL — see Data Health section for context." The researcher needs to know about data quality issues. |
| Config history is unclear from git log | Ask the user to confirm key regime change dates. Don't guess — wrong regime boundaries corrupt the analysis. |
| Researcher will need code that's in an engine file (not bot.py) | Include snippets from the relevant engine file (spx_engine.py, weather_engine.py, etc.) with file paths and line numbers. |
| Package file exceeds 50KB | This is fine — research packages are meant to be comprehensive. Only trim if >200KB, in which case truncate the raw data to the most recent 2000 rows and note the truncation. |
| User asks to package a system with very little data (< 20 settled) | Compile it anyway but add a prominent warning: "WARNING: Only N settled observations. Any analysis will have wide confidence intervals. Consider waiting for more data before commissioning research." |
