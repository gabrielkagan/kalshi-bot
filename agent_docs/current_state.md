# Current State

> Update like a dashboard, not a doc. Re-state the date on every change.

**Last updated:** 2026-05-19

## Live trading

- **OBSERVATION_MODE = False** — LIVE TRADING with real money
- **15M live assets:** BTC (88c+), ETH (75c+, 50-contract cap sub-80c), SOL (86c+, taker-first), XRP (92c+), HYPE (90c+, P2.3 2026-05-14), DOGE (85c+, P2.3 2026-05-14), BNB (90c+, P2.4 2026-05-19)
- **15M shadow assets:** ADA + BCH (T1 shadow 2026-05-30, branch `ada-bch-15m-shadow-t1`). `ADA_15M_SHADOW=True` / `BCH_15M_SHADOW=True`: bot subscribes Coinbase ADA-USD/BCH-USD + Kalshi KXADA15M/KXBCH15M, writes `filter_stage='ada_shadow'`/`'bch_shadow'` rows, submits ZERO live orders. NO per-asset MIN_ENTRY_PRICE/MAX_RISK/blend-weight constants (T3/T4 surface; never reached under shadow). At wiring time both Kalshi series had 0 markets minted (registered but cycle not started) → inert until Kalshi starts the cycle. ADA has no hourly series (KXADAD not live); BCH's KXBCHD exists but is NOT subscribed (15M-only). Both in `HOURLY_EXCLUDED_ASSETS` + `HOURLY_NO_EXCLUDED_ASSETS` as a safety belt. — Prior: BNB T4-promoted 2026-05-19 via P2.4 (T1→T1.5→T2→T3→T4 collapsed at T3 when Brier-sweep argmin converged w=0.20 in 2 days, n=721). Hourly HYPE/DOGE/BNB remain in `HOURLY_EXCLUDED_ASSETS` + `HOURLY_NO_EXCLUDED_ASSETS`. Onboarding history: HYPE/DOGE T1 shadow 2026-05-10 → T4 live 2026-05-14 via Brier-sweep raw_prob direct-promote (cal_mlp training arc retired); BNB T1 shadow 2026-05-17 → T4 live 2026-05-19 via same precedent.
- **XRP_15M_SHADOW = False** — XRP promoted to live at 92c+ (data: 41W/2L, 95.3% WR)
- **HYPE_15M_SHADOW = False / DOGE_15M_SHADOW = False** — P2.3 live promotion 2026-05-14. Per-asset constants wired (HYPE_MIN_ENTRY_PRICE=90, DOGE_MIN_ENTRY_PRICE=85, both MAX_RISK_PER_TRADE=0.10). MARKET_BLEND_W_BY_ASSET extended: HYPE 0.80, DOGE 0.60. NBBO_FALLBACK_GATES INTENTIONALLY omits HYPE/DOGE (orderbook-only first step). 14d Brier-monitored soak runs through 2026-05-28.
- **BNB_15M_SHADOW = False** — P2.4 live promotion 2026-05-19 (ticket 86b9zmj37, sibling to P2.3 86b9xv66a). Per-asset constants wired: BNB_MIN_ENTRY_PRICE=90 (per-tier WR n=272 100% at 90c+; 85-89c is +0.08c sub-fee EV), BNB_MAX_RISK_PER_TRADE=0.10 (conservative new-asset precedent), TM_ASSET_RISK_CAPS["BNB"]=0.10 (mechanical mirror), MARKET_BLEND_W_BY_ASSET["BNB"]=0.20 (B.1-equivalent Brier sweep argmin at n=721 — matches ETH pattern; raw model beats market by ~10% Brier), NBBO_FALLBACK_GATES["BNB"]=(90,99,300.0) (analog default mirror of ETH; post-T4 refinement follow-up when NBBO observations accumulate). Kill-switch clauses preserved at TM/WKND/OVN/DC eligibility sites — flipping BNB_15M_SHADOW=True reverts to shadow in lock-step. Hourly BNB stays in `HOURLY_EXCLUDED_ASSETS` + `HOURLY_NO_EXCLUDED_ASSETS` (15M-only promotion; matches HYPE/DOGE P2.3 post-T4 state). T1.5 external-feed bundle (ticket 86b9zmj15) SHIPPED 2026-05-17 — CROSS_EXCHANGE_SYMBOLS["BNB"]={binance,kraken,bybit} + COINGLASS_SYMBOLS["BNB"] + OKX poller FUNDING_SYMBOLS/OI_SYMBOLS + shadow_coverage_backfill OKX_FUNDING_INSTRUMENTS extended. Deribit BNB perp ABSENT (documented gap, mirrors HYPE). 14d Brier-monitored soak runs through 2026-06-02. **Regime caveat:** 100% WR at 90c+ in shadow window was directional-regime-conditioned (BNB pumped 2026-05-17 → 2026-05-19); rollback rule in plan doc fires if Brier exceeds baseline+5% on ≥3 consecutive days, net 14d PnL ≤ -$50, or single-day catastrophic loss ≥ -$25.
- **ADA_15M_SHADOW = True / BCH_15M_SHADOW = True** — T1 15M shadow onboarding 2026-05-30 (branch `ada-bch-15m-shadow-t1`, plan `kb/decisions/ada-bch-15m-shadow-t1-plan.md`). Registries wired: SERIES_TICKERS (KXADA15M/KXBCH15M), HOURLY_SERIES_TICKERS (KXADAD inert/KXBCHD), COINBASE_PRODUCTS (ADA-USD/BCH-USD), HOURLY_EXCLUDED_ASSETS + HOURLY_NO_EXCLUDED_ASSETS belt. Kill-switch clauses at TM/WKND/OVN/DC eligibility sites. state.py `ada_spot_at_decision`/`bch_spot_at_decision` columns wired end-to-end (closes the BNB-T1 silent-drop gap 86b9zn5pq proactively). NO per-asset live constants (T3/T4). T3 calibration sweep blocks on ~2-4wk settled corpus. Regression lock: `tests/integration/test_ada_bch_onboarding_t1.py`.
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
- **P4.1 band-calibrated sizing LIVE (2026-05-17, ClickUp 86b9zjrp7):** 15M `_sizer.compute()` now receives `calibrated_prob_for_sizing(asset, best_ask, final_prob, product_type)` instead of bare `final_prob`. Helper home: `bot/helpers/band_calibration.py`. 42-cell empirical lookup (6 assets × 7 bands: 70-79 / 80-85 / 86-89 / 90-93 / 94-96 / 97-98 / 99) with hierarchical shrinkage k=30 toward band-aggregate prior. Hybrid lookback: 30d for bands 70-93c (regime-sensitive), 60d for bands 94-100c (thin-cell stability). Baseline frozen 2026-05-17 09:35 UTC against VPS HEAD `e3aecd4`. **Trade-selection gates unchanged** — only Kelly magnitude changes. **V2 sizing (`_v2_prob`) and NO-side (`no_prob`) explicitly out of scope.** Hourly/SPX/weather pass-through. 14d band-stratified Brier soak runs through 2026-05-31; rollback rule = per-(asset×band) realized rate ±5pp of baseline. Baseline sidecar: `agent_docs/p4_1_calibration_baseline.md`.

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
- **Multi-venue synthetic RTI (B2b-1, ticket 86ba64h2w):** in-bot 4-venue (Coinbase/Kraken/Bitstamp/Gemini) L2 → CFB-shape synthetic, logged to `evaluated_opportunities.rti_synthetic/rti_constituent_count/rti_confidence`. **SHADOW by default — feeds a decision only for assets in `SYNTHETIC_RTI_LIVE_ASSETS` (RTI-6 per-asset go-live gate; default EMPTY ⇒ shadow for every asset).** Kill-switch `SYNTHETIC_RTI_ENABLED` (env, default OFF): when OFF the feed (`bot/feeds/synthetic_rti_feed.py`) opens no sockets/threads. Computed off the scan hot path by a sampler daemon; the scan loop reads an O(1) cache. NEVER flip the signal live before the Bit-4 validation gate (`kb/decisions/b2b-multi-venue-signal-program-plan.md`).

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
- **Series (15M):** KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M, KXHYPE15M (live, P2.3 2026-05-14), KXDOGE15M (live, P2.3 2026-05-14), KXBNB15M (live, P2.4 2026-05-19, 86b9zmj37), KXADA15M (shadow, T1 2026-05-30), KXBCH15M (shadow, T1 2026-05-30)
- **Series (hourly):** KXBTCD, KXETHD, KXSOLD, KXXRPD, KXHYPED (excluded), KXDOGED (excluded), KXBNBD (excluded — 15M-only promotion at P2.4), KXADAD (not live on Kalshi; inert registry entry), KXBCHD (excluded — not subscribed; ADA/BCH 15M-shadow-only T1)
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
