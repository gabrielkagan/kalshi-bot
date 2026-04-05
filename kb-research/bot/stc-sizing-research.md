---
status: active
updated: 2026-04-05
tags: [research, stc, sizing, sol, kelly, vulnerability]
---
# Research: Time-to-Close Sizing Vulnerability & SOL 85c Analysis

**Date:** April 5, 2026
**Scope:** Main 15M pipeline, all assets (SOL, ETH, BTC, XRP)
**Data Source:** Supabase `trades` + `evaluations` tables (project: `srbdajecmkjxinmcozxl`)
**Status:** Research complete. Ready for implementation investigation.

---

## Executive Summary

Two related vulnerabilities are systematically destroying PnL in the main 15M pipeline:

1. **SOL ≤85c far-from-expiry entries** — a specific, surgical problem costing -$289 net
2. **Universal time-to-close overexposure** — the EGARCH model is systematically overconfident on trades with high seconds_to_close, costing -$254 across all assets at 7m+ STC

Combined, these represent a potential **+$353 (+73%) PnL improvement** via two principled fixes.

---

## Finding 1: SOL ≤85c Entry Price — The Specific Problem

### The Data

SOL 85c is the #1 PnL-destroying price tier across the entire pipeline:

| Entry Price | n | Win Rate | Net PnL | Avg Loss Size | Avg Win Size |
|-------------|---|----------|---------|---------------|--------------|
| 84c | 4 | 100% | +$73.34 | — | 114.0 |
| **85c** | **17** | **76.5%** | **-$273.65** | **132.3** | **90.2** |
| 86c | 29 | 93.1% | +$244.67 | 31.0 | 78.8 |
| 87c | 31 | 87.1% | +$19.04 | 45.3 | 60.0 |

- 85c accounts for **35.6% of ALL SOL losses** from just 4 losing trades
- Model miscalibration at 85c: model says 91.2% prob → actual WR is 76.5% → **14.7pp gap** (worst of any tier)
- Loss positions are 47% larger than win positions (132 vs 90 contracts)

### Root Cause: Time-to-Close Interaction

The price level alone is NOT the problem. The real discriminator is **seconds_to_close**:

| Sub-86c SOL + STC | n | Win Rate | PnL |
|-------------------|---|----------|-----|
| < 300s (5 min) | 17 | **100%** | **+$228.00** |
| ≥ 300s (5 min) | 23 | 78.3% | **-$288.99** |

**Every single near-expiry sub-86c trade is a winner. All the damage comes from far-from-expiry entries.**

Mechanical explanation: At ≤85c, the market is pricing in downside risk. Near expiry, there isn't enough time for that move to materialize → free money. With 5+ minutes left, SOL has enough runway to breach the threshold, and the market's skepticism is justified.

### All 4 Losses in Detail (for reference)

```
KXSOL15M-26APR051245-45: MAKER_PATIENT, 196ct, spot=$79.69, threshold=$79.60 (9 cent buffer), sigma=0.000163, -$166.60
KXSOL15M-26APR050915-15: TAKER_NOW, 198ct, spot=$79.14, threshold=$78.99 (15 cent buffer), sigma=0.0000933, -$168.30
KXSOL15M-26APR020115-15: MAKER_PATIENT, 45ct, -$38.25
KXSOL15M-26MAR311330-30: TAKER_NOW, 90ct, -$76.50
```

All 4 losses had seconds_to_close > 350s. The two catastrophic losses on April 5 had extremely low EGARCH sigma estimates (model thought vol was near-zero) → oversized positions → razor-thin spot-to-threshold buffers breached.

### Recommended Fix: SOL Sub-86c Time Gate

```python
# In entry filter logic:
if asset == 'SOL' and entry_price_cents <= 85 and seconds_to_close >= 300:
    # SKIP — do not enter this trade
    rejection_reason = 'sol_low_entry_high_stc'
```

**Impact:** +$289 PnL improvement. Cuts 23 losing-aggregate trades, preserves 17 trades at 100% WR.

---

## Finding 2: Universal Time-to-Close Overexposure

### The Data

Across ALL assets in the main pipeline, PnL degrades monotonically with seconds_to_close:

| STC Bucket | n | Win Rate | PnL |
|-----------|---|----------|------|
| 0-3 min | 121 | 90.1% | +$33.83 |
| 3-5 min | 354 | 94.6% | +$667.23 |
| 5-7 min | 131 | 90.8% | +$35.40 |
| 7-9 min | 184 | 87.5% | **-$118.70** |
| 9+ min | 135 | 82.2% | **-$132.98** |

**The sweet spot is 3-5 minutes (94.6% WR, $667 PnL). Everything over 7 minutes is net negative.**

Per-asset breakdown confirms universality:

