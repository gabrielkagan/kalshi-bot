---
status: in-progress
updated: 2026-04-07
tags: [research, ppo, position-monitoring, questions, data-collection]
---
# PPO Research Questions & Findings

Position price monitor v2 logs spot_price, threshold, spot_buffer_pct, and Kalshi quotes every tick for held 15M positions.

> **Data quality note (Apr 7):** First 18h had three bugs: STC off by 14,400s (correctable), zero orderbook data (irrecoverable — `yes_ask` field deprecated, should be `yes_ask_dollars`), and 65% of obs from stuck positions. Bugs fixed in 3a6fe53. Clean data collection resumes Apr 7. See [[failures/ppo-monitor-bugs.md]].

## Findings (Apr 7 — 63 settled tickers, 61W/2L, spot-only data with corrected STC)

### Q1: How tight is the buffer at entry?
**Median entry buffer: 0.17%.** Most entries are 0.10-0.20% above threshold. Distribution:

| Bucket | Count |
|--------|-------|
| <0.05% | 1 |
| 0.05-0.10% | 8 |
| 0.10-0.20% | 31 (49%) |
| 0.20-0.30% | 15 |
| >0.30% | 8 |

Losses entered at 0.146% (median) — slightly tighter than wins (0.170%) but not a useful discriminator.

### Q2: Are we entering at peaks or mid-move?
**Even split** — no systematic peak-buying. Entry is near the peak in 37% of positions, mid-range 30%, near trough 33%. Healthy.

### Q3: Does entry buffer predict win/loss?
**Weakly.** Wins mean=0.185%, losses mean=0.146%. The gap (0.039pp) is small relative to the std (0.084%). Entry buffer alone won't predict losses. **n=2 losses — not statistically meaningful.**

### Q4: How fast does the buffer erode on losses?
**Gradual, with 2-3 minutes of warning.** Both losses showed steady erosion, not sudden cliffs:

| Loss | First negative | Warning time | Erosion pattern |
|------|---------------|-------------|-----------------|
| SOL -$60.90 | 54% into hold (STC=94s) | ~2 min | Gradual: 0.14% → -0.10% |
| XRP -$198.00 | 16% into hold (STC=182s) | ~3 min | Early signal, steady decline: 0.09% → -0.16% |

**Key insight**: The XRP loss went negative very early (16% into hold). This is the kind of trade where an early-exit signal could have saved $198.

### Q5: Is there a buffer threshold that predicts losses?
**YES — strong gradient.** The deeper the minimum buffer dip, the more likely loss:

| Min buffer threshold | Positions | W/L | WR |
|---------------------|-----------|-----|-----|
| < 0.00% (any negative) | 9 | 7W/2L | 78% |
| < -0.02% | 7 | 5W/2L | 71% |
| < -0.05% | 5 | 3W/2L | 60% |
| < -0.10% | 3 | 1W/2L | 33% |
| **< -0.15%** | **2** | **0W/2L** | **0%** |

**Every position that dipped below -0.15% lost.** But n=2 — need confirmation.

### Q6: Do winning positions dip into danger?
**11% of wins briefly go negative** (7/61). All had small dips and small PnL:

- Deepest win dip: -0.130% (BTC, pnl=$2.36)
- Most persistent: XRP -0.046% with 39% neg observations (pnl=$2.00)
- No win ever dipped below -0.13%
- Wins that dip have tiny PnL — they're marginal trades teetering on the edge

### Q7: Per-asset buffer volatility
ETH is the safest asset by buffer — fattest buffers, zero losses:

| Asset | W/L | Avg buffer | Avg volatility |
|-------|-----|-----------|----------------|
| ETH | 9/0 | 0.290% | 0.044% |
| BTC | 16/0 | 0.166% | 0.032% |
| SOL | 22/1 | 0.159% | 0.040% |
| XRP | 14/1 | 0.156% | 0.044% |

SOL and XRP have thinnest buffers and highest volatility relative to buffer — explains their loss susceptibility.

### Q8: Entry quality by asset
ETH enters with the fattest buffer, SOL the thinnest:

| Asset | Mean entry buffer | Median |
|-------|-------------------|--------|
| ETH | 0.273% | 0.276% |
| BTC | 0.183% | 0.162% |
| XRP | 0.172% | 0.160% |
| SOL | 0.157% | 0.148% |

