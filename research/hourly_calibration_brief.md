# Hourly Market Calibration Brief

**Date:** February 28, 2026
**Status:** Hourly trading reverted to observation-only after -$101 loss on 33 trades
**Objective:** Fix calibration overconfidence in hourly markets so we can trade them profitably

---

## 1. System Context

We trade crypto above/below prediction markets on Kalshi. Our **15-minute (15M) markets** are profitable: 120 settled trades, 111W/9L (92.5% WR), with calibration that slightly underestimates win rates (+2.5pp average gap). We use the same calibration engine for **hourly markets** — and it's systematically overconfident there, producing 22W/11L (66.7% WR) on 33 actual trades, net -$101.11.

### How the pipeline works

1. **Raw probability**: Black-Scholes model using EGARCH(1,1) volatility, spot price, strike, and time-to-close
2. **Beta calibration**: `logit(P_cal) = c + a*log(p) + b*log(1-p)` transforms raw prob
3. **Dynamic cap**: Caps calibrated prob by seconds_to_close (STC) — different schedule for 15M vs hourly
4. **Uncertainty shrinkage**: Blends toward 0.5 based on estimation uncertainty
5. **Market blend**: Blends calibrated prob with market price (40% weight for hourly, 50% for 15M)
6. **Edge calculation**: `edge = calibrated_prob - market_price - fees`
7. **Trade decision**: Enter if edge > 0.9% and market price in [70c, 99c] for hourly

### Key config (hourly)

| Parameter | Value | Notes |
|-----------|-------|-------|
| HOURLY_MAX_SECONDS_BEFORE_CLOSE | 1800 | 30 min before close |
| HOURLY_MIN_ENTRY_PRICE | 70 | Cents |
| HOURLY_MARKET_BLEND_W | 0.40 | 40% market blend |
| MIN_EDGE_PCT | 0.9% | Shared with 15M |
| HOURLY_MAX_RISK_PER_TRADE | 0.15 | Max 15% bankroll |
| HOURLY_DYNAMIC_CAP_SCHEDULE | (1800, 0.97), (900, 0.98), (300, 0.99), (60, 0.995) | Probability ceiling by STC |

### Beta-cal parameters (current, trained on ALL data)

```
a = 8.720716
b = -0.298106
c = 2.399366
n_observations = 3018
temperature = 1.075
overall_brier = 0.052035
```

### Beta-cal transform at key points

| Raw Prob | Calibrated Prob | Difference |
|----------|----------------|------------|
| 0.800 | 0.718 | -8.2pp |
| 0.850 | 0.825 | -2.5pp |
| 0.900 | 0.897 | -0.3pp |
| 0.920 | 0.919 | -0.1pp |
| 0.940 | 0.937 | -0.3pp |
| 0.950 | 0.945 | -0.5pp |
| 0.960 | 0.953 | -0.7pp |
| 0.970 | 0.960 | -1.0pp |
| 0.980 | 0.967 | -1.3pp |
| 0.990 | 0.976 | -1.5pp |
| 0.995 | 0.981 | -1.4pp |

**Key observation:** The transform barely compresses high probabilities. At raw=0.97 (common hourly input), it only subtracts 1pp. At raw=0.99, only 1.5pp. This is far too little squashing for hourly markets where actual win rates at high predicted probs are ~84%, not ~96%.

---

## 2. The Problem: Hourly Overconfidence

### 2.1 By calibrated probability bucket (insufficient_edge, n=115 settled)

| Cal Bucket | N | W/L | Actual WR | Predicted WR | Gap | p-value |
|------------|---|-----|-----------|-------------|-----|---------|
| 0.96-1.00 | 44 | 37/7 | 84.1% | 97.4% | -13.3pp | 0.0001*** |
| 0.94-0.96 | 8 | 8/0 | 100.0% | 94.6% | +5.4pp | 1.0000 |
| 0.92-0.94 | 24 | 24/0 | 100.0% | 92.4% | +7.6pp | 1.0000 |
| 0.90-0.92 | 10 | 10/0 | 100.0% | 91.6% | +8.4pp | 1.0000 |
| 0.85-0.90 | 13 | 11/2 | 84.6% | 87.9% | -3.3pp | 0.4773 |
| 0.80-0.85 | 6 | 3/3 | 50.0% | 82.4% | -32.4pp | 0.0719* |
| <0.80 | 10 | 8/2 | 80.0% | 75.3% | +4.7pp | 0.7491 |

