---
clickup: 86b9vecw9
status: t1-in-progress
date: 2026-05-10
discipline: RCA → TDD → adversarial review (2 consecutive 0C/0M)
spike: agent_docs/asset-onboarding-doge-hype-spike.md (9 adversarial rounds, 2026-05-09)
---

# T1 Plan: HYPE + DOGE onboarding (shadow observation)

## Goal

Activate HYPE and DOGE 15M + hourly observation. Bot subscribes to Coinbase/Kalshi feeds, runs full scan→evaluate pipeline, writes diagnostic rows to `evaluated_opportunities`, but submits **zero live orders**. Data accumulates ~3–4 wk (660 settles/wk/asset × 4wk ≈ 2,640) → cal_mlp training (T3) → promotion (T4).

## Atomic activation principle

T1 is ONE commit. Splitting "registry edits" from "shadow gate wiring" breaks the safety invariant — the moment `config.py:ASSETS` contains `"HYPE"`/`"DOGE"`, the scan loop iterates them. Without the shadow gates in the same commit, the elif chain could fall through to scaffolded defaults and submit real orders.

**Edit order within the commit (worst-case-partial-revert = inert state):**
1. Define `HYPE_15M_SHADOW = True` / `DOGE_15M_SHADOW = True` in `bot/constants.py`
2. Extend `HOURLY_EXCLUDED_ASSETS` to `{"SOL", "XRP", "HYPE", "DOGE"}`
3. Extend `HOURLY_NO_EXCLUDED_ASSETS` to `{"HYPE", "DOGE"}` (NO-side safety belt)
4. Wire `HYPE_15M_SHADOW` + `DOGE_15M_SHADOW` into `bot/scanner/__init__.py` at the two XRP gate sites
5. Extend `bot/state.py` SQL backfill + inline product_type tuples
6. Extend `bot/executor.py:_HOURLY_SERIES_PREFIXES`
7. Extend `market_config.py:117` to match new HOURLY_EXCLUDED_ASSETS (startup-assertion mirror)
8. Extend `bot/constants.py` registries: SERIES_TICKERS, HOURLY_SERIES_TICKERS, COINBASE_PRODUCTS
9. Extend all downstream lists/tuples (dashboard, supabase, snapshot, bot/ai/auditor.py, fifteenm_shadow, scripts)
10. Refactor log-format sites (cosmetic, but needed for clean observation log lines)
11. **LAST: extend `config.py:ASSETS = ["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE"]`** — activation lever
12. Update test fixtures + assertions + add regression tests
13. Doc-drift

