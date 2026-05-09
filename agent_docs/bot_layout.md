# bot/_impl.py Layout

bot/_impl.py is **22,361 lines** as of 2026-05-09 (post-Bit-6.1 VolatilityEngine extraction; Bit 3.1 moved ~1,200 lines of constants to `bot/constants.py`, Bit 3.2 moved ~790 lines of helpers to `bot/helpers/*`, Bit 4.1 moved 65 lines of Logger to `bot/logger.py`, Bit 4.2 moved 30 lines of TelegramNotifier to `bot/notifier.py`, Bit 4.3 moved 348 lines of KalshiClient to `bot/kalshi_client.py`, Bit 4.4 moved 141 lines of DeribitDVOLFetcher + CoinGlassFetcher to `bot/fetchers/*`, Bit 4.5a moved ~660 lines of CoinbaseFeed + OrderbookSchemaError + CrossExchangeFeed to `bot/feeds/*`, Bit 4.5b moved ~1,790 lines of KalshiFeed to `bot/feeds/kalshi.py`, Bit 6.1 moved ~960 lines of VolatilityEngine to `bot/engines/volatility.py`). Class line ranges below
are auto-verifiable. Repo modularization plan (`kb/decisions/repo-modularization-plan-may05.md`)
will turn bot/_impl.py into a thin entrypoint shim with logic in a `bot/` package.

## Regenerate

This doc drifts when bot/_impl.py grows. Regression test
`tests/test_repo_hygiene.py::test_bot_layout_class_lines_match_bot_impl`
fails when class line numbers diverge by >5. Regenerate with:

```bash
python3 -c "
import re
text = open('bot/_impl.py').read(); lines = text.splitlines(); total = len(lines)
classes = []
for i, line in enumerate(lines, 1):
    m = re.match(r'^class ([A-Za-z_][A-Za-z0-9_]*)', line)
    if m: classes.append((i, m.group(1)))
print(f'Total bot/_impl.py lines: {total}')
print('| Lines | Class | Size |'); print('|---|---|---|')
for idx, (start, name) in enumerate(classes):
    end = classes[idx+1][0]-1 if idx+1 < len(classes) else total
    print(f'| {start}–{end} | \`{name}\` | {end-start+1} |')
"
```

## Top-level structure (approximate)

bot/_impl.py interleaves imports, residual helpers, classes, and a tail
entry point. Boundaries below are inexact (no AST split exists yet);
verify by reading 5-10 lines around each line number before quoting.

| Lines | Section |
|---|---|
| 1–~110 | Header import block (`bot._thread_env` imports BEFORE `numpy` — load-bearing per CLAUDE.md; `scripts/cal_mlp/` is also added to sys.path here for the bare `from integration import` calls later in the file). `from bot.constants import *` (Bit 3.1) at ~83; `from bot.helpers import *` + explicit underscore re-exports for `bot.helpers.validators` and `bot.helpers.breakers` (Bit 3.2) immediately after. `from bot.logger import Logger` (Bit 4.1), `from bot.notifier import TelegramNotifier` (Bit 4.2), `from bot.kalshi_client import KalshiClient` (Bit 4.3), `from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher` (Bit 4.4), and `from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError` (Bit 4.5a + 4.5b) follow at ~100–~104. Verify with `grep -n "^from bot\." bot/_impl.py`. |
| ~125–~605 | Residual helpers + runtime-state singletons that stay in `bot/_impl.py` (`_derive_subtype`/`_derive_asset_filter`/`_resolve_cal_engine` — deferred to Sprint 6 with `CalibrationEngine` because they read `_CAL_REGISTRY` module-level mutable state — `_append_raw_api_journal`, `_HPSB_VALIDATOR_UNAVAILABLE_REASON`, `_HPSB_MISSING_BLEEDERS = ...` / `_BLEED_BLOCK_MISSING_BLEEDERS = ...` boot-time invocations of the validators that themselves moved to `bot/helpers/validators.py`, plus the orphan-DB watchdog Layer-3 helpers `_run_lsof_for_db`/`_get_pid_cmdline`/`_alert_orphan_db_holder`/`detect_orphan_db_holders`). `_swallow_persist_exception` moved to `bot/feeds/coinbase.py` in Bit 4.5a alongside its sole consumer. |
| 608–22361 | Class definitions (see table below). One module-level `def discover_active_windows()` sits in the body region between SettlementTracker's class body and the MainLoop class def; it ships with MainLoop in Bit 9.3. |

### Class-body end vs class-range note

