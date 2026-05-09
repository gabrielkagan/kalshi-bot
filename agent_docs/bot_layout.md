# bot/_impl.py Layout

bot/_impl.py is **25,011 lines** as of 2026-05-08 (post-Bit-4.5a small-feeds extraction; Bit 3.1 moved ~1,200 lines of constants to `bot/constants.py`, Bit 3.2 moved ~790 lines of helpers to `bot/helpers/*`, Bit 4.1 moved 65 lines of Logger to `bot/logger.py`, Bit 4.2 moved 30 lines of TelegramNotifier to `bot/notifier.py`, Bit 4.3 moved 348 lines of KalshiClient to `bot/kalshi_client.py`, Bit 4.4 moved 141 lines of DeribitDVOLFetcher + CoinGlassFetcher to `bot/fetchers/*`, Bit 4.5a moved ~660 lines of CoinbaseFeed + OrderbookSchemaError + CrossExchangeFeed to `bot/feeds/*`). Class line ranges below
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
| 1–~110 | Header import block (`bot._thread_env` imports BEFORE `numpy` — load-bearing per CLAUDE.md; `scripts/cal_mlp/` is also added to sys.path here for the bare `from integration import` calls later in the file). `from bot.constants import *` (Bit 3.1) at ~83; `from bot.helpers import *` + explicit underscore re-exports for `bot.helpers.validators` and `bot.helpers.breakers` (Bit 3.2) immediately after. `from bot.logger import Logger` (Bit 4.1), `from bot.notifier import TelegramNotifier` (Bit 4.2), `from bot.kalshi_client import KalshiClient` (Bit 4.3), `from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher` (Bit 4.4), and `from bot.feeds import CoinbaseFeed, OrderbookSchemaError, CrossExchangeFeed` (Bit 4.5a) follow at ~100–~104. Verify with `grep -n "^from bot\." bot/_impl.py`. |
| ~125–~605 | Residual helpers + runtime-state singletons that stay in `bot/_impl.py` (`_derive_subtype`/`_derive_asset_filter`/`_resolve_cal_engine` — deferred to Sprint 6 with `CalibrationEngine` because they read `_CAL_REGISTRY` module-level mutable state — `_append_raw_api_journal`, `_HPSB_VALIDATOR_UNAVAILABLE_REASON`, `_HPSB_MISSING_BLEEDERS = ...` / `_BLEED_BLOCK_MISSING_BLEEDERS = ...` boot-time invocations of the validators that themselves moved to `bot/helpers/validators.py`, plus the orphan-DB watchdog Layer-3 helpers `_run_lsof_for_db`/`_get_pid_cmdline`/`_alert_orphan_db_holder`/`detect_orphan_db_holders`). `_swallow_persist_exception` moved to `bot/feeds/coinbase.py` in Bit 4.5a alongside its sole consumer. |
| 606–25011 | Class definitions (see table below). One module-level `def discover_active_windows()` sits in the body region between SettlementTracker's class body and the MainLoop class def; it ships with MainLoop in Bit 9.3. |

### Class-body end vs class-range note

The class table below uses *next-class-start − 1* as the range end. So
`SettlementTracker 21836–23014` includes the inter-class
`discover_active_windows()` def. The class body itself ends earlier.
The class size column counts those inter-class lines, which is
conservative (over-counts by ~80 for SettlementTracker, by ~219 for
`StateManager` because the inter-class space previously occupied by
KalshiClient + the KalshiClient/StateManager orphan-DB watchdog block
is rolled into the StateManager range — see search anchor
`# ── Orphan-DB watchdog (Layer 3 of orphan prevention) ──`, and now
also over-counts `KalshiFeed` substantially: post-Bit-4.5a the
StateManager → KalshiFeed gap and the KalshiFeed → OrderFlowEngine gap
both contain breadcrumbs only — CoinbaseFeed/OrderbookSchemaError before
KalshiFeed, CrossExchangeFeed after KalshiFeed).
Bit 4.5a deliberately inserted 3-line breadcrumb comments at each cut
site (`# CoinbaseFeed → bot/feeds/coinbase.py (Bit 4.5a, 2026-05-08).`,
`# OrderbookSchemaError → bot/feeds/orderbook_schema.py (Bit 4.5a, 2026-05-08).`,
`# CrossExchangeFeed → bot/feeds/cross_exchange.py (Bit 4.5a, 2026-05-08).`)
and `tests/test_feeds_extraction.py::test_class_not_defined_in_bot_impl`
enforces the negative contract for all three classes.

