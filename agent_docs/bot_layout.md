# bot/_impl.py Layout

bot/_impl.py is **26,999 lines** as of 2026-05-08 (post-Bit-3.1 constants extraction; ~1,200 lines moved to `bot/constants.py`). Class line ranges below
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
| 1–~245 | Header import block (`bot._thread_env` imports BEFORE `numpy` — load-bearing per CLAUDE.md; `scripts/cal_mlp/` is also added to sys.path here for the bare `from integration import` calls later in the file). New `from bot.constants import *` lands at line 240, after `from models import (...)`. |
| ~245–~1140 | Helpers (cell-block predicates `should_block_*`, `compute_derived_features`, `compute_time_regime_features`, `evaluate_execution_strategy()`, sizing helpers, dollars/fp helpers, `_validate_bleeders_against_runtime_registry`). Pre-Bit-3.1 this region was constants + helpers interleaved across ~5 gap regions; Bit 3.1 cut all 484 module-level UPPER_SNAKE constants to `bot/constants.py`. Helpers stay; future split: helpers → `bot/helpers/*` (Bit 3.2). |
| 1178–26999 | Class definitions (see table below). One module-level `def discover_active_windows()` sits in the body region between SettlementTracker's class body and the MainLoop class def at line 25008; it ships with MainLoop in Bit 9.3. |

### Class-body end vs class-range note

The class table below uses *next-class-start − 1* as the range end. So
`SettlementTracker 23829–25007` includes the inter-class
`discover_active_windows()` def. The class body itself
ends earlier. The class size column counts those
inter-class lines, which is conservative (over-counts by ~80 for
SettlementTracker).

## Classes (auto-verifiable)

Generated 2026-05-08 from `grep -nE '^class ' bot/_impl.py` (post-Bit-3.1 constants extraction).

| Lines | Class | Size |
|---|---|---|
| 1178–1530 | `KalshiClient` | 353 |
| 1531–1593 | `Logger` | 63 |
| 1594–1842 | `TelegramNotifier` | 249 |
| 1843–4412 | `StateManager` | 2570 |
| 4413–4734 | `CoinbaseFeed` | 322 |
| 4735–4746 | `OrderbookSchemaError` | 12 |
| 4747–6516 | `KalshiFeed` | 1770 |
| 6517–6597 | `DeribitDVOLFetcher` | 81 |
| 6598–6925 | `CrossExchangeFeed` | 328 |
| 6926–7002 | `CoinGlassFetcher` | 77 |
| 7003–7124 | `OrderFlowEngine` | 122 |
| 7125–7285 | `KalshiOrderFlowTracker` | 161 |
| 7286–8245 | `VolatilityEngine` | 960 |
| 8246–8448 | `ProbabilityEngine` | 203 |
| 8449–9458 | `CalibrationEngine` | 1010 |
| 9459–18538 | `OpportunityScanner` | 9080 |
| 18539–23828 | `OrderExecutor` | 5290 |
| 23829–25007 | `SettlementTracker` | 1179 |
| 25008–26999 | `MainLoop` | 1992 |

## Project file map (root, 2026-05-05)

Live trading process:
- `bot/_impl.py` — main bot, all trading logic (26,999 lines post-Bit-3.1)
- `bot/constants.py` — module-level UPPER_SNAKE constants extracted from `bot/_impl.py` (Bit 3.1, 1,722 lines, 484 constants). Re-exported into `bot/_impl.py` via `from bot.constants import *` near top of file.
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
- Sprint 3 → constants + helpers extracted to `bot/constants.py` + `bot/helpers/*`
- Sprint 4–6 → leaf classes (Logger, TelegramNotifier, KalshiClient, fetchers, feeds, engines)
- Sprint 7 → StateManager → `bot/state.py`
- Sprint 8 → OpportunityScanner → `bot/scanner/` (verbatim then internal split)
- Sprint 9 → OrderExecutor + SettlementTracker + MainLoop → `bot/`
- After Sprint 9, bot/_impl.py = ~50-line entrypoint.
