---
clickup: 86b9zmj0c
umbrella: 86b9zmhyk
status: t1-in-progress
date: 2026-05-17
discipline: RCA → TDD → adversarial review (2 consecutive 0C/0M)
precedent: agent_docs/doge-hype-t1-plan-may10.md (HYPE/DOGE T1 SHIPPED 2026-05-10, 2 adv rounds)
---

# T1 Plan: BNB onboarding (shadow observation)

## Goal

Activate BNB 15M + hourly observation. Bot subscribes to Coinbase/Kalshi feeds, runs full scan→evaluate pipeline, writes diagnostic rows to `evaluated_opportunities`, but submits **zero live orders**. Data accumulates ~3–4 wk → T3 calibration sweep → T4 promotion.

Mirrors `agent_docs/doge-hype-t1-plan-may10.md` with site refs re-anchored to current HEAD `5ad86f6` (post-Bit-9.3-iii.c, post-Sprint-10 sibling-reorg, post-D1.6 collector health monitor, post-P4.1 band-calibrated sizing).

## Pre-flight verified 2026-05-17

- Kalshi `KXBNB15M` 15M markets: LIVE (status=active, volume_fp=267.44 on KXBNB15M-26MAY171600, fractional_trading_enabled=true)
- Kalshi `KXBNBD` hourly markets: LIVE (status=active, multiple strikes per event)
- Coinbase `BNB-USD`: LIVE (status=online, trading_disabled=false, margin_enabled=false)
- Resolution source: CF Benchmarks `BNBUSDRTI` (15m) / `BNBUSD_RTI` (hourly) — same precedent as BTC/ETH/SOL/XRP/HYPE/DOGE
- Price level structure: 15M = `tapered_deci_cent` (matches other 15M markets), hourly = `linear_cent`

## Atomic activation principle

T1 is ONE commit. Splitting "registry edits" from "shadow gate wiring" breaks the safety invariant — the moment `bot/config.py:ASSETS` contains `"BNB"`, the scan loop iterates it. Without the shadow gates in the same commit, the elif chain could fall through to scaffolded defaults and submit real orders.

**Edit order within the commit (worst-case-partial-revert = inert state):**
1. Define `BNB_15M_SHADOW = True` in `bot/constants.py` near :81 (after `DOGE_15M_SHADOW`)
2. Extend `HOURLY_EXCLUDED_ASSETS` :358 to `{"SOL", "XRP", "HYPE", "DOGE", "BNB"}`
3. Extend `HOURLY_NO_EXCLUDED_ASSETS` :369 to `{"HYPE", "DOGE", "BNB"}`
4. Wire `BNB_15M_SHADOW` into `bot/scanner/__init__.py` (imports + YES-side gate + NO-side elif + 4 strategy kill-switches)
5. Extend `bot/state.py` SQL backfill + inline product_type tuples
6. Extend `bot/executor.py:3162 _HOURLY_SERIES_PREFIXES`
7. Extend `market_config.py:144` to match new HOURLY_EXCLUDED_ASSETS (startup-assertion mirror at :318)
8. Extend `bot/constants.py` registries: SERIES_TICKERS, HOURLY_SERIES_TICKERS, COINBASE_PRODUCTS
9. Extend all downstream lists/tuples (dashboard, supabase, snapshot, bot/ai/auditor.py, fifteenm_shadow, scripts)
10. Update test fixtures + strict-equality assertions
11. **LAST: extend `bot/config.py:ASSETS = ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"]`** — activation lever
12. Doc-drift