The class table below uses *next-class-start − 1* as the range end. So
`SettlementTracker 20133–21311` includes the inter-class
`discover_active_windows()` def. The class body itself ends earlier.
The class size column counts those inter-class lines, which is
conservative (over-counts by ~80 for SettlementTracker, by ~219 for
`StateManager` because the inter-class space previously occupied by
KalshiClient + the KalshiClient/StateManager orphan-DB watchdog block
is rolled into the StateManager range — see search anchor
`# ── Orphan-DB watchdog (Layer 3 of orphan prevention) ──`. Post-Bit-6.1
the `StateManager → OrderFlowEngine` gap and the
`KalshiOrderFlowTracker → ProbabilityEngine` gap together contain
the breadcrumbs for every Sprint-4 + Sprint-6 leaf extracted so far —
CoinbaseFeed (Bit 4.5a), KalshiFeed (Bit 4.5b), DeribitDVOLFetcher
(Bit 4.4), CoinGlassFetcher (Bit 4.4), CrossExchangeFeed (Bit 4.5a),
and VolatilityEngine (Bit 6.1) — all named with one-line
`→ bot/<dest>` breadcrumbs.
Bit 4.5a/4.5b/6.1 deliberately inserted breadcrumb comments at each cut
site (`# CoinbaseFeed → bot/feeds/coinbase.py (Bit 4.5a, 2026-05-08).`,
`# KalshiFeed → bot/feeds/kalshi.py (Bit 4.5b, 2026-05-09).`,
`# OrderbookSchemaError → bot/feeds/orderbook_schema.py (Bit 4.5a, 2026-05-08).`,
`# CrossExchangeFeed → bot/feeds/cross_exchange.py (Bit 4.5a, 2026-05-08).`,
`# VolatilityEngine → bot/engines/volatility.py (Bit 6.1, 2026-05-09).`)
and `tests/test_feeds_extraction.py::test_class_not_defined_in_bot_impl`
+ `tests/test_engines_extraction.py::test_class_not_defined_in_bot_impl`
enforce the negative contract for all five extracted classes.

## Classes (auto-verifiable)

Generated 2026-05-09 from `grep -nE '^class ' bot/_impl.py` (post-Bit-6.1 VolatilityEngine extraction; VolatilityEngine now lives in `bot/engines/volatility.py` — first leaf in the new `bot/engines/` subpackage — alongside Bit-4.5a/4.5b feeds + Bit-4.4 fetchers).

| Lines | Class | Size |
|---|---|---|
| 608–3296 | `StateManager` | 2689 |
| 3297–3418 | `OrderFlowEngine` | 122 |
| 3419–3591 | `KalshiOrderFlowTracker` | 173 |
| 3592–3794 | `ProbabilityEngine` | 203 |
| 3795–4804 | `CalibrationEngine` | 1010 |
| 4805–13895 | `OpportunityScanner` | 9091 |
| 13896–19185 | `OrderExecutor` | 5290 |
| 19186–20364 | `SettlementTracker` | 1179 |
| 20365–22361 | `MainLoop` | 1997 |

## Project file map (root, 2026-05-05)

Live trading process:
- `bot/_impl.py` — main bot, all trading logic (22,361 lines post-Bit-6.1 VolatilityEngine extraction)
- `bot/constants.py` — module-level UPPER_SNAKE constants extracted from `bot/_impl.py` (Bit 3.1, 1,722 lines, 484 constants). Re-exported into `bot/_impl.py` via `from bot.constants import *` near top of file.
- `bot/helpers/` — feature/sizing/cell-block helpers extracted from `bot/_impl.py` (Bit 3.2, ~790 lines, 25 functions across 9 submodules: `time_features`, `derived_features`, `tm_sweep`, `sizing`, `cell_blocks`, `strings`, `strategy`, `validators`, `breakers`). Re-exported into `bot/_impl.py` via `from bot.helpers import *` plus explicit underscore re-exports for `validators` (3) and `breakers` (5) — star-import skips underscored names.
- `bot/logger.py` — `Logger` class (structured JSONL logging with fill dedup) extracted from `bot/_impl.py` (Bit 4.1, ~90 lines, stdlib + `bot.constants` only). Re-exported into `bot/_impl.py` via `from bot.logger import Logger` so `MainLoop.__init__` instantiation + type annotations on `OpportunityScanner`/`OrderExecutor`/`SettlementTracker` resolve.
- `bot/notifier.py` — `TelegramNotifier` class (fire-and-forget Telegram alerts) extracted from `bot/_impl.py` (Bit 4.2, ~50 lines, stdlib + `requests` only — zero `bot.constants` deps, zero `bot.helpers` deps). Re-exported into `bot/_impl.py` via `from bot.notifier import TelegramNotifier` so the runtime construction in `MainLoop.__init__` (search `self.telegram = TelegramNotifier` for the current line) resolves. The `Optional["TelegramNotifier"]` forward-ref on the module-level `_TELEGRAM` singleton has no in-tree `typing.get_type_hints` consumer as of Bit 4.2, so it does not justify the import on its own.
- `bot/kalshi_client.py` — `KalshiClient` class (Kalshi REST API auth + rate limiting + breaker-wrapped GETs + raw POST/DELETE writes) extracted from `bot/_impl.py` (Bit 4.3, ~388 lines, stdlib + `requests` + `cryptography` + `bot.constants` (4 explicit names) + `bot.helpers.breakers` (4 explicit names) + `circuit_breaker.REGISTRY`). Re-exported into `bot/_impl.py` via `from bot.kalshi_client import KalshiClient` so MainLoop construction (`self.client = KalshiClient(...)`) + 7 type-annotation sites (`reconcile_with_api`, `_reconcile_positions`, `_reconcile_orders`, `OpportunityScanner.__init__`, `OrderExecutor.__init__`, `SettlementTracker.__init__`, `discover_active_windows()`) all resolve.
- `bot/fetchers/` — daemon-thread HTTP fetcher classes extracted from `bot/_impl.py` (Bit 4.4, ~237 lines across 3 files: `__init__.py` re-export shim, `deribit.py` for `DeribitDVOLFetcher` (Deribit DVOL implied-vol index, BTC/ETH), `coinglass.py` for `CoinGlassFetcher` (CoinGlass funding rates, BTC/ETH/SOL/XRP — disabled when `COINGLASS_API_KEY` env unset)). Re-exported into `bot/_impl.py` via `from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher` so MainLoop construction (`self.dvol_fetcher = DeribitDVOLFetcher()`, `self.coinglass = CoinGlassFetcher()`) + the `Optional[DeribitDVOLFetcher]` type annotation on `VolatilityEngine.__init__` resolve. `DVOL_ANNUALIZED_TO_5S` (the only constant Bit 3.1 left in `config.py`) is imported via `from config import DVOL_ANNUALIZED_TO_5S` rather than `bot.constants`.
- `bot/feeds/` — WebSocket feed classes extracted from `bot/_impl.py` (Bit 4.5a + Bit 4.5b, ~2,580 lines across 5 files: `__init__.py` re-export shim, `coinbase.py` for `CoinbaseFeed` (Coinbase WS spot prices BTC/ETH/SOL/XRP with persistent 30-min snapshot buffer; also hosts the `_swallow_persist_exception` done-callback helper alongside its sole consumer; Bit 4.5a), `orderbook_schema.py` for `OrderbookSchemaError` (exception raised by KalshiFeed on Kalshi WS schema migrations; Bit 4.5a), `cross_exchange.py` for `CrossExchangeFeed` (Binance/Kraken/Bybit WS feeds for lead/lag detection — takes a `CoinbaseFeed` reference at construction time via `from bot.feeds.coinbase import CoinbaseFeed`; Bit 4.5a), `kalshi.py` for `KalshiFeed` (Kalshi WS feed for fill notifications + per-ticker orderbook snapshots/deltas — largest leaf in the Sprint 4 modularization track at ~1,790 lines; uses `from bot.feeds.orderbook_schema import OrderbookSchemaError` for the sibling exception; Bit 4.5b)). Re-exported into `bot/_impl.py` via `from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError` so MainLoop construction (`self.feed = CoinbaseFeed()`, `self.cross_feed = CrossExchangeFeed(self.feed) if CROSS_EXCHANGE_ENABLED else None`, `self.kalshi_feed = KalshiFeed(api_key, self.client.private_key)`) + `feed: CoinbaseFeed` type annotations on `VolatilityEngine.__init__` (now in `bot/engines/volatility.py`) and `OpportunityScanner.__init__` resolve. `ASSETS` (Bit 3.1 left in `config.py`) imported via `from config import ASSETS` by `coinbase.py` + `cross_exchange.py`.
- `bot/engines/` — math-layer engine classes extracted from `bot/_impl.py` (Bit 6.1 onward, ~960 lines so far across 2 files: `__init__.py` re-export shim, `volatility.py` for `VolatilityEngine` (Realized Kernel + Deribit DVOL volatility engine with adaptive jump detection and EGARCH variance-space blending; ~960 lines; Bit 6.1)). Re-exported into `bot/_impl.py` via `from bot.engines import VolatilityEngine` so MainLoop construction (`self.vol = VolatilityEngine(self.feed, dvol_fetcher=self.dvol_fetcher, egarch_estimator=..., mz_tracker=...)`) + the `vol: VolatilityEngine` type annotation on `OpportunityScanner.__init__` resolve. Imports 35 explicit names from `bot.constants` + 5 from `config` (ASSETS, EGARCH_BLEND_LOG_INTERVAL, EGARCH_BLEND_SHADOW_MODE, EGARCH_RV_RATIO_CLAMP, VOL_RETURN_INTERVAL — all pre-Bit-3.1 constants that stayed in `config.py`) + `compute_tv_rk_weights` from `models` + `CoinbaseFeed` from `bot.feeds.coinbase` + `DeribitDVOLFetcher` from `bot.fetchers.deribit` (the two type-annotation pins per L33). `Optional['EGARCHEstimator']` and `Optional['MincerZarnowitzTracker']` remain string-quoted forward refs because both classes still live in `models.py`. ProbabilityEngine (Bit 6.2) and CalibrationEngine (Bit 6.3) follow.
- `ops/kalshi-bot.service` — systemd unit, source of truth (installed via `ops/install.sh`); see `ops/CLAUDE.md`.
- `start.sh` — wrapper invoked by `ops/kalshi-bot.service` (venv + .env + `python -m bot`)
- `bot/_thread_env.py` — sets OMP/MKL/OpenBLAS thread caps. bot/_impl.py imports `bot._thread_env` BEFORE numpy. Order is load-bearing per CLAUDE.md and AST-asserted by `tests/test_cal_mlp_invariants.py::test_thread_env_imported_before_numerical_libs_in_bot_impl`. (Pre-Bit-2.3 the file lived at `scripts/cal_mlp/_thread_env.py` and required a sys.path.insert to locate; Bit 2.3 moved it into the `bot/` package and retired the pre-_thread_env hack — though `scripts/cal_mlp/` is still added to sys.path post-_thread_env for `from integration import` calls.)

Engines (separate threads/processes):
- `spx_engine.py` — SPX hourly (Polygon, EGARCH, RK, VIX)
- `weather_engine.py` — weather ensemble (Open-Meteo GFS/ECMWF)
- `sports_engine.py` — sports comeback (ESPN, Bayesian posterior)
- `sports_data.py` — sports data fetcher

Shadows (observation-only):
- `fifteenm_shadow.py` — 15M variants A1/A2/A3/A4
- `hourly_alt_shadow.py` — hourly alternate sims
- `spx_harrv_shadow.py` — SPX HAR-RV shadow

AI helpers:
- `analyst.py`, `auditor.py`, `researcher.py` — Telegram-driven analysis

Snapshots/sync:
- `dashboard_snapshot.py` — Supabase syncer (paired with `dashboard/index.html` on `gh-pages`)
- `bot_state_snapshot.py` — bot microstate forward-capture
- `market_observations_snapshotter.py` — NBBO continuous snapshotter
- `supabase_sync.py` — Postgres mirror

Infra:
- `capital_allocator.py`, `circuit_breaker.py`, `watchdog.py`, `models.py`

Config:
- `market_config.py` — MarketTypeConfig dataclass; asserts against bot/_impl.py at startup
- `config.py`, `config.json`, `dist_config.json` — multiple sources (consolidation pending Phase II)

Operator scripts: `scripts/` (~80 files; subdir reorg pending Phase HH)

Tests: `tests/` (3,744 collected post-Bit-6.1; verify with `pytest tests/ --collect-only -q | tail -1`. Unit/integration/regression split pending Phase JJ)

KB (local-only, never committed): `kb/`, `kb-research/`

## Modularization destination

Per `kb/decisions/repo-modularization-plan-may05.md`:
- Sprint 3 → constants + helpers extracted to `bot/constants.py` + `bot/helpers/*` (Bit 3.1 + Bit 3.2 + Bit 3.3 SHIPPED 2026-05-08)
- Sprint 4–6 → leaf classes (Bit 4.1 Logger SHIPPED 2026-05-08 → `bot/logger.py`; Bit 4.2 TelegramNotifier SHIPPED 2026-05-08 → `bot/notifier.py`; Bit 4.3 KalshiClient SHIPPED 2026-05-08 → `bot/kalshi_client.py`; Bit 4.4 fetchers (DeribitDVOLFetcher + CoinGlassFetcher) SHIPPED 2026-05-08 → `bot/fetchers/`; Bit 4.5a small feeds (CoinbaseFeed + OrderbookSchemaError + CrossExchangeFeed) SHIPPED 2026-05-08 → `bot/feeds/`; Bit 4.5b KalshiFeed SHIPPED 2026-05-09 → `bot/feeds/kalshi.py` — largest single leaf at ~1,790 lines; Bit 6.1 VolatilityEngine SHIPPED 2026-05-09 → `bot/engines/volatility.py` — first leaf in new `bot/engines/` subpackage at ~960 lines; remaining: ProbabilityEngine + CalibrationEngine)
- Sprint 7 → StateManager → `bot/state.py`
- Sprint 8 → OpportunityScanner → `bot/scanner/` (verbatim then internal split)
- Sprint 9 → OrderExecutor + SettlementTracker + MainLoop → `bot/`
- After Sprint 9, bot/_impl.py = ~50-line entrypoint.