## Classes (auto-verifiable)

Generated 2026-05-08 from `grep -nE '^class ' bot/_impl.py` (post-Bit-4.5a small-feeds extraction; CoinbaseFeed/OrderbookSchemaError/CrossExchangeFeed now live in `bot/feeds/{coinbase,orderbook_schema,cross_exchange}.py`).

| Lines | Class | Size |
|---|---|---|
| 606–3209 | `StateManager` | 2604 |
| 3210–4999 | `KalshiFeed` | 1790 |
| 5000–5121 | `OrderFlowEngine` | 122 |
| 5122–5282 | `KalshiOrderFlowTracker` | 161 |
| 5283–6242 | `VolatilityEngine` | 960 |
| 6243–6445 | `ProbabilityEngine` | 203 |
| 6446–7455 | `CalibrationEngine` | 1010 |
| 7456–16545 | `OpportunityScanner` | 9090 |
| 16546–21835 | `OrderExecutor` | 5290 |
| 21836–23014 | `SettlementTracker` | 1179 |
| 23015–25011 | `MainLoop` | 1997 |

## Project file map (root, 2026-05-05)

Live trading process:
- `bot/_impl.py` — main bot, all trading logic (25,011 lines post-Bit-4.5a small-feeds extraction)
- `bot/constants.py` — module-level UPPER_SNAKE constants extracted from `bot/_impl.py` (Bit 3.1, 1,722 lines, 484 constants). Re-exported into `bot/_impl.py` via `from bot.constants import *` near top of file.
- `bot/helpers/` — feature/sizing/cell-block helpers extracted from `bot/_impl.py` (Bit 3.2, ~790 lines, 25 functions across 9 submodules: `time_features`, `derived_features`, `tm_sweep`, `sizing`, `cell_blocks`, `strings`, `strategy`, `validators`, `breakers`). Re-exported into `bot/_impl.py` via `from bot.helpers import *` plus explicit underscore re-exports for `validators` (3) and `breakers` (5) — star-import skips underscored names.
- `bot/logger.py` — `Logger` class (structured JSONL logging with fill dedup) extracted from `bot/_impl.py` (Bit 4.1, ~90 lines, stdlib + `bot.constants` only). Re-exported into `bot/_impl.py` via `from bot.logger import Logger` so `MainLoop.__init__` instantiation + type annotations on `OpportunityScanner`/`OrderExecutor`/`SettlementTracker` resolve.
- `bot/notifier.py` — `TelegramNotifier` class (fire-and-forget Telegram alerts) extracted from `bot/_impl.py` (Bit 4.2, ~50 lines, stdlib + `requests` only — zero `bot.constants` deps, zero `bot.helpers` deps). Re-exported into `bot/_impl.py` via `from bot.notifier import TelegramNotifier` so the runtime construction in `MainLoop.__init__` (search `self.telegram = TelegramNotifier` for the current line) resolves. The `Optional["TelegramNotifier"]` forward-ref on the module-level `_TELEGRAM` singleton has no in-tree `typing.get_type_hints` consumer as of Bit 4.2, so it does not justify the import on its own.
- `bot/kalshi_client.py` — `KalshiClient` class (Kalshi REST API auth + rate limiting + breaker-wrapped GETs + raw POST/DELETE writes) extracted from `bot/_impl.py` (Bit 4.3, ~388 lines, stdlib + `requests` + `cryptography` + `bot.constants` (4 explicit names) + `bot.helpers.breakers` (4 explicit names) + `circuit_breaker.REGISTRY`). Re-exported into `bot/_impl.py` via `from bot.kalshi_client import KalshiClient` so MainLoop construction (`self.client = KalshiClient(...)`) + 7 type-annotation sites (`reconcile_with_api`, `_reconcile_positions`, `_reconcile_orders`, `OpportunityScanner.__init__`, `OrderExecutor.__init__`, `SettlementTracker.__init__`, `discover_active_windows()`) all resolve.
- `bot/fetchers/` — daemon-thread HTTP fetcher classes extracted from `bot/_impl.py` (Bit 4.4, ~237 lines across 3 files: `__init__.py` re-export shim, `deribit.py` for `DeribitDVOLFetcher` (Deribit DVOL implied-vol index, BTC/ETH), `coinglass.py` for `CoinGlassFetcher` (CoinGlass funding rates, BTC/ETH/SOL/XRP — disabled when `COINGLASS_API_KEY` env unset)). Re-exported into `bot/_impl.py` via `from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher` so MainLoop construction (`self.dvol_fetcher = DeribitDVOLFetcher()`, `self.coinglass = CoinGlassFetcher()`) + the `Optional[DeribitDVOLFetcher]` type annotation on `VolatilityEngine.__init__` resolve. `DVOL_ANNUALIZED_TO_5S` (the only constant Bit 3.1 left in `config.py`) is imported via `from config import DVOL_ANNUALIZED_TO_5S` rather than `bot.constants`.
- `bot/feeds/` — WebSocket feed classes extracted from `bot/_impl.py` (Bit 4.5a, ~790 lines across 4 files: `__init__.py` re-export shim, `coinbase.py` for `CoinbaseFeed` (Coinbase WS spot prices BTC/ETH/SOL/XRP with persistent 30-min snapshot buffer; also hosts the `_swallow_persist_exception` done-callback helper alongside its sole consumer), `orderbook_schema.py` for `OrderbookSchemaError` (exception raised by KalshiFeed on Kalshi WS schema migrations — KalshiFeed STAYS in `bot/_impl.py` until Bit 4.5b, so the re-import from this module preserves its `raise`/`except` sites), `cross_exchange.py` for `CrossExchangeFeed` (Binance/Kraken/Bybit WS feeds for lead/lag detection — takes a `CoinbaseFeed` reference at construction time via `from bot.feeds.coinbase import CoinbaseFeed`)). Re-exported into `bot/_impl.py` via `from bot.feeds import CoinbaseFeed, OrderbookSchemaError, CrossExchangeFeed` so MainLoop construction (`self.feed = CoinbaseFeed()`, `self.cross_feed = CrossExchangeFeed(self.feed) if CROSS_EXCHANGE_ENABLED else None`) + `feed: CoinbaseFeed` type annotations on `VolatilityEngine.__init__` and `OpportunityScanner.__init__` + KalshiFeed's `raise OrderbookSchemaError(...)` sites resolve. `ASSETS` (Bit 3.1 left in `config.py`) imported via `from config import ASSETS`.
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

