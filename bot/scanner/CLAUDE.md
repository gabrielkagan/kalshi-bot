# bot/scanner/ — package guide

Single-class subpackage extracted in Sprint 8 Bit 8.1 (2026-05-10):
`OpportunityScanner` lives at `bot/scanner/__init__.py` (~9,400 lines,
36 instance methods + 7 staticmethods). Entry point is `scan()`, called
once per tick by `MainLoop`. Callers reach `OpportunityScanner` via
`from bot.scanner import OpportunityScanner` (or `bot.scanner.OpportunityScanner`)
directly. Bit 9.3-iii.b (2026-05-11) retired the `_BotProxy`; Bit 9.3-iii.c
(2026-05-11) DELETED `bot/_impl.py` entirely. The pre-deletion re-export
chain `bot.OpportunityScanner → bot._impl.OpportunityScanner → bot.scanner.OpportunityScanner`
is GONE; the canonical home (`bot.scanner.OpportunityScanner`) is the only
resolution path.

## Cross-class coupling (read before any edit)

The scanner is the most cross-coupled class in the codebase — three
distinct access patterns coexist, each load-bearing:

### 1. `OrderExecutor` direct top-level import (Bit 9.1, 2026-05-10)

The 34 `OrderExecutor.X(...)` static-method call sites in `scan()` use
the top-level `from bot.executor import OrderExecutor` import directly.
The previous `_get_order_executor()` late-binding helper retired in
Sprint 9 Bit 9.1 atomically with the OrderExecutor extraction; the
`scanner-no-impl-toplevel` `.importlinter` contract dropped in the same
commit (net contracts: 6 → 5).

The cycle break (bot.executor ↔ bot.scanner) is now from the executor
side via a `_get_opportunity_scanner()` method-body helper inside
`bot/executor.py` — the 7 `OpportunityScanner.X(...)` staticmethod call
sites in OrderExecutor body go through it. The asymmetry keeps scanner's
top-level import clean.

### 2. `_telegram_state._TELEGRAM` module-attribute access (path-A++ from Bit 8.1)

`_TELEGRAM` is the live `TelegramNotifier` singleton, owned by
`bot/notifier.py`. Reads here use `_telegram_state._TELEGRAM` after
`import bot.notifier as _telegram_state` near the top of `bot/scanner/__init__.py`
(search anchor: `import bot.notifier as _telegram_state`).
Plain `from bot.notifier import _TELEGRAM` would capture the binding
by value at import time and silently freeze at `None` when
`MainLoop.__init__` later mutates the singleton (L83). Post-Bit-9.3-iii.b
(2026-05-11) the `_BotProxy` is retired so `from bot import notifier as ...`
no longer compiles at all (AttributeError); pre-9.3-iii.b the form would
have triggered `_BotProxy.__getattr__` → circular `ImportError` (L84). The
canonical form remains `import bot.notifier as _telegram_state`. Post-Bit-9.3-ii
this pattern has 5 consumers — bot/orphan_db_watchdog.py (for the orphan-DB
Layer-3 watchdog helpers — `_alert_orphan_db_holder` and the
`detect_orphan_db_holders` lsof-not-found Telegram alert branch — clean
leaf relocated from bot/_impl.py at Bit 9.3-ii REPLACING that slot in the
enumeration) + bot/main_loop.py (MainLoop reads + the singleton WRITE in
`__init__`) + bot/scanner/__init__.py + bot/executor.py + bot/settlement.py.

### 3. `_cal_state._CALIBRATION_ENGINE` (path-B from Bit 6.3)

Same module-attribute access pattern, but for the calibration
runtime: `from bot.engines import calibration as _cal_state` →
`_cal_state._CALIBRATION_ENGINE` / `_cal_state._resolve_cal_engine`.
The `bot.engines` parent goes through Python's normal submodule
import — pre-Bit-9.3-iii.b the `_BotProxy` would have intercepted top-level
`bot.X` reads but submodule loads (`bot.engines.calibration`) bypassed it
via Python's package-import semantics; post-9.3-iii.b the proxy is gone
entirely and `bot.engines.calibration` resolves via Python's default
package-import path. The `from bot.engines import calibration` form remains
safe. Identical
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
`tests/integration/test_scanner_extraction.py::SCANNER_MAIN_LOOP_ATTRS`.

## Forbidden top-level imports

- **No torch / sklearn / pandas direct imports.** numpy + scipy reach
  the scanner transitively through `models.PositionSizer` etc., but
  the scanner module body itself MUST NOT import them. Locked by
  `tests/integration/test_scanner_extraction.py::test_scanner_no_forbidden_numerical_imports`.
