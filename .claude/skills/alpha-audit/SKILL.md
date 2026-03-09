---
name: alpha-audit
description: "Full-funnel opportunity audit — traces the decision pipeline, analyzes rejections, computes counterfactual PnL, evaluates shadow promotion readiness. Use when: \"run the alpha audit\", \"find where we're leaving money\", \"check the shadow strategies\", \"are any shadows ready to go live?\", \"what are we filtering out?\""
---

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
Scripts: `scripts/alpha_audit.py`, `scripts/shadow_eval.py`

## Usage
```
/alpha-audit              # Full audit (14-day lookback)
/alpha-audit 7            # Custom lookback in days
/alpha-audit shadows      # Shadow evaluation only
```

## Steps

### 1. Sync the database
Follow `.claude/skills/references/db-sync.md` to sync the database.

### 2. Run full opportunity audit
```bash
python3 scripts/alpha_audit.py --db /tmp/state.db --days 14 2>&1
```
Outputs: filter funnel, rejection analysis by price band, counterfactual PnL, WR by STC/z-score/asset, capital utilization, shadow status, recommendations.

### 3. Run shadow strategy evaluation
```bash
python3 scripts/shadow_eval.py --db /tmp/state.db --days 14 2>&1
```
Outputs: per-strategy performance, breakeven analysis, Wilson CI, promotion decision (PROMOTE / KEEP / KILL).

**Includes hourly shadow strategies:**
- Crypto hourly shadows: MM (market-making) and HAR-RV per asset (BTC, ETH, SOL)
- SPX hourly shadow: HAR-RV with OLS fitting status
- MM fill rates (confirmed vs hypothetical fills)
- Per-asset routing recommendation (which approach works best per asset)

### 4. Interpret results

#### Promotion thresholds and WHY each one exists

| Threshold | Value | Why |
|-----------|-------|-----|
| WR > breakeven + 2pp | e.g., 93c breakeven is ~94.0%, need ≥96.0% | The +2pp buffer accounts for estimation error. At n=50, the standard error of a 95% WR is ~3pp. Without a buffer, you'd promote strategies that are right at breakeven 50% of the time. 2pp gives ~75% confidence the true WR exceeds breakeven. |
| Settled sample ≥ 50 | Minimum trades to evaluate | Below 50, Wilson CIs are too wide to distinguish a winning strategy from noise. At n=50 with 90% WR, the 95% CI is (78%-97%) — barely useful. At n=100 it narrows to (82%-95%). 50 is the absolute floor; prefer 100+. |
| Counterfactual PnL > 0 | Net positive after fees | A strategy can have high WR but still lose money if average loss > average win (e.g., 90% WR but losses are at 97c = $3 loss vs $7 win). PnL integrates both WR and payoff asymmetry. Must use Kelly sizing, not flat 1-contract. |
| Wilson 95% CI lower bound > breakeven WR | Statistical significance | This is the real gate. If the CI lower bound is below breakeven, you CANNOT reject the null hypothesis that the strategy is break-even or worse. Promoting based on point estimate WR alone is gambling on noise. |

**Breakeven WR formula:** At price P cents, breakeven = (P + fee) / 100, where fee = ceil(0.07 * P/100 * (1-P/100) * 100). Examples: 86c→87.0%, 90c→90.6%, 93c→93.5%, 95c→95.3%, 97c→97.2%.

#### Key metrics to check
- **Filter funnel:** is `insufficient_edge` still the dominant rejection? (Expected: ~25-30%). If another reason dominates, something may have changed.
- **Calibration gap:** model prob vs realized WR by price band — any systematic underestimate means the model is leaving money on the table.
- **Capital utilization:** % of bankroll deployed, trades per day, idle hours. Low utilization + high WR = opportunity to widen parameters.
- **15M shadow strategies:** any approaching promotion thresholds? Report distance to each threshold.
- **Crypto hourly shadows:** MM fill rates, per-asset WR and PnL, routing recommendations.
- **SPX hourly shadow:** HAR-RV fitting status (prior vs OLS), gate pass rate.
- **Regressions:** has any live metric degraded since last audit? Compare to previous audit if available.

### 5. Recommend actions
Based on audit results:
- Identify top 3 opportunities by estimated daily $ impact
- Flag any shadows ready for promotion (ALL four criteria met — not just one or two)
- Flag any regressions (WR or PnL declining vs prior audit)
- Propose new shadow strategies if data reveals untapped patterns
- Always compute statistical significance before recommending changes

### 6. Present summary

```
## Alpha Audit Summary (last 14 days)

### Pipeline Funnel
| Stage | Count | % of total |
|-------|------:|----------:|
| Evaluated | 4,230 | 100% |
| Edge too low | 2,810 | 66.4% |
| Price out of range | 890 | 21.0% |
| Candidates | 312 | 7.4% |
| Traded (filled) | 48 | 1.1% |

### Shadow Promotion Status
| Strategy | Settled | WR | Wilson 95% CI | vs Breakeven | Verdict |
|----------|--------:|---:|:-------------|:------------|---------|
| A1 RecalEGARCH | 142 | 91.5% | (85.8-95.4) | +4.2pp above | PROMOTE |
| A2 LightGBM | 89 | 84.3% | (75.0-91.1) | -3.0pp below | KEEP (need data) |
| STC 500-900s | 67 | 89.6% | (79.7-95.7) | +2.3pp above | KEEP (n<100) |

### Top 3 Opportunities
1. [description, estimated $/day impact]
2. [description, estimated $/day impact]
3. [description, estimated $/day impact]
```

## Error Handling

| Situation | Action |
|-----------|--------|
| `alpha_audit.py` not found | Check: `ls scripts/alpha*`. The script may have been renamed. |
| Script errors with `no such column` | The DB schema may have changed since the script was written. Show the error, run `PRAGMA table_info(evaluated_opportunities)` to check current schema, and report the mismatch. |
| Script returns 0 rows | Not an error if the lookback period is too short or the system is new. Try widening: `--days 30`. If still 0, the system isn't generating data. |
| Shadow eval shows a strategy with 100% WR at n=5 | Do NOT recommend promotion. Say: "100% WR but n=5 is meaningless — Wilson CI is (56.6%-100%). Need ≥50 settled trades." |
| Counterfactual PnL is positive but WR is below breakeven | This can happen with asymmetric payoffs (big wins, small losses). Flag it: "WR is below breakeven but PnL is positive due to payoff asymmetry. This is fragile — a few extra losses could flip PnL negative. KEEP, don't promote." |

## Key design principles
- NEVER recommend config changes without backing data and p-values
- Always filter to current config regime (scripts handle this via `--regime auto`)
- Use Wilson score CI, not raw WR, for promotion decisions (see WHY section above)
- Scripts and dashboard must use same data source and definitions
- **A strategy must pass ALL FOUR promotion criteria simultaneously.** Meeting 3 of 4 is not enough — each criterion catches a different failure mode.
