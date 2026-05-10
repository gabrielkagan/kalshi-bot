# bot/scanner/ — package guide

Single-class subpackage extracted in Sprint 8 Bit 8.1 (2026-05-10):
`OpportunityScanner` lives at `bot/scanner/__init__.py` (~9,400 lines,
36 instance methods + 7 staticmethods). Entry point is `scan()`, called
once per tick by `MainLoop`. Re-imported into `bot/_impl.py` via the
line-115 `from bot.scanner import OpportunityScanner` re-export so the
runtime construction in `MainLoop.__init__`, ~30 test instantiation
sites, and 13 staticmethod call sites in OrderExecutor/MainLoop all
resolve through the proxy chain.

## Cross-class coupling (read before any edit)

The scanner is the most cross-coupled class in the codebase — three
distinct access patterns coexist, each load-bearing:

### 1. `_get_order_executor()` late-binding (until Sprint 9 Bit 9.1)

The 34 `OrderExecutor.X(...)` static-method call sites in `scan()` go
through the module-top helper `_get_order_executor()` which does
`import bot._impl as _bot_impl; return _bot_impl.OrderExecutor`.

**Don't** rewrite to bare `OrderExecutor.X(...)` — there is no
top-level `OrderExecutor` symbol in `bot/scanner/__init__.py` and the
forbidden contract `scanner-no-impl-toplevel` (`.importlinter`)
prevents adding one. Sprint 9 Bit 9.1 will retire the helper when
`OrderExecutor` extracts to `bot/executor.py`; until then the helper
is the contract.

### 2. `_telegram_state._TELEGRAM` module-attribute access (path-A++ from Bit 8.1)

`_TELEGRAM` is the live `TelegramNotifier` singleton, owned by
`bot/notifier.py`. Reads here use `_telegram_state._TELEGRAM` after
`import bot.notifier as _telegram_state` at `bot/scanner/__init__.py:302`.
Plain `from bot.notifier import _TELEGRAM` would capture the binding
by value at import time and silently freeze at `None` when
`MainLoop.__init__` later mutates the singleton (L83). Likewise
`from bot import notifier as _telegram_state` triggers
`_BotProxy.__getattr__` → circular `ImportError` (L84). The
canonical form is `import bot.notifier as _telegram_state`.

### 3. `_cal_state._CALIBRATION_ENGINE` (path-B from Bit 6.3)

Same module-attribute access pattern, but for the calibration
runtime: `from bot.engines import calibration as _cal_state` →
`_cal_state._CALIBRATION_ENGINE` / `_cal_state._resolve_cal_engine`.
The `bot.engines` parent goes through Python's normal submodule
import (no `_BotProxy` interception, since `bot.engines` is itself a
submodule package, not a `bot.X` top-level), so the
`from bot.engines import calibration` form is safe here. Identical
mutation-freshness reasoning as `_TELEGRAM` above.

### 4. `self._ml.X` constructor injection

`OpportunityScanner.__init__(..., main_loop=None)` accepts the parent
`MainLoop` reference; 11 distinct `self._ml.X` sub-attribute accesses
are constructor-injected references, NOT bare-name lookups: `executor`,
`spx_engine`, `spx_harrv_shadow`, `fifteenm_shadow`, `weather_engine`,
`cross_feed`, `capital_allocator`, `hourly_alt_shadow`, `_scan_iter`,
`_scan_loop_start`, `_open_positions_count_cache`. Construction order
in `MainLoop.__init__` guarantees the dependencies are populated
before any scanner method runs. Full enumeration locked by
`tests/test_scanner_extraction.py::SCANNER_MAIN_LOOP_ATTRS`.

## Forbidden top-level imports

- **No torch / sklearn / pandas direct imports.** numpy + scipy reach
  the scanner transitively through `models.PositionSizer` etc., but
  the scanner module body itself MUST NOT import them. Locked by
  `tests/test_scanner_extraction.py::test_scanner_no_forbidden_numerical_imports`.
- **No `bot._impl` at module top.** Forbidden by the
  `scanner-no-impl-toplevel` contract in `.importlinter`. Two
  `ignore_imports` edges allow-list this:
  1. `bot.scanner -> bot._impl` — the `_get_order_executor()`
     helper's function body.
  2. `bot.state -> bot._impl` — transitive: scanner imports
     `from bot.state import StateManager` at runtime (it's used as
     the `state: StateManager` annotation on `__init__`, not a
     `TYPE_CHECKING`-guarded typing-only import), and `bot.state`
     has its own method-body `import bot._impl` for
     `_get_compute_for_15m_main_path()` (Bit 7.1 fu1). Without
     this second edge, grimp's reachability check would surface
     the transitive path and the contract would fail.
  Bit 7.1 fu1 commit `5bfc116` is the precedent shape for the
  contract's peer-pin tests.