**Finding:** The overconfidence is concentrated in the **0.96-1.00 bucket** (p=0.0001). All other buckets with n>6 are either well-calibrated or underconfident. The high-cal bucket contains all 7 of the losses that occurred in insufficient_edge observations.

### 2.2 By seconds-to-close (insufficient_edge, n=115 settled)

| STC Bucket | N | W/L | Actual WR | Predicted WR | Gap | p-value |
|------------|---|-----|-----------|-------------|-----|---------|
| >1500s (25-30min) | 68 | 59/9 | 86.8% | 93.4% | -6.6pp | 0.0348** |
| 1200-1500s (20-25min) | 15 | 11/4 | 73.3% | 89.0% | -15.7pp | 0.0741* |
| 900-1200s (15-20min) | 13 | 13/0 | 100.0% | 93.8% | +6.2pp | 1.0000 |
| 600-900s (10-15min) | 9 | 8/1 | 88.9% | 88.3% | +0.6pp | 0.6742 |
| 300-600s (5-10min) | 4 | 4/0 | 100.0% | 87.7% | +12.3pp | 1.0000 |
| <300s (<5min) | 6 | 6/0 | 100.0% | 86.1% | +13.9pp | 1.0000 |

**Finding:** Overconfidence is **time-dependent**. At >1200s, overconfidence is statistically significant (p=0.008 combined). Below 1200s, calibration is accurate or underconfident (p=0.158 for underconfidence). All 14 hourly losses in insufficient_edge occurred at STC > 1100s.

### 2.3 By asset (insufficient_edge, n=115 settled)

| Asset | N | W/L | Actual WR | Predicted WR |
|-------|---|-----|-----------|-------------|
| BTC | 62 | 56/6 | 90% | 91.6% |
| ETH | 25 | 24/1 | 96% | 91.4% |
| SOL | 19 | 15/4 | 79% | 92.2% |
| XRP | 9 | 6/3 | 67% | 94.7% |

**Finding:** SOL and XRP appear worst, but sample sizes are small. XRP's 67% at predicted 94.7% is alarming (9 obs, 6W/3L, p~0.018) but may reflect SOL/XRP having higher realized volatility than the model estimates.

### 2.4 Combined significance test

| STC Range | N | W/L | Actual WR | Predicted WR | p-value |
|-----------|---|-----|-----------|-------------|---------|
| >1200s | 83 | 70/13 | 84% | 92.6% | 0.0079** |
| <=1200s | 32 | 31/1 | 97% | 90.0% | 0.1579 (undconf.) |

---

## 3. Actual Hourly Trades (33 total, 22W/11L, -$101.11)

All trades from Feb 28, 2026 (the only day of live hourly trading before revert):

