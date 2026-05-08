# bot/_impl.py Layout

bot/_impl.py is **28,220 lines** as of 2026-05-08 (post-Bit-3.0.5 validator decoupling). Class line ranges below
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
| 1–~60 | Header import block (`bot._thread_env` imports BEFORE `numpy` — load-bearing per CLAUDE.md; `scripts/cal_mlp/` is also added to sys.path here for the bare `from integration import` calls later in the file) |
| ~60–~2150 | Module-level constants AND helpers, interleaved (cell-block bleeder lists, MIN_EDGE/TM_SWEEP tables, validators, `compute_derived_features`, `compute_time_regime_features`, sizing helpers, dollars/fp helpers, cell-block predicates `should_block_*`). Future split: constants → `bot/constants.py` (Bit 3.1); helpers → `bot/helpers/*` (Bit 3.2). The exact split-point lands in Bit 3.1. |
| ~2150–~2364 | `evaluate_execution_strategy()` + tail helpers. (Future home undecided — likely `bot/helpers/execution.py` since it's diagnostic-only.) |
| 2387–28220 | Class definitions (see table below). One module-level `def discover_active_windows()` sits in the body region between SettlementTracker's class body and the MainLoop class def at line 26229; it ships with MainLoop in Bit 9.3. |

### Class-body end vs class-range note

The class table below uses *next-class-start − 1* as the range end. So
`SettlementTracker 25050–26228` includes the inter-class
`discover_active_windows()` def at line 26144. The class body itself
ends earlier (around line 26143). The class size column counts those
inter-class lines, which is conservative (over-counts by ~80 for
SettlementTracker).

## Classes (auto-verifiable)

Generated 2026-05-08 from `grep -nE '^class ' bot/_impl.py` (post-Bit-3.0.5 validator decoupling).

| Lines | Class | Size |
|---|---|---|
| 2387–2739 | `KalshiClient` | 353 |
| 2740–2802 | `Logger` | 63 |
| 2803–3051 | `TelegramNotifier` | 249 |
| 3052–5621 | `StateManager` | 2570 |
| 5622–5955 | `CoinbaseFeed` | 334 |
| 5956–5967 | `OrderbookSchemaError` | 12 |
| 5968–7737 | `KalshiFeed` | 1770 |
| 7738–7818 | `DeribitDVOLFetcher` | 81 |
| 7819–8146 | `CrossExchangeFeed` | 328 |
| 8147–8223 | `CoinGlassFetcher` | 77 |
| 8224–8345 | `OrderFlowEngine` | 122 |
| 8346–8506 | `KalshiOrderFlowTracker` | 161 |
| 8507–9466 | `VolatilityEngine` | 960 |
| 9467–9669 | `ProbabilityEngine` | 203 |
| 9670–10679 | `CalibrationEngine` | 1010 |
| 10680–19759 | `OpportunityScanner` | 9080 |
| 19760–25049 | `OrderExecutor` | 5290 |
| 25050–26228 | `SettlementTracker` | 1179 |
| 26229–28220 | `MainLoop` | 1992 |

## Project file map (root, 2026-05-05)

Live trading process:
- `bot/_impl.py` — main bot, all trading logic (28,220 lines post-Bit-3.0.5)
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
