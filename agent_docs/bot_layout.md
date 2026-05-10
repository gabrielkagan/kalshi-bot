# bot/_impl.py Layout

bot/_impl.py is **9,461 lines** as of 2026-05-10 (post-Bit-8.1 OpportunityScanner extraction to `bot/scanner/__init__.py` (path-A++ — `_TELEGRAM` module-level singleton relocated alongside `TelegramNotifier` in `bot/notifier.py` via the `_telegram_state._TELEGRAM` module-attribute access pattern, parallel to Bit 6.3 path-B `_cal_state._CALIBRATION_ENGINE`; the 34 `OrderExecutor.X` static-method call sites preserved via a `_get_order_executor()` single-name late-binding helper until Sprint 9 Bit 9.1 extracts OrderExecutor); post-Smell-3 fu — `_calmlp_predictors` cache + warmup orchestration relocated to `scripts/cal_mlp/integration.py` per ticket 86b9vhcat, replacing the module-level construction + warmup loop with a thin `warmup_predictor_cache()` callsite that preserves the bot._impl logger namespace for the operator-runbook boot-log grep contract; post-Bit-7.1 StateManager extraction to `bot/state.py` (path-A++ — `parity_assert`/`sizing_parity_assert` refactored in-Bit to drop their `bot_globals` parameter per the modularization strategic goal of reducing code smells); post-Bit-6.3 CalibrationEngine extraction + path-B singleton/helper relocation; Bit 3.1 moved ~1,200 lines of constants to `bot/constants.py`, Bit 3.2 moved ~790 lines of helpers to `bot/helpers/*`, Bit 4.1 moved 65 lines of Logger to `bot/logger.py`, Bit 4.2 moved 30 lines of TelegramNotifier to `bot/notifier.py`, Bit 4.3 moved 348 lines of KalshiClient to `bot/kalshi_client.py`, Bit 4.4 moved 141 lines of DeribitDVOLFetcher + CoinGlassFetcher to `bot/fetchers/*`, Bit 4.5a moved ~660 lines of CoinbaseFeed + OrderbookSchemaError + CrossExchangeFeed to `bot/feeds/*`, Bit 4.5b moved ~1,790 lines of KalshiFeed to `bot/feeds/kalshi.py`, Bit 6.1 moved ~960 lines of VolatilityEngine to `bot/engines/volatility.py`, Bit 6.2 moved ~197 lines of ProbabilityEngine to `bot/engines/probability.py`, Bit 6.3 moved ~1,004 lines of CalibrationEngine to `bot/engines/calibration.py` (Sprint 6 closed there), Bit 7.1 moved ~2,607 lines of StateManager to `bot/state.py`, Bit 8.1 moved ~9,085 lines of OpportunityScanner to `bot/scanner/__init__.py` — Sprint 8 closes here). Class line ranges below
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

## Public API surface (separate concern)

The class line ranges below describe `bot/_impl.py`'s **internal layout**.
For the **public API contract** of the `bot` package, the source of truth is
`tests/contracts/public_api.json` (Pillar 1 of the testing-foundation-sprint,
ticket 86b9ve0xt). Regenerate with `make api-snapshot-regen` after
intentional surface changes; CI gates on zero-diff via
`tests/contracts/test_public_api_snapshot.py`.

The snapshot has three layers:

1. **Static walk of public submodules under `bot.*`** — every public name
   in `bot.engines.*`, `bot.feeds.*`, `bot.fetchers.*`, `bot.helpers.*`,
   `bot.constants`, `bot.kalshi_client`, `bot.logger`, `bot.notifier`,
   etc., with full signatures (parameters + returns + class methods).
   `bot._impl` and `bot._thread_env` are excluded from this walk — their
   contents are covered by Layers 2 and 3.

2. **Static walk of locally-defined public classes inside `bot/_impl.py`** —
   post-Bit-8.1 the resident classes are `OrderFlowEngine`,
   `KalshiOrderFlowTracker`, `OrderExecutor`, `SettlementTracker`,
   `MainLoop` (verify with the class table below).
   The list is auto-derived, not hardcoded, so extractions naturally
   move classes out of this layer and into Layer 1 (`OpportunityScanner` →
   Bit 8.1; `StateManager` → Bit 7.1; `CalibrationEngine` → Bit 6.3;
   `ProbabilityEngine` → Bit 6.2; `VolatilityEngine` → Bit 6.1; etc.).
   Full class signatures.