| Time | Asset | Price | Qty | PnL | STC | Edge | Cal | Result | Event |
|------|-------|-------|-----|-----|-----|------|-----|--------|-------|
| 05:02 | SOL | 92c | 25 | +$2.00 | 1740s | 5.1% | 0.941 | W | ...2800 |
| 07:02 | SOL | 93c | 10 | -$9.29 | 1550s | 3.6% | 0.956 | L | ...2802 |
| 07:02 | BTC | 93c | 18 | -$16.74 | 1788s | 3.6% | 0.956 | L | ...2802 |
| 07:02 | BTC | 93c | 6 | -$5.59 | 1685s | 2.0% | 0.980 | L | ...2802 |
| 07:02 | BTC | 89c | 6 | -$5.34 | 1394s | 4.6% | 0.946 | L | ...2802 |
| 07:02 | BTC | 92c | 6 | +$0.48 | 1358s | 2.0% | 0.980 | W | ...2802 |
| 07:02 | BTC | 95c | 1 | +$0.05 | 1363s | 2.0% | 0.980 | W | ...2802 |
| 07:02 | ETH | 92c | 49 | -$45.07 | 1796s | 3.2% | 0.952 | L | ...2802 |
| 07:02 | ETH | 94c | 9 | +$0.54 | 1402s | 3.3% | 0.953 | W | ...2802 |
| 07:31 | XRP | 93c | 9 | -$8.40 | 1757s | 3.0% | 0.960 | L | ...2802 |
| 07:31 | XRP | 91c | 9 | -$8.19 | 1399s | 4.2% | 0.952 | L | ...2802 |
| 08:09 | BTC | 92c | 4 | +$0.32 | 1251s | 4.3% | 0.943 | W | ...2803 |
| 08:09 | BTC | 90c | 5 | +$0.52 | 1348s | 3.0% | 0.930 | W | ...2803 |
| 08:09 | BTC | 88c | 7 | +$0.83 | 1657s | 4.1% | 0.931 | W | ...2803 |
| 08:09 | BTC | 91c | 7 | +$0.64 | 1799s | 4.0% | 0.940 | W | ...2803 |
| 08:09 | SOL | 91c | 6 | +$0.54 | 1509s | 4.1% | 0.951 | W | ...2803 |
| 08:09 | ETH | 93c | 3 | +$0.21 | 1259s | 2.6% | 0.956 | W | ...2803 |
| 08:09 | ETH | 93c | 7 | +$0.50 | 1665s | 3.8% | 0.938 | W | ...2803 |
| 08:31 | XRP | 94c | 4 | +$0.24 | 1544s | 3.5% | 0.955 | W | ...2803 |
| 09:02 | BTC | 90c | 2 | -$1.80 | 1121s | 2.1% | 0.931 | L | ...2804 |
| 09:02 | BTC | 89c | 2 | +$0.22 | 1581s | 2.1% | 0.911 | W | ...2804 |
| 09:31 | XRP | 91c | 7 | +$0.60 | 1755s | 4.1% | 0.951 | W | ...2804 |
| 10:02 | BTC | 92c | 4 | +$0.31 | 1720s | 1.9% | 0.949 | W | ...2805 |
| 10:02 | SOL | 90c | 3 | +$0.30 | 798s | 4.9% | 0.949 | W | ...2805 |
| 11:03 | SOL | 95c | 2 | +$0.10 | 140s | 2.1% | 0.941 | W | ...2806 |
| 11:03 | SOL | 93c | 6 | +$0.42 | 1792s | 2.5% | 0.955 | W | ...2806 |
| 11:03 | BTC | 90c | 1 | +$0.10 | 1274s | 1.9% | 0.909 | W | ...2806 |
| 11:03 | BTC | 91c | 6 | +$0.54 | 1799s | 2.7% | 0.917 | W | ...2806 |
| 12:03 | BTC | 90c | 2 | -$1.80 | 1748s | 1.9% | 0.919 | L | ...2807 |
| 12:03 | BTC | 94c | 2 | +$0.12 | 1138s | 2.2% | 0.922 | W | ...2807 |
| 12:03 | ETH | 90c | 7 | +$0.69 | 1155s | 3.1% | 0.921 | W | ...2807 |
| 12:03 | SOL | 92c | 6 | -$5.52 | 1776s | 3.5% | 0.945 | L | ...2807 |
| 12:32 | XRP | 91c | 4 | -$3.64 | 1690s | 2.6% | 0.946 | L | ...2807 |

### Loss clustering

8 of 19 hourly windows had at least 1 loss (42% of windows). Even with 1 asset per window: 12W/7L (63% WR), -$72.39. The problem is NOT correlated multi-asset exposure — it's fundamental calibration.

---

## 4. 15M Calibration Comparison (works correctly)

15M insufficient_edge settled: **887 observations**

