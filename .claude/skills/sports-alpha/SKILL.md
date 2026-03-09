---
name: sports-alpha
description: "Deep sports comeback alpha research — SPRT sequential test, deficit analysis, per-sport breakdown, CLV, calibration. Use when: \"sports deep dive\", \"sports alpha research\", \"is sports ready?\", \"how are comebacks doing?\", \"sports shadow analysis\""
---

# Sports Comeback Alpha Analyzer

Systematic alpha research on sports comeback prediction market data. Discovers profitable configurations by sport group, league, deficit, timing, and pregame strength. Validates robustness with Wilson CIs, Fisher tests, SPRT, and time stability.

## Usage
```
/sports-alpha
/sports-alpha fresh    # Force fresh DB copy from VPS
```

## Steps

1. **Copy fresh state.db** (unless recently copied):
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Run the existing audit first** (for context on data quality and settlement gaps):
   ```
   python3 scripts/sports_shadow_audit.py --db /tmp/state.db --regime auto 2>&1 | head -100
   ```

3. **Run the alpha research script**:
   ```
   python3 scripts/sports_alpha_research.py --db /tmp/state.db 2>&1
   ```
   Or with regime filter:
   ```
   python3 scripts/sports_alpha_research.py --db /tmp/state.db --regime auto 2>&1
   ```
   Or focused on one sport:
   ```
   python3 scripts/sports_alpha_research.py --db /tmp/state.db --sport-group basketball 2>&1
   ```

4. **Present findings** with focus on:
   - Has the verdict changed since last run? (STRONG ALPHA / ALPHA / MARGINAL / NO ALPHA)
   - Which sport groups are profitable vs losing?
   - Top robust configurations (PnL>0, CI_lo>50%, PF>1.2)
   - SPRT convergence status (per group and overall)
   - Key calibration issues (overconfident / underconfident per group)

5. **Track evolution** -- note whether:
   - Any sport group SPRT has converged to a decision
   - CLV is positive (entry price < closing price = real edge)
   - Time stability holds (H1 vs H2 drift < 10pp)
   - Specific deficit sizes or time windows show concentrated alpha

## Key Metrics to Watch
- Overall game WR and 95% Wilson CI lower bound (need > 50%)
- Per-sport-group SPRT decisions (REJECT_H0 = confirmed edge)
- Profit Factor per group (> 1.3 credible, > 1.5 strong)
- Model Brier vs Market Brier (model should beat market)
- CLV average (positive = genuine information advantage)
- Robust config count from grid search

## Interpretation Guide
- **STRONG ALPHA**: 5+/6 checks pass (WR>55%, PnL>0, CI_lo>50%, SPRT rejects H0)
- **ALPHA DETECTED**: 4/6 checks pass
- **MARGINAL ALPHA**: 3/6 checks pass with positive PnL
- **NO ALPHA**: <3 checks pass or negative PnL
- **Time stability**: H1/H2 WR within 10pp = stable, 10-20pp = moderate, >20pp = regime shift
- **Fisher p < 0.05**: Statistically significant difference between groups

## Script Sections (16 total)
1. Data Overview + verdict
2. Sport Group analysis (WR, PnL, PF, Brier per group)
3. League analysis (per-league breakdown)
4. Deficit analysis (1pt vs 2pt vs 3pt+ comebacks)
5. Time remaining (optimal entry timing)
6. Pregame favorite strength (does it predict comeback?)
7. LR scale analysis (model sensitivity parameter)
8. Price analysis (WR by entry price band with breakeven)
9. Signal quality / counterfactual (live vs hypothetical thresholds)
10. Pregame capture method impact
11. SPRT sequential test (overall + per group convergence)
12. Calibration (predicted vs actual, Brier scores)
13. Game flow (score changes, period patterns)
14. Robustness (Wilson CIs, Fisher tests, time stability)
15. Optimal configuration grid search
16. Closing Line Value analysis
+ Executive summary with checklist and sport group ranking
