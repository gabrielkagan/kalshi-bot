# Hourly Alt Shadow Strategies — Implementation Summary

**Date**: 2026-03-06
**Status**: Ready for deployment
**Risk Level**: LOW — shadow-only, structurally cannot place real orders

## What Was Built

Two parallel shadow strategies for ETH, SOL, XRP hourly markets:

### Strategy A: Market-Making Shadow
- Computes fair value from orderbook midpoint
- Generates shadow buy/sell orders at configurable spread buffers
- Tracks theoretical fills based on price movements
- NO directional model — edge comes from spread capture

### Strategy B: HAR-RV Shadow
- Replaces EGARCH with Heterogeneous Autoregressive Realized Volatility model
- Per-asset RV components: hourly, daily, weekly
- Per-asset temperature scaling (ETH: 2.76, SOL: 4.0, XRP: 5.0)
- Higher market blend weights (ETH: 60%, SOL: 70%, XRP: 80%)
- Multi-gate abstention: min/max edge, max confidence, vol spike, price blacklist, STC filter

## Files Created

| File | Purpose |
|------|---------|
| `hourly_alt_shadow.py` | Main engine — both strategies, DB logging, settlement |

## Files Modified

| File | Changes |
|------|---------|
| `bot.py` | 4 hooks: init (line ~11579), price ingestion (~12071), evaluation (~8010), settlement (~11319), cleanup (~6482) |
| `firebase_push.py` | 1 hook: dashboard data push (end of `_build_snapshot`) |
| `dashboard/index.html` | New "Hourly Alt Shadow" panel + JS rendering function |

## Database

New table `hourly_alt_shadow_signals` in `state.db`:
- Separate from existing tables — no schema changes to existing tables
- Unique index on (ticker, strategy) for dedup
- Settlement tracking with status/market_result/shadow_pnl_cents

New journal: `hourly_alt_shadow_journal.jsonl`

## Safety Architecture

1. `hourly_alt_shadow.py` has NO import of KalshiClient or any order code
2. All hooks wrapped in try/except — failures cannot propagate to live system
3. Separate shadow bankrolls ($1000 notional each) — no impact on real balance
4. HOURLY_ALT_SHADOW_ENABLED master switch in hourly_alt_shadow.py
5. Only evaluates at the hourly observation gate (after existing pipeline)
6. Uses existing price feed (CoinbaseFeed) — no new API calls

## Rollback Plan

### Quick disable (no deploy needed):
Set `HOURLY_ALT_SHADOW_ENABLED = False` in `hourly_alt_shadow.py`

### Full removal:
1. Remove the 4 hooks from bot.py (search for "hourly_alt_shadow")
2. Remove the firebase_push.py hook
3. Delete `hourly_alt_shadow.py`
4. Remove dashboard panel from index.html
5. Optionally: `DROP TABLE hourly_alt_shadow_signals` from state.db

## Data Collection Timeline

- HAR-RV needs ~1 hour to accumulate enough 5-second returns for RV_1h
- HAR-RV needs ~24 hours for RV_1d, 7 days for RV_1w
- Market-Making starts producing signals immediately
- Meaningful evaluation: 5-7 days minimum

## Config Tuning (all in hourly_alt_shadow.py)

| Config | Current | Notes |
|--------|---------|-------|
| MM_SPREAD_BUFFER | ETH:3, SOL:4, XRP:5 | Cents each side of mid |
| MM_MIN_SPREAD | ETH:4, SOL:5, XRP:6 | Min spread to participate |
| HARRV_TEMPERATURE | ETH:2.76, SOL:4.0, XRP:5.0 | From research brief |
| HARRV_MARKET_BLEND | ETH:0.60, SOL:0.70, XRP:0.80 | Market weight |
| HARRV_MAX_EDGE | ETH:3%, SOL:2.5%, XRP:2% | Edge inversion protection |
| HARRV_PRICE_BLACKLIST | ETH:80-84, SOL:90-94, XRP:70-79 | Known disaster zones |