| Cal Bucket | N | W/L | Actual WR | Predicted WR | Gap |
|------------|---|-----|-----------|-------------|-----|
| 0.96-1.00 | 220 | 217/3 | 99% | 96.9% | +1.8pp |
| 0.94-0.96 | 179 | 170/9 | 95% | 95.2% | -0.2pp |
| 0.92-0.94 | 114 | 110/4 | 96% | 93.0% | +3.5pp |
| 0.90-0.92 | 132 | 124/8 | 94% | 91.0% | +2.9pp |
| 0.85-0.90 | 188 | 174/14 | 93% | 88.2% | +4.3pp |
| <0.85 | 54 | 47/7 | 87% | 82.7% | +4.4pp |

**Overall 15M:** Actual 94.9%, Predicted 92.5%, gap +2.5pp (slightly underconfident — correct behavior). Average STC: 199s.

**Contrast with hourly:** At the same beta-cal output values, hourly markets have dramatically lower win rates than 15M markets. The same calibrated_prob of 0.95 means ~95% WR in 15M but ~85% WR in hourly.

---

## 5. Training Data Contamination (Critical Finding)

The `load_training_data_from_db()` function in the calibration engine loads **all** evaluated_opportunities rows with settlement outcomes — it does NOT filter by product_type.

### Current training data composition

| Source | Count | % of Total |
|--------|-------|-----------|
| Non-hourly (15M) | 1948 | 64.5% |
| Hourly | 1070 | 35.5% |
| **Total** | **3018** | |

### Hourly training data by filter stage

| Stage | Count | Avg Raw Prob | WR |
|-------|-------|-------------|-----|
| price_out_of_range | 899 | 0.993 | 98.4% |
| insufficient_edge | 115 | 0.939 | 87.8% |
| zero_sizing | 32 | 0.967 | 65.6% |
| strategy_wait | 24 | 0.899 | 50.0% |

**The problem:** 899 price_out_of_range hourly entries (30% of all training data) have raw_prob ~0.993 and 98.4% WR. These are deep in-the-money strikes where the market itself prices them at 98-99c. They teach beta_cal that raw_prob near 1.0 is almost always correct — which is true for those cases but NOT for the borderline hourly strikes (cal ~0.94-0.97) that the bot actually trades. This contamination may explain why beta_cal barely squashes probabilities above 0.90.

---

## 6. Why Simple Fixes Don't Work

### 6.1 Reducing HOURLY_MAX_SECONDS_BEFORE_CLOSE to 1200s

At STC < 1200s, calibration looks good (31W/1L, 97% actual vs 90% predicted). But:
- Only **4 insufficient_edge observations** had positive edge below 1200s
- **Zero candidates** were ever generated below 1200s
- This effectively turns hourly trading off

### 6.2 Capping calibrated_prob at 0.90 for >1200s

Simulation on actual trades: keeps only 5 trades (4W/1L, -$0.59). Filters out 28 trades (18W/10L, -$100.52). Again, nearly turns it off.

### 6.3 Current HOURLY_DYNAMIC_CAP_SCHEDULE

The cap at 1800s is 0.97. Since beta_cal outputs ~0.96 for raw=0.97, the cap is rarely binding. The cap helps but doesn't solve the fundamental problem that beta_cal itself is wrong for hourly markets.

---

## 7. What We Need

### 7.1 Hourly-specific calibration model

The core hypothesis: **time-to-close matters for calibration in a way the current model doesn't capture.** A raw probability of 0.97 with 1800s remaining is fundamentally different from 0.97 with 200s remaining. The Black-Scholes model accounts for time mathematically, but the calibration residuals appear time-dependent for hourly (and NOT for 15M, where STC is 100-270s and calibration works).

Options to explore:
1. **Separate beta_cal parameters for hourly** — train on hourly data only (currently 115 tradeable obs, growing ~40-90/day)
2. **Time-aware calibration** — add STC as a feature in the calibration model
3. **Different calibration method entirely** — isotonic regression, Platt scaling with time feature, etc.
4. **Product-type filter in training data** — at minimum, stop contaminating the 15M model with hourly data

### 7.2 Data availability