3. **Runtime proxy probe** — imports `bot` at runtime, enumerates every
   public name accessible via `getattr(bot, name)` (the `_BotProxy`
   forwards attribute access to `bot._impl`, including `from config
   import *` and `from bot.constants import *` resolutions). Captures
   what static analysis cannot see: a removed star-import or lost
   re-export drops names from this list and surfaces as a snapshot diff.

This layout doc and the snapshot are orthogonal: this doc tracks *where*
code lives in `bot/_impl.py` (line ranges shift on every extraction);
`public_api.json` tracks *what* the public surface is (the 3-layer
structure keeps it stable across extractions).

## Top-level structure (approximate)

bot/_impl.py interleaves imports, residual helpers, classes, and a tail
entry point. Boundaries below are inexact (no AST split exists yet);
verify by reading 5-10 lines around each line number before quoting.

| Lines | Section |
|---|---|
| 1–~110 | Header import block (`bot._thread_env` imports BEFORE `numpy` — load-bearing per CLAUDE.md; `scripts/cal_mlp/` is also added to sys.path here for the bare `from integration import` calls later in the file). `from bot.constants import *` (Bit 3.1) at ~83; `from bot.helpers import *` + explicit underscore re-exports for `bot.helpers.validators` and `bot.helpers.breakers` (Bit 3.2) immediately after. `from bot.logger import Logger` (Bit 4.1), `from bot.notifier import TelegramNotifier` (Bit 4.2), `from bot.kalshi_client import KalshiClient` (Bit 4.3), `from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher` (Bit 4.4), and `from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError` (Bit 4.5a + 4.5b) follow at ~100–~104. Verify with `grep -n "^from bot\." bot/_impl.py`. |
| ~125–~558 | Residual helpers + runtime-state singletons that stay in `bot/_impl.py` (`_append_raw_api_journal`, `_HPSB_VALIDATOR_UNAVAILABLE_REASON`, `_HPSB_MISSING_BLEEDERS = ...` / `_BLEED_BLOCK_MISSING_BLEEDERS = ...` boot-time invocations of the validators that themselves moved to `bot/helpers/validators.py`, plus the orphan-DB watchdog Layer-3 helpers `_run_lsof_for_db`/`_get_pid_cmdline`/`_alert_orphan_db_holder`/`detect_orphan_db_holders`). `_swallow_persist_exception` moved to `bot/feeds/coinbase.py` in Bit 4.5a alongside its sole consumer. **Bit 6.3 path-B (2026-05-10) relocated** `_CALIBRATION_ENGINE` singleton + `_CAL_REGISTRY` dict + `_derive_subtype` + `_derive_asset_filter` + `_resolve_cal_engine` to `bot/engines/calibration.py`; bot/_impl.py reaches them via the `from bot.engines import calibration as _cal_state` alias near the top of the file. **Bit 8.1 path-A++ (2026-05-10) relocated** the `_TELEGRAM` module-level singleton to `bot/notifier.py` alongside its `TelegramNotifier` class; bot/_impl.py reaches it via `import bot.notifier as _telegram_state` plus `_telegram_state._TELEGRAM` module-attribute access (mirrors the `_cal_state` pattern; preserves mutation freshness across the bot/_impl.py + bot/scanner/__init__.py consumers). |
| 644–9461 | Class definitions (see table below). One module-level `def discover_active_windows()` sits in the body region between SettlementTracker's class body and the MainLoop class def; it ships with MainLoop in Bit 9.3. |

### Class-body end vs class-range note