Tests: `tests/` (3,565 collected post-Bit-4.5a; verify with `pytest tests/ --collect-only -q | tail -1`. Unit/integration/regression split pending Phase JJ)

KB (local-only, never committed): `kb/`, `kb-research/`

## Modularization destination

Per `kb/decisions/repo-modularization-plan-may05.md`:
- Sprint 3 → constants + helpers extracted to `bot/constants.py` + `bot/helpers/*` (Bit 3.1 + Bit 3.2 + Bit 3.3 SHIPPED 2026-05-08)
- Sprint 4–6 → leaf classes (Bit 4.1 Logger SHIPPED 2026-05-08 → `bot/logger.py`; Bit 4.2 TelegramNotifier SHIPPED 2026-05-08 → `bot/notifier.py`; Bit 4.3 KalshiClient SHIPPED 2026-05-08 → `bot/kalshi_client.py`; Bit 4.4 fetchers (DeribitDVOLFetcher + CoinGlassFetcher) SHIPPED 2026-05-08 → `bot/fetchers/`; Bit 4.5a small feeds (CoinbaseFeed + OrderbookSchemaError + CrossExchangeFeed) SHIPPED 2026-05-08 → `bot/feeds/`; remaining: KalshiFeed (Bit 4.5b — large), engines)
- Sprint 7 → StateManager → `bot/state.py`
- Sprint 8 → OpportunityScanner → `bot/scanner/` (verbatim then internal split)
- Sprint 9 → OrderExecutor + SettlementTracker + MainLoop → `bot/`
- After Sprint 9, bot/_impl.py = ~50-line entrypoint.
