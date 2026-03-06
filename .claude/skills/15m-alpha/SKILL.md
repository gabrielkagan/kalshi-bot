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
1. Copy state.db from VPS: `scp botuser@45.55.181.30:~/kalshi-bot-repo/state.db /tmp/state.db`
2. Script location: `scripts/15m_alpha_research.py`

## Usage

### Full analysis (all 12 sections)
```bash
python3 scripts/15m_alpha_research.py --db /tmp/state.db --regime auto
```

### Single section
```bash
python3 scripts/15m_alpha_research.py --db /tmp/state.db --regime auto --section calibration
```

Available sections: `regime`, `asset`, `price`, `stc`, `execution`, `calibration`, `edge`, `counterfactual`, `loss`, `robustness`, `vol`, `time`

### Filter by asset
```bash
python3 scripts/15m_alpha_research.py --db /tmp/state.db --regime auto --asset XRP
```

### Custom date range
```bash
python3 scripts/15m_alpha_research.py --db /tmp/state.db --since 2026-03-03
```

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

## Key design principles
- All statistical claims include Wilson CIs and Fisher exact p-values
- Price tier analysis matches the actual MIN_EDGE_BY_PRICE schedule (not flat thresholds)
- Counterfactuals use per-contract maker fee model for fair comparison
- Regime detection uses git diff to find last config-changing commit
- 15M filter: `event_ticker NOT LIKE '%D-%'` for settled_trades, product_type filter for evals
