# bot/_impl.py Layout

bot/_impl.py is **26,147 lines** as of 2026-05-08 (post-Bit-4.1 Logger extraction; Bit 3.1 moved ~1,200 lines of constants to `bot/constants.py`, Bit 3.2 moved ~790 lines of helpers to `bot/helpers/*`, Bit 4.1 moved 65 lines of Logger to `bot/logger.py`). Class line ranges below
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

bot/_impl.py interleaves imports, constants, helpers, classes, and a tail
entry point. Boundaries below are inexact (no AST split exists yet);
verify by reading 5-10 lines around each line number before quoting.

| Lines | Section |
|---|---|
| 1–~250 | Header import block (`bot._thread_env` imports BEFORE `numpy` — load-bearing per CLAUDE.md; `scripts/cal_mlp/` is also added to sys.path here for the bare `from integration import` calls later in the file). `from bot.constants import *` (Bit 3.1) at ~240; `from bot.helpers import *` + explicit underscore re-exports for `bot.helpers.validators` and `bot.helpers.breakers` (Bit 3.2) immediately after. |
| ~250–~385 | Residual helpers + runtime-state singletons that stay in `bot/_impl.py` (`_swallow_persist_exception`, `_derive_subtype`/`_derive_asset_filter`/`_resolve_cal_engine` — deferred to Sprint 6 with `CalibrationEngine` because they read `_CAL_REGISTRY` module-level mutable state — `_append_raw_api_journal`, `_HPSB_MISSING_BLEEDERS = ...` / `_BLEED_BLOCK_MISSING_BLEEDERS = ...` boot-time invocations of the validators that themselves moved to `bot/helpers/validators.py`). |
| 388–26147 | Class definitions (see table below). One module-level `def discover_active_windows()` sits in the body region between SettlementTracker's class body and the MainLoop class def at line 24151; it ships with MainLoop in Bit 9.3. |

### Class-body end vs class-range note

The class table below uses *next-class-start − 1* as the range end. So
`SettlementTracker 22972–24150` includes the inter-class
`discover_active_windows()` def. The class body itself
ends earlier. The class size column counts those
inter-class lines, which is conservative (over-counts by ~80 for
SettlementTracker).

## Classes (auto-verifiable)

Generated 2026-05-08 from `grep -nE '^class ' bot/_impl.py` (post-Bit-4.1 Logger extraction; Logger now lives in `bot/logger.py`).

| Lines | Class | Size |
|---|---|---|
| 388–736 | `KalshiClient` | 349 |
| 737–985 | `TelegramNotifier` | 249 |
| 986–3555 | `StateManager` | 2570 |
| 3556–3877 | `CoinbaseFeed` | 322 |
| 3878–3889 | `OrderbookSchemaError` | 12 |
| 3890–5659 | `KalshiFeed` | 1770 |
| 5660–5740 | `DeribitDVOLFetcher` | 81 |
| 5741–6068 | `CrossExchangeFeed` | 328 |
| 6069–6145 | `CoinGlassFetcher` | 77 |
| 6146–6267 | `OrderFlowEngine` | 122 |
| 6268–6428 | `KalshiOrderFlowTracker` | 161 |
| 6429–7388 | `VolatilityEngine` | 960 |
| 7389–7591 | `ProbabilityEngine` | 203 |
| 7592–8601 | `CalibrationEngine` | 1010 |
| 8602–17681 | `OpportunityScanner` | 9080 |
| 17682–22971 | `OrderExecutor` | 5290 |
| 22972–24150 | `SettlementTracker` | 1179 |
| 24151–26147 | `MainLoop` | 1997 |

## Project file map (root, 2026-05-05)

Live trading process:
- `bot/_impl.py` — main bot, all trading logic (26,147 lines post-Bit-4.1)
- `bot/constants.py` — module-level UPPER_SNAKE constants extracted from `bot/_impl.py` (Bit 3.1, 1,722 lines, 484 constants). Re-exported into `bot/_impl.py` via `from bot.constants import *` near top of file.
- `bot/helpers/` — feature/sizing/cell-block helpers extracted from `bot/_impl.py` (Bit 3.2, ~790 lines, 25 functions across 9 submodules: `time_features`, `derived_features`, `tm_sweep`, `sizing`, `cell_blocks`, `strings`, `strategy`, `validators`, `breakers`). Re-exported into `bot/_impl.py` via `from bot.helpers import *` plus explicit underscore re-exports for `validators` (3) and `breakers` (5) — star-import skips underscored names.
- `bot/logger.py` — `Logger` class (structured JSONL logging with fill dedup) extracted from `bot/_impl.py` (Bit 4.1, ~90 lines, stdlib + `bot.constants` only). Re-exported into `bot/_impl.py` via `from bot.logger import Logger` so `MainLoop.__init__` instantiation + type annotations on `OpportunityScanner`/`OrderExecutor`/`SettlementTracker` resolve.
- `ops/kalshi-bot.service` — systemd unit, source of truth (installed via `ops/install.sh`); see `ops/CLAUDE.md`.
- `start.sh` — wrapper invoked by `ops/kalshi-bot.service` (venv + .env + bot/_impl.py)
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

Tests: `tests/` (~3,000 collected; verify with `pytest tests/ --collect-only -q | tail -5`. Unit/integration/regression split pending Phase JJ)

KB (local-only, never committed): `kb/`, `kb-research/`

## Modularization destination

Per `kb/decisions/repo-modularization-plan-may05.md`:
- Sprint 3 → constants + helpers extracted to `bot/constants.py` + `bot/helpers/*` (Bit 3.1 + Bit 3.2 + Bit 3.3 SHIPPED 2026-05-08)
- Sprint 4–6 → leaf classes (Bit 4.1 Logger SHIPPED 2026-05-08 → `bot/logger.py`; remaining: TelegramNotifier, KalshiClient, fetchers, feeds, engines)
- Sprint 7 → StateManager → `bot/state.py`
- Sprint 8 → OpportunityScanner → `bot/scanner/` (verbatim then internal split)
- Sprint 9 → OrderExecutor + SettlementTracker + MainLoop → `bot/`
- After Sprint 9, bot/_impl.py = ~50-line entrypoint.