If anyone reverts the activation commit, partial state from steps 1–10 is inert (no asset routes through them yet because ASSETS isn't extended).

## RCA findings (new, since 9-round spike on 2026-05-09)

### Finding 1: market_config.py:117 + :284 is a startup-crash lock-step site

`market_config.py:284` asserts `cfg_h.excluded_assets == frozenset(bot.HOURLY_EXCLUDED_ASSETS)`. The hardcoded `frozenset({"SOL", "XRP"})` at line 117 must update in lock-step with `bot/constants.py:322`, or the bot crash-loops on startup. **The spike doc didn't enumerate this site.** CLAUDE.md warns about it generically ("After constant changes in bot/constants.py … grep across the repo, especially market_config.py").

### Finding 2: tests/test_scan_pipeline.py:686 + :702 are strict-equality assertions

```python
# :686
self.assertEqual(HOURLY_EXCLUDED_ASSETS, {"SOL", "XRP"})  # breaks on add
# :702
self.assertEqual(HOURLY_NO_EXCLUDED_ASSETS, set())        # breaks if I add HYPE/DOGE to NO-side
```

Both break on T1. Must update in lock-step. Acceptable: these tests assert "current state"; their natural update is to reflect the new state.

### Finding 3: Spike's "4 shadow flags" specification is over-spec'd

Spike T1 scope lists `*_15M_SHADOW, *_HOURLY_SHADOW flags (×4)` and says "wire into existing XRP_15M_SHADOW pattern". But the XRP pattern is 15M-specific; there is no analogous hourly pattern. Wiring `HYPE_HOURLY_SHADOW` into a 15M-shaped gate is a category error.

**Resolution**: drop `HYPE_HOURLY_SHADOW` and `DOGE_HOURLY_SHADOW` from T1. Hourly observation is already covered by adding HYPE/DOGE to `HOURLY_EXCLUDED_ASSETS` (which inserts full-diagnostic `evaluated_opportunities` rows with `filter_stage="hourly_asset_excluded"` per the consumer at `bot/scanner/__init__.py:4982-4995`). Adding HOURLY_SHADOW flags with no wiring = dead code.

Net flag inventory for T1: **2 flags only** (`HYPE_15M_SHADOW=True`, `DOGE_15M_SHADOW=True`).

### Finding 4: All bot/_impl.py:NNNN refs in the spike are stale

Bits 8.1 / 9.1 / 9.2 / 9.3-i shipped between spike (2026-05-09) and now (2026-05-10). Re-anchored map below.

## Full site inventory (current-HEAD, post-Bit-9.3-i)

`bot/_impl.py` is now 1,036 lines (orphan-DB watchdog only). All T1-relevant sites have migrated.

### Required edits

| Site | File:line | Edit |
|---|---|---|
| ASSETS registry (activation lever) | `config.py:17` | extend to `["BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE"]` |
| SERIES_TICKERS dict | `bot/constants.py:22-27` | add `"HYPE": "KXHYPE15M", "DOGE": "KXDOGE15M"` |
| HOURLY_SERIES_TICKERS dict | `bot/constants.py:232-237` | add `"HYPE": "KXHYPED", "DOGE": "KXDOGED"` |
| COINBASE_PRODUCTS dict | `bot/constants.py:676-681` | add `"HYPE": "HYPE-USD", "DOGE": "DOGE-USD"` |
| HOURLY_EXCLUDED_ASSETS set | `bot/constants.py:322` | extend to `{"SOL", "XRP", "HYPE", "DOGE"}` |
| HOURLY_NO_EXCLUDED_ASSETS set | `bot/constants.py:333` | extend from `set()` to `{"HYPE", "DOGE"}` (NO-side belt) |
| 15M shadow flags (NEW) | `bot/constants.py` near :57 | add `HYPE_15M_SHADOW = True` + `DOGE_15M_SHADOW = True` after `XRP_15M_SHADOW` |
| market_config.py hourly excluded | `market_config.py:117` | extend `excluded_assets=frozenset({"SOL", "XRP"})` to include HYPE/DOGE (startup-assertion mirror) |
| Scanner YES-side 15M shadow gate | `bot/scanner/__init__.py:5867` | add two more `if HYPE_15M_SHADOW and asset == "HYPE"...` / DOGE blocks mirroring XRP |
| Scanner NO-side 15M shadow gate | `bot/scanner/__init__.py:7610` | extend `_no_filter_stage` elif chain w/ HYPE/DOGE analogues |
| Scanner imports | `bot/scanner/__init__.py:147-160` | add HYPE_15M_SHADOW, DOGE_15M_SHADOW to import list |
| state.py SQL backfill (15m) | `bot/state.py:894-895, 916-917` | extend LIKE clauses w/ KXHYPE15M%, KXDOGE15M% |
| state.py SQL backfill (hourly) | `bot/state.py:898-899, 920-921` | extend LIKE clauses w/ KXHYPED%, KXDOGED% |
| state.py inline 15m categorizer | `bot/state.py:1311` | extend tuple w/ "KXHYPE15M", "KXDOGE15M" |
| state.py inline hourly categorizer | `bot/state.py:1313` | extend tuple w/ "KXHYPED", "KXDOGED" |
| executor _HOURLY_SERIES_PREFIXES | `bot/executor.py:3082` | extend tuple w/ "KXHYPED-", "KXDOGED-" |
| Dashboard ASSETS list | `dashboard_snapshot.py:11` | extend |
| Dashboard inline ASSETS list | `dashboard_snapshot.py:2837` | extend |
| Dashboard _hourly_pfx tuple | `dashboard_snapshot.py:1276` | extend w/ "KXHYPED", "KXDOGED" |
| Supabase ASSETS | `supabase_sync.py:22` | extend |
| Supabase display-name triple | `supabase_sync.py:324-327` | add `("HYPE","Hyperliquid","KXHYPE15M")`, `("DOGE","Dogecoin","KXDOGE15M")` |
| bot_state_snapshot ASSETS | `bot_state_snapshot.py:127` | extend |
| auditor active_assets | `bot/ai/auditor.py:269` | extend |
| fifteenm_shadow DEFAULT_TEMPERATURES | `fifteenm_shadow.py:48` | add HYPE/DOGE entries (1.00 neutral) |
| fifteenm_shadow DEFAULT_BLEND_W | `fifteenm_shadow.py:50` | add (0.50 neutral) |
| fifteenm_shadow DEFAULT_DEBIAS | `fifteenm_shadow.py:52` | add (0.00 neutral) |
| fifteenm_shadow recalibration loops (4) | `fifteenm_shadow.py:166, 763, 1484, 1552, 1583` | extend tuples |
| fifteenm_shadow asset→idx map | `fifteenm_shadow.py:619` | add `"HYPE": 4, "DOGE": 5` |
| scripts/hourly_shadow_audit.py CASE-WHEN | `scripts/hourly_shadow_audit.py` (search anchor) | extend SQL CASE |
| scripts/cal_mlp_mac_drain.py ASSETS | `scripts/cal_mlp_mac_drain.py:58` | extend tuple |
| Log-format: models.py | `models.py:208, 693-694` | refactor to ASSETS-driven `", ".join(...)` |
| Log-format: bot/main_loop.py | `bot/main_loop.py:2159, 2170, 2175` | refactor |
| Log-format: bot/engines/volatility.py | `bot/engines/volatility.py:179, 454, 509` | refactor |
| Log-format: tests/regression/test_buffer_persistence.py | `:104` | refactor |
| Test: HOURLY_EXCLUDED_ASSETS equality | `tests/test_scan_pipeline.py:686` | update to new set |
| Test: HOURLY_NO_EXCLUDED_ASSETS equality | `tests/test_scan_pipeline.py:702` | update to `{"HYPE","DOGE"}` |
| Test: test_config_consistency | `tests/test_config_consistency.py:55` | auto-passes (uses `frozenset(bot.HOURLY_EXCLUDED_ASSETS)`) |

### NEW regression tests (TDD-RED-first)

| Test | What it locks |
|---|---|
| `tests/test_doge_hype_onboarding_t1.py::test_ticker_parser_hype_15m` | `StateManager._asset_from_ticker("KXHYPE15M-...")` → `"HYPE"` |
| `…::test_ticker_parser_hype_hourly` | `StateManager._asset_from_ticker("KXHYPED-...")` → `"HYPE"` |
| `…::test_ticker_parser_doge_15m` | DOGE 15M parser |
| `…::test_ticker_parser_doge_hourly` | DOGE hourly parser |
| `…::test_product_type_categorizer_hype_doge` | `_categorize_product_type` returns `"15m"`/`"hourly"` for new prefixes |
| `…::test_settled_trades_sql_backfill_hype_doge` | Backfill SQL UPDATE sets product_type correctly |
| `…::test_evaluated_opportunities_sql_backfill_hype_doge` | Same for evaluated_opportunities |
| `…::test_hype_15m_shadow_gate_blocks_live` | HYPE_15M_SHADOW=True path inserts shadow row + skips live |
| `…::test_doge_15m_shadow_gate_blocks_live` | DOGE_15M_SHADOW=True path inserts shadow row + skips live |
| `…::test_hourly_excluded_blocks_hype_doge_yes_side` | HOURLY_EXCLUDED_ASSETS gate fires for HYPE/DOGE |
| `…::test_hourly_no_excluded_blocks_hype_doge_no_side` | HOURLY_NO_EXCLUDED_ASSETS gate fires |
| `…::test_executor_hourly_prefixes_block_maker_hype_doge` | Maker order on KXHYPED-/KXDOGED- is blocked |
| `…::test_coinbase_products_includes_hype_doge` | COINBASE_PRODUCTS contains HYPE-USD, DOGE-USD |
| `…::test_assets_registry_consistency` | All 6 assets in config.ASSETS, SERIES_TICKERS, HOURLY_SERIES_TICKERS, COINBASE_PRODUCTS |
| `…::test_shadow_flags_defined_for_hype_doge` | HYPE_15M_SHADOW=True, DOGE_15M_SHADOW=True (constants exist + correct value) |

### T1 explicitly DOES NOT touch (defer per spike)

| Site | Defer rationale |
|---|---|
| Per-asset MIN_ENTRY_PRICE / MAX_RISK_PER_TRADE constants + elif chains | T4 promotion (under shadow=True these never fire — generic defaults are operationally correct; matches XRP precedent) |
| `STC_EXTENDED_*_MIN_PRICE` | graceful `.get(asset, MAX_ENTRY_PRICE)` fallback; safe absent |
| `TM_ASSET_RISK_CAPS` | graceful `.get(asset, 0.15)` fallback |
| `NBBO_FALLBACK_GATES` | T4 — set values from observed spread distribution |
| `cal_subtypes` in `market_config.py:91-96` | T3 — only matters when bundles trained |
| `CROSS_EXCHANGE_SYMBOLS`, `COINGLASS_SYMBOLS`, `DERIBIT_DVOL_CURRENCIES` | T3 feature derivation decision |
| `scripts/cal_mlp/*` argparse choices | T3 training |
| HYPE_HOURLY_SHADOW / DOGE_HOURLY_SHADOW flags | Over-spec'd; HOURLY_EXCLUDED_ASSETS covers (Finding 3) |

## Adversarial review record

| Round | C / M / m / info | Disposition |
|---|---|---|
| R1 | 0 / 4 / 7 / 5 | **M1 promoted to CRITICAL** (TM/WKND/OVN/DC strategy escape paths bypass the XRP_15M_SHADOW gate). M2 deferred (audit script undercounting → T1.5). M3 RCA'd as NOT a bug (whitelist correctly flags rogue HYPE/DOGE NO-side trades). M4 deferred (cal_mlp _calmlp_predictors → T3 per plan). |
| R2 | 0 / 0 / 1 / 2 | Cadence target met. Confirmed all 10 `candidates.append` sites in scanner are properly gated for HYPE/DOGE. XRP behavior unchanged. |

### R1 CRITICAL fix (post-spike RCA)

The spike doc claimed `HYPE_15M_SHADOW=True` is comprehensive — it isn't. The XRP_15M_SHADOW pattern at `bot/scanner/__init__.py:5867` fires AFTER the strategy-eligibility checks at lines ~3303 (TM), ~3786 (WKND), ~3951 (OVN), ~4212 (DC). Those 4 strategies each call `candidates.append({...})` for HYPE/DOGE BEFORE the shadow gate runs, then proceed to live execution.

**Fix**: each strategy gate now includes explicit `and not (HYPE_15M_SHADOW and asset == "HYPE")` and `and not (DOGE_15M_SHADOW and asset == "DOGE")` clauses (or, for DC, a `_dc_live_enabled = False` clear-block). LPNE was already safe via `LPNE_ASSETS = {"BTC"}`. The main 15M path is downstream of the shadow gate so its `continue` covers it.

**Regression locks**: `tests/test_doge_hype_onboarding_t1.py::TestStrategyEscapePathGates` source-walks the scanner module body and asserts HYPE_15M_SHADOW + DOGE_15M_SHADOW are referenced inside each strategy's eligibility region.

### Latent issue noted in R2 (out of T1 scope)

If `XRP_15M_SHADOW` is ever re-enabled (currently False — XRP promoted to live), the same 4 strategies would route XRP live despite the flag. This was the pre-existing condition before T1. Filed as follow-up — fix when XRP is next re-shadowed, not before.

## Post-T1 (out of scope, ticket chain)

- **T1.5 (external-feed verification + add)**: research session — verify HYPE + DOGE availability on each external feed (Binance spot, Kraken spot, Bybit spot, OKX perp, CoinGlass) via public REST APIs. Add only verified entries to `CROSS_EXCHANGE_SYMBOLS`, `COINGLASS_SYMBOLS`, `scripts/external_market_poller.py` FUNDING_SYMBOLS/OI_SYMBOLS. Skip `DERIBIT_DVOL_CURRENCIES` (Deribit DVOL is BTC/ETH options only). **Critical for cal_mlp parity** — T3 training needs HYPE/DOGE rows to have the same feature columns BTC/ETH/SOL/XRP have. Must ship within days of T1, before meaningful settled data accumulates.
- **T2 (post-deploy verify)**: immediate single-shot regression check after deploy — commit hash propagated, journalctl clean, HYPE/DOGE log signature firing, scan tick OK, evaluated_opportunities rows for HYPE/DOGE appearing, zero `order_submitted` events for new assets. No soak window.
- **T3 (cal_mlp training)**: triggered when ~2000 settled rows accumulate per asset (~3–4 wk wall-clock from T1+T1.5 ship). Extend cal_mlp argparse choices + train/validate/conformal.
- **T4 (promotion)**: add per-asset MIN_ENTRY_PRICE/MAX_RISK_PER_TRADE + NBBO_FALLBACK_GATES (from observed spread) + cal_subtypes + flip HYPE_15M_SHADOW=False / DOGE_15M_SHADOW=False / remove from HOURLY_EXCLUDED_ASSETS.

## Rationale: why is T1 deliberately narrow on external feeds?

The user emphasized: "we need to be collecting all the data for every asset". First-principles, HYPE/DOGE need feature parity with BTC/ETH/SOL/XRP by T3 training time. **But** adding external feeds in T1 without verification causes runtime errors (Binance WS rejects subscribe to non-existent symbols, etc.) and asymmetric coverage (DOGE on all exchanges, HYPE only on some) which biases training. The disciplined sequence:

- **T1 (this commit)**: activate observation with the minimum that's verified clean — Coinbase + Kalshi + safety gates. Symmetric across HYPE and DOGE.
- **T1.5 (days, not weeks, after T1)**: verify external-feed availability per asset per exchange, then add in one bundle. Symmetric across all assets that have the data source available.

This separates "activation" from "feature expansion" so each can be reviewed cleanly. The gap between T1 and T1.5 is small (days) compared to data-accumulation time (weeks), so it doesn't bias the eventual T3 training set.

If T1.5 verification reveals HYPE is missing from an exchange, that asset/exchange entry stays absent — documented gap, not silent NULL.