If anyone reverts the activation commit, partial state from steps 1–10 is inert (no asset routes through them yet because ASSETS isn't extended).

## RCA findings (BNB-specific, post-HYPE/DOGE-precedent)

### Finding 1: 4-strategy escape-path class is unchanged — kill-switch clauses required

The HYPE/DOGE T1 R1 CRITICAL applies verbatim. The `XRP_15M_SHADOW` gate at `bot/scanner/__init__.py:5975` fires AFTER:
- Terminal Momentum (TM) at `:3362-3366` (already has HYPE/DOGE kill-switches)
- Weekend Discount (WKND) at `:3845-3848`
- Overnight Discount (OVN) at `:4011-4015`
- Decided Contracts (DC) at `:4286`

Each strategy must get explicit `and not (BNB_15M_SHADOW and asset == "BNB")` (or DC's `_dc_live_enabled = False` clear-block). Pin via source-walk regression test mirroring `TestStrategyKillSwitchClauses`.

**Why it still matters now even though HYPE/DOGE shadow flags are False (post-T4):** the kill-switch clauses are preserved as REVERT levers — flipping any `*_15M_SHADOW = True` reverts that asset from those 4 strategies' live routing. The BNB clauses serve the same dual role: pre-T4 = active gate, post-T4 = preserved kill-switch.

### Finding 2: market_config.py startup-assertion lock-step

`market_config.py:144` hardcodes `excluded_assets=frozenset({"SOL", "XRP", "HYPE", "DOGE"})`. The `:318` assertion will crash startup on mismatch. Must update in lock-step. CLAUDE.md rule encodes this.

### Finding 3: Two strict-equality test assertions break on T1

```python
# tests/integration/test_scan_pipeline.py:698
self.assertEqual(HOURLY_EXCLUDED_ASSETS, {"SOL", "XRP", "HYPE", "DOGE"})   # breaks on add
# tests/integration/test_scan_pipeline.py:719
self.assertEqual(HOURLY_NO_EXCLUDED_ASSETS, {"HYPE", "DOGE"})              # breaks on add
# tests/integration/test_hourly_15m_isolation.py:105
assert cfg.excluded_assets == frozenset({"SOL", "XRP", "HYPE", "DOGE"})    # breaks on add
```

Both/all three must update in lock-step. Acceptable: these tests assert "current state"; their natural update is to reflect the new state.

### Finding 4: BNB_HOURLY_SHADOW flag deliberately NOT added

Same rationale as HYPE/DOGE T1 Finding 3: hourly observation is already covered by adding BNB to `HOURLY_EXCLUDED_ASSETS` (full-diagnostic `evaluated_opportunities` rows with `filter_stage="hourly_asset_excluded"`). Adding a hourly_shadow flag with no wiring = dead code.

Net flag inventory for BNB T1: **1 flag only** (`BNB_15M_SHADOW=True`).

### Finding 5: cal_mlp training arc retired — T3 likely raw_prob+blend_w sweep

Per current state per CLAUDE.md: HYPE/DOGE T4 was promoted 2026-05-14 via "P2.3 raw_prob + per-asset MARKET_BLEND_W; cal_mlp training arc retired". T3 for BNB likely follows the raw_prob path — sweep settled corpus to choose `MARKET_BLEND_W_BY_ASSET["BNB"]` plateau and verify edge calibration. Recorded so T3 doesn't accidentally re-open the cal_mlp bundle path.

Out-of-scope for T1 (don't touch in this Bit). Filed under T3 ticket 86b9zmj2q.

### Finding 6: BNB has full external-feed coverage — T1.5 is simpler than HYPE

Unlike HYPE (Binance.US-only, no Binance.com listing), BNB is listed on Binance.com (BNBUSDT spot). T1.5 will likely be symmetric across {Binance, Kraken, Bybit, OKX} — verify each via public REST before adding. Skip `DERIBIT_DVOL_CURRENCIES` (Deribit DVOL = BTC/ETH options only).

This means BNB feature coverage by T3 should be at parity with BTC/ETH/SOL/XRP/DOGE (not asymmetric like HYPE). Important for cal_mlp parity if T3 reverts to cal_mlp path.

## Full site inventory (current HEAD `5ad86f6`, post-D1.6 post-P4.1)

### Required edits — `bot/`

| Site | File:line | Edit |
|---|---|---|
| ASSETS registry (**activation lever — LAST**) | `bot/config.py:46` | extend to `["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB"]` |
| SERIES_TICKERS dict | `bot/constants.py:26-`(line varies in dict body) | add `"BNB": "KXBNB15M"` |
| HOURLY_SERIES_TICKERS dict | `bot/constants.py:266-` (line varies in dict body) | add `"BNB": "KXBNBD"` |
| COINBASE_PRODUCTS dict | `bot/constants.py:712-` (line varies in dict body) | add `"BNB": "BNB-USD"` |
| HOURLY_EXCLUDED_ASSETS set | `bot/constants.py:358` | extend to `{"SOL", "XRP", "HYPE", "DOGE", "BNB"}` |
| HOURLY_NO_EXCLUDED_ASSETS set | `bot/constants.py:369` | extend to `{"HYPE", "DOGE", "BNB"}` (NO-side belt) |
| 15M shadow flag (NEW) | `bot/constants.py:80-81` | add `BNB_15M_SHADOW = True` after `DOGE_15M_SHADOW = False` |
| market_config.py hourly excluded | `market_config.py:144` | extend `excluded_assets=frozenset({"SOL", "XRP", "HYPE", "DOGE"})` to include "BNB" |
| Scanner imports | `bot/scanner/__init__.py` (HYPE_15M_SHADOW import region near :164) | add `BNB_15M_SHADOW` to imports |
| Scanner YES-side 15M shadow gate | `bot/scanner/__init__.py:5975-6037` region | add BNB block mirroring HYPE/DOGE (after DOGE block at :6037, before downstream code) |
| Scanner NO-side 15M shadow gate | `bot/scanner/__init__.py:7789-7793` | add `elif BNB_15M_SHADOW and asset == "BNB":` block |
| Scanner TM kill-switch | `bot/scanner/__init__.py:3365-3366` | add `and not (BNB_15M_SHADOW and asset == "BNB")` |
| Scanner WKND kill-switch | `bot/scanner/__init__.py:3847-3848` | add same |
| Scanner OVN kill-switch | `bot/scanner/__init__.py:4014-4015` | add same |
| Scanner DC kill-switch | `bot/scanner/__init__.py:4286` | extend the `or` clause with `or (BNB_15M_SHADOW and asset == "BNB")` |
| state.py SQL backfill (4 LIKE clauses) | `bot/state.py:941-948, 965-972` | extend each w/ `OR ticker LIKE 'KXBNB15M%'` (15m sites) and `OR ticker LIKE 'KXBNBD%'` (hourly sites) |
| state.py inline 15m categorizer | `bot/state.py:1472` | extend tuple w/ `"KXBNB15M"` |
| state.py inline hourly categorizer | `bot/state.py:1474` | extend tuple w/ `"KXBNBD"` |
| executor _HOURLY_SERIES_PREFIXES | `bot/executor.py:3162` | extend tuple w/ `"KXBNBD-"` |

### Required edits — snapshots + ai + shadows

| Site | File:line | Edit |
|---|---|---|
| Dashboard ASSETS list | `bot/snapshots/dashboard_snapshot.py:11` | extend |
| Dashboard _hourly_pfx tuple | `bot/snapshots/dashboard_snapshot.py:1498` | extend w/ `"KXBNBD"` |
| Dashboard inline ASSETS list | `bot/snapshots/dashboard_snapshot.py:3062` | extend |
| Supabase ASSETS | `bot/snapshots/supabase_sync.py:22` | extend |
| Supabase display-name triple | `bot/snapshots/supabase_sync.py:~324-327` (search the existing HYPE/DOGE block) | add `("BNB","Binance Coin","KXBNB15M")` |
| bot_state_snapshot _DEFAULT_ASSETS | `bot/snapshots/bot_state_snapshot.py:127` | extend |
| auditor active_assets | `bot/ai/auditor.py:272` | extend |
| analyst MARKET_BLEND_W_BY_ASSET doc-string | `bot/ai/analyst.py:65` | update inline registry-doc string to include BNB shadow note |
| analyst trades-on-assets paragraph | `bot/ai/analyst.py:668` | extend the asset list (textual only) |
| fifteenm_shadow DEFAULT_TEMPERATURES | `bot/shadows/fifteenm_shadow.py:49` | add `"BNB": 1.00` (neutral default) |
| fifteenm_shadow DEFAULT_BLEND_W | `bot/shadows/fifteenm_shadow.py:51` | add `"BNB": 0.50` (neutral default) |
| fifteenm_shadow DEFAULT_DEBIAS | `bot/shadows/fifteenm_shadow.py:53` | add `"BNB": 0.00` (neutral default) |
| fifteenm_shadow recalibration loops (4) | `bot/shadows/fifteenm_shadow.py:167, 764, 1485, 1553, 1584` | extend tuples (5 sites — all use the same 6-asset tuple) |
| fifteenm_shadow asset→idx map | `bot/shadows/fifteenm_shadow.py:620` | add `"BNB": 6` |

### Required edits — feeds + fetchers docstring drift

| Site | File:line | Edit |
|---|---|---|
| feeds/__init__.py docstring | `bot/feeds/__init__.py:9` | append "+ BNB post-T1 2026-05-17" |
| feeds/coinbase.py docstring | `bot/feeds/coinbase.py:5` | append "+ BNB post-T1 2026-05-17" |
| fetchers/__init__.py docstring | `bot/fetchers/__init__.py:12` | append "+ BNB post-T1 2026-05-17" |
| fetchers/coinglass.py docstring | `bot/fetchers/coinglass.py:6` | append "+ BNB post-T1 2026-05-17" |
| band_calibration docstring | `bot/helpers/band_calibration.py:198` | append "+ BNB" |
| test_feeds_fetchers_docstring_drift SIX_ASSETS | `tests/integration/test_feeds_fetchers_docstring_drift.py:37` | rename + extend to SEVEN_ASSETS adding "BNB" |

### Required edits — scripts (NOT yet T3, but inventory completeness)

| Site | File:line | Edit |
|---|---|---|
| `scripts/cal_mlp/conformal.py:402` argparse | extend `choices=['BTC',...,'DOGE']` to include 'BNB' | LOW priority (T3, but if not added scripts break for BNB) |
| `scripts/cal_mlp/train.py:595` argparse | same | LOW priority (T3) |
| `scripts/cal_mlp/validate.py:456` argparse | same | LOW priority (T3) |
| `scripts/vps_mcp_server.py:189, 556` docstring | extend asset list | docstring drift |

**Decision**: argparse choices are T3 surface. Extend them now (small, safe, lock-step with ASSETS) to avoid drift. Docstring drift in vps_mcp_server.py: extend.

### Required edits — tests (lock-step + new regression)

| Site | File:line | Edit |
|---|---|---|
| test_scan_pipeline.py HOURLY_EXCLUDED_ASSETS strict eq | `tests/integration/test_scan_pipeline.py:698` | update to new set |
| test_scan_pipeline.py HOURLY_NO_EXCLUDED_ASSETS strict eq | `tests/integration/test_scan_pipeline.py:719` | update to new set |
| test_hourly_15m_isolation.py frozenset assertion | `tests/integration/test_hourly_15m_isolation.py:105` | update to new set |
| **NEW** | `tests/integration/test_bnb_onboarding_t1.py` | mirror `test_doge_hype_onboarding_t1.py` w/ BNB-specific asserts |

### T1 explicitly DOES NOT touch (defer)

| Site | Defer rationale |
|---|---|
| `BNB_MIN_ENTRY_PRICE` / `BNB_MAX_RISK_PER_TRADE` per-asset constants + elif chains | T4 promotion (under shadow=True these never fire — generic defaults are operationally correct; matches HYPE/DOGE precedent) |
| `STC_EXTENDED_*_MIN_PRICE` for BNB | graceful `.get(asset, MAX_ENTRY_PRICE)` fallback; safe absent |
| `TM_ASSET_RISK_CAPS` for BNB | graceful `.get(asset, 0.15)` fallback |
| `NBBO_FALLBACK_GATES` for BNB | T4 — set values from observed spread distribution |
| `MARKET_BLEND_W_BY_ASSET` for BNB | T3 — value from BNB-corpus sweep |
| `cal_subtypes` in `market_config.py` | T3 — only matters if cal_mlp path revived for BNB |
| `CROSS_EXCHANGE_SYMBOLS["BNB"]` | T1.5 — Binance/Kraken/Bybit symbol verification needed |
| `COINGLASS_SYMBOLS["BNB"]` | T1.5 — verify CoinGlass coverage |
| `FUNDING_SYMBOLS` / `OI_SYMBOLS` in `scripts/backfill/external_market_poller.py` | T1.5 — OKX BNB-USDT-SWAP verification |
| `DERIBIT_DVOL_CURRENCIES` | Never — Deribit DVOL is BTC/ETH options only |
| `BNB_HOURLY_SHADOW` flag | Not needed (HOURLY_EXCLUDED_ASSETS covers — Finding 4) |
| log-format hardcoded asset lists in `models.py` / `bot/main_loop.py` / `bot/engines/volatility.py` | None found in current HEAD via grep — already ASSETS-driven post-Bit-9.x. **Confirmed by grep**, no refactor needed. |
| `bot/snapshots/dashboard_snapshot.py:2438 _HNO_ASSETS = ["XRP", "SOL"]` | Distinct list — Hourly-NO live assets, NOT all-assets. BNB stays out per HOURLY_NO_EXCLUDED_ASSETS belt (T1 = shadow). Do NOT extend. |

## NEW regression tests (TDD-RED-first) — `tests/integration/test_bnb_onboarding_t1.py`

Mirror `test_doge_hype_onboarding_t1.py` 1:1 with BNB-specific asserts. Test classes:

1. `TestTickerParserBnb` — `StateManager._asset_from_ticker("KXBNB15M-...")` → `"BNB"`, `"KXBNBD-..."` → `"BNB"` (plus regression for existing assets)
2. `TestAtomicActivationSafety` — `BNB ∈ ASSETS ⟹ BNB ∈ HOURLY_EXCLUDED_ASSETS ∧ BNB ∈ HOURLY_NO_EXCLUDED_ASSETS`; T4-prereqs-when-shadow-False skipped under T1 (shadow=True)
3. `TestRegistryCompleteness` — BNB present in SERIES_TICKERS, HOURLY_SERIES_TICKERS, COINBASE_PRODUCTS; cross-registry consistency loop over ASSETS
4. `TestProductTypeCategorizer` — `"KXBNB15M"` and `"KXBNBD"` substrings present in `bot.state` source
5. `TestExecutorHourlyPrefixes` — `"KXBNBD-" in OrderExecutor._HOURLY_SERIES_PREFIXES`
6. `TestStateBackfillSQL` — `KXBNB15M%` ≥ 2 sites + `KXBNBD%` ≥ 2 sites in `bot.state` source
7. `TestScannerShadowGateWiring` — `BNB_15M_SHADOW` substring in `bot.scanner` source; `"bnb_shadow"` strategy-name in source
8. `TestMarketConfigMirror` — `MARKET_CONFIGS["hourly"].excluded_assets == frozenset(HOURLY_EXCLUDED_ASSETS)`
9. `TestStrategyKillSwitchClauses` — `BNB_15M_SHADOW` clause present in each of TM/WKND/OVN/DC eligibility region

## Adversarial review record

| Round | C / M / m / info | Disposition |
|---|---|---|
| R1 | TBD | _to be filled_ |
| R2 | TBD | _target: 0C/0M consecutive with R1_ |

(Discipline per `feedback_discipline_applies_to_xs_followups`: full 2-zero gate even though HYPE/DOGE precedent makes T1 well-charted.)

## Post-T1 chain (filed in ClickUp)

- T1.5 (`86b9zmj15`) — external-feed verify+add. **Target: ship within 7 days of T1.**
- T2 (`86b9zmj2a`) — post-deploy single-shot regression check.
- T3 (`86b9zmj2q`) — when ~2000 settled rows accumulate (~3-4wk). Likely raw_prob+blend_w sweep, not cal_mlp.
- T4 (`86b9zmj37`) — per-asset MIN_ENTRY_PRICE / MAX_RISK_PER_TRADE / NBBO_FALLBACK_GATES + cal_subtypes (if cal_mlp) + flip shadow flag + remove from HOURLY_EXCLUDED_ASSETS.

## Rationale: why T1 deliberately narrow on external feeds

User explicitly wants "all the data we'll ever need" from day-1 — T1.5 is therefore URGENT, not "weeks later". Atomic separation of T1 (activation) from T1.5 (external feeds) preserves clean adversarial-review surface per HYPE/DOGE precedent. Gap T1→T1.5 measured in days, not weeks; doesn't bias T3 training set since meaningful settled corpus needs weeks of wall-clock accumulation.

If T1.5 verification reveals BNB is missing from an exchange, that exchange entry stays absent — documented gap, not silent NULL. Mirrors HYPE precedent (no Binance.com entry in `CROSS_EXCHANGE_SYMBOLS["HYPE"]`).