SOL's thin entry buffers + high volatility = highest loss risk. ETH's fat buffers = lowest risk.

### Q9: Buffer trajectory by entry STC
**Late entries (STC≤250s) have all the losses:**

| Entry STC | Positions | W/L | Avg buffer | Volatility |
|-----------|-----------|-----|-----------|------------|
| >250s | 35 | 35W/0L | 0.225% | 0.043% |
| ≤250s | 28 | 26W/2L | 0.122% | 0.035% |

Early entries have 80% fatter buffers and zero losses in this sample. Makes sense — more time for price to diverge from threshold.

### Q10: Does the STC scaler correctly calibrate risk?
**YES — buffer range scales linearly with STC:**

| STC bucket | Positions | Avg buffer range | Max range |
|-----------|-----------|-----------------|-----------|
| 0-100s | 5 | 0.046% | 0.088% |
| 100-200s | 13 | 0.129% | 0.290% |
| 200-350s | 38 | 0.180% | 0.365% |
| 350-600s | 6 | 0.231% | 0.478% |

350-600s positions have 5x the price swing of 0-100s positions. The current scaler (300/STC) seems roughly appropriate but could be more aggressive at high STC.

### Q11-Q13: Kalshi Quote Availability
**Blocked** — zero orderbook data due to API field name bug. Fixed Apr 7; re-evaluate after Apr 10-12 with clean data.

### Q14: Can we predict losses from first 30 observations?
**Magnitude of initial slope matters, direction alone doesn't:**

| Outcome | Avg 30-obs slope | Declining? |
|---------|-----------------|------------|
| Wins (n=61) | -0.008% | 59% declining |
| Losses (n=2) | -0.083% | 50% declining |

Losses decline 10x faster in the first 30 observations. But 59% of wins also decline initially — direction alone is useless, magnitude is the signal.

### Q15: Entry buffer as sizing signal
**No clear signal:**

| Group | n | WR | Total PnL |
|-------|---|-----|-----------|
| Fat entry (>0.15%) | 39 | 97% | -$88.63 |
| Thin entry (≤0.15%) | 24 | 96% | +$75.69 |

Counterintuitive — fat-entry has negative total PnL. Likely confounded by position sizing (more high-count TM trades in the fat-entry group). **Not actionable.**

## Summary of Actionable Signals (pending more data)

| Signal | Strength | Actionable? | What it needs |
|--------|----------|-------------|---------------|
| Min buffer < -0.15% → loss | Strong (0% WR) | **Not yet** — n=2 | 10+ losses to confirm |
| >50% neg obs in 30-obs window | Strong ($255 net) | **Not yet** — n=2 | 10+ losses to confirm |
| Late entry (STC≤250s) riskier | Moderate | Supports existing STC scaler | More data |
| SOL/XRP thinner buffers | Moderate | Supports existing per-asset floors | More data |
| Initial 30-obs slope magnitude | Moderate (10x) | **Not yet** — n=2 | Need threshold calibration |

## Next Steps

1. **Collect clean data with orderbook** (Apr 7-14) — answers Q11-Q13
2. **Re-run full analysis at n=10+ losses** — validate buffer threshold and early-exit signals
3. **If confirmed**: design early-exit mechanism using Kalshi bid data (need orderbook observations)
4. **Retroactively fix STC** in existing data: `UPDATE position_price_observations SET seconds_to_close = seconds_to_close + 14400`

## Data Availability Timeline

- ~~**Apr 5-6**: First 24h.~~ Bugged (no orderbook, STC off). Spot data salvageable with +14400 correction.
- **Apr 7-10**: Clean data with orderbook. Basic distributions and OB availability analysis.
- **Apr 12-14**: 1 week clean. Statistically meaningful answers for all questions.

## Related

- [[same-ticker-reentry-analysis.md]] — Original motivation (debunked, but led to discovering 97.6% data blind spot)
- [[price-drift-analysis.md]] — Price drift is net profitable; PPO data will help understand why
- [[goldmine-hunt-apr5.md]] — Broader alpha search context
- [[failures/ppo-monitor-bugs.md]] — Three bugs that corrupted first 18h of data
- [[failures/settlement-watermark-race.md]] — Stuck positions that generated 65% of PPO noise
