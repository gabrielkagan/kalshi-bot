# Config Reference

All values mirror constants in `bot/constants.py` (canonical home post-Bit-3.1) + `bot/config.py` (probability/EGARCH/sizing peer, Bit 12.1 — relocated from repo-root `config.py` 2026-05-12; bot/_impl.py was DELETED in Bit 9.3-iii.c so the historical `from bot.constants import *` re-export chain is gone — callers reach constants directly via `bot.constants.X` or via `from bot.constants import X`). `market_config.py` asserts they match at startup.
On change, run `make doc-drift` (alias for `python3 scripts/audit/doc_drift_check.py`) and update this file in the same commit.

## Trading mode (live/shadow control) — ticket 86ba747ke, 2026-05-30

Modular global + per-asset live/shadow gate (single source of truth:
`bot/trading_mode.py::is_live`). Consulted at the order chokepoints
`bot/executor.py::execute` + the hard backstop `bot/kalshi_client.py::place_order`.
Added ALONGSIDE (defense-in-depth with) the still-present scattered inline
`_15M_SHADOW` scanner checks — both fail toward shadow. Read live →
flipping a flag is a runtime kill-switch (no restart). SHIPPED OFF (everything
shadow) after a verified ~80% account drawdown ($800→$150) on a structurally-losing
strategy (settlement-convergence edge hunt: buying 90-99¢ near-certain favorites
is −3.2¢/contract; lifetime fees $651 > +$379 gross). To put an asset back live,
flip BOTH `GLOBAL_LIVE_TRADING=True` AND `ASSET_LIVE_TRADING["<ASSET>"]=True`
(double fail-safe) — and only after a strategy clears the adversarial gate.

| Config | Value | Notes |
|--------|-------|-------|
| GLOBAL_LIVE_TRADING | False | Master kill — False = entire bot shadow (no real crypto-15M orders) |
| ASSET_LIVE_TRADING | all False | Per-asset enable, all 11 crypto-15M series explicit (NEAR/ZEC added False at T1 2026-09-05, `86bbvdc8y`); lock-step with `SERIES_TICKERS` (pinned by `tests/unit/test_trading_mode.py::test_asset_live_trading_covers_all_series_no_drift`) |
| ASSET_LIVE_TRADING_DEFAULT | False | Unknown/unlisted asset → shadow (fail-safe) |

## Longshot premium-harvest maker (Bit L-1, 2026-06-11)

Engine `bot/longshot.py`; plan `kb/decisions/longshot-twap-live-small-plan.md`.
Validated via `scripts/research/genhunt/02b_longshot_fillable_validation.py`
(fillable-only +4.58¢/ct, day-bootstrap CI [+2.82, +6.26], 12/12 days, all 6
assets positive). Sell deep-OTM sides as a maker (post opposite-side bid at
100−ask), hold to settlement. Live/shadow control stays with the trading-mode
gate at `executor.execute()` (single chokepoint). Regression lock:
`tests/integration/test_longshot_strategy.py`.

| Config | Value | Notes |
|--------|-------|-------|
| LONGSHOT_ENABLED | False | Master enable; default OFF — flipped only at explicit operator go-live |
| LONGSHOT_MIN_ASK_CENTS | 4 | Sold-side executable ask band lower edge (validated 4-15c) |
| LONGSHOT_MAX_ASK_CENTS | 15 | Sold-side executable ask band upper edge |
| LONGSHOT_MIN_STC_SECONDS | 180.0 | T-3min — stop quoting / cancel resting below this STC |
| LONGSHOT_MAX_STC_SECONDS | 720.0 | T-12min — earliest entry |
| LONGSHOT_EDGE_RATIO | 0.5 | Condition: p_normal ≤ ask × ratio (prob units = ask_cents/200 at 0.5) |
| LONGSHOT_MAX_CONTRACTS_PER_WINDOW_SIDE | 3 | Live-small sizing per (ticker, side); counts max(open positions, ls- FILLED contracts per `pending_orders.recorded_fill_count`) + unfilled resting quotes. 86baf07y3 (live 2026-06-15): the fill-ledger term closes the same-side re-quote-after-fill race — on fill the registry entry is popped and the positions row lags/clobbered (ticker-PK), so positions alone under-read a window-side already at cap and a re-quote sized a fresh full cap (DOGE 151515 double-fill); recorded_fill_count is per-order and immune to the overwrite. R4-M1: `_allowed_size` additionally returns 0 on ANY open opposite-side longshot row (one open longshot row per ticker — `record_position_from_fill` matches WHERE ticker+strategy_group with no side predicate, so an opposite-side fill would accumulate under the old side; ticker-PK stopgap, 86badbf9t). R5-M2 extends the same invariant to opposite-side RESTING quotes (registry quotes are future rows; incl. CANCEL_FILL_MISMATCH-held entries) via `has_opposite_side_resting_quote`. The scanner overlay mirrors both guards defensively |
| LONGSHOT_MAX_CONCURRENT_COLLATERAL_DOLLARS | 150.0 | Across resting quotes + open longshot positions |
| LONGSHOT_CLIENT_OID_PREFIX | "ls-" | client_order_id prefix on every longshot maker (R1-M1/M4 + R3-M1 + R4-MN1/MN2): boot orphan reconciliation (first-tick adopt-and-kill of restart survivors) + `place_order` backstop strategy recognition (`trading_mode.strategy_from_client_order_id`) + startup-reconciler carve-out (Bit T-1 generalized: `bot/state.py` keys off `ENGINE_OWNED_CLIENT_OID_PREFIXES` — `_reconcile_orders` skips engine-owned orders in cancel/row-flip sweeps; `cleanup_expired_resting_orders` also skips them — R4-MN1, the settlement daemon calls it concurrently and an 'expired' flip would hide the row from boot step 2; `_reconcile_positions` stamps imports with the mapped strategy_group only when the MOST RECENT pending_orders row on the ticker carries an engine-owned prefix — R4-MN2 recency, not existence) |
| LONGSHOT_LIVE_OVERRIDE | False | Longshot-ONLY go-live (R1-M4; asset-scoped at R4-M1): `trading_mode.strategy_is_live('longshot', asset)` = (`is_live(asset)` OR this flag) AND `asset ∈ LONGSHOT_LIVE_ASSETS`. Main pipeline UNAFFECTED (non-longshot strategies reduce exactly to `is_live`). Consulted at `executor.execute()` + the `place_order` backstop; a live→shadow flip also cancels resting quotes on the next tick (R1-MN4) |
| LONGSHOT_LIVE_ASSETS | frozenset BTC/ETH/SOL/XRP/HYPE/DOGE/BNB | Longshot live universe (R4-M1 mechanism): gates the WHOLE longshot branch of `strategy_is_live` (override leg AND any future GLOBAL+asset dual-live flip). Evidence = the 02b validation run's "all 6 assets positive" headline (`scripts/research/genhunt/02b_longshot_fillable_validation.py`); BNB INCLUDED per the 2026-06-12 operator directive ("everything available" at go-live) — its 02b absence is a corpus artifact (no replayable spot source; the live engine computes p_normal from the bot's own feeds, which cover BNB) and risk is bounded by the live-small rails. ADA/BCH excluded: their Kalshi 15M series do not exist yet (zero corpus windows) + T1 zero-live-orders shadow designation — add when listed. NEAR/ZEC excluded (T1 shadow 2026-09-05, `86bbvdc8y`): series listed since 2026-06-30 but outside the 02b corpus and under `NEAR_15M_SHADOW`/`ZEC_15M_SHADOW` — pinned by `tests/integration/test_near_zec_onboarding_t1.py` |
| LONGSHOT_MAX_SPOT_STALENESS_SECONDS | 30.0 | Frozen/unmeasured-spot gate (Bit V.1 R1-M1 fix round — longshot shipped with NO staleness gate; twaplock had its 5s R2-MN1 gate). 30.0s = the validated backtest's abstention horizon: `02_longshot_tick_floor` `STALE_S=30.0` makes its decision-point reads (`_at`/`_rv`) return None — the backtest ABSTAINED — whenever the spot was >30s event-stale (tracked in-repo equivalent: `scripts/research/genhunt/02b_longshot_fillable_validation.py::_at`/`_rv_pure`, same `STALE_S=30.0`; the 02 script itself is not committed). Live, CoinbaseFeed's sampler re-stamps a frozen price with fresh timestamps every 1s, so only the Bit-S.1 event-time reading (`state._scan_spot_staleness_cache`) can see the freeze; `evaluate_market` emits NO SIGNAL + writes NO eval rows when the reading is missing or > this (log `LONGSHOT_SPOT_STALE`, info, 60s/asset throttle — BNB gaps 34% of 1-min intervals per the S.2 RCA). Lockstep with `bot.helpers.tape_rv.TAPE_RV_MAX_STALENESS_S` (the seam-side event-time gate on rv300), pinned by `tests/contracts/test_tape_rv_estimator_parity.py` |
| LONGSHOT_STALE_CANCEL_GRACE_SECONDS | 10.0 | Stale-episode quote-down grace (Bit V.1 R2-M1 fix round — the gate above returned [] BEFORE the condition-refresh pass, so resting quotes stayed up un-refreshed through ≤12-min stale episodes whose fills the 02b fill model — zscore→None on a >30s-stale spot at print time — had EXCLUDED from the +4.58c economics). Once an episode persists for at least this many wall-clock seconds on a ticker, `evaluate_market` cancels that ticker's resting quotes (reason `spot_stale`; cancel_order intentionally ungated — cancels only reduce exposure). 10.0 = 02b's measured cancel-latency arm (`LATENCY_S=10.0`, `fills_latency10` pickoff -0.16c/ct — the ONLY measured tolerance for a quote lingering past signal death), so the grace must stay ≤ 10.0; grace > 0 absorbs flickery staleness (no churn — fresh eval clears the per-ticker episode clock `_stale_first_seen`). Twaplock is taker/IOC (rests nothing) — abstain-only stays the complete fix there. Pinned by `tests/integration/test_longshot_strategy.py::TestSpotStaleQuoteDown` |