| Metric | Current | Growth Rate | Target |
|--------|---------|-------------|--------|
| Hourly insufficient_edge (settled) | 115 | ~40-90/day | 500+ |
| Days of data | 2 | +1/day | 7-10 |
| Hourly price_out_of_range (settled) | 899 | ~400/day | N/A |

We're collecting data in observation mode now. At ~40-90 tradeable observations per day, we should have 500+ in ~5-8 more days.

### 7.3 Immediate action: filter training data — DONE

**Implemented Feb 28.** Two changes to `bot.py`:

1. **`load_training_data_from_db()` (line 5206):** Added `AND (product_type IS NULL OR product_type != 'hourly')` to the SQL query. On startup, the calibration engine now loads only 15M observations (~1948 rows instead of ~3018).

2. **Runtime `add_observation()` call (line 9361):** Added `is_hourly = row.get("product_type") == "hourly"` check. Hourly settlements no longer feed into the live calibration engine.

**Effect:** Beta_cal will retrain on next restart using only 15M data. The 899 price_out_of_range hourly entries (avg raw_prob 0.993, 98.4% WR) that were teaching the model "high raw probs are always right" will be excluded. This may slightly increase squashing at the high end, improving 15M calibration as a side effect.

**Not yet done:** The existing `calibration_state.json` on the VPS still contains parameters trained on contaminated data. These will be overwritten on next bot restart when `maybe_retrain()` runs on the filtered dataset.

### 7.4 Recommended research priorities

1. **STC-dependent correction** (highest value) — Design a penalization function that increases probability squashing as STC increases. The data clearly shows calibration is accurate below 1200s but overconfident above. This could be a multiplicative correction on top of beta_cal, or a separate calibration path for hourly.

2. **Temperature scaling on hourly subset** (quick win) — Fit a single temperature parameter to the 115 hourly insufficient_edge observations. This captures the overall overconfidence (~8.5pp) without needing 500+ obs for full beta_cal. Could serve as an interim fix to get hourly trading back online sooner.

3. **EGARCH volatility validation** (root cause) — Compare EGARCH-implied hourly volatility vs realized hourly returns. If sqrt(t) scaling systematically underestimates hourly vol, that's the upstream cause of raw_prob overconfidence, and a vol adjustment would be more principled than post-hoc calibration correction.

4. **Literature review** (parallel) — Time-aware probability calibration approaches: isotonic regression with covariates, Platt scaling with time features, recalibration with auxiliary variables.

---

## 8. Raw Data Access

All data is in `state.db` (SQLite) on the VPS:

```sql
-- Hourly tradeable observations (settled)
SELECT calibrated_prob, raw_prob, market_price, market_result,
       seconds_to_close, edge, calibrated_prob_raw, asset, evaluation_time
FROM evaluated_opportunities
WHERE product_type = 'hourly'
  AND filter_stage = 'insufficient_edge'
  AND market_result IS NOT NULL;
-- Returns 115 rows (growing)

-- Actual hourly trades
SELECT settled_at, asset, entry_price_cents, count, pnl_cents,
       market_result, seconds_to_close, edge, calibrated_prob, event_ticker
FROM settled_trades
WHERE event_ticker LIKE '%D-2%'
ORDER BY settled_at;
-- Returns 33 rows

-- 15M comparison data
SELECT calibrated_prob, raw_prob, market_result, seconds_to_close
FROM evaluated_opportunities
WHERE (product_type IS NULL OR product_type != 'hourly')
  AND filter_stage = 'insufficient_edge'
  AND market_result IS NOT NULL
  AND calibrated_prob IS NOT NULL;
-- Returns 887 rows

-- Current calibration parameters
-- File: calibration_state.json in the repo root
```

Columns in `evaluated_opportunities`:
- `calibrated_prob`: final calibrated probability (post-cal, post-cap, post-shrinkage, post-blend)
- `calibrated_prob_raw`: post-calibration but pre-cap/pre-shrinkage/pre-blend
- `raw_prob`: Black-Scholes model output before any calibration
- `market_price`: Kalshi best ask in cents (YES side)
- `market_result`: 'yes' (price stayed above/below) or 'no'
- `seconds_to_close`: seconds until market settlement at evaluation time
- `edge`: calibrated_prob - market_price_decimal - fees
- `filter_stage`: pipeline stage where this opportunity was filtered
- `product_type`: 'hourly' or NULL (for 15M)

