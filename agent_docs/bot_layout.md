# bot/_impl.py Layout

bot/_impl.py is **28,160 lines** as of 2026-05-07 (Bit 2.1a rename). Class line ranges below
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
| 1–~60 | Header import block (sys.path tweak at line 11 inserts `scripts/cal_mlp/`; `_thread_env` import at line 12 loads BEFORE `numpy` at line 34 — load-bearing per CLAUDE.md) |
| ~60–~2150 | Module-level constants AND helpers, interleaved (cell-block bleeder lists, MIN_EDGE/TM_SWEEP tables, validators, `compute_derived_features`, `compute_time_regime_features`, sizing helpers, dollars/fp helpers, cell-block predicates `should_block_*`). Future split: constants → `bot/constants.py` (Bit 3.1); helpers → `bot/helpers/*` (Bit 3.2). The exact split-point lands in Bit 3.1. |
| ~2150–~2364 | `evaluate_execution_strategy()` + tail helpers. (Future home undecided — likely `bot/helpers/execution.py` since it's diagnostic-only.) |
| 2365–28160 | Class definitions (see table below). One module-level `def discover_active_windows()` at line 26084 sits in the body region between SettlementTracker's class body and the MainLoop class def at line 26169; it ships with MainLoop in Bit 9.3. |

### Class-body end vs class-range note

The class table below uses *next-class-start − 1* as the range end. So
`SettlementTracker 24990–26168` includes the inter-class
`discover_active_windows()` def at line 26084. The class body itself
ends earlier (around 26083). The class size column counts those inter-class
lines, which is conservative (over-counts by ~80 for SettlementTracker).

## Classes (auto-verifiable)

Generated 2026-05-07 from `grep -nE '^class ' bot/_impl.py` (post-Bit-2.1a rename).

| Lines | Class | Size |
|---|---|---|
| 2365–2717 | `KalshiClient` | 353 |
| 2718–2780 | `Logger` | 63 |
| 2781–3029 | `TelegramNotifier` | 249 |
| 3030–5599 | `StateManager` | 2570 |
| 5600–5933 | `CoinbaseFeed` | 334 |
| 5934–5945 | `OrderbookSchemaError` | 12 |
| 5946–7715 | `KalshiFeed` | 1770 |
| 7716–7796 | `DeribitDVOLFetcher` | 81 |
| 7797–8124 | `CrossExchangeFeed` | 328 |
| 8125–8201 | `CoinGlassFetcher` | 77 |
| 8202–8323 | `OrderFlowEngine` | 122 |
| 8324–8484 | `KalshiOrderFlowTracker` | 161 |
| 8485–9444 | `VolatilityEngine` | 960 |
| 9445–9647 | `ProbabilityEngine` | 203 |
| 9648–10657 | `CalibrationEngine` | 1010 |
| 10658–19735 | `OpportunityScanner` | 9078 |
| 19736–24989 | `OrderExecutor` | 5254 |
| 24990–26168 | `SettlementTracker` | 1179 |
| 26169–28160 | `MainLoop` | 1992 |

## Project file map (root, 2026-05-05)

Live trading process:
- `bot/_impl.py` — main bot, all trading logic (28,160 lines post-Bit-2.1a)
- `ops/kalshi-bot.service` — systemd unit, source of truth (installed via `ops/install.sh`); see `ops/CLAUDE.md`.
- `start.sh` — wrapper invoked by `ops/kalshi-bot.service` (venv + .env + bot/_impl.py)
- `scripts/cal_mlp/_thread_env.py` — sets OMP/MKL/OpenBLAS thread caps. bot/_impl.py inserts `scripts/cal_mlp/` into sys.path at line 11 then imports `_thread_env` at line 12, BEFORE numpy on line 34. Order is load-bearing per CLAUDE.md and AST-asserted by `tests/test_cal_mlp_invariants.py::test_thread_env_imported_before_numerical_libs_in_bot_impl`.

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
