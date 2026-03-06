# SPX Hourly Alpha Research

Systematic alpha research on SPX hourly observation data. Analyzes EGARCH blend performance, VIX regime effects, intraday patterns, calibration quality, edge integrity, multi-position correlation, and counterfactual config optimization.

## Usage
```
/spx-alpha
/spx-alpha fresh    # Force fresh DB copy from VPS
```

## Steps

1. **Copy fresh state.db** (unless recently copied):
   ```
   ssh botuser@45.55.181.30 "cd ~/kalshi-bot-repo && python3 -c \"import sqlite3; c=sqlite3.connect('state.db'); c.execute('PRAGMA wal_checkpoint(PASSIVE)'); c.close()\""
   scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db
   ```

2. **Run the alpha research script**:
   ```
   python3 scripts/spx_alpha_research.py --db /tmp/state.db 2>&1
   ```

3. **Present findings** with focus on:
   - Is SPX hourly profitable under any configuration?
   - EGARCH blend weight: adapting or stuck?
   - VIX regime impact on accuracy
   - Intraday patterns (open, lunch, close)
   - Optimal temperature, min price, STC range, blend weight
   - Multi-position window correlation risk
   - Readiness assessment for live trading promotion

4. **Track evolution** -- note whether:
   - Observation count is growing toward readiness thresholds
   - Calibration is improving or degrading
   - EGARCH blend is adapting to SPX-specific dynamics
   - Edge signal is monotonic and informative
   - Any intraday period shows consistent alpha

## Report Sections

| Section | Purpose |
|---------|---------|
| 1. Performance Overview | WR, PnL, profit factor, Brier, Wilson CI, filter funnel |
| 2. EGARCH Blend Analysis | Blend weight distribution, WR by weight, sigma distribution |
| 3. VIX Regime Analysis | VIX implied RV terciles, seasonal factor, vol_regime impact |
| 4. Intraday Pattern | Market period WR (open/lunch/close), hour-by-hour, day-of-week |
| 5. Price Tier Analysis | WR by price band vs breakeven, profit factor per tier |
| 6. Multi-Position Window | Correlation, all-win/all-loss clustering, ENB, position limit sim |
| 7. Edge Integrity | Quintile monotonicity, edge threshold sweep, $/day |
| 8. Calibration Diagnostics | Probability bucket gaps, Brier, raw vs calibrated, overconfidence |
| 9. Trading Hours | Regular vs extended hours WR, volatility by session |
| 10. Counterfactual Configs | Temperature sweep, min price sweep, STC range sweep, blend sweep |
| 11. Robustness & Stability | Half-split stability, daily PnL, drawdown, streaks, concentration |
| 12. Price x STC Cross-Tab | Best/worst zones for targeting |
| 13. Temperature Tournament | Shadow column Brier comparison (if populated) |
| 14. Data Quality Audit | Column fill rates, CalEngine pipeline status |
| 15. Readiness Assessment | 7-point checklist for live trading promotion |

## Key Differences from Crypto Hourly

- **Finance fee category**: taker 0.035 (half of crypto's 0.07), maker 0.0175
- **Single asset**: SPX only (no multi-asset exclusion needed)
- **Market hours**: 9:30 AM - 4:00 PM ET cash session (strong intraday patterns)
- **VIX integration**: Uses implied volatility from VIX for vol estimation
- **EGARCH+RK blend**: Same as crypto but SPX-specific calibration needed
- **T=1.0**: No temperature correction yet (needs data to determine optimal)

## Readiness Checklist

| Check | Threshold | Rationale |
|-------|-----------|-----------|
| Trading days | >= 10 | Minimum for regime coverage |
| Observations | >= 100 | Minimum for statistical significance |
| Min calibration bucket | >= 20 | Need data across full probability range |
| Blend weight adapting | Not stuck | EGARCH model must be responsive |
| 80c+ WR > 85% | Breakeven+margin | Must beat breakeven at tradeable prices |
| Overall WR > breakeven | Positive edge | Net profitable after fees |
| Positive flat PnL | > $0 | 1-contract profitability |

## Interpretation Guide

- **Brier Score**: < 0.10 excellent, 0.10-0.15 good, > 0.15 poor
- **Profit Factor**: > 1.5 strong, > 1.3 credible, < 1.2 noise
- **Edge monotonicity**: Higher edge must produce higher WR or signal is noise
- **Wilson CI lower bound > BE**: Required for statistical confidence in profitability
- **ENB**: < 1.5 means positions are highly correlated within windows
- **Temperature**: > 1.2 suggests overconfidence, < 0.9 suggests underconfidence