The class table below uses *next-class-start − 1* as the range end. So
`SettlementTracker 15360–16538` includes the inter-class
`discover_active_windows()` def. The class body itself ends earlier.
The class size column counts those inter-class lines, which is
conservative (over-counts by ~80 for SettlementTracker; the chronic
~219-line over-count on `StateManager` from the pre-Bit-7.1 era is now
moot — StateManager moved out to `bot/state.py` in Bit 7.1).
Post-Bit-8.1 the `pre-OrderFlowEngine` gap (where the StateManager
class body used to live, now a one-line breadcrumb) and the
`KalshiOrderFlowTracker → OrderExecutor` gap (where OpportunityScanner
class body used to live, now a one-line breadcrumb) together contain
the breadcrumbs for every Sprint-4 + Sprint-6 + Sprint-7 + Sprint-8 leaf
extracted so far — CoinbaseFeed (Bit 4.5a), KalshiFeed (Bit 4.5b),
DeribitDVOLFetcher (Bit 4.4), CoinGlassFetcher (Bit 4.4),
CrossExchangeFeed (Bit 4.5a), VolatilityEngine (Bit 6.1),
ProbabilityEngine (Bit 6.2), CalibrationEngine (Bit 6.3),
StateManager (Bit 7.1), and OpportunityScanner (Bit 8.1) — all named
with one-line `→ bot/<dest>` breadcrumbs.
Bit 4.5a/4.5b/6.1/6.2/6.3 deliberately inserted breadcrumb comments at
each cut site (`# CoinbaseFeed → bot/feeds/coinbase.py (Bit 4.5a, 2026-05-08).`,
`# KalshiFeed → bot/feeds/kalshi.py (Bit 4.5b, 2026-05-09).`,
`# OrderbookSchemaError → bot/feeds/orderbook_schema.py (Bit 4.5a, 2026-05-08).`,
`# CrossExchangeFeed → bot/feeds/cross_exchange.py (Bit 4.5a, 2026-05-08).`,
`# VolatilityEngine → bot/engines/volatility.py (Bit 6.1, 2026-05-09).`,
`# ProbabilityEngine → bot/engines/probability.py (Bit 6.2, 2026-05-09).`,
`# CalibrationEngine → bot/engines/calibration.py (Bit 6.3, 2026-05-10).`)
and `tests/test_feeds_extraction.py::test_class_not_defined_in_bot_impl`
+ `tests/test_engines_extraction.py::test_class_not_defined_in_bot_impl`
+ `tests/test_engines_extraction.py::test_probability_class_not_defined_in_bot_impl`
+ `tests/test_engines_extraction.py::test_calibration_class_not_defined_in_bot_impl`
enforce the negative contract for all seven extracted classes.

## Classes (auto-verifiable)

Generated 2026-05-10 from `grep -nE '^class ' bot/_impl.py` (post-Smell-3 fu — `_calmlp_predictors` relocation to `scripts/cal_mlp/integration.py` shifted all 6 class start lines up; post-Bit-7.1 StateManager extraction; StateManager now lives in `bot/state.py` — first peer-module leaf at top-level `bot/` and Sprint 7 closeout — alongside the `bot/engines/` subpackage from Sprint 6 + the `bot/feeds/` and `bot/fetchers/` subpackages from Sprint 4).

| Lines | Class | Size |
|---|---|---|
| 653–774 | `OrderFlowEngine` | 122 |
| 775–989 | `KalshiOrderFlowTracker` | 215 |
| 990–6279 | `OrderExecutor` | 5290 |
| 6280–7458 | `SettlementTracker` | 1179 |
| 7459–9461 | `MainLoop` | 1999 |

## Project file map (root, 2026-05-05)

