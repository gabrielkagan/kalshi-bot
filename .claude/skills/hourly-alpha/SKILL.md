---
name: hourly-alpha
description: "Deep hourly alpha research — 600+ config grid search, robustness validation, BTC-only analysis, CalEngine Brier, edge inversion checks. Use when: \"hourly deep dive\", \"should we promote hourly?\", \"optimize hourly config\", \"hourly alpha research\", \"is hourly ready?\""
---

# Hourly Strategy Alpha Analyzer (Enhanced)

Systematic alpha research on hourly trading data. Discovers profitable configurations through exhaustive multi-dimensional grid search, validates robustness with statistical tests, tracks regime changes, and provides nuanced per-price-tier recommendations.

## Usage
```
/hourly-alpha
/hourly-alpha fresh    # Force fresh DB copy from VPS
```

## Steps

1. **Copy fresh state.db** (unless recently copied):
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Run the alpha research script**:
   ```
   python3 scripts/hourly_alpha_research.py --db /tmp/state.db 2>&1
   ```

3. **Present findings** with focus on:
   - Has the verdict changed since last run? (alpha/no-alpha/marginal)
   - Top 3 profitable configurations with robustness grades (A-F)
   - Per-price-tier sizing recommendations
   - New structural issues or regime shifts
   - Shadow CalEngine Brier comparison
   - Comparison to previous run if available

4. **Track evolution** -- note whether:
   - BTC-only alpha is strengthening or degrading
   - New assets are becoming viable
   - Edge inversion is worsening or improving
   - Shadow CalEngine calibration is converging
   - Regime shifts have occurred (data-driven detection)

## Report Sections

| Section | Purpose |
|---------|---------|
| 1. Baseline & Asset Contribution | Overall performance, per-asset WR/PnL, exclusion analysis |
| 2. Per-Price-Tier Breakeven | Matches bot's MIN_EDGE_BY_PRICE schedule, sizing recs |
| 3. Loss Concentration | By asset, price, hour, correlated multi-loss windows, ENB |
| 4. Edge & Calibration Diagnostics | Edge monotonicity, prob bucket calibration, shadow cal, T sweep |
| 5. Exhaustive Config Search | 600+ configs: asset x price x edge x STC x hour x window limit |
| 6. Alpha Discovery | Ranked by $/day with max drawdown |
| 7. Robustness Validation | Wilson CI, Fisher exact, time stability, concentration, grade A-F |
| 8. Regime Map | Data-driven regime detection (WR shifts, vol shifts) |
| 9. Position Sizing | Per-tier Kelly fraction, recommended fraction, max risk |
| 10. Recommended Config | Best config with bot parameter translation |
| 11. Final Verdict | Alpha/marginal/none + actionable recommendations |
| 12. Alt Shadow | MM + HAR-RV shadow strategy performance vs EGARCH baseline |

## Key Metrics to Watch
- BTC-only WR vs breakeven (currently +3.5pp at P>=70c)
- Shadow CalEngine Brier (currently 0.085 vs live 0.333)
- XRP loss contribution (currently 43% of all losses)
- Edge inversion severity (higher edge = worse WR)
- Time stability (H1 vs H2 WR split)
- Robustness grades (A=5/5 checks, B=4/5, etc.)
- Max drawdown for top configs

## Interpretation Guide
- **ALPHA EXISTS**: At least one config with n>=50, positive PnL, PF>1.3, time-stable, Wilson lower > BE
- **MARGINAL ALPHA**: Positive PnL configs exist but fail one or more robustness checks
- **WEAK ALPHA**: Only n>=20 configs show positive PnL
- **NO ALPHA**: No profitable config at any setting
- **Profit Factor**: >1.5 strong, >1.3 credible, <1.2 noise
- **Time stability**: H1/H2 WR within 15pp = stable
- **Robustness grade**: A (all 5 checks), B (4/5), C (3/5), D (2/5), F (0-1/5)
- **Robustness checks**: SAMPLE_OK (n>=50), TIME_STABLE, PF_STRONG (>1.3), WILSON_CLEAR (lower > BE), BOTH_HALVES_PROFITABLE

## Edge Schedule Reference (bot's MIN_EDGE_BY_PRICE)
| Price | Min Edge |
|-------|----------|
| 86c   | 0.25%    |
| 89c   | 0.25%    |
| 91c   | 0.35%    |
| 93c   | 0.90%    |
| 95c   | 1.25%    |
| 97c   | 2.00%    |
