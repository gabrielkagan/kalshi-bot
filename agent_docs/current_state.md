# Current State

> Update like a dashboard, not a doc. Re-state the date on every change.

**Last updated:** 2026-04-26

## Live trading

- **OBSERVATION_MODE = False** — LIVE TRADING with real money
- **15M live assets:** BTC (88c+), ETH (75c+, 50-contract cap sub-80c), SOL (86c+, taker-first), XRP (92c+)
- **XRP_15M_SHADOW = False** — XRP promoted to live at 92c+ (data: 41W/2L, 95.3% WR)
- **SOL_TAKER_FIRST = True** — bypasses maker, direct IOC at all STC
- **Decided contracts LIVE:** T1 (z≤-5), T1B (z≤-4, 95c+), T2 (z≤-3, 93-96c), T2-Z25 (z≤-2.5, 93-96c). All @ 20% fixed sizing. **T2-Z2 SHADOWED** (97a365f Apr 1, -$313 on 47 trades). Canonical: `kb/concepts/dc-strategy.md`
- **Overnight discount LIVE:** weekday 04-11 UTC, 89c+, STC≤600s, no DC overlap; sub-89c/STC>600s remain shadow
- **Weekend discount LIVE:** Sat/Sun, 90c+, STC≤600s, no DC overlap; sub-90c/STC>600s remain shadow
- **Loss burst cooldown LIVE (Apr 11):** per-asset 2h lockout after any 15M loss (+$441/30d counterfactual)
- **Weather NO-side LIVE (Apr 11):** NO 36-40c, STC ≥ 16h, 1-contract. 36c floor added Apr 20.
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
- Analyst: Claude API via `analyst.py` → Telegram (high-confidence only)

## Kalshi API

- **Auth:** RSA-PSS signature with `/trade-api/v2` prefix
- **Orderbook:** YES and NO are SEPARATE; YES + NO prices do NOT always sum to 100
- **All orders are limit orders** (no market orders)
- **Tier:** Advanced (30 reads/sec, 30 writes/sec)
- **Series (15M):** KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M
- **Series (hourly):** KXBTCD, KXETHD, KXSOLD, KXXRPD
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