| Asset | Near <7m WR | Near PnL | Far 7m+ WR | Far PnL |
|-------|-----------|----------|-----------|---------|
| SOL | 92.6% | +$488.00 | 87.7% | -$10.13 |
| ETH | 92.9% | +$224.30 | 87.0% | -$147.08 |
| BTC | 92.2% | +$8.80 | 82.4% | -$79.76 |
| XRP | 94.3% | +$15.36 | 69.2% | -$14.71 |

### Era Robustness Check

Pattern holds in both calibration eras:

| Era | Near <7m WR | Near PnL | Far 7m+ WR | Far PnL |
|-----|-----------|----------|-----------|---------|
| Pre-passthrough | 92.2% | +$569.62 | 84.1% | -$121.45 |
| Passthrough (Mar 30+) | 95.5% | +$166.84 | 88.2% | -$130.23 |

Note: Passthrough era far-7m+ trades have avg_size 55.8 (vs 22.8 in old era) due to balance growth → losses are proportionally worse.

### Why Not a Hard STC Cutoff?

A hard 7m cutoff would improve PnL by +$254 but kills ALL far-from-expiry trades, including some that are legitimately profitable. Tested finer-grained approaches:

**Edge-based threshold (4% min for 7m+ trades):** Backtested +$614 improvement, BUT:
- Edge has essentially **zero correlation with winning** (r = -0.04 across all trades)
- Within the 7m+ bucket, the mid-edge (3.5-4%) trades were paradoxically worse than low-edge (2-3%) trades
- This is likely **overfitting** — the improvement is suspiciously large and the threshold is not mechanically justified
- **NOT RECOMMENDED** for deployment without further validation

### Recommended Fix: Universal STC Size Scaler

```python
# In Kelly sizing logic, after computing kelly_f:
if seconds_to_close > 300:
    stc_scaler = 300.0 / seconds_to_close
    kelly_f *= stc_scaler
```

Behavior:
- At 300s (5m): 1.0x (no change)
- At 420s (7m): 0.71x
- At 600s (10m): 0.50x
- At 900s (15m): 0.33x

**Why this approach:**
- **No trades cut** — all trades still taken, just sized proportionally to time exposure
- **One parameter** (the 300s knee) — nearly impossible to overfit
- **Monotonic, mechanically grounded** — more time = more uncertainty = reduce position
- **Universal** — applies to all assets without asset-specific tuning
- **Modest but robust impact:** +$64 additional PnL (on top of SOL gate), with near-zero overfitting risk

### Scaler Impact Per Asset (after SOL gate applied)

| Asset | Actual PnL | With Scaler | Change |
|-------|-----------|-------------|--------|
| BTC | -$70.96 | -$24.86 | +$46.10 |
| ETH | +$77.22 | +$139.09 | +$61.87 |
| SOL | +$766.86 | +$709.79 | -$57.07 |
| XRP | +$0.65 | +$14.16 | +$13.51 |
| **Total** | **$773.77** | **$838.18** | **+$64.41** |

Note: SOL slightly negative from scaler because some profitable SOL trades at STC 300-420 get sized down. Net is still positive across all assets combined.

---

## Combined Recommendation

### Layer 1: SOL Sub-86c Time Gate (High Confidence)
```python
if asset == 'SOL' and entry_price_cents <= 85 and seconds_to_close >= 300:
    SKIP  # rejection_reason = 'sol_low_entry_high_stc'
```

### Layer 2: Universal STC Size Scaler (High Confidence)
```python
if seconds_to_close > 300:
    kelly_f *= (300.0 / seconds_to_close)
```

### Combined Impact

| Metric | Current | After Both Fixes |
|--------|---------|-----------------|
| Trades | 925 | 902 (23 cut) |
| Win Rate | 90.3% | 90.6% |
| Total PnL | $484.78 | $838.18 |
| **Improvement** | | **+$353.40 (+73%)** |

---

## Rejected / Deferred Approaches

### Option A: Hard Floor at 86c for SOL Entries
- Impact: +$61 — too blunt, kills $228 of perfect near-expiry wins
- **Rejected:** SOL gate is strictly better

### Option B: 50-Contract Cap on ≤85c SOL
- Impact: +$119 — better than A but still a blunt instrument
- **Rejected:** SOL gate captures more PnL with more precision

### Option C: Recalculate Kelly at Fill Price (Not Eval Price)
- Aggregate improvement but **WORSE tail risk** — worst single loss goes from -$168 → -$243
- On MAKER_PATIENT fills where entry < eval_mkt, the "real edge" is HIGHER → sizes UP → bigger catastrophic losses
- **Deferred:** Dangerous without a companion size cap. Revisit after Layer 1+2 are deployed.