## Forward-refs: `Optional["OrderFlowEngine"]` / `Optional["KalshiOrderFlowTracker"]`

Both classes still live in `bot/_impl.py` (lines 656 + 778). The
scanner `__init__` signature uses string forward-refs to avoid
cycle-loading at the line-115 re-export firing time; do not
unquote until those classes also extract.

## `_best_ask_depth` lives on OrderExecutor, NEVER on scanner

Four `OrderExecutor.X(...)` call sites in `bot/_impl.py` reference
`OpportunityScanner._best_ask_depth(...)`, but `_best_ask_depth` does
NOT exist on scanner — it lives on `OrderExecutor`. The four calls
are a latent `AttributeError` tracked as ticket `86b9vn9r5`. **Don't
"fix"** by adding `_best_ask_depth` here; the right fix is updating
the call sites to reference `self._best_ask_depth` (when called from
within OrderExecutor) or whichever live owner is appropriate post-
Sprint-9 Bit 9.1. Locked by
`tests/test_scanner_extraction.py::test_opportunity_scanner_does_not_define_best_ask_depth`.

## `filter_stage` string literals (cell-block discipline)

The scanner emits many distinct `filter_stage` values into
`evaluated_opportunities` — string literals (`"low_probability"`,
`"insufficient_edge"`, `"silent_vol_none"`, etc.), three cell-block
constants from `bot/constants.py`
(`HIGH_PRICE_STC_BLOCK_FILTER_STAGE`,
`TM98_HIGHPRICE_BLEED_BLOCK_FILTER_STAGE`,
`SOL_TAKER_LOWPRICE_BLEED_BLOCK_FILTER_STAGE`), plus dynamic stages
assigned via conditionals (`_cand_filter_stage`, `_obs_label`,
`_wknd_stage`, `_ovn_stage`).

The three cell-block constants resolve to these string-literal
values stored in DB (which audit scripts grep for, NOT the constant
names):

- `'96C_SOL_XRP_STC_DANGER_BAND'`
- `'TM98_97_98C_2_5MIN_BLEED'`
- `'SOL_TAKER_85_89C_2_5MIN_BLEED'`

These three DEFLATE rollups filtered with `WHERE filter_stage =
'candidate'`. Any new filter_stage value emitted here must be added
to the cell-block UNION in audit/dashboard scripts (full list in
`bot/CLAUDE.md` → "Cell-block activations deflate `filter_stage='candidate'` rollups").

## Editing this file

- Most repo-wide rules (threading, cal_mlp four-site, SQLite WAL,
  `_shadow_diag` schema chain, Engine→CalEngine wiring,
  `discover_active_windows()` cross-checks) live in `bot/CLAUDE.md`
  and apply transitively when editing the scanner. **Don't duplicate
  them here** — keep this file scanner-specific.
- After signature changes to `OpportunityScanner.__init__` or any
  staticmethod consumed by callers (OrderExecutor / MainLoop): grep
  call sites in `bot/_impl.py` and update
  `tests/test_scanner_extraction.py::test_scanner_method_count_matches_ast`
  + `test_scanner_constants_resolve_from_bot_constants` /
  `_from_config` / `test_scanner_helpers_resolve_from_bot_helpers`
  if the surface changes.
- **Patch-target retargeting is per-test, not per-name (L85).** When
  adding/changing constants read by both scanner and OrderExecutor
  (e.g., `OBSERVATION_MODE`, `WEATHER_NO_SIDE_LIVE`), scanner-targeted
  tests use `@patch("bot.scanner.X")` (or `bot.scanner.<read site>`),
  while executor-targeted tests still use `@patch("bot.X")` /
  `@patch("bot._impl.X")` because OrderExecutor reads the constant
  via bot._impl's bare-name (laundered through
  `from bot.constants import *`). Bulk-retargeting from `bot.X` to
  `bot.scanner.X` will break ~5 tests in `tests/test_execution.py` /
  `tests/test_weather_no_side.py` — see L85 in
  `kb/concepts/extraction-pre-flight-checklist.md`.
- Single-class file by design — Bit 8.3 (DEFERRED 2026-05-17) plans
  the internal split into `discover.py` / `evaluate.py` / `gates.py`
  / `shadow.py`. Until then, keep the body in `__init__.py`.
