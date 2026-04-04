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

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database. If user says "fresh", always re-sync regardless of cache age.

2. **Run the alpha research script**:
   ```bash
   python3 scripts/hourly_alpha_research.py --db /tmp/state.db 2>&1
   ```

3. **Present findings** with example summary:

   ```
   ## Hourly Alpha Research

   ### Verdict: MARGINAL ALPHA
   One robust config found (BTC-only, P>=70c, edge>=0.5%), but fails time stability.

   ### Baseline (all assets, current config)
   - 391 settled: 264W/127L (67.5%, Wilson 95% CI: 62.6-72.1%)
   - Sim PnL: -$1,318 (Kelly-sized)
   - Brier: 0.198 (poor — overconfident by +19.7pp)

   ### Top Config: BTC-only P>=70c
   - 89 settled: 71W/18L (79.8%, CI: 70.0-87.4%)
   - Sim PnL: +$42.30
   - Robustness: B (4/5 — fails TIME_STABLE, H1=84% H2=73%)

   ### Key Issues
   1. Edge inversion persists: 5%+ edge has LOWER WR than 1-2% edge
   2. CalEngine shadow Brier (0.085) dramatically better than live (0.198)
   3. XRP contributes 43% of losses — correct to exclude

   ### Recommendation
   Do NOT promote. BTC-only shows promise but time stability fails (11pp drift).
   Keep collecting data — need another 5-7 days for H2 sample to grow.
   ```

4. **Track evolution** — note whether:
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
- **BTC-only WR vs breakeven** (currently +3.5pp at P>=70c) — BTC is the only asset with consistent alpha
- **Shadow CalEngine Brier** (currently 0.085 vs live 0.333) — if shadow is dramatically better, CalEngine promotion would help
- **XRP loss contribution** (currently 43% of all losses) — validates the asset exclusion decision
- **Edge inversion severity** — higher edge should produce higher WR. If inverted, the edge signal is noise and the model can't rank opportunities
- **Time stability** (H1 vs H2 WR split) — WHY 15pp threshold: at n~100 per half, SE is ~5pp. 15pp = 3σ, clearly significant. Below 15pp is noise.
- **Robustness grades** (A=5/5 checks, B=4/5, etc.)
- **Max drawdown** for top configs — a config with great WR but $200 max drawdown is unusable

## Interpretation Guide
- **ALPHA EXISTS**: At least one config with n>=50, positive PnL, PF>1.3, time-stable, Wilson lower > BE
- **MARGINAL ALPHA**: Positive PnL configs exist but fail one or more robustness checks
- **WEAK ALPHA**: Only n>=20 configs show positive PnL
- **NO ALPHA**: No profitable config at any setting
- **Profit Factor**: >1.5 strong, >1.3 credible, <1.2 noise
- **Time stability**: H1/H2 WR within 15pp = stable (WHY 15pp? See above)
- **Robustness grade**: A (all 5 checks), B (4/5), C (3/5), D (2/5), F (0-1/5)
- **Robustness checks**: SAMPLE_OK (n>=50), TIME_STABLE, PF_STRONG (>1.3), WILSON_CLEAR (lower > BE), BOTH_HALVES_PROFITABLE

## Edge Schedule Reference (bot's MIN_EDGE_BY_PRICE)
| Price | Min Edge |
|-------|----------|
| 86c   | 0.25%    |
| 89c   | 0.25%    |
| 91c   | 0.20%    |
| 93c   | 0.50%    |
| 95c   | 0.75%    |
| 97c   | 1.00%    |

## Error Handling

| Situation | Action |
|-----------|--------|
| Script not found | Check: `ls scripts/hourly*`. May have been renamed. |
| 0 settled in current regime | Hourly is observation-only with limited volume. Try `--since 2026-02-28` to include more data from the three-layer optimization regime. |
| Grid search takes >2 min | Normal — 600+ configs is CPU-intensive. Let it run. If it hangs >5 min, there may be a DB issue. |
| Edge inversion section shows perfect monotonicity | Unusual — double-check that the edge column is populated. Perfect monotonicity at low n is likely noise. |
| Alt shadow section shows 0 entries | The hourly alt shadow strategies (MM, HAR-RV) may not have enough data. Check `hourly_alt_shadow_signals` table. |
| Script shows hourly data mixed with 15M | The `product_type` filter may be wrong. Verify: `SELECT DISTINCT product_type FROM evaluated_opportunities WHERE product_type LIKE '%hourly%'`. |
| CalEngine shadow Brier dramatically better than live | This is expected — CalEngine was disabled for hourly (`HOURLY_CALIBRATION_ENABLED=False`). The shadow Brier shows what CalEngine WOULD achieve. This is a key data point for promotion decisions. |
