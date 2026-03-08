# Alpha Audit — Opportunity & Shadow Strategy Analysis

## Description
Full-funnel opportunity audit: traces the decision pipeline, analyzes rejected trades, computes counterfactual PnL, evaluates shadow strategies, and recommends promotions.

## When to use
- "run the alpha audit"
- "find where we're leaving money"
- "check the shadow strategies"
- "are any shadows ready to go live?"
- "what's the opportunity pipeline look like?"
- "how much money are we missing?"
- "what should we promote next?"

## Prerequisites
1. Copy state.db from VPS: checkpoint WAL first, then SCP
2. Scripts: `scripts/alpha_audit.py`, `scripts/shadow_eval.py`

## Usage
```
/alpha-audit              # Full audit (14-day lookback)
/alpha-audit 7            # Custom lookback in days
/alpha-audit shadows      # Shadow evaluation only
```

## Steps

### 1. Get fresh data from VPS
```bash
# Checkpoint WAL to ensure consistent read
ssh botuser@45.55.181.30 "cd kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()\""
# Copy DB
scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
```

Or use MCP tools: `mcp__kalshi-vps__checkpoint_wal` then `mcp__kalshi-vps__query_db`.

### 2. Run full opportunity audit
```bash
python3 scripts/alpha_audit.py --db /tmp/state.db --days 14
```
Outputs: filter funnel, rejection analysis by price band, counterfactual PnL, WR by STC/z-score/asset, capital utilization, shadow status, recommendations.

### 3. Run shadow strategy evaluation
```bash
python3 scripts/shadow_eval.py --db /tmp/state.db --days 14
```
Outputs: per-strategy performance, breakeven analysis, Wilson CI, promotion decision (PROMOTE / KEEP / KILL).

### 4. Interpret results

**Promotion thresholds:**
- WR > breakeven + 2pp
- Settled sample >= 50
- Counterfactual PnL > 0
- Wilson 95% CI lower bound > breakeven WR

**Key metrics to check:**
- Filter funnel: is insufficient_edge still the dominant rejection? (Expected: ~25-30%)
- Calibration gap: model prob vs realized WR by price band — any systematic underestimate?
- Capital utilization: % of bankroll deployed, trades per day, idle hours
- Shadow strategies: any approaching promotion thresholds?
- Regressions: has any live metric degraded since last audit?

### 5. Recommend actions
Based on audit results:
- Identify top 3 opportunities by estimated daily $ impact
- Flag any shadows ready for promotion (all criteria met)
- Flag any regressions (WR or PnL declining)
- Propose new shadow strategies if data reveals new patterns
- Always compute statistical significance before recommending changes

## Key design principles
- NEVER recommend config changes without backing data and p-values
- Always filter to current config regime (check CONFIG_REGIME_SINCE)
- Breakeven WR at price P = (P + fee) / 100 where fee = ceil(0.07 * P/100 * (1-P/100) * 100)
- Use Wilson score CI, not raw WR, for promotion decisions
- Scripts and dashboard must use same data source and definitions
