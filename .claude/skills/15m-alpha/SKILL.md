---
name: 15m-alpha
description: "Deep 15M alpha research — regime-filtered performance, price tier analysis, STC zones, calibration diagnostics, loss patterns, shadow approaches. Use when: \"investigate 15M performance\", \"diagnose a loss\", \"should we change 15M config?\", \"15M deep dive\", \"what's driving losses?\""
---

# 15M Alpha Research Skill

## Description
Run comprehensive alpha research on the 15-minute crypto prediction market trading system. Analyzes live performance, calibration quality, edge signals, loss patterns, and counterfactual config changes with statistical robustness.

## When to use
- When investigating 15M trading performance or profitability
- When evaluating config changes (edge thresholds, STC limits, asset exclusions)
- When diagnosing losses, calibration drift, or WR decline
- When preparing data for researcher prompts about 15M optimization
- When the user asks about alpha, edge, or profit drivers

## Prerequisites
1. Script location: `scripts/15m_alpha_research.py`

## Usage

### Full analysis (all 13 sections)
```bash
python3 scripts/15m_alpha_research.py --db /tmp/state.db --regime auto 2>&1
```

### Single section
```bash
python3 scripts/15m_alpha_research.py --db /tmp/state.db --regime auto --section calibration 2>&1
```

Available sections: `regime`, `asset`, `price`, `stc`, `execution`, `calibration`, `edge`, `counterfactual`, `loss`, `robustness`, `vol`, `time`, `shadow`

### Filter by asset
```bash
python3 scripts/15m_alpha_research.py --db /tmp/state.db --regime auto --asset XRP 2>&1
```

### Custom date range
```bash
python3 scripts/15m_alpha_research.py --db /tmp/state.db --since 2026-03-03 2>&1
```

## Steps

1. **Sync the database.** Follow `.claude/skills/references/db-sync.md` to sync the database.

2. **Run the script** (full or targeted section).

3. **Present findings** with example summary:

   ```
   ## 15M Alpha Research (regime: Mar 3 – present)

   ### Performance
   - 237 trades: 218W/19L (92.0%, Wilson 95% CI: 87.8-95.0%)
   - Total PnL: +$142.30, fees: $12.40
   - Daily avg: +$14.23/day (10 trading days)

   ### Top 3 Findings
   1. [VERIFIED] STC 500-900s shadow zone: 32 counterfactual trades, 93.8% WR, +$28
      → Promotion candidate if WR holds above breakeven CI
   2. [VERIFIED] XRP contributes 42% of losses but only 15% of trades
      → XRP_15M_SHADOW=True is correctly protecting against this
   3. [VERIFIED] Edge inversion at 97c+: higher edge → lower WR (n=12, p=0.08)
      → Current MIN_EDGE_BY_PRICE of 2.0% at 97c may be insufficient

   ### Recommendations
   - No config changes recommended (all findings are within expected variance)
   - Continue monitoring STC 500-900s shadow (need 50+ settled for promotion)
   ```

4. **For loss investigations**, always include individual loss detail from Section 9.

## Sections

| # | Section | What it answers |
|---|---------|-----------------|
| 1 | Regime Detection & Performance | Overall PnL, WR with Wilson CI, rolling 3-day windows |
| 2 | Per-Asset Alpha | Which assets generate most/least alpha, pairwise Fisher tests |
| 3 | Price Tier Analysis | WR by price band matching MIN_EDGE_BY_PRICE schedule, breakeven margins |
| 4 | STC Analysis | Optimal entry timing, shadow zone (500-900s) counterfactual, STC-WR correlation |
| 5 | Execution Analysis | Maker vs taker WR/PnL, fee classification, escalation types, fill latency impact |
| 6 | Calibration Diagnostics | Predicted vs actual by probability bucket, Brier score, per-asset calibration |
| 7 | Edge Inversion Check | Quintile WR monotonicity, point-biserial correlation (edge vs win) |
| 8 | Counterfactual Simulations | What-if for STC limits, edge thresholds, asset exclusions, MIN_ENTRY changes |
| 9 | Loss Pattern Analysis | Individual loss detail, clustering, streaks, concentration, time-of-day |
| 10 | Robustness & Statistical Tests | Wilson CI, time stability, drawdown, daily Sharpe, Kelly analysis |
| 11 | Volatility Regime | Performance by vol_regime with Wilson CIs |
| 12 | Time-of-Day | Hourly PnL distribution, best/worst trading hours |
| 13 | Shadow Approaches | RecalibratedEGARCH + LightGBM alpha comparison, gate failures, training readiness |

## Error Handling

| Situation | Action |
|-----------|--------|
| Script not found | Check: `ls scripts/15m*`. May have been renamed. |
| `--regime auto` picks wrong date | The regime detector looks for the last commit that changed 15M trading constants. If it picks too recent a date (small n), override with `--since <date>` using the actual config change timestamp from CLAUDE.md or git log. |
| 0 settled trades in regime | Regime may be very new. Widen with `--since` to include more data. Note: mixing regimes is risky (see CLAUDE.md rules), but some analysis (calibration, vol regime) is regime-agnostic. |
| Section output is empty | That section may not have enough data (e.g., vol_regime needs vol_regime column populated). Skip it and note: "Section N: insufficient data." |
| Loss section shows a trade the user didn't know about | This is the point — surface it. Show the trade details and whether it was correctly executed. |
| Script hangs or takes >60s | The DB may be very large. Try running a single section: `--section regime` to verify the script works, then run full. |
| Shadow section shows 0 signals | fifteenm_shadow engine may not be running. Check: `SELECT COUNT(*) FROM fifteenm_shadow_signals`. If 0, the engine needs investigation. |

## Key design principles
- All statistical claims include Wilson CIs and Fisher exact p-values
- Price tier analysis matches the actual MIN_EDGE_BY_PRICE schedule (not flat thresholds)
- Counterfactuals use per-contract maker fee model for fair comparison
- Regime detection uses git diff to find last config-changing commit
- 15M filter: `event_ticker NOT LIKE '%D-%'` for settled_trades, product_type filter for evals