The Bit L-1 per-strategy rails `LONGSHOT_DAILY_LOSS_CAP_DOLLARS` /
`LONGSHOT_CONSECUTIVE_LOSING_DAYS_DISABLE` / `LONGSHOT_STREAK_RESET_UTC_DATE`
were RETIRED at Bit T-1 into the combined `LIVE_SMALL_*` rails below
(closing the R2-MN4 note — `LongshotEngine._refresh_disabled` now consumes
`bot/strategy_caps.py`, same numbers as twaplock's latch; log signatures
LONGSHOT_DAILY_CAP_HIT / LONGSHOT_CONSEC_DAYS_DISABLE unchanged).

## TWAP-lock endgame taker (Bit T-1, 2026-06-11)

Engine `bot/twaplock.py`; plan `kb/decisions/longshot-twap-live-small-plan.md`.
Validated via `scripts/research/genhunt/01b_twap_lock_validation.py`
(+14.4¢/ct, day-bootstrap CI [+11.1, +17.7], n=359 over 12 days, 29.9
locks/day on the honest 4-venue index, print cross-check 99.2%, all 7 assets
positive, on the validated [10,90]s grid). In the [60, 90]s-to-close window (live floor raised to 60s
2026-06-14 — the <60s window is a TWAP variance-collapse/basis-error bug, NOT vol;
see the `TWAPLOCK_ENTRY_WINDOW_SECONDS` row + `kb/decisions/twaplock-stc60-floor-plan.md`),
compute `p_lock` from the
accrued Coinbase-anchored settlement-TWAP (per-asset spot ring buffer fed
from the scanner's per-tick read) + a remaining-variance term from
`blended_rv` (with the 60s floor the live path prices off current spot + the
Brownian term; the accrued-mean branch is dormant, retained for a future
<60s reopening); when the locked side clears the threshold, BUY it as a TAKER
(IOC) if the executable ask leaves ≥ fee + margin vs ~100¢ settlement; hold
to settlement. No resting lifecycle (an IOC never rests — no registry, no
cancel sweeps). Live/shadow control stays with the trading-mode gate at
`executor.execute()` (single chokepoint). Regression lock:
`tests/integration/test_twaplock_strategy.py`.

| Config | Value | Notes |
|--------|-------|-------|
| TWAPLOCK_ENABLED | False | Master enable; default OFF — flipped only at explicit operator go-live |
| TWAPLOCK_P_LOCK_THRESHOLD | 0.99 | STRICTER than the validated 0.95: the Coinbase-anchored MVP index adds proxy error vs the honest 4-venue validation index; undercounting costs frequency, not correctness (degraded-index lesson) |
| TWAPLOCK_TWAP_WINDOW_SECONDS | 60.0 | Kalshi settles on a 60s TWAP of its reference index |
| TWAPLOCK_ENTRY_WINDOW_SECONDS | 90.0 | Upper edge of the entry window — the validated decision grid's DEC_FROM (`01b_twap_lock_validation.py`); no backtest evidence for (90, 120]. The engine-side `_MIN_SUBMIT_STC_SECONDS` lower bound was RAISED 10.0→60.0 on 2026-06-14 (`kb/decisions/twaplock-stc60-floor-plan.md`): the shadow soak measured the <60s window locking only 0.754 vs a predicted 0.99, because the model's Brownian remaining-variance term vanishes in the final seconds while the omitted Coinbase-vs-RTI basis error stays constant — p_lock is driven spuriously to ~1 on near-strike calls. NOT vol deflation (honest vol would need to be 4× larger; measured 1.19×). The retained [60, 90]s window locks 0.990 and is vol-calibrated; the original DEC_TO=10 settlement-race rationale still holds a fortiori below 60s. Reopening <60s needs a basis-error variance model (separate ticket) |
| TWAPLOCK_MAX_CONTRACTS_PER_ENTRY | 2 | Live-small sizing (plan doc: 1-2 ct/entry) |
| TWAPLOCK_MIN_EDGE_CENTS | 3 | Executable ask must be ≤ 100 − taker_fee(1ct) − this margin |
| TWAPLOCK_MAX_SPOT_STALENESS_SECONDS | 5.0 | Frozen-spot false-lock gate (R2-MN1): a frozen Coinbase WS price keeps feeding the engine's ring buffer with fresh receive timestamps, freezing the accrued TWAP at a stale price — p_lock can clear 0.99 spuriously and `_accrued_mean`'s absent-sample → None layer never fires. The scanner's per-asset Bit-S.1 staleness reading (`state._scan_spot_staleness_cache`) must exist and be ≤ this or `evaluate_market` emits NO SIGNAL (`TWAPLOCK_SPOT_STALE`, info — fires routinely on thin assets: the S.2 RCA pre-flight (ticket `86ba1wrh7`, 2026-05-21) measured Coinbase 1-min candle coverage May 9-21 at BNB 65.9% / HYPE 89.5% / DOGE 99.2%; BTC/ETH/SOL/XRP ~100%). Trades frequency on thin assets for signal integrity — same direction as the stricter 0.99 threshold |
| TWAPLOCK_CLIENT_OID_PREFIX | "tw-" | client_order_id prefix on every twaplock taker: reconciler carve-outs (via `ENGINE_OWNED_CLIENT_OID_PREFIXES`) + `place_order` backstop strategy recognition. Cross-strategy ticker exclusion: the engine never enters a ticker with ANY open position or pending/resting order from ANY strategy (single-ticker positions PK until 86badbf9t; fail-closed) |
| TWAPLOCK_API_ERROR_CIRCUIT_THRESHOLD | 3 | Consecutive `place_order` None across tickers → no more twaplock POSTs until the next UTC date. Per-ticker one-shot cannot stop the drip (each 15M window is a new ticker), and the executor's `TICKER_API_ERROR_CAP` Gate 3 sits below the twaplock dispatch so it never fires on this path. VPS 2026-09-06: 2582 `tw-` api_error / 2 fills ever. Reconstructed on engine init from trailing tw- `pending_orders` `api_error` rows for today's UTC date so a deploy/crash does not re-arm POSTs. |
| TWAPLOCK_LIVE_OVERRIDE | False | Twaplock-ONLY go-live (asset-scoped at R4-M1): `trading_mode.strategy_is_live('twaplock', asset)` = (`is_live(asset)` OR this flag) AND `asset ∈ TWAPLOCK_LIVE_ASSETS`. Main pipeline + longshot UNAFFECTED |
| TWAPLOCK_LIVE_ASSETS | frozenset BTC/ETH/SOL/XRP/HYPE/DOGE/BNB | Twaplock validated live universe (R4-M1): gates the WHOLE twaplock branch of `strategy_is_live` (override leg AND any future GLOBAL+asset dual-live flip). Mirrors the `TRACKED` tuple in `scripts/research/genhunt/01b_twap_lock_validation.py` — the 7 assets the +14.4¢/ct "all 7 assets positive" verdict covers (BNB on a single-venue Kraken index, flagged but positive). ADA/BCH/NEAR/ZEC excluded on BOTH grounds: T1 zero-live-orders shadow designation (`ADA_15M_SHADOW`/`BCH_15M_SHADOW`; `NEAR_15M_SHADOW`/`ZEC_15M_SHADOW`) + not in 01b TRACKED (no KXADA15M/KXBCH15M markets existed, and KXNEAR15M/KXZEC15M were not yet listed, in the validation corpus) |

One entry per window per asset is STRUCTURAL, not a config knob: the
engine's in-memory latch is binary and ANY tw- pending_orders row on the
ticker (even a zero-fill canceled IOC; survives restart) consumes the
shot. The former `TWAPLOCK_MAX_ENTRIES_PER_WINDOW` constant was RETIRED
at R1-MN4 (a value other than 1 could never be honored); pinned-absent by
`tests/integration/test_twaplock_strategy.py::TestTwaplockConstants::test_sizing_rails`.

## Live-small combined risk rails (longshot + twaplock; Bit T-1)

Single source of truth `bot/strategy_caps.py` — BOTH engines' disable
latches consume the same functions (realized fee-inclusive PnL from
`settled_trades WHERE strategy IN ('longshot','twaplock')` + marked open
losses from each engine's registered mark provider).

| Config | Value | Notes |
|--------|-------|-------|
| LIVE_SMALL_DAILY_LOSS_CAP_DOLLARS | 20.0 | COMBINED realized+marked PnL today ≤ −cap → same-day auto-disable of BOTH engines (logs: LONGSHOT_DAILY_CAP_HIT / TWAPLOCK_DAILY_CAP_HIT) |
| LIVE_SMALL_CONSECUTIVE_LOSING_DAYS_DISABLE | 3 | N consecutive completed COMBINED losing days → persistent disable of BOTH |
| LIVE_SMALL_STREAK_RESET_UTC_DATE | "" | Operator re-enable: combined losing days on/before this UTC date ignored ("" = never reset) |
| ENGINE_OWNED_OID_PREFIX_TO_STRATEGY | {ls-: longshot, tw-: twaplock} | Single-sourced prefix→strategy map driving the `bot/state.py` reconciler carve-outs + `trading_mode.strategy_from_client_order_id`; extend when a new engine-owned strategy lands |
| ENGINE_OWNED_CLIENT_OID_PREFIXES | ("ls-", "tw-") | Tuple form for `str.startswith` checks (derived from the map) |

## Vol-honesty monitor (Bit V.3, 2026-06-12)

L-VOL-2 (`kb/failures/vol-engine-beta-dvol-deflation-jun12.md`): continuous
independent check of the engine's raw `blended_rv` against the tape's
`trailing_rv300`, fed by the scanner at the V.1 `_strategy_vol` seam via
`bot/helpers/vol_honesty.py::VolHonestyMonitor`. The alert band is
deliberately WIDER than the pre-registered [0.8, 1.25] re-arm soak gate
(`kb/decisions/longshot-twap-live-small-plan.md`) — the monitor is the
always-on tripwire for the deflation CLASS (alts ran stored/realized
0.15-0.59 for ~4 months); the soak pass-bar reads the persisted
`tape_rv300`/`raw_blended_rv` columns via the `/live-small` soak section.

| Config | Value | Notes |
|--------|-------|-------|
| VOL_HONESTY_LOW | 0.6 | Breach floor for median(raw_blended_rv / tape_rv300) — below BTC/ETH's honest ~0.7-1.0 weekly stored/realized baseline with margin; the broken alts ran 0.15-0.59 |
| VOL_HONESTY_HIGH | 1.8 | Breach ceiling (inflation is dishonest too) — ≈ symmetric to 0.6 in log-space (1/0.6 ≈ 1.67) |
| VOL_HONESTY_WINDOW_S | 600.0 | Trailing ratio window the per-asset median runs over (10 min); median not mean so one garbage tick can't trip a 10-min verdict |
| VOL_HONESTY_ALERT_THROTTLE_S | 3600.0 | Telegram throttle per asset; the `VOL_HONESTY_BREACH` WARN keeps firing on the 60s check cadence (journal-greppable soak evidence) |

## Global / 15M

| Config | Value | Notes |
|--------|-------|-------|
| OBSERVATION_MODE | False | LIVE trading |
| MIN_ENTRY_PRICE | 75 | Cents (global floor — lowered from 80 for ETH 75-79c) |
| BTC_MIN_ENTRY_PRICE | 88 | Cents (88c = 96.2% WR on n=53 shadow, 96.3% on n=27 recent) |
| ETH_MIN_ENTRY_PRICE | 90 | Cents (raised from 85 — ETH 85-89c 86.2% WR, -$23.76 PnL; 90c+ 95.2% WR) |
| SOL_MIN_ENTRY_PRICE | 86 | Cents (raised from 80: SOL@85c 68.2% WR -$496/22 vs 86c 94.4% WR +$375/36) |
| ETH_SUB80_POSITION_CAP | 50 | Max contracts for ETH 75-79c (half-Kelly clamp [20,50]) |
| XRP_MIN_ENTRY_PRICE | 92 | Cents (PnL negative at every floor <90c, PF=1.68 at ≥92c) |
| HYPE_MIN_ENTRY_PRICE | 90 | Cents (P2.3 2026-05-14 live promotion; B.1b post-blend 90+ WR 94.4% n=250; conservative borderline-EV pick) |
| DOGE_MIN_ENTRY_PRICE | 85 | Cents (P2.3 2026-05-14 live promotion; B.1b post-blend 85+ WR 95.5% n=445 PnL +1.93c/trade pre-maker-discount) |
| BNB_MIN_ENTRY_PRICE | 90 | Cents (P2.4 2026-05-19 live promotion; per-tier WR 90c+ 100% n=272 +7.60c/trade; 85-89c is sub-fee EV at +0.08c naive. 100% WR is directional-regime-conditioned — 14d soak monitor active.) |
| MAX_ENTRY_PRICE | 99 | Cents |
| MIN_EDGE_PCT | 0.25 | Flat fallback for execution paths (was 0.7) |
| MIN_EDGE_BY_PRICE | 0.20%-1.0% | 80-88c→0.25%, 89-90c→0.25%, 91-92c→0.20%, 93-94c→0.50%, 95-96c→0.75%, 97-99c→1.0% |
| MARKET_BLEND_W | 0.40 | Legacy 15M scalar fallback for non-15M paths and unknown assets. P2.1.d (2026-05-13) + P2.3 (2026-05-14) + P2.4 (2026-05-19) superseded for all 7 production 15M assets by MARKET_BLEND_W_BY_ASSET (per-asset map). |
| MARKET_BLEND_W_BY_ASSET | `{BNB:0.20,BTC:0.10,DOGE:0.60,ETH:0.20,HYPE:0.80,SOL:0.80,XRP:0.90}` | P2.1.d (2026-05-13) per-asset 15M blend weights from cal_mlp v1.1 4×6 sweep — interior-pulled argmaxes (BTC 0.0→0.10, ETH 0.20, SOL 0.80, XRP 1.0→0.90). Extended 2026-05-14 (P2.3, 86b9xv66a) with HYPE 0.80 + DOGE 0.60 from B.1 Brier sweep on T1 shadow data (n=1469/1710 settled rows; both interior argmins). Extended 2026-05-19 (P2.4, 86b9zmj37) with BNB 0.20 from B.1-equivalent Brier sweep on T1 shadow data (n=721 settled rows; interior argmin matching ETH pattern — raw model beats market by ~10% Brier). Origins: kb/findings/p2-1-c-fu1-blend-weight-sweep-resolves-eth-may13.md + kb/findings/p2-3-b-live-promotion-blend-weights-may14.md + kb/decisions/p2-4-bnb-live-promotion-plan.md. Canonical no-space alphabetical form is the doc-drift contract (scripts/audit/doc_drift_check.py). |
| MAX_RISK_PER_TRADE | 0.25 | Max 25% bankroll per trade |
| MAX_SECONDS_BEFORE_CLOSE | 900 | 15 min before close (600-900s shadow, 0-600s live) |

## STC zones

| Config | Value | Notes |
|--------|-------|-------|
| STC_SHADOW_THRESHOLD | 600 | 15M trades above this STC are shadow-only |
| STC_EXTENDED_LIVE_FLOOR | 300 | 300-600s zone: per-asset higher floors apply |
| STC_EXTENDED_BTC_MIN_PRICE | 93 | BTC floor for 300-600s (93c+ = 98.1% WR, n=52) |
| STC_EXTENDED_ETH_MIN_PRICE | 90 | ETH floor for 300-600s (same as main floor) |
| STC_EXTENDED_SOL_MIN_PRICE | 95 | SOL floor for 300-600s (95c+ = 100% WR, n=14) |
| STC_EXTENDED_XRP_MIN_PRICE | 92 | XRP floor for 300-600s (same as main floor) |
| SOL_LOW_ENTRY_STC_GATE | True | Block SOL ≤85c at STC≥300s (78.3% WR -$289; <300s is 100% WR +$228) |
| HWM_SPIKE_ALERT_ENABLED | env-default `0` | Telegram send for HWM spike-rejection alert; muted 2026-05-31 (balance bounces spam channel while not trading; logging.warning still fires) |
| HIGH_PRICE_STC_BLOCK_ENABLED | env-default `0` | 96¢ × {SOL,XRP} × 2-5min STC strategy-aware filter; saves $895/30d |
| HIGH_PRICE_STC_BLOCK_ASSETS | {SOL, XRP} | Cell scope; BTC/ETH 96¢ profitable, untouched |
| HIGH_PRICE_STC_BLOCK_PRICE_CENTS | 96 | Exact match — DO NOT widen, see KB |
| HIGH_PRICE_STC_BLOCK_STC_LO_S | 121 | STC inclusive lower bound |
| HIGH_PRICE_STC_BLOCK_STC_HI_S | 300 | STC inclusive upper bound |
| HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES | {decided_t2, decided_t2_z2, decided_t2_z25, MAKER_PATIENT} | Strategies dropped within cell; wins (TM-96, TAKER_NOW, decided_t1*) preserved |
| TM98_HIGHPRICE_BLEED_BLOCK_ENABLED | env-default `0` | {BTC,ETH,XRP} × TM-98 × 97-98¢ × 121-300s STC; -$980/30d projected (R-bleed-1) |
| TM98_HIGHPRICE_BLEED_BLOCK_ASSETS | {BTC, ETH, XRP} | SOL TM-98 NOT catastrophic, untouched |
| TM98_HIGHPRICE_BLEED_BLOCK_PRICE_LO | 97 | Covers maker fill 1c below TM trigger |
| TM98_HIGHPRICE_BLEED_BLOCK_PRICE_HI | 98 | TM-99 NOT blocked (profitable per 14d) |
| TM98_HIGHPRICE_BLEED_BLOCK_STC_LO_S | 121 | Lower bound of 2-5min danger zone (inclusive) |
| TM98_HIGHPRICE_BLEED_BLOCK_STC_HI_S | 300 | Upper bound of 2-5min danger zone (inclusive) |
| TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES | {terminal_momentum_98} | Strategy-aware; TM-99 in same range = profitable, untouched |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_ENABLED | env-default `0` | SOL × TAKER_NOW × 85-89¢ × 121-300s STC; -$782/30d projected (R-bleed-1) |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_ASSETS | {SOL} | Other assets do not have this bleed pattern |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_LO | 85 | Near-asset-floor thin-buffer disaster zone |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_PRICE_HI | 89 | 90+¢ TAKER profitable, untouched |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_LO_S | 121 | Lower bound of 2-5min danger zone (inclusive) |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_STC_HI_S | 300 | Upper bound of 2-5min danger zone (inclusive) |
| SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES | {TAKER_NOW} | MAKER cohorts not blocked |
| SOL_BLEED_V2_BLOCK_ENABLED | env-default `0` | Supersedes SOL_TAKER_LOWPRICE; SOL × {TAKER_NOW, MAKER_PATIENT} × 88-93¢ × 121-300s; +$200/30d expected (ticket 86b9vqt3f, May 10) |
| SOL_BLEED_V2_BLOCK_ASSETS | {SOL} | Same asset scope as v1; SOL is the calibration-pocket asset |
| SOL_BLEED_V2_BLOCK_PRICE_LO | 88 | Cell drifted up post-v2 (May 5 cross-asset deploy); 85-87¢ remains productive |
| SOL_BLEED_V2_BLOCK_PRICE_HI | 93 | Recent catastrophic-tail trades extend to 92-93¢; 94+¢ profitable per 30d data |
| SOL_BLEED_V2_BLOCK_STC_LO_S | 121 | Same 2-5min danger zone; sub-2min has different bleed shape |
| SOL_BLEED_V2_BLOCK_STC_HI_S | 300 | 5/9 KXSOL082215 weekend_discount @ 361s correctly excluded from gate |
| SOL_BLEED_V2_BLOCK_STRATEGIES | {TAKER_NOW, MAKER_PATIENT} | bot/executor.py force-routes ALL SOL through `sol_taker_override` regardless of label; widened beyond v1's TAKER_NOW-only filter. MAKER_AGGRESSIVE / weekend_discount / overnight_discount / decided_t1/t2 are productive — NOT blocked |
| BINANCE_FEED_ENABLED | env-default `0` | US-VPS HTTP-451 geoblocked; CROSS_EXCHANGE_CONSENSUS_MIN auto-lowers to 2 (Kraken+Bybit) when off |
| SYNTHETIC_RTI_ENABLED | env-default `0` (OFF) | (Ticket 86ba64h2w, B2b-1, 2026-05-28) Kill-switch for the in-bot 4-venue (Coinbase/Kraken/Bitstamp/Gemini) L2 → CFB-shape synthetic RTI shadow feed (`bot/feeds/synthetic_rti_feed.py`). **SHADOW-ONLY** — logged to `evaluated_opportunities.rti_synthetic/rti_constituent_count/rti_confidence` for the Bit-3 retrain corpus; feeds a trade decision ONLY for assets in `SYNTHETIC_RTI_LIVE_ASSETS` (RTI-6 per-asset go-live gate; default EMPTY ⇒ shadow for every asset). When OFF (default) the feed opens no WS sockets / spawns no threads (`start()` no-ops) → zero scan-latency footprint. When ON it adds 4 L2 WS connections + a sampler daemon (compute off the scan hot path). NEVER flip the live signal before the Bit-4 shadow-validation gate. See `kb/decisions/b2b-1-core-shadow-plan.md`. |
| SYNTHETIC_RTI_LIVE_ASSETS | `set()` (empty) | (RTI-6, umbrella 86ba6hdqr) Per-asset go-live gate: assets here use the synthetic RTI as the decision spot (via `OpportunityScanner._effective_decision_spot` + `OrderExecutor._addon_decision_spot`) instead of Coinbase. Default EMPTY ⇒ zero behavior change (the B2b-1 shadow invariant holds for all). Promote an asset ONLY after it clears the RTI-3 beats-market Brier gate + the RMSE gate; mirrors the per-asset `MARKET_BLEND_W_BY_ASSET` pattern. |
| RTI_LIVE_MIN_CONFIDENCE | 0.75 | (RTI-6) Min `rti_confidence` for a synthetic value to be trusted as the decision spot; below this the scanner/executor fall back to Coinbase. `rti_confidence` = contributed venues / expected venues, where a venue counts only if its book is fresh, in-sync (Kraken CRC32), rebuilt since that venue's last (re)connect, two-sided (a book missing either side is excluded as `one_sided`) and not crossed — a COVERAGE ratio over healthy books, not a probability. It still cannot see a silently-wrong book that is neither crossed nor stale on the three venues carrying no checksum (Coinbase/Bitstamp/Gemini). **UNVALIDATED placeholder** — must be tuned from the RTI-3 corpus before any promotion. **Ticket `86bbvztem`: any `rti_confidence` written before the reconnect book-epoch fix is NOT comparable to a post-fix value and must NOT be pooled with it** — pre-fix, a reconnecting Gemini kept contributing a stale book, so confidence read marginally HIGHER on corrupt rows than clean ones and this gate actively preferred bad data. Note the fix is coverage-monotone-decreasing (both new checks can only remove venues), so post-fix confidence sits LOWER and 0.75 is correspondingly harder to clear. |
| STC_SIZING_SCALER_KNEE | 300 | Seconds — start scaling contracts by 300/STC above this |
| STC_SIZING_SCALER_ENABLED | True | Universal STC scaler: contracts *= 300/STC for 15M at STC>300s |

## Terminal Momentum sizing

| Config | Value | Notes |
|--------|-------|-------|
| TM_BASE_CONTRACTS | 100 | Base multiplier for margin-proportional sizing (ct = BASE × margin × stc_mult × buf_mult) |
| TM_MIN_CONTRACTS | 25 | Floor — always collect data |
| TM_MAX_CONTRACTS | 500 | Hard ceiling — caps the buf-multiplier upside |
| TM_THIN_BUFFER_PCT | 0.20 | Below this buf_pct%, apply TM_THIN_BUFFER_CONTRACT_CAP (BACKSTOP) |
| TM_THIN_BUFFER_CONTRACT_CAP | 50 | 50ct cap at buf<0.20% — bounds catastrophic-tail (Apr 23 ETH -$178 motivating loss) |
| TM_BUFFER_SIZE_MULTIPLIER | ((0.00,1.0),(0.20,1.0),(0.40,2.0),(0.80,3.0)) | Sim B (2026-05-19, ticket 86ba0v6z1). Wide-buffer scale-up: 0.40-0.80% → 2× ($+1.40/ct realized); ≥0.80% → 3× ($+1.67/ct realized). Thin band kept 1× (cap binds). Per-asset risk caps + TM_MAX_CONTRACTS still bound upside |
| TM_SHADOW_KELLY_FRACTION | 0.50 | Sim C (2026-05-19, ticket 86ba0v7fc). Half-Kelly multiplier applied to `cal_mlp_p_mean`-derived raw Kelly when computing the SHADOW-ONLY counterfactual Kelly size logged to `evaluated_opportunities.tm_shadow_kelly_*`. Quarter-Kelly was over-conservative in 30d counterfactual ($-49 vs actual $+125); half-Kelly was the data-justified choice ($+183 vs actual $+125, +$57 delta). NEVER consumed by production sizing. See `kb/decisions/tm-half-kelly-shadow-plan.md`. |
| TM_SHADOW_KELLY_ABS_LOSS_BOUND_CENTS | 10000 | Sim C (2026-05-19, ticket 86ba0v7fc). $100 absolute-loss bound on the shadow Kelly size — caps catastrophic-tail. At 99c entry, 10000/99 ≈ 101 ct. Mirrors the empirical loss-distribution constraint motivating `TM_THIN_BUFFER_CONTRACT_CAP=50` (Apr 1-23: 8/14 TM losses ≥100ct at sub-0.20% buffer; the abs-bound caps each at ~$100). NEVER consumed by production sizing. |

## Per-asset risk

| Config | Value | Notes |
|--------|-------|-------|
| XRP_MAX_RISK_PER_TRADE | 0.15 | XRP: 15% per-trade (was 12%) |
| BTC_MAX_RISK_PER_TRADE | 0.15 | BTC: 15% per-trade (was 12%) |
| HYPE_MAX_RISK_PER_TRADE | 0.10 | HYPE: 10% per-trade (P2.3 2026-05-14 live promotion, conservative new-asset default) |
| DOGE_MAX_RISK_PER_TRADE | 0.10 | DOGE: 10% per-trade (P2.3 2026-05-14 live promotion, conservative new-asset default) |
| SOL_MIN_EDGE | 0.010 | SOL-specific edge floor (>=1.0% = 94.2% WR n=258; <1.0% drops to 82%) |
| SOL_TAKER_FIRST | True | SOL bypasses maker entirely, direct IOC at all STC |
| SOL_RESCUE_CONTRACT_CAP | 25 | SOL rescue sizing clamp |

## Loss cooldown / LPNE / monitor

| Config | Value | Notes |
|--------|-------|-------|
| LOSS_COOLDOWN_ENABLED | True | Per-asset 2h 15M lockout after any loss (+$441/30d counterfactual) |
| LOSS_COOLDOWN_SECONDS | 7200 | 2-hour cooldown — first-ship per-asset (conservative) |
| LPNE_ENABLED | True (env var) | BTC 80-87c near-expiry overlay (STC 10-120s, prob >= price/100) |
| LPNE_FIXED_CONTRACTS | 50 | Fixed sizing for LPNE |
| LPNE_MAX_CONCURRENT | 2 | Max simultaneous LPNE positions |
| POSITION_PRICE_MONITOR_ENABLED | True | Post-entry price logging via WS (change-only dedup) |

## Execution

| Config | Value | Notes |
|--------|-------|-------|
| IOC_TICKER_COOLDOWN | 15 | Seconds cooldown per ticker after IOC attempt (was 60) |
| IOC_RETRY_OFFSET | 1 | Cents above ask for taker-first IOC + retry offset |
| DIP_ADDON_ENABLED | False | Killed — 55.2% WR, no edge |
| SETTLEMENT_PNL_DIVERGENCE_THRESHOLD_CENTS | 50 | B4 (ticket `86b9zudcc`, 2026-05-18). At settlement Telegram alert time, `SettlementTracker._process_settlement` compares `balance_delta` (post − pre `client.get_balance()`) against the locally-expected credit (`aggregate_count × 100` on WIN, `0` on LOSS — only `revenue` moves cash at settle; cost/fee were debited at fill). When `abs(expected_credit − balance_delta) > 50¢`, logs `SETTLEMENT_PNL_DIVERGENCE` + appends ⚠️ `KALSHI_DELTA=` tag to the alert. Catches WIN-side phantom-count bugs the existing single-row count-mismatch check inside `_process_settlement` (the `if revenue > 0 and outcome == "WIN" and side == "yes":` block) doesn't auto-correct — multi-row stacked positions land in the `else:` arm that emits `SETTLEMENT_MULTI_MISMATCH: %s — NOT auto-correcting stacked positions` and falls through with the inflated `aggregate_count` intact. LOSS-side phantoms are structurally invisible here — cash moves $0 at LOSS settle — and are caught by `scripts/audit/phantom_pnl_audit.py` retroactively. |

## Decided contracts

| Config | Value | Notes |
|--------|-------|-------|
| DECIDED_T1_ENABLED | True | Overlay: z≤-5, any price (env var) |
| DECIDED_CONTRACT_Z_T1B | -4.0 | T1B z-score threshold |
| DECIDED_CONTRACT_T1B_MIN_PRICE | 95 | T1B minimum price in cents |
| DECIDED_T1B_ENABLED | True | Overlay: z≤-4, 95c+ (env var) |
| DECIDED_T2_ENABLED | True | Overlay: z≤-3, 93-96c (env var) |
| DECIDED_T2_Z25_ENABLED | True | Overlay: z≤-2.5, 93-96c (7/7 WR) |
| DECIDED_T2_Z2_ENABLED | False | SHADOWED Apr 1 — z∈[-2.5,-1.75], -$313 on 47 trades |
| DECIDED_CONTRACT_Z_T2_Z2 | -1.75 | Expanded from -2.0 pre-shadow |
| DECIDED_CONTRACT_RISK | 0.20 | Fixed 20% bankroll for T1/T1B/T2 |
| DECIDED_CONTRACT_T2_Z25_RISK | 0.10 | T2-Z25 cut from 0.20 Apr 21 (14d bleed -$95) |
| DECIDED_CONTRACT_T2_Z2_RISK | 0.20 | Moot — shadowed |
| DECIDED_CONTRACT_MAX_WINDOW_RISK | 0.35 | Hard cap across all DC signals in one window |
| SOL_DC_RISK_TIERS | [(97, 0.05), (95, 0.10)] | SOL price-tiered DC risk: ≥97c→5%, 95-96c→10%, <95c→20% |

Canonical reference: `kb/concepts/dc-strategy.md`

## Discount strategies

| Config | Value | Notes |
|--------|-------|-------|
| OVERNIGHT_DISCOUNT_LIVE | True | Weekday 04-11 UTC live (kill switch) |
| OVERNIGHT_DISCOUNT_MIN_PRICE | 89 | Cents — 89c+ floor for live |
| OVERNIGHT_DISCOUNT_MAX_STC | 600 | STC gate for live |
| WEEKEND_DISCOUNT_LIVE | True | Sat/Sun live |
| WEEKEND_DISCOUNT_MIN_PRICE | 90 | Cents (raised from 89 to match ETH floor) |
| WEEKEND_DISCOUNT_MAX_STC | 600 | STC gate for live |
| WEEKEND_EDGE_DISCOUNT | 0.60 | 40% edge reduction applied on weekends |

## Low-Price Shadow (observation-only; Phase C of shadow coverage expansion 2026-05-02)

`low_price_shadow` is shadow-only data collection — never affects live trades. `MIN_ENTRY_PRICE` (live floor) is unchanged. See `kb/decisions/shadow-coverage-expansion-may01.md`.

| Config | Value | Notes |
|--------|-------|-------|
| LOW_PRICE_SHADOW_ENABLED | True | Master kill switch |
| LOW_PRICE_SHADOW_MIN_PRICE | 20 | Floor (was 70 pre-Phase-C; 20 leaves room for far-from-BE training data) |
| LOW_PRICE_SHADOW_MAX_PRICE | 79 | Ceiling (80c+ already live for some assets) |
| LOW_PRICE_SHADOW_MAX_STC | 900 | Full scan-window (was 600; captures entire decision life) |
| LP_MAX_RISK_PER_TRADE | 0.10 | Capped sizing — 10% bankroll cap |
| LP_KELLY_FRACTION | 0.25 | Capped sizing — quarter-Kelly |
| LP_WINDOW_CAP | 2 | Max signals per 15M window (correlation cap) |
| LP_HOUR_CAP | 4 | Max signals per hour (correlation cap) |

## Hourly (DISABLED Apr 18 — both kill switches 0)

To re-enable: set `HOURLY_LIVE_ENABLED=1` (YES) and/or `HOURLY_NO_SIDE_LIVE=1` (NO) in VPS .env + restart.

| Config | Value | Notes |
|--------|-------|-------|
| HOURLY_LIVE_ENABLED | env var (0 on VPS) | Kill switch for YES-side |
| HOURLY_OBSERVATION_ONLY | not HOURLY_LIVE_ENABLED | Derived from kill switch |
| HOURLY_MAX_ENTRY_PRICE | 59 | Sub-60c only — edge lives at low prices, 70-79c death zone |
| HOURLY_BANKROLL_FRACTION | 0.10 | Hourly sizes off 10% of balance |
| HOURLY_FIXED_CONTRACTS | 25 | Fixed sizing — bypass Kelly entirely |
| HOURLY_MAX_EDGE | 0.05 | Reject >5% edge (10%+ zone has 24.2% WR — edge inversion) |
| HOURLY_NO_SIDE_LIVE | env var (0 on VPS) | Kill switch for NO-side |
| HOURLY_NO_MIN_PRICE | 40 | Minimum NO entry price (cents) |
| HOURLY_NO_MAX_PRICE | 54 | Maximum NO entry price (cents) |
| HOURLY_NO_FIXED_CONTRACTS | 1 | Flat 1-contract — verification mode |
| HOURLY_NO_KILL_THRESHOLD | -2000 | Auto-disable if cumulative hourly NO PnL < -$20 |
| HOURLY_TAKER_ONLY | True | IOC only — no maker orders |
| HOURLY_MARKET_BLEND_W | 0.40 | Optimal Brier per 134K simulation |
| HOURLY_MIN_ENTRY_PRICE | 50 | Floor for data collection |
| HOURLY_MAX_RISK_PER_TRADE | 0.15 | 60% of 15M's 0.25 |
| HOURLY_TEMPERATURE_T | 1.45 | Softens overconfident probs: 95%→88.4% |
| HOURLY_KELLY_FRACTION | 0.25 | Quarter-Kelly (unused — fixed sizing active) |
| HOURLY_CALIBRATION_ENABLED | False | Engine disabled — passthrough + T=1.45 |
| HOURLY_MIN_STC_ENTRY | 600 | 10 min minimum (5-10m zone 56.5% WR — too thin) |
| HOURLY_MAX_STC_ENTRY | 1800 | 30 min maximum (25-30m sweet spot at 69.4% WR) |
| HOURLY_EXCLUDED_ASSETS | {SOL, XRP, HYPE, DOGE, BNB, ADA, BCH, NEAR, ZEC} | BTC+ETH only — XRP 42.9% WR (toxic), SOL marginal; HYPE/DOGE/BNB/ADA/BCH/NEAR/ZEC excluded per the 15M-only onboarding design (lock-step with `market_config.py` hourly `excluded_assets`, startup-asserted) |
| HOURLY_MAX_POSITIONS_PER_WINDOW | 2 | Limit correlated exposure |
| HOURLY_MAX_WINDOW_RISK | 0.15 | Max aggregate risk per hourly window |

## SPX Hourly (observation)

| Config | Value | Notes |
|--------|-------|-------|
| SPX_HOURLY_OBSERVATION_ONLY | True | Reverted Mar 17 — Polygon 403 broke vol engine |
| SPX_HOURLY_MIN_ENTRY_PRICE | 90 | Cents (SPX-C: 90.9% WR at 90c+) |
| SPX_HOURLY_MAX_ENTRY_PRICE | 99 | Cents |
| SPX_HOURLY_MARKET_BLEND_W | 0.00 | No blend — CalEngine calibration only (SPX-D) |
| SPX_HOURLY_TEMPERATURE_T | 1.0 | No temperature correction yet |
| SPX_HOURLY_KELLY_FRACTION | 0.125 | Eighth-Kelly: ultra-conservative |
| SPX_HOURLY_MAX_RISK_PER_TRADE | 0.10 | Conservative (down from 0.15) |
| SPX_HOURLY_FEE_MULTIPLIER_TAKER | 0.035 | Finance category — half of crypto's 0.07 |
| SPX_HOURLY_FEE_MULTIPLIER_MAKER | 0.0 | Kalshi charges $0 on maker fills |
| SPX_HOURLY_BANKROLL_FRACTION | 0.15 | SPX sizes off 15% of total balance |
| SPX_HOURLY_MAX_POSITIONS_PER_WINDOW | 2 | Prevent correlated multi-strike blowups |
| SPX_HOURLY_MAX_WINDOW_RISK | 0.15 | Max aggregate risk per SPX window |

## Weather

| Config | Value | Notes |
|--------|-------|-------|
| WEATHER_OBSERVATION_ONLY | True | Observation-only — collecting ensemble data |
| WEATHER_MIN_ENTRY_PRICE | 10 | Cents |
| WEATHER_MAX_ENTRY_PRICE | 99 | Cents |
| WEATHER_MARKET_BLEND_W | 0.20 | 80% model, 20% market |
| WEATHER_MIN_EDGE_PCT | 0.001 | 0.1% — observation-only |
| WEATHER_MAX_RISK_PER_TRADE | 0.10 | Conservative sizing |
| WEATHER_KELLY_FRACTION | 0.25 | Quarter-Kelly |
| WEATHER_MIN_SECONDS_BEFORE_CLOSE | 3600 | At least 1 hour before settlement |
| WEATHER_MAX_SECONDS_BEFORE_CLOSE | 86400 | Weather settles daily — always eligible |
| WEATHER_NO_SIDE_LIVE | False | KILLED 2026-05-16 (ce8e2d2). Lifetime n=167, 38.3% WR vs 70% assumed prior (Wilson 95% CI [23.6%, 47.0%]) empirically falsified. Near-ATM zone (NO 39-40c ↔ YES 60-61c) is market-maker zone with no edge. `bracket_no_live` (far-ITM NO 4-12c, 91.7% WR) is where NO edge actually lives. Re-research underway: see ClickUp Weather Initiative folder `90149436180`. |
| WEATHER_NO_MIN_PRICE | 39 | Tightened May 2 from 37 — 37c/38c sub-bands bleeding (25%/14.3% WR), 39-40c band profitable |

## Calibrator (P2 cal_mlp)

| Config | Value | Source | Notes |
|--------|-------|--------|-------|
| `SIGMA_WINSOR_ABS_CAP` | 25.0 | `scripts/cal_mlp/features.py` | Clip for `spot_distance_to_strike_sigma` at extract + post-hoc + sync-gate. Below empirical max benign (~19); above the 30+ outlier tail. Captured in cfg_fp; changing it changes the bundle fingerprint. |
| `GLOBAL_MIN_ENTRY_PRICE` | 75 | `features.py` | Floor when `--include-sub-floor` is set. Per-asset floors (88/90/86/92) used otherwise. |
| `INCLUDE_SUB_FLOOR` (env) | 1 | `scripts/cal_mlp/run_pipeline.sh` | Default ON. Pulls 75¢-MIN_ENTRY-1¢ shadow rows into v2/v3 training. |
| `RAW_PROB_CLIP_EPS` | 1e-6 | `features.py` | Logit clipping for the skip term. |
| `SPOT_BUFFER_PERSIST_PATH` | `state/spot_buffer.json` | `bot/constants.py` | 30-min spot price buffer persisted to disk every 30s. |
| `SPOT_BUFFER_PERSIST_INTERVAL_S` | 30 | `bot/constants.py` | Flush cadence. Runs on a dedicated `CoinbaseFeed._sampler_loop` daemon thread (post-D2.3 2026-05-17, ticket `86b9zkppt`) so disk I/O doesn't block the `coinbase_wire.WSClient` asyncio event loop. |
| `PRICE_BUFFER_SIZE` | 1800 | `bot/constants.py` | 30 min @ 1s sampling. |

## Volatility engine (Bit V.2, 2026-06-12)

| Config | Value | Notes |
|--------|-------|-------|
| DERIBIT_DVOL_CURRENCIES | `{"BTC": "BTC", "ETH": "ETH"}` | The ONLY assets with implied vol. **Bit V.2 (2026-06-12) killed the beta-scaled DVOL path**: `_get_implied_vol`/`_get_implied_vol_hourly` now return `None` for every asset outside this set (previously `btc_dvol × _estimate_beta(asset)` — Epps-effect vol-ratio understatement + index-vs-time alignment bug + 0.5 clamp floor deflated alt `blended_rv` up to ~6x; cross-asset-identical ~8.5e-5 cluster, 2026-06-12 incident). Alts take the rv/EGARCH fallback. |
| BETA_LOOKBACK_RETURNS | 60 | DEAD CODE consumer post-Bit-V.2 — only `_estimate_beta` reads it, which has no production caller (deletion is a follow-up Bit). |
| ~~IV_RV_SPREAD_THRESHOLD~~ | DELETED | **Deleted in Bit V.4 (2026-06-12)** along with both IV blend branches (`inverse_variance` + `stress_override`): the stress trigger was a level-spread that VRP + the diurnal trough satisfy every quiet evening (fired 60-91% of BTC/ETH ticks on the 2026-06-12 soak night, honesty band breached 1.8-2.7x), and the inverse-variance weights measured QLIKE-negative on 9 days of journal counterfactuals. DVOL is diagnostic-only — never blended into sigma; BTC/ETH take the same rv/EGARCH path as alts. Regression lock `tests/integration/test_vol_engine_iv_diagnostic_only_regression.py`. |

## External market data poller

| Config | Source | Notes |
|--------|--------|-------|
| OKX funding + OI | `https://www.okx.com/api/v5/public/{funding-rate,open-interest}` | Switched from Binance.com (HTTP 451 from US). 4 symbols × 2 endpoints. |
| Deribit DVOL | `https://www.deribit.com/api/v2/public/get_index_price?index_name={btcdvol_usdc,ethdvol_usdc}` | BTC + ETH only. |
| Poll interval | 60 s | `scripts/backfill/external_market_poller.py --once` cron. |
| Stale threshold | 900 s (15 min) | Above Deribit's typical weekly maintenance window (~10 min). |
| Process-start grace | 900 s | Suppresses NEVER-stale alerts in cron mode for the first 15 min. |

## B1 composite adverse-selection gate (ClickUp 86ba1zdwm, 2026-05-21)

Two independent gates protecting against catastrophic 15M losses diagnosed from a 7d -$235.83 PnL investigation. Full design: `kb/decisions/b1-orderbook-prior-gate-plan.md`. Constants live in `bot/constants.py` (post-Bit-3.1 canonical home). R0 sim row "COMPOSITE (entry>=98 only): ob OR HYPE-only buf<0.75" (14d window): +$355 net retention, blocks 5/13 catastrophic losses (~38%), 14% high-95c winner block. The 14% is the R0-sim winner-block rate measured within the entry>=98 composite-gate sweep — see plan-doc Gate sim results table for the full sweep. Gate A is asset-agnostic at entry>=90c (catches Class A "orderbook disagrees"); Gate B is HYPE-only at entry>=98c (catches Class B "CFB RTI divergence" measurement-noise).

### Gate A — orderbook-prior (asset-agnostic, entry >= 90c)

| Constant | Value | Data justification |
|---|---|---|
| `ORDERBOOK_PRIOR_GATE_ENABLED` | `True` | Kill-switch; True = trade-block when gate fires. Shadow rows log regardless (TM96 R-p7-deploy-r10 precedent). |
| `ORDERBOOK_PRIOR_GATE_MIN_ENTRY_CENTS` | `90` | R0 sim measured entry >= 90c only; sub-90c is out-of-scope. |
| `ORDERBOOK_PRIOR_GATE_MIN_DISAGREE` | `0.05` | Strict `>`. Bot's cal_p must beat market floor `(100-no_ask)/100` by more than 5pts. At 0.02 catches 1 extra catastrophic but blocks 130+ extra winners (net negative). |
| `ORDERBOOK_PRIOR_GATE_MIN_CONVICTION_CENTS` | `500` | Strict `>`. Sum of `depth * no_bid_price` for NO bids at price >= 2c. At `> 200` blocks 13 more winners for marginal gain. At `> 1000` misses 1 catastrophic. |
| `ORDERBOOK_PRIOR_GATE_MIN_NO_BID_PRICE` | `2` | Drop 0-1c market-maker liquidity bids that exist on every contract (signal-free). |
| `ORDERBOOK_PRIOR_GATE_FILTER_STAGE` | `"orderbook_prior_block"` | DB filter_stage literal. Registered in `bot.helpers.cohort_attribution.COHORT_PARTITION_STAGES`. |

### Gate B — HYPE high-price buf (HYPE-only, entry >= 98c)

| Constant | Value | Data justification |
|---|---|---|
| `HYPE_HIGH_PRICE_BUF_GATE_ENABLED` | `True` | Kill-switch. Retires when B2 (ticket `86ba1zf5j`) ships multi-venue synthetic RTI. |
| `HYPE_HIGH_PRICE_BUF_GATE_MIN_ENTRY_CENTS` | `98` | R0 sim showed 98c is where asymmetric risk dominates. At 95c retains $235 but blocks 72% of winners (net negative). |
| `HYPE_HIGH_PRICE_BUF_GATE_MIN_BUF_PCT` | `0.75` | Strict `<`. HYPE p99 positive-divergence = 76.6 bps (per R0 sim, signed positive direction = bot's loss direction). All 4 HYPE losers at entry>=95c in 14d had buf<0.50%. 0.75 is conservative bound including p99 noise. |
| `HYPE_HIGH_PRICE_BUF_GATE_FILTER_STAGE` | `"hype_high_price_buf_block"` | DB filter_stage literal. Registered in `COHORT_PARTITION_STAGES`. |

HYPE-only because HYPE has the widest per-asset feed divergence (single-venue Coinbase blindspot — HYPE doesn't trade on Kraken/Bitstamp/Gemini). Per-asset p99 divergence from settlement_journal × evaluated_opportunities join: BTC 33.1 bps, ETH 33.3 bps, SOL 45.5 bps, DOGE 46.9 bps, XRP 26.2 bps, HYPE **76.6 bps** (2x BTC). For non-HYPE assets, winners and losers have overlapping buf distributions — buf gates for them are net-negative in the R0 sim.