---

## 9. Anticipated Questions

**Q: Why not just retrain beta_cal on hourly data only?**
A: 115 observations is too few to fit a 3-parameter model reliably. Beta_cal was trained on 3018 obs for the combined model. With ~500+ hourly obs (5-8 more days), we could fit hourly-specific parameters with reasonable confidence. In the meantime, we could try a simpler model (single-parameter temperature scaling) on the hourly subset.

**Q: Is the overconfidence due to the EGARCH volatility model being wrong for hourly timescales?**
A: Possibly. EGARCH is trained on 5-second returns and scaled to 15M/hourly windows via square-root-of-time. This scaling assumes returns are roughly IID, which breaks down at longer horizons where mean-reversion or momentum effects matter. The raw probabilities for hourly are higher (avg 0.939 vs 0.927 for 15M), suggesting the vol model may underestimate hourly volatility. This is a testable hypothesis: compare EGARCH-implied hourly volatility vs realized hourly returns.

**Q: Could this be market microstructure rather than calibration?**
A: Unlikely. The Kalshi market prices at 93-98c imply 93-98% win probability. Our calibration says ~95-97%. Actual is ~85%. Both the market AND our model are wrong — but our model is what we control. Also, the 15M market prices are similarly high (92-99c) but calibration works there, suggesting the issue is hourly-specific model error, not market mispricing.

**Q: Is 115 observations enough to draw these conclusions?**
A: For the top-level finding (>1200s overconfidence, p=0.008), yes — this is statistically significant. Individual sub-buckets (asset-level, specific STC bands) have small samples and should be treated as directional. The overall pattern — overconfident at high STC, accurate at low STC — is robust.

**Q: What about the market blend weight (0.40)?**
A: The 40% market blend was optimized for Brier score across a 134K-row backtest. However, that backtest used the same (contaminated) calibration model. If we fix the calibration model, the optimal blend weight may change. Worth re-optimizing after calibration fix. Note: increasing blend weight toward 1.0 would make us closer to market consensus, which might reduce overconfidence but also reduce edge and volume.

**Q: What's the interaction between the dynamic cap schedule and beta_cal?**
A: The dynamic cap applies AFTER beta_cal. At 1800s STC, the cap is 0.97. Beta_cal outputs ~0.96 for raw=0.97, so the cap is rarely binding. The pipeline is: raw_prob → beta_cal → dynamic_cap → uncertainty_shrinkage → market_blend → edge_calc. The cap provides a ceiling but doesn't fix the underlying beta_cal overconfidence.

**Q: Should we use different raw probability models for hourly vs 15M?**
A: The Black-Scholes + EGARCH pipeline is shared. The raw inputs differ (longer time horizon = higher raw probs). The calibration is where the product types should diverge — the raw model's errors have different structure at hourly vs 15M timescales. A time-aware calibration model that explicitly handles the STC dimension is probably the right approach.

---

## 10. Updates Log

### Feb 28, 2026 — Training data filter implemented

**What changed:** `load_training_data_from_db()` and the runtime `add_observation()` path now exclude hourly observations from the calibration engine. The 15M calibration model will retrain on ~1948 observations (down from ~3018) on next restart.

**Researcher's initial assessment:** Confirmed training data contamination (Section 5) as a "smoking gun." Agreed that the time-dependent pattern (100% of losses at STC > 1100s) supports the hypothesis that sqrt(t) volatility scaling breaks down at longer horizons. Proposed four research directions: (1) hourly-specific calibration design, (2) training data filter implementation, (3) STC-dependent correction modeling, (4) literature review on time-aware calibration.

**Status:** Direction #2 (training data filter) is implemented and ready to deploy. Directions #1 and #3 are the highest-priority research items. Data collection continues in observation mode (~40-90 tradeable obs/day).
