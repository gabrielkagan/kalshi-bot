# Current State

> Update like a dashboard, not a doc. Re-state the date on every change.

**Last updated:** 2026-05-16

## Live trading

- **OBSERVATION_MODE = False** — LIVE TRADING with real money
- **15M live assets:** BTC (88c+), ETH (75c+, 50-contract cap sub-80c), SOL (86c+, taker-first), XRP (92c+), HYPE (90c+, P2.3 2026-05-14), DOGE (85c+, P2.3 2026-05-14)
- **15M shadow assets:** (none — HYPE/DOGE T4 promoted 2026-05-14 via P2.3 raw_prob + per-asset MARKET_BLEND_W blend, ClickUp 86b9xv66a). Hourly HYPE/DOGE remain in `HOURLY_EXCLUDED_ASSETS` + `HOURLY_NO_EXCLUDED_ASSETS` (hourly path not yet promoted). Original T1-T4 onboarding history: T1 shadow 2026-05-10 → T4 live 2026-05-14 via Brier-sweep raw_prob direct-promote (cal_mlp training arc retired).
- **XRP_15M_SHADOW = False** — XRP promoted to live at 92c+ (data: 41W/2L, 95.3% WR)
- **HYPE_15M_SHADOW = False / DOGE_15M_SHADOW = False** — P2.3 live promotion 2026-05-14. Per-asset constants wired (HYPE_MIN_ENTRY_PRICE=90, DOGE_MIN_ENTRY_PRICE=85, both MAX_RISK_PER_TRADE=0.10). MARKET_BLEND_W_BY_ASSET extended: HYPE 0.80, DOGE 0.60. NBBO_FALLBACK_GATES INTENTIONALLY omits HYPE/DOGE (orderbook-only first step). 14d Brier-monitored soak runs through 2026-05-28.
- **SOL_TAKER_FIRST = True** — bypasses maker, direct IOC at all STC
- **Decided contracts LIVE:** T1 (z≤-5), T1B (z≤-4, 95c+), T2 (z≤-3, 93-96c), T2-Z25 (z≤-2.5, 93-96c). All @ 20% fixed sizing. **T2-Z2 SHADOWED** (97a365f Apr 1, -$313 on 47 trades). Canonical: `kb/concepts/dc-strategy.md`
- **Overnight discount LIVE:** weekday 04-11 UTC, 89c+, STC≤600s, no DC overlap; sub-89c/STC>600s remain shadow
- **Weekend discount LIVE:** Sat/Sun, 90c+, STC≤600s, no DC overlap; sub-90c/STC>600s remain shadow
- **Loss burst cooldown LIVE (Apr 11):** per-asset 2h lockout after any 15M loss (+$441/30d counterfactual)
- **Weather NO-side KILLED 2026-05-16 (ce8e2d2):** lifetime n=167, 38.3% WR vs 70% assumed prior (Wilson 95% CI [23.6%, 47.0%]). Near-ATM zone (NO 39-40c ↔ YES 60-61c) is market-maker zone with no edge; `bracket_no_live` (far-ITM NO 4-12c, 91.7% WR n=157) is where NO edge actually lives. Now observation-only on both sides; re-research initiative live (ClickUp folder `90149436180`). Prior LIVE history (Apr 11 → May 16): NO 39-40c, STC ≥ 16h, 1-contract, floor tightened 36→37 May 1 then 37→39 May 2 as sub-bands underperformed.
- **STC zones:** scan 0-900s, core live 0-300s, extended live 300-600s (per-asset higher floors), shadow 600-900s
- **STC sizing scaler:** contracts *= 300/STC for 15M at STC>300s
- **SOL sub-86c gate:** blocks SOL ≤85c at STC≥300s
- **LPNE LIVE:** BTC 80-87c near-expiry (STC 10-120s), 50ct fixed, prob ≥ price/100

## Disabled / observation

- **Hourly FULLY DISABLED (Apr 18):** Both `HOURLY_LIVE_ENABLED=0` and `HOURLY_NO_SIDE_LIVE=0` on VPS. Pre-kill NO-side: BTC NO 40-54c had 53.9% WR (n=1,113, p=0.005). Re-enable: flip env vars + restart.
- **SPX Hourly:** observation (was briefly live Mar 17, reverted — Polygon 403 broke vol engine)
- **Weather YES-side:** observation only (NWP ensemble GFS+ECMWF, 82 members, 19 cities)
- **Sports:** observation only (basketball best, 69.2% WR n=39, SPRT CONTINUE_COLLECTING). Apr 12 fix: 31-day data outage from MLB code mismatch + LA→LAK + hockey re-enabled for playoffs.

## Shadow strategies

- **15M:** A1 (RecalibratedEGARCH), A2 (LightGBM), A3 (EGARCH gating), A4 (LateWindow 55-74c) — `fifteenm_shadow.py`
- **DC variants:** dc_shadow_t1b_93c, dc_shadow_t2_90c, dc_shadow_t2_90c_xrp, dc_shadow_no_side
- **Calibration:** Per-city weather + per-sport-group CalEngines learning in shadow
- **Hourly NO 40-54c:** 1-contract verification mode (env var kill switch)
- **Low-price 70-79c:** dual-sizing sim (full Kelly vs LP_KELLY=0.25, LP_MAX_RISK=0.10)
- **Position price monitor:** logs yes_ask/bid for held 15M positions via WS

## Tests

3,368 tests across 16+ test files (Apr 26: +28 for 15M silence watchdog hardening).

## Tech stack

- Python 3, virtualenv
- DigitalOcean droplet (45.55.181.30), Ubuntu 24.04, `botuser`, systemd `kalshi-bot`
- Dashboard: Supabase Realtime (`dashboard_state` table)
- Analyst: Claude API via `bot/ai/analyst.py` → Telegram (high-confidence only)

## Kalshi API

- **Auth:** RSA-PSS signature with `/trade-api/v2` prefix
- **Orderbook:** YES and NO are SEPARATE; YES + NO prices do NOT always sum to 100
- **All orders are limit orders** (no market orders)
- **Tier:** Advanced (30 reads/sec, 30 writes/sec)
- **Series (15M):** KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M, KXHYPE15M (live, P2.3 2026-05-14), KXDOGE15M (live, P2.3 2026-05-14)
- **Series (hourly):** KXBTCD, KXETHD, KXSOLD, KXXRPD, KXHYPED (excluded), KXDOGED (excluded)
- **Series (weather, 19 cities):** KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHDEN, KXHIGHLAX, KXHIGHAUS, KXHIGHTATL, KXHIGHTSFO, KXHIGHTDAL, KXHIGHTPHX, KXHIGHPHIL, KXHIGHTMIN, KXHIGHTSEA, KXHIGHTHOU, KXHIGHTBOS, KXHIGHTLV, KXHIGHTOKC, KXHIGHTDC, KXHIGHTNOLA

## Fees

- **Taker:** `ceil(0.07 * C * P * (1-P))` — ~1% of edge at typical prices
- **Maker:** $0

## Order execution

- **Default:** maker-first (post_only=True) → escalates to taker if unfilled
- **SOL exception:** taker-first (`SOL_TAKER_FIRST=True`)
- **Decided contracts:** route direct taker
- **Taker allowed at all STC** (`MAKER_ONLY_THRESHOLD=0`)
- **Escalation:** maker → poll queue → cancel-replace IOC taker
- **Fill detection:** WS primary, REST fallback
- **Candidate logging:** observation_trade (obs mode) + candidate (live mode) → `evaluated_opportunities`