- **No `bot._impl` at module top.** Post-Bit-9.1 (2026-05-10), scanner
  has zero top-level `bot._impl` imports — the `_get_order_executor()`
  late-binding helper retired and the `scanner-no-impl-toplevel`
  `.importlinter` contract dropped atomically with the OrderExecutor
  extraction (net contracts: 6 → 5). Post-Bit-9.3-iii.a (2026-05-11)
  the transitive `bot.scanner → bot.state → bot._impl` edge ALSO
  vanished — bot/state.py now top-imports `compute_for_15m_main_path`
  from clean-leaf `bot/boot.py` directly, the Bit 7.1 helper retired,
  and the `state-no-impl-toplevel` carve-out was removed (net contracts
  7 → 6; `helpers-leaf` was extended with `bot.boot` but remained a
  single contract — the count change reflects the retired
  `state-no-impl-toplevel` block). The bot.executor ↔ bot.scanner
  cycle is still broken from the executor side via a
  `_get_opportunity_scanner()` method-body helper inside
  `bot/executor.py` (NOT scanner; scanner stays clean).

## Annotations: `Optional[OrderFlowEngine]` / `Optional[KalshiOrderFlowTracker]`

Both classes live in `bot/order_flow.py` post-Bit-9.3.5 (2026-05-10).
The annotations in `OpportunityScanner.__init__` are UNQUOTED — scanner
has a top-level `from bot.order_flow import OrderFlowEngine,
KalshiOrderFlowTracker` (search anchor: `from bot.order_flow import`).
This is safe because bot/order_flow.py is a clean leaf (stdlib +
bot.constants only) with zero bot.scanner edges.

## `_best_ask_depth` lives on OrderExecutor, NEVER on scanner

`_best_ask_depth` is a staticmethod on `OrderExecutor` (in
`bot/executor.py` post-Bit-9.1), NOT on `OpportunityScanner`. The 4
latent AttributeError sites in OrderExecutor body that incorrectly
called `OpportunityScanner._best_ask_depth(...)` were FIXED in Bit 9.1
(rewritten to `OrderExecutor._best_ask_depth(...)`); ticket
`86b9vn9r5` closed. **Don't "fix"** by adding `_best_ask_depth` here;
the staticmethod's home is OrderExecutor. Locked by
`tests/integration/test_scanner_extraction.py::test_opportunity_scanner_does_not_define_best_ask_depth`
+ `tests/integration/test_executor_extraction.py::test_best_ask_depth_lives_on_executor_not_scanner`.

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

- Most repo-wide rules (threading, cal_mlp feature-transform lock-step, SQLite WAL,
  `_shadow_diag` schema chain, Engine→CalEngine wiring,
  `discover_active_windows()` cross-checks) live in `bot/CLAUDE.md`
  and apply transitively when editing the scanner. **Don't duplicate
  them here** — keep this file scanner-specific.
- After signature changes to `OpportunityScanner.__init__` or any
  staticmethod consumed by callers (OrderExecutor / MainLoop): grep
  call sites in `bot/_impl.py` and update
  `tests/integration/test_scanner_extraction.py::test_scanner_method_count_matches_ast`
  + `test_scanner_constants_resolve_from_bot_constants` /
  `_from_config` / `test_scanner_helpers_resolve_from_bot_helpers`
  if the surface changes.
- **Patch-target retargeting is per-test, not per-name (L85).** When
  adding/changing constants read by both scanner and OrderExecutor
  (e.g., `OBSERVATION_MODE`, `WEATHER_NO_SIDE_LIVE`), scanner-targeted
  tests use `@patch("bot.scanner.X")` (or `bot.scanner.<read site>`),
  while executor-targeted tests use `@patch("bot.executor.X")`.
  Post-Bit-9.3-iii.b (2026-05-11) `@patch("bot.X")` no longer works
  (proxy retired). Post-Bit-9.3-iii.c (2026-05-11) `@patch("bot._impl.X")`
  no longer works either (bot/_impl.py was DELETED). The canonical forms
  are `@patch("bot.<canonical_module>.X")` OR — for the 3 kill-switch
  flags (WEATHER_NO_SIDE_LIVE, HOURLY_NO_SIDE_LIVE, BRACKET_NO_ENABLED)
  whose runtime-freshness fix in Bit 9.3-iii.c routes reads through
  `bot.constants.X` module-attribute access — `@patch.object(bot.constants, "X", ...)`.
  See L85 in `kb/concepts/extraction-pre-flight-checklist.md`.
- Single-class file by design — Bit 8.3 (DEFERRED 2026-05-17) plans
  the internal split into `discover.py` / `evaluate.py` / `gates.py`
  / `shadow.py`. Until then, keep the body in `__init__.py`.
