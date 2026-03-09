# Generate Research Data Package

Compile a self-contained research data package for the external researcher/analyst. The package should contain ALL data and context needed — the researcher should need nothing else.

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

2. **Gather raw data from VPS** — SCP state.db, then query:

   For the target system, extract:
   - **Settlement data**: all settled trades/evaluations with full columns
   - **Pipeline data**: filter_stage distribution, rejection reasons
   - **Calibration data**: predicted vs actual by bucket
   - **Config sensitivity**: performance at different parameter values
   - **Time series**: day-over-day performance trend

   Format as CSV sections with headers.

3. **Extract relevant code architecture from bot.py**:
   - All constants for the target system (with line numbers)
   - The scan loop section that handles this product_type
   - Key functions: probability computation, edge calculation, sizing
   - Filter stages and what triggers each one
   - Show actual code snippets (10-30 lines) for critical decision points

4. **Include current config and regime history**:
   - Current config values (all relevant constants)
   - When each was last changed (from git log)
   - What the previous values were
   - Why they were changed (from commit messages)

5. **Include constraints and context**:
   - Owner risk appetite and constraints (from CLAUDE.md)
   - What NOT to change (e.g., OBSERVATION_MODE, MIN_ENTRY_PRICE)
   - Fee structure (maker/taker formulas)
   - Known issues and active investigations
   - Related research briefs (list files in research/)

6. **State the problem clearly**:
   - What specific question should the researcher answer?
   - What data supports or contradicts current approach?
   - What are the candidate changes being considered?
   - What would success look like? (quantitative criteria)

7. **Compile into file**:
   ```
   researcher_data_package_<topic>.txt
   ```
   Format:
   - `# RESEARCH PACKAGE: <topic>` header
   - `## PROBLEM STATEMENT` — 3-5 sentences
   - `## CURRENT CONFIG` — table of all relevant constants
   - `## RAW DATA` — CSV sections with headers
   - `## CODE ARCHITECTURE` — snippets with line numbers
   - `## REGIME HISTORY` — what changed when
   - `## CONSTRAINTS` — what can/cannot change
   - `## ANALYSIS SO FAR` — what we've already tried
   - `## SPECIFIC QUESTIONS` — numbered list of what to answer

8. **Verify completeness**:
   - Can someone with NO access to the codebase understand the full context?
   - Are all numbers traceable to raw data?
   - Are constraints and risk appetite clearly stated?
   - Is the problem statement specific enough to produce actionable output?
