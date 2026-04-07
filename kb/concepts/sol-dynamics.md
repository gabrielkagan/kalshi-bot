---
status: active
updated: 2026-04-07
tags: [sol, edge-floor, sizing, taker-first, stc-gate, nbbo]
---
# SOL Trading Dynamics

## Summary
SOL drives the majority of bot trade volume but is only marginally profitable (+$102 on 474 trades, $0.21/trade as of Apr 7). The primary drag is NBBO fallback pricing at low prices — when the orderbook is empty, stale NBBO + IOC drift creates phantom fills at unprofitable prices.

## Key Numbers (Apr 7, 2026)
- SOL with orderbook pricing: 116 trades, 92.2% WR, **+$470**
- SOL with NBBO fallback: 226 trades, 90.7% WR, **-$328**
- SOL 85c (all phantom IOC drift): 20 trades, 70% WR, **-$456**

## NBBO Fallback Gate (Apr 7)
`NBBO_FALLBACK_GATES["SOL"] = (90, 99, 300)` — raised from 86c to 90c.

**Data:** SOL NBBO sub-90c = 98 trades, 85.7% WR, -$319. Orderbook trades at same prices = +$470 (unaffected). Optimal threshold search: 88c saves $156, **90c saves $319**, 92c saves $210.

**Impact:** +$46/day improvement, 86% boost to total 15M PnL ($369→$688).

## IOC Drift (Phantom Fills)
All 20 SOL 85c trades were scanned at 86-92c but filled at 85c via IOC drift. The 2-4c drift zone is toxic (77% WR, -$382). The bot's probability model was calibrated at the scanned price, not the fill price.

**Common fingerprint of SOL sub-88c losses:**
- 71% NBBO fallback (empty orderbook)
- 71% IOC drift (fill below scanned ask)
- 64% passthrough calibration (learned calibrator not active)
- 79% high STC (>200s)
- 71% thin buffer (<0.2%)

See [[failures/ioc-subfloor-fill.md]].

## Edge Floor
SOL minimum edge: 1.0% (`SOL_MIN_EDGE`). Data: <1.0% = 82% WR, ≥1.0% = 94.2% WR on 258 trades.

## Sub-86c Time Gate
`SOL_LOW_ENTRY_STC_GATE = True` — blocks SOL ≤85c at STC≥300s.

**Data:** SOL sub-86c near-expiry (<300s): 100% WR, +$228. Far-from-expiry (≥300s): 78.3% WR, -$289. Note: this gate helps but doesn't prevent phantom 85c fills from IOC drift at higher scanned prices.

## PPO Buffer Analysis (Apr 7)
From position price observations (limited data, 18h):
- SOL has the **thinnest entry buffers** (mean 0.157%, median 0.148%) of all assets
- SOL has **high buffer volatility** (0.040%) — second only to XRP
- Combined with the lowest per-asset floor (80c), this creates the highest loss exposure

## Related
- [[concepts/per-asset-rules.md]] — Full per-asset config table
- [[concepts/dc-strategy.md]] — DC tiered risk caps for SOL
- [[failures/ioc-subfloor-fill.md]] — IOC sub-floor fill bug
- [[failures/sol-maker-adverse-selection.md]] — Why SOL is taker-first
- [[../kb-research/bot/stc-sizing-research.md]] — Source data for sub-86c gate