### Option E: Hard STC+Edge Filter (STC ≥ 420 + edge < 4% → SKIP)
- Backtested +$614 — suspicious
- Edge doesn't predict winning (r = -0.04)
- Cuts 271 trades (29% of volume) — too aggressive
- **Deferred:** Likely overfitting. Shadow-tag and evaluate after 3+ weeks with Layer 1+2 live.

---

## Key Correlations (Reference)

From correlation analysis across all 925 main pipeline trades:

| Variable Pair | Correlation |
|---------------|------------|
| STC ↔ winning | **-0.207** |
| Entry price ↔ winning | **+0.236** |
| Edge ↔ winning | -0.037 (negligible) |
| Size ↔ winning | +0.013 (negligible) |
| STC ↔ entry price | -0.245 |

**Edge does not predict winning.** STC and entry price are the real predictors.

---

## Schema Reference (for implementation)

### Trades Table Key Columns
- `ticker` (text) — e.g., `KXSOL15M-26APR051245-45`
- `asset` (text) — SOL, ETH, BTC, XRP
- `entry_price_cents` (integer) — fill price in cents
- `count` (integer) — number of contracts
- `seconds_to_close` (real) — time remaining at fill
- `edge` (real) — model edge at eval time
- `calibrated_prob` (real) — model probability
- `kelly_f` (real) — Kelly fraction used for sizing
- `strategy` (text) — MAKER_PATIENT, TAKER_NOW, MAKER_AGGRESSIVE
- `strategy_group` (text) — 'main', etc.
- `is_win` (boolean, generated column — do NOT INSERT)
- `pnl_cents` (integer)
- `shadow_tags` (array) — includes `sol_low_entry_sub86` on affected trades

### Evaluations Table Key Columns
- `market_price` (integer) — best ask at evaluation time
- `calibrated_prob` (real)
- `edge` (real)
- `egarch_sigma` (real)
- `seconds_to_close` (real)
- `spot_price` (real) — underlying asset price
- `threshold` (real) — contract boundary price
- `z_score` (real)
- `calibration_method` (text) — 'passthrough' for current era

### Join Pattern
```sql
JOIN evaluations e ON e.ticker = t.ticker AND e.filter_stage = 'candidate'
```

---

## SQL Queries Used (Reproducible)

### Core analysis: SOL by price tier
```sql
SELECT entry_price_cents, COUNT(*) AS n,
  ROUND(100.0 * SUM(CASE WHEN is_win THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_rate,
  SUM(pnl_cents) AS total_pnl
FROM trades WHERE asset = 'SOL' AND strategy_group = 'main'
GROUP BY entry_price_cents ORDER BY entry_price_cents;
```

### Sub-86c time gate validation
```sql
SELECT
  CASE WHEN seconds_to_close < 300 THEN 'near_<5m' ELSE 'far_5m+' END AS time_gate,
  COUNT(*) AS n,
  ROUND(100.0 * SUM(CASE WHEN is_win THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr,
  SUM(pnl_cents) AS pnl
FROM trades
WHERE asset = 'SOL' AND strategy_group = 'main' AND entry_price_cents <= 85
GROUP BY time_gate;
```

### Universal STC analysis
```sql
SELECT
  CASE
    WHEN seconds_to_close < 180 THEN '0-3m'
    WHEN seconds_to_close < 300 THEN '3-5m'
    WHEN seconds_to_close < 420 THEN '5-7m'
    WHEN seconds_to_close < 540 THEN '7-9m'
    ELSE '9m+'
  END AS stc_bucket,
  COUNT(*) AS n,
  ROUND(100.0 * SUM(CASE WHEN is_win THEN 1 ELSE 0 END) / COUNT(*), 1) AS wr,
  SUM(pnl_cents) AS pnl
FROM trades WHERE strategy_group = 'main'
GROUP BY stc_bucket ORDER BY stc_bucket;
```

### Correlation analysis
```sql
SELECT
  ROUND(CORR(seconds_to_close, CASE WHEN is_win THEN 1 ELSE 0 END)::numeric, 4) AS stc_win_corr,
  ROUND(CORR(entry_price_cents, CASE WHEN is_win THEN 1 ELSE 0 END)::numeric, 4) AS entry_win_corr,
  ROUND(CORR(edge, CASE WHEN is_win THEN 1 ELSE 0 END)::numeric, 4) AS edge_win_corr
FROM trades WHERE strategy_group = 'main';
```

---

## Next Steps

1. Add this document to project knowledge base (CLAUDE.md or equivalent)
2. Investigate safest implementation path — where in the codebase do these checks belong?
3. Determine if shadow-tagging should precede live deployment
4. Consider adding Telegram alert when these filters would have blocked a trade (counterfactual tracking)
5. After deployment, monitor for 1-2 weeks before considering more aggressive approaches (Option E)
