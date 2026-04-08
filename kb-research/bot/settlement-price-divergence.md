---
status: pending
updated: 2026-04-07
tags: [research, settlement, cfb-rti, multi-exchange, spot-price, buffer]
---
# Settlement Price Divergence Research

## Problem
Bot uses Coinbase spot for probability/buffer calculations. Kalshi settles on CF Benchmarks Real-Time Index (CFB RTI) — a multi-exchange, 60-second averaged price. If these diverge, our buffer measurement is wrong and our model is subtly miscalibrated.

## How CFB RTI Works

### Calculation (NOT simple mid/VWAP — order book based)
1. **Consolidate order books** from all constituent exchanges into one book
2. **Build price-volume curves** at integer volume granularity (V=1,2,3... units)
3. **Determine utilized depth**: max V where bid-ask spread < 0.5%
4. **Exponential weighting**: weight mid price-volume curve by exponential PDF (highest weight at top of book, decaying into depth)
5. **Sum** weighted curve = RTI value

Key implication: RTI heavily weights top-of-book. A simple average of best bid/ask mid-prices across constituent exchanges is a reasonable approximation.

### Constituent Exchanges (DIFFERENT per asset)
| Asset | Exchanges | Count |
|-------|-----------|-------|
| BTC | Bitstamp, Coinbase, Kraken, Gemini, Bullish, Crypto.com, LMAX | 7 |
| ETH | Bitstamp, Coinbase, Kraken, Gemini, itBit, LMAX, Bullish, Crypto.com | 8 |
| **SOL** | **Coinbase, Kraken, Gemini, LMAX, Bitstamp** | **5** |
| **XRP** | **Bitstamp, Kraken, Coinbase, LMAX** | **4** |

**SOL and XRP have far fewer constituent exchanges.** XRP only has 4 — our Coinbase+Kraken coverage is 2/4 (50%). SOL is 2/5 (40%). This means our approximation is better for these assets than for BTC (2/7 = 29%).

### Other Details
- **Update frequency**: 1 reading/second, 24/7/365
- **Settlement**: arithmetic mean of 60 readings in the final minute
- **Outlier filter**: any exchange whose mid-price deviates >25% from median of all constituents is excluded for that tick
- **No free API**: CF Benchmarks requires a paid license
- **Documentation**: [Kalshi BTC Contract Terms](https://kalshi-public-docs.s3.amazonaws.com/contract_terms/BTC.pdf), [CFB RTI Methodology](https://docs.cfbenchmarks.com/CME%20CF%20Real%20Time%20Indices%20Methodology.pdf)

## Two Separate Divergence Sources

### 1. Multi-Exchange vs Single Exchange
Coinbase is 1 of 8 constituent exchanges. During volatile moments, Coinbase may deviate from the aggregate. Estimated divergence: 0.01-0.03% for BTC (most liquid), possibly 0.05%+ for SOL/XRP (thinner on institutional exchanges).

**What we have**: CrossExchangeFeed already collects Kraken, Binance, Bybit. Only Kraken is a CFB RTI constituent. Binance/Bybit are NOT constituents — averaging them in would move us AWAY from RTI, not toward it.

**What we'd need for full coverage by asset**:
- XRP (4 constituents): Coinbase ✓ + Kraken ✓ + Bitstamp (free WS) + LMAX (institutional) → 3/4 achievable
- SOL (5 constituents): Coinbase ✓ + Kraken ✓ + Gemini (free WS) + Bitstamp (free WS) + LMAX → 4/5 achievable
- BTC (7 constituents): add Bitstamp + Gemini + Crypto.com (all free WS) → 5/7 achievable
- LMAX/Bullish/itBit are institutional-only, likely no free API

**Key insight**: For XRP and SOL (fewest constituents), Coinbase+Kraken already covers 50% and 40% respectively. Adding Bitstamp alone gets XRP to 75%. These smaller-constituent assets are where divergence risk is highest AND where our approximation is most achievable.

### 2. 60-Second Averaging vs Instantaneous
Settlement uses a 60-reading average, not instantaneous price. A position 0.15% above threshold at the close moment might lose if it was below for 40 of the last 60 seconds. This is probably MORE impactful than multi-exchange divergence.

**What we have**: CoinbaseFeed already stores 1-second snapshots (`_snapshot_loop`). Computing a trailing 60s average is trivial.

## What We Currently DON'T Capture
- `expiration_value`: Kalshi API field with the actual CFB RTI settlement price. Zero references in bot.py. Should log this at settlement time to measure divergence.
- `settlement_value`: Available on `market_lifecycle_v2` WebSocket channel.

## Research Plan

### Phase 1: Measure (implement now)
1. **Log `expiration_value`** at settlement — one line in `_process_settlement()` or `_sweep_stuck_positions()`. Compare to Coinbase spot at close.
2. **Compute Coinbase 60s trailing average** — use existing 1-second snapshots. Log alongside instantaneous spot for comparison.
3. **Log Coinbase + Kraken average** — both already collected, both RTI constituents. See if 2-exchange average tracks RTI better.

### Phase 2: Analyze (after 1 week of data)
- How much does Coinbase diverge from `expiration_value`? By asset?
- Does the 60s average reduce the divergence?
- Does Coinbase+Kraken average reduce it further?
- Does divergence correlate with our losses? (i.e., do we lose more when Coinbase is optimistic vs RTI?)

### Phase 3: Act (based on data)
- If divergence > 0.03% systematically: switch buffer calculation to multi-exchange average
- If 60s averaging matters: use trailing average for buffer and possibly for probability model input
- If neither matters much: keep Coinbase-only (simplicity wins)

## Why NOT to Rush Multi-Exchange

1. **Binance/Bybit are NOT RTI constituents** — adding them moves us away from settlement truth, not toward it
2. **Calibration already absorbs some bias** — the model trains on Coinbase→settlement_outcome, implicitly learning the mapping
3. **The 60s average is the cheaper, bigger win** — uses existing data, directly approximates settlement mechanism
4. **We need measurement before implementation** — `expiration_value` logging tells us the actual magnitude of the problem

## Estimated Impact
With current buffer thresholds:
- Win avg buffer: 0.39%. If Coinbase overstates by 0.03%, real buffer is 0.36% → still comfortable
- Loss avg buffer: 0.14%. If Coinbase overstates by 0.03%, real buffer is 0.11% → crosses into our "thin" zone (< 0.10%)
- The divergence matters most at the margins where trades are borderline — exactly where buffer sizing would kick in

## Related
- [[buffer-rescue-analysis.md]] — Buffer-gated trade rescue (depends on buffer accuracy)
- [[ppo-research-questions.md]] — PPO findings on buffer vs outcome
- kb/concepts/sol-dynamics.md — SOL NBBO problem (related: stale pricing)