Live trading process:
- `bot/_impl.py` — main bot, all trading logic (9,461 lines post-Bit-8.1 OpportunityScanner extraction to `bot/scanner/__init__.py` (path-A++ — `_TELEGRAM` module-level singleton relocated to `bot/notifier.py` via the `_telegram_state._TELEGRAM` module-attribute access pattern; the 34 `OrderExecutor.X` static-method call sites preserved via a `_get_order_executor()` single-name late-binding helper). Sprint 8 closes here.
- `bot/constants.py` — module-level UPPER_SNAKE constants extracted from `bot/_impl.py` (Bit 3.1, 1,722 lines, 484 constants). Re-exported into `bot/_impl.py` via `from bot.constants import *` near top of file.
- `bot/helpers/` — feature/sizing/cell-block helpers extracted from `bot/_impl.py` (Bit 3.2, ~790 lines, 25 functions across 9 submodules: `time_features`, `derived_features`, `tm_sweep`, `sizing`, `cell_blocks`, `strings`, `strategy`, `validators`, `breakers`). Re-exported into `bot/_impl.py` via `from bot.helpers import *` plus explicit underscore re-exports for `validators` (3) and `breakers` (5) — star-import skips underscored names.
- `bot/logger.py` — `Logger` class (structured JSONL logging with fill dedup) extracted from `bot/_impl.py` (Bit 4.1, ~90 lines, stdlib + `bot.constants` only). Re-exported into `bot/_impl.py` via `from bot.logger import Logger` so `MainLoop.__init__` instantiation + type annotations on `OpportunityScanner`/`OrderExecutor`/`SettlementTracker` resolve.
- `bot/notifier.py` — `TelegramNotifier` class (fire-and-forget Telegram alerts) extracted from `bot/_impl.py` (Bit 4.2, ~50 lines, stdlib + `requests` only — zero `bot.constants` deps, zero `bot.helpers` deps). Re-exported into `bot/_impl.py` via `from bot.notifier import TelegramNotifier` so the runtime construction in `MainLoop.__init__` (search `self.telegram = TelegramNotifier` for the current line) resolves. **Bit 8.1 path-A++ (2026-05-10)**: also hosts the module-level `_TELEGRAM: Optional["TelegramNotifier"] = None` singleton (relocated from `bot/_impl.py`). Both `bot/_impl.py` and `bot/scanner/__init__.py` reach it via `import bot.notifier as _telegram_state` plus `_telegram_state._TELEGRAM` module-attribute access — the access pattern preserves mutation freshness across consumers (parallel to the Bit 6.3 path-B `_cal_state._CALIBRATION_ENGINE` pattern). `MainLoop.__init__` writes via `_telegram_state._TELEGRAM = self.telegram` (drops the previous `global _TELEGRAM` declaration). The `Optional["TelegramNotifier"]` forward-ref on the singleton type annotation lives at the def site here and has no in-tree `typing.get_type_hints` consumer.
- `bot/kalshi_client.py` — `KalshiClient` class (Kalshi REST API auth + rate limiting + breaker-wrapped GETs + raw POST/DELETE writes) extracted from `bot/_impl.py` (Bit 4.3, ~388 lines, stdlib + `requests` + `cryptography` + `bot.constants` (4 explicit names) + `bot.helpers.breakers` (4 explicit names) + `circuit_breaker.REGISTRY`). Re-exported into `bot/_impl.py` via `from bot.kalshi_client import KalshiClient` so MainLoop construction (`self.client = KalshiClient(...)`) + 7 type-annotation sites (`reconcile_with_api`, `_reconcile_positions`, `_reconcile_orders`, `OpportunityScanner.__init__`, `OrderExecutor.__init__`, `SettlementTracker.__init__`, `discover_active_windows()`) all resolve.
- `bot/fetchers/` — daemon-thread HTTP fetcher classes extracted from `bot/_impl.py` (Bit 4.4, ~237 lines across 3 files: `__init__.py` re-export shim, `deribit.py` for `DeribitDVOLFetcher` (Deribit DVOL implied-vol index, BTC/ETH), `coinglass.py` for `CoinGlassFetcher` (CoinGlass funding rates, BTC/ETH/SOL/XRP — disabled when `COINGLASS_API_KEY` env unset)). Re-exported into `bot/_impl.py` via `from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher` so MainLoop construction (`self.dvol_fetcher = DeribitDVOLFetcher()`, `self.coinglass = CoinGlassFetcher()`) + the `Optional[DeribitDVOLFetcher]` type annotation on `VolatilityEngine.__init__` resolve. `DVOL_ANNUALIZED_TO_5S` (the only constant Bit 3.1 left in `config.py`) is imported via `from config import DVOL_ANNUALIZED_TO_5S` rather than `bot.constants`.
- `bot/feeds/` — WebSocket feed classes extracted from `bot/_impl.py` (Bit 4.5a + Bit 4.5b, ~2,580 lines across 5 files: `__init__.py` re-export shim, `coinbase.py` for `CoinbaseFeed` (Coinbase WS spot prices BTC/ETH/SOL/XRP with persistent 30-min snapshot buffer; also hosts the `_swallow_persist_exception` done-callback helper alongside its sole consumer; Bit 4.5a), `orderbook_schema.py` for `OrderbookSchemaError` (exception raised by KalshiFeed on Kalshi WS schema migrations; Bit 4.5a), `cross_exchange.py` for `CrossExchangeFeed` (Binance/Kraken/Bybit WS feeds for lead/lag detection — takes a `CoinbaseFeed` reference at construction time via `from bot.feeds.coinbase import CoinbaseFeed`; Bit 4.5a), `kalshi.py` for `KalshiFeed` (Kalshi WS feed for fill notifications + per-ticker orderbook snapshots/deltas — largest leaf in the Sprint 4 modularization track at ~1,790 lines; uses `from bot.feeds.orderbook_schema import OrderbookSchemaError` for the sibling exception; Bit 4.5b)). Re-exported into `bot/_impl.py` via `from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError` so MainLoop construction (`self.feed = CoinbaseFeed()`, `self.cross_feed = CrossExchangeFeed(self.feed) if CROSS_EXCHANGE_ENABLED else None`, `self.kalshi_feed = KalshiFeed(api_key, self.client.private_key)`) + `feed: CoinbaseFeed` type annotations on `VolatilityEngine.__init__` (now in `bot/engines/volatility.py`) and `OpportunityScanner.__init__` resolve. `ASSETS` (Bit 3.1 left in `config.py`) imported via `from config import ASSETS` by `coinbase.py` + `cross_exchange.py`.
- `bot/engines/` — math-layer engine classes extracted from `bot/_impl.py` (Bit 6.1 + 6.2 + 6.3 shipped — Sprint 6 closes here; ~2,170 lines across 4 files: `__init__.py` re-export shim, `volatility.py` for `VolatilityEngine` (Realized Kernel + Deribit DVOL volatility engine with adaptive jump detection and EGARCH variance-space blending; ~960 lines; Bit 6.1, byte-for-byte), `probability.py` for `ProbabilityEngine` (Student-t / NIG win-prob CDF + adaptive calibration cascade with BLR clamp + model-vs-market sanity gate; ~197 lines, 5 staticmethods; Bit 6.2), `calibration.py` for `CalibrationEngine` (adaptive 3-method calibrator: Platt / Beta / BLR + STC-aware Platt + temperature-scaling shadow pipeline + lifecycle methods _load_state/_save_state/load_training_data_from_db; ~1,004-line class body, 29 methods, 2 staticmethods; **Bit 6.3 path-B**: byte-for-byte class body PLUS singleton/helper relocation — the `_CALIBRATION_ENGINE` singleton, `_CAL_REGISTRY` dict, and `_derive_subtype`/`_derive_asset_filter`/`_resolve_cal_engine` helpers live alongside the class in this file)). Re-exported into `bot/_impl.py` via `from bot.engines import VolatilityEngine, ProbabilityEngine, CalibrationEngine` so MainLoop construction (`self.vol = VolatilityEngine(self.feed, dvol_fetcher=self.dvol_fetcher, egarch_estimator=..., mz_tracker=...)` + the 3 `CalibrationEngine(...)` instantiation sites: 1× legacy 15M `_CALIBRATION_ENGINE` singleton + 2× per-product `_CAL_REGISTRY` populator) + the `vol: VolatilityEngine` type annotation on `OpportunityScanner.__init__` + the bare-name `ProbabilityEngine.X(...)` call sites in `bot/_impl.py` (scan-loop edge computation, counterfactual probability, dynamic cap lookup) all resolve. **`volatility.py` imports**: 35 explicit names from `bot.constants` + 5 from `config` (ASSETS, EGARCH_BLEND_LOG_INTERVAL, EGARCH_BLEND_SHADOW_MODE, EGARCH_RV_RATIO_CLAMP, VOL_RETURN_INTERVAL — all pre-Bit-3.1 constants that stayed in `config.py`) + `compute_tv_rk_weights` from `models` + `CoinbaseFeed` from `bot.feeds.coinbase` + `DeribitDVOLFetcher` from `bot.fetchers.deribit` (the two type-annotation pins per L33). `Optional['EGARCHEstimator']` and `Optional['MincerZarnowitzTracker']` remain string-quoted forward refs because both classes still live in `models.py`. **`probability.py` imports**: 5 explicit names from `bot.constants` (DISCREPANCY_PRICE, DISCREPANCY_PROB, DYNAMIC_CAP_SCHEDULE, FIFTEEN_M_CALIBRATION_ENABLED, HOURLY_DYNAMIC_CAP_SCHEDULE) + 4 from `config` (BETA_SLOPE, DIST_CONFIG, MAX_EFFECTIVE_PROB, STUDENT_T_DF — all pre-Bit-3.1 constants that stayed in `config.py`; the L39 partition was the Plan-agent CRITICAL catch in pre-flight) + `get_market_config` from `market_config` + `student_t` (aliased from `t`) and `norminvgauss` from `scipy.stats` (the only `bot/engines/` module that imports scipy; the per-module forbidden-imports gate in `tests/test_engines_extraction.py` allows it for probability while still banning numpy/torch/sklearn/pandas) + `from bot.engines import calibration as _cal_state` (path-B: replaces the Bit 6.2 method-body late-binding `from bot import _impl as _bot_impl`). **`calibration.py` imports**: 10 explicit names from `bot.constants` (`CALIBRATION_BRIER_WINDOW`, `CALIBRATION_MIN_SAMPLES_BETA`, `CALIBRATION_MIN_SAMPLES_BLR`, `CALIBRATION_MIN_SAMPLES_PLATT`, `CALIBRATION_RETRAIN_INTERVAL`, `CALIBRATION_STATE_PATH`, `MARKET_BLEND_W`, `MIN_EDGE_PCT`, `SHADOW_BLEND_W`, `SHADOW_CAL_PIPELINE`) + 3 from `config` (`BETA_SLOPE`, `MAX_EFFECTIVE_PROB`, `NUMERICAL_SAFETY_CEILING` — same pre-Bit-3.1 leftover-in-config pattern as Bits 6.1 and 6.2; pre-flight L39 verified) + `get_cal_excluded_types` + `get_market_config` from `market_config` + `calculate_taker_fee` from `models` + stdlib (`math`, `os`, `time`, `json`, `logging`, `datetime`, `timezone`, `deque`, `Optional`, `Dict`). **Bit 6.3 path-B singleton + helper relocation (2026-05-10)**: `_CALIBRATION_ENGINE` (typed `Optional[CalibrationEngine]` — bare, no quotes, since the class is now in scope), `_CAL_REGISTRY` (typed `Dict[str, CalibrationEngine]`), `_derive_subtype`, `_derive_asset_filter`, `_resolve_cal_engine` ALL moved from `bot/_impl.py` into `bot/engines/calibration.py` alongside the class. The class itself never read these names (free-variable analysis returned zero hits); they're written by `MainLoop.__init__` (3 instantiation sites) and read by callers OUTSIDE the class — `OpportunityScanner.scan()` and downstream sites. Both `bot/_impl.py` and `bot/engines/probability.py` reach them via top-level `from bot.engines import calibration as _cal_state` plus `_cal_state.X` attribute access — module-attribute access preserves singleton-mutation freshness without late-binding. The path-B move LIFTED the Bit 6.2 `from bot import _impl as _bot_impl` late-binding inside ProbabilityEngine and REMOVED the matching `.importlinter` `bot.engines.probability -> bot._impl` ignore_imports carve-out (Pillar 2). Sprint 6 closes here.
- `bot/scanner/__init__.py` — `OpportunityScanner` class (the trading hot path; ~9,085-line class body, 36 methods + 7 staticmethods) extracted from `bot/_impl.py` (Bit 8.1, 2026-05-10). Strict-ban for torch/sklearn/pandas (zero direct numerical imports; numpy/scipy reached transitively through `models.PositionSizer` etc.). Re-exported into `bot/_impl.py` via `from bot.scanner import OpportunityScanner` so MainLoop construction (`self.scanner = OpportunityScanner(..., main_loop=self)`) + 13 consumer call sites (12 `OrderExecutor.X` static-method calls + 1 MainLoop static-method call) + ~30 test-suite instantiation/`bot.OpportunityScanner.X` references all resolve via the proxy chain. **Path-A++ extraction (NOT byte-for-byte)**: in-Bit relocation of `_TELEGRAM` module-level singleton from `bot/_impl.py` to `bot/notifier.py` (with the `_telegram_state._TELEGRAM` module-attribute access pattern shared between this module and `bot/_impl.py`); the 34 `OrderExecutor.X(...)` static-method call sites in `scan()` use a `_get_order_executor()` single-name late-binding helper (returns `bot._impl.OrderExecutor` via method-body `import bot._impl`; lifts when Sprint 9 Bit 9.1 extracts OrderExecutor to `bot/executor.py`). **Imports**: 205 explicit names from `bot.constants` + 8 from `config` (ASSETS, DRAWDOWN_HALF/HALT/QUARTER_THRESHOLD, EGARCH_BLEND_SHADOW_MODE, MAX_RISK_PER_TRADE, NUMERICAL_SAFETY_CEILING, SIZING_TIERS) + 9 from `bot.helpers` (cell_blocks/sizing/strategy/strings/tm_sweep helpers — explicit per-leaf imports replacing `from bot.helpers import *` star-import laundering per L40 lesson) + `KalshiClient` from `bot.kalshi_client` (type annotation) + `StateManager` from `bot.state` (type annotation + heavy method-call surface) + `CoinbaseFeed` from `bot.feeds` (type annotation) + `VolatilityEngine` (type annotation) + `ProbabilityEngine` (15 bare-name `ProbabilityEngine.X(...)` static-method call sites — scan-loop edge computation, counterfactual prob, dynamic cap) from `bot.engines` + `from bot.engines import calibration as _cal_state` (16 `_cal_state._CALIBRATION_ENGINE` / `._resolve_cal_engine` reads — Bit 6.3 path-B alias) + `Logger` from `bot.logger` (type annotation) + `import bot.notifier as _telegram_state` (Bit 8.1 path-A++ alias for `_telegram_state._TELEGRAM` reads) + `tracked_write` from `bot.db_writer_registry` (instrumentation) + `_calmlp_predictors`, `annotate_evaluation_async_enqueue as _calmlp_annotate_async` from `integration` (mirrors bot/_impl.py:67) + `PositionSizer`, `calculate_fee`, `calculate_taker_fee`, `strategy_to_group` from `models` + `get_market_config`, `validate_market_configs` from `market_config` + stdlib (datetime, deque, inspect, json, logging, math, os, random, re, sys, threading, time, typing). The `Optional["OrderFlowEngine"]` and `Optional["KalshiOrderFlowTracker"]` annotations in `__init__` are quoted forward-refs — both classes still live in `bot/_impl.py` (lines 653 + 775); top-level import would partial-module read at the line-115 re-export firing time. **`.importlinter` carve-out**: `scanner-no-impl-toplevel` contract added (parallel to Bit 7.1's `state-no-impl-toplevel`); permits the method-body `import bot._impl` inside `_get_order_executor()`. Sister Bit 8.2 (`bot/scanner/CLAUDE.md`) ships separately per master plan; Bit 8.3 internal scanner split (into `discover.py` / `evaluate.py` / `gates.py` / `shadow.py`) deferred 7d post-Bit-8.1 per master plan. Sprint 8 closes here.
- `bot/state.py` — `StateManager` class (SQLite-backed persistent state for positions/orders/evaluated_opportunities/settled_trades/etc., 2,607-line class body, 38 methods, 2 staticmethods) extracted from `bot/_impl.py` (Bit 7.1, 2026-05-10). Strict-ban for numerical imports (zero numpy/scipy/torch/sklearn/pandas — pure stdlib + sqlite3). Re-exported into `bot/_impl.py` via `from bot.state import StateManager` so `MainLoop.__init__`'s `self.state = StateManager()` + 3 consumer-class type annotations (`OpportunityScanner.__init__`, `OrderExecutor.__init__`, `SettlementTracker.__init__`: `state: StateManager`) + ~50 test-suite instantiation sites resolve. **Path-A++ extraction (NOT byte-for-byte)**: in-Bit refactor of `scripts/cal_mlp/integration.py::parity_assert(conn) -> tuple[str, int]` and `sizing_parity_assert(conn, *, rowid, compute_for_15m_main_path)` dropped their `bot_globals` parameter — the laundered-namespace coupling smell is fixed in-Bit per the modularization strategic goal. The `_get_compute_for_15m_main_path()` single-name late-binding helper inside `bot/state.py` returns `bot._impl.compute_for_15m_main_path` (the closure created via `make_compute_for_15m_main_path()` — search anchor: `compute_for_15m_main_path = make_compute_for_15m_main_path`). **Bit 7.1 fu (Smell 4, ticket 86b9vhccw, 2026-05-10)**: `make_compute_for_15m_main_path` itself was subsequently refactored to drop its `bot_globals: dict` parameter — the closure now imports its 11 dependent names from `bot.constants` + `config` inside the function body (mirrors path-A++). **Imports**: 5 explicit names from `bot.constants` (DB_PATH, OB_CACHE_EVICT_AGE_SECONDS, OB_CACHE_FRESHNESS_SECONDS, SOL_RESCUE_CONTRACT_CAP, STACKING_ENABLED) + 3 from `bot.db_writer_registry` (recent_writes, snapshot_active, tracked_write — load-bearing for the cf34b5c db-locked SLOW_BATCH_BREAKDOWN instrumentation surface) + `from bot.engines import calibration as _cal_state` (Bit 6.3 path-B alias used at 3 sites in the failure-path logger reading `_cal_state._CALIBRATION_ENGINE` and `_cal_state._resolve_cal_engine`) + `KalshiClient` from `bot.kalshi_client` (type annotation only on `reconcile_with_api`/`_reconcile_positions`/`_reconcile_orders`) + 5 explicit names from `bot.helpers` (compute_derived_features, compute_time_regime_features, dollars_str_to_cents, fp_str_to_int, tm_sweep_counterfactual_pnl — these came into `bot/_impl.py` via `from bot.helpers import *` star-import laundering pre-extraction; explicit per-leaf imports replaced the smell at extraction time per L40 lesson) + 5 from `integration` (CalMLPParityError, CalMLPSchemaError, migrate_schema, parity_assert, sizing_parity_assert — refactored in-Bit per path-A++) + 2 from `models` (calculate_fee, strategy_to_group) + stdlib (datetime, json, logging, random, re, sqlite3, threading, time, datetime.timezone, typing). Sister Bit 7.2 (`agent_docs/db_schema.md` refresh) shipped in same atomic commit — the schema doc's source-of-truth for the 17 tables is now `StateManager._create_tables` in this file. Sprint 7 closes here.
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

Tests: `tests/` (~4,190 collected post-Bit-6.3 — varies ~5 with hypothesis parameterization; verify with `pytest tests/ --collect-only -q | tail -1`. Bit 6.3 added CalibrationEngine cases in `tests/test_engines_extraction.py` — identity / drift-guard / forbidden-imports / module-level singleton-relocation pins (path-B) / behavioral module-attribute access regression — atop the Bit 6.1 + 6.2 surface. Pillar 3 added the `tests/equivalence/` engine-equivalence harness (24 tests, hypothesis + pytest-regressions snapshots). Unit/integration/regression split pending Phase JJ)

KB (local-only, never committed): `kb/`, `kb-research/`

## Modularization destination

Per `kb/decisions/repo-modularization-plan-may05.md`:
- Sprint 3 → constants + helpers extracted to `bot/constants.py` + `bot/helpers/*` (Bit 3.1 + Bit 3.2 + Bit 3.3 SHIPPED 2026-05-08)
- Sprint 4–6 → leaf classes (Bit 4.1 Logger SHIPPED 2026-05-08 → `bot/logger.py`; Bit 4.2 TelegramNotifier SHIPPED 2026-05-08 → `bot/notifier.py`; Bit 4.3 KalshiClient SHIPPED 2026-05-08 → `bot/kalshi_client.py`; Bit 4.4 fetchers (DeribitDVOLFetcher + CoinGlassFetcher) SHIPPED 2026-05-08 → `bot/fetchers/`; Bit 4.5a small feeds (CoinbaseFeed + OrderbookSchemaError + CrossExchangeFeed) SHIPPED 2026-05-08 → `bot/feeds/`; Bit 4.5b KalshiFeed SHIPPED 2026-05-09 → `bot/feeds/kalshi.py` — largest single leaf at ~1,790 lines; Bit 6.1 VolatilityEngine SHIPPED 2026-05-09 → `bot/engines/volatility.py` — first leaf in new `bot/engines/` subpackage at ~960 lines; Bit 6.2 ProbabilityEngine SHIPPED 2026-05-09 → `bot/engines/probability.py` — second leaf at ~197 lines, first non-byte-for-byte extraction (late-binding pattern for mutable bot._impl singletons); Bit 6.3 CalibrationEngine SHIPPED 2026-05-10 → `bot/engines/calibration.py` — third leaf at ~1,004 lines (29 methods, 2 staticmethods); **path-B refactor** moved the `_CALIBRATION_ENGINE` singleton + `_CAL_REGISTRY` dict + `_derive_subtype`/`_derive_asset_filter`/`_resolve_cal_engine` helpers alongside the class (lifting Bit 6.2's late-binding inside ProbabilityEngine + removing the `.importlinter` carve-out); Sprint 6 closes here)
- Sprint 7 → StateManager SHIPPED 2026-05-10 → `bot/state.py` — first peer-module top-level extraction (path-A++; in-Bit smell-fix on parity_assert/sizing_parity_assert in scripts/cal_mlp/integration.py); Sprint 7 closes here
- Sprint 8 → OpportunityScanner SHIPPED 2026-05-10 → `bot/scanner/__init__.py` — largest single-class extraction at ~9,085 lines (path-A++; in-Bit `_TELEGRAM` singleton relocation to `bot/notifier.py` via `_telegram_state._TELEGRAM` module-attribute access pattern; 34 `OrderExecutor.X` static-method call sites preserved via `_get_order_executor()` single-name late-binding helper). Bit 8.2 `bot/scanner/CLAUDE.md` ships separately; Bit 8.3 internal split (`discover.py`/`evaluate.py`/`gates.py`/`shadow.py`) deferred 7d post-Bit-8.1. Sprint 8 closes here.
- Sprint 9 → OrderExecutor + SettlementTracker + MainLoop → `bot/`
- After Sprint 9, bot/_impl.py = ~50-line entrypoint.
