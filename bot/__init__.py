"""kalshi-bot package — runtime body at `bot/_impl.py`, entrypoint at `bot/__main__.py`.

PEP 562 `__getattr__` alone is insufficient: ~30+ external WRITE sites
(`unittest.mock.patch("bot.X", ...)` × 94 in test_execution.py + ~25 in
test_ladder_escalation.py + ~10 elsewhere; `monkeypatch.setattr(bot, X, ...)`;
direct `bot.X = value`) plus 3 production kill-switch self-mutation sites
in `bot/_impl.py` (search for `_self_module.` — currently
WEATHER_NO_SIDE_LIVE / HOURLY_NO_SIDE_LIVE / BRACKET_NO_ENABLED
auto-disable on circuit-breaker trip). A bare `__getattr__` makes
writes go to `sys.modules['bot'].__dict__` while readers in `bot/_impl.py`
read `sys.modules['bot._impl'].__dict__` — silent mismatch breaks
mock.patch'd tests AND kill switches.

Solution: `types.ModuleType` subclass with both `__getattr__` AND
`__setattr__` proxying. `bot.X = value` writes through to `bot._impl.X`.

Underscored names are proxied too — no `__all__` filtering — so consumers
of `bot._BREAKER_REGISTRY` etc. work without per-caller updates. Note:
`_CALIBRATION_ENGINE`, `_CAL_REGISTRY`, and the `_resolve_cal_engine` /
`_derive_subtype` / `_derive_asset_filter` helpers live in
`bot.engines.calibration` post-Bit-6.3 path-B (2026-05-10), NOT in
`bot._impl` — reach them via `bot.engines.calibration.X` directly, not via
the `bot.X` proxy. Note: `_TELEGRAM` lives in `bot.notifier` post-Bit-8.1
path-A++ (2026-05-10), NOT in `bot._impl` — `bot._TELEGRAM` no longer
resolves via the proxy. Reach via `bot.notifier._TELEGRAM` (or via the
`_telegram_state._TELEGRAM` alias inside bot/orphan_db_watchdog.py + bot/main_loop.py +
bot/scanner/__init__.py + bot/executor.py + bot/settlement.py — 5 consumers
post-Bit-9.3-ii, with bot/orphan_db_watchdog.py REPLACING bot/_impl.py as the
5th consumer slot per the Bit-9.3-ii relocation).

Also note: `StateManager` lives in `bot.state` post-Bit-7.1 (2026-05-10),
NOT in `bot._impl`. Reach it via `bot.StateManager` (proxy chain:
`bot.X` → `bot._impl.X` → `bot.state.X` via the line-109 re-export
`from bot.state import StateManager`) or `bot.state.StateManager`
(direct). Post-Bit-9.3-iii.a (2026-05-11), `compute_for_15m_main_path`
lives in clean-leaf `bot.boot` (relocated from bot/_impl.py); bot/state.py
top-imports it directly via `from bot.boot import compute_for_15m_main_path`
and feeds it to `scripts/cal_mlp/integration.py::sizing_parity_assert`
inside StateManager.__init__. The Bit 7.1 `_get_compute_for_15m_main_path()`
late-binding helper retired in the same atomic commit — no longer needed
because bot.boot has zero bot.state edges (no load-order cycle to avoid).
The `.importlinter` `state-no-impl-toplevel` carve-out also retired.

Bit 8.1 (path-A++, 2026-05-10) — post-Bit-9.2 state: `OpportunityScanner`
lives in `bot.scanner` post-extraction, NOT in `bot._impl`. Reach via
`bot.OpportunityScanner` (proxy chain: `bot.X` → `bot._impl.X` →
`bot.scanner.X` via the line-115 re-export `from bot.scanner import
OpportunityScanner`) or `bot.scanner.OpportunityScanner` (direct). The
7 staticmethods (`_compute_maker_counterfactual`, `_parse_threshold`,
`_parse_weather_market_info`, `_best_yes_ask_cents`, `_is_severe_drift`,
`_convert_orderbook_fp`, `_window_timeslot`) called from OrderExecutor
(12 sites) + MainLoop (1 site) all resolve via the re-export. Bit 8.1
also relocated the `_TELEGRAM` module-level singleton from `bot._impl`
to `bot.notifier` (alongside the `TelegramNotifier` class). Post-Bit-9.3
(2026-05-10), all five of `bot._impl` (for the orphan-DB Layer-3 watchdog
helpers — `_alert_orphan_db_holder` and the `detect_orphan_db_holders`
lsof-not-found Telegram alert branch — the only remaining
`_telegram_state._TELEGRAM` consumer block in bot/_impl.py
post-MainLoop-extraction), `bot.main_loop` (MainLoop reads + the
singleton WRITE in `__init__`), `bot.scanner`, `bot.executor` (Bit 9.1,
2026-05-10), and `bot.settlement` (Bit 9.2, 2026-05-10) reach it via
`import bot.notifier as _telegram_state` plus `_telegram_state._TELEGRAM`
module-attribute access — preserves mutation freshness across consumers
(parallel to the Bit 6.3 path-B `_cal_state._CALIBRATION_ENGINE`
pattern). Tests using `patch.object(bot, "_TELEGRAM", ...)` were
retargeted to `patch.object(bot.notifier, "_TELEGRAM", ...)` in the
Bit 8.1 atomic commit.

Bit 9.1 (path-A++, 2026-05-10): `OrderExecutor` lives in `bot.executor`
post-extraction, NOT in `bot._impl`. Reach via `bot.OrderExecutor` (proxy
chain: `bot.X` → `bot._impl.X` → `bot.executor.X` via the line-116ish
re-export `from bot.executor import OrderExecutor`) or `bot.executor.OrderExecutor`
(direct). The cleanup contract retired the `_get_order_executor()`
helper from `bot/scanner/__init__.py` — scanner now uses top-level
`from bot.executor import OrderExecutor` directly. The
`scanner-no-impl-toplevel` `.importlinter` contract dropped (net
contracts: 6 → 5). bot/executor.py uses a `_get_opportunity_scanner()`
method-body helper to break the symmetric bot.executor ↔ bot.scanner
cycle (the 7 OpportunityScanner staticmethod call sites in OrderExecutor
go through it; a future Sprint 10 sibling-reorg Bit may relocate those
2 staticmethods to `bot/helpers/orderbook.py` to eliminate the helper
entirely). `_append_raw_api_journal` relocated path-A++ to
`bot/helpers/raw_api_journal.py`. 4 latent
`OpportunityScanner._best_ask_depth(...)` AttributeError sites in
OrderExecutor body fixed (closes ticket 86b9vn9r5).

Bit 9.2 (path-A++, 2026-05-10): `SettlementTracker` lives in
`bot.settlement` post-extraction, NOT in `bot._impl`. Reach via
`bot.SettlementTracker` (proxy chain: `bot.X` → `bot._impl.X` →
`bot.settlement.X` via the re-export `from bot.settlement import
SettlementTracker, discover_active_windows`) or
`bot.settlement.SettlementTracker` (direct). `discover_active_windows()`
also lives in `bot.settlement`. The L81 alias-import for
`_append_raw_api_journal` in bot/_impl.py:285 RETIRED atomically.

Bit 9.3 (path-A method-body late-binding, 2026-05-10): `MainLoop` lives
in `bot.main_loop` post-extraction. Reach via `bot.MainLoop` (proxy chain)
or `bot.main_loop.MainLoop` (direct). Bit 9.3-ii (2026-05-10) swapped
bot/__main__.py to direct `from bot.main_loop import MainLoop`. Post-Bit-9.3-iii.a
(2026-05-11), the HPSB pair (`_HPSB_MISSING_BLEEDERS`,
`_HPSB_VALIDATOR_UNAVAILABLE_REASON`) was relocated to clean-leaf bot/boot.py
and bot/main_loop.py top-imports them — the Bit 9.3 method-body late-binding
block in `MainLoop.__init__` is GONE. `MainLoop.startup` still late-binds
`detect_orphan_db_holders` from `bot.orphan_db_watchdog` (Bit 9.3-ii
relocation, orthogonal to bot._impl). NO `.importlinter` carve-out —
bot/main_loop.py has zero bot._impl edge.

Bit 9.3.5 (clean leaf, 2026-05-10): `OrderFlowEngine` (122 LOC) +
`KalshiOrderFlowTracker` (240 LOC) live in `bot.order_flow`
post-extraction. Reach via `bot.OrderFlowEngine` /
`bot.KalshiOrderFlowTracker` (proxy chain) or `bot.order_flow.X`
(direct). The two `# REMOVE BIT 9.3.5` markers in MainLoop's
late-binding block collapsed to a top-level `from bot.order_flow import
OrderFlowEngine, KalshiOrderFlowTracker` in bot/main_loop.py. Sister
cleanup atomic in the same commit: bot/scanner/__init__.py forward-refs
for both classes UNQUOTED. bot/_impl.py became class-free; final deletion
deferred to Bit 9.3-iii.

Bit 9.3-ii (clean leaf + bot/__main__.py swap, 2026-05-10): the orphan-DB
Layer-3 watchdog block (5 functions + `_ORPHAN_DB_WATCHDOG_PATTERNS` list,
~200 LOC, was bot/_impl.py:375-578 pre-9.3-ii) relocated to NEW
`bot/orphan_db_watchdog.py` clean leaf (stdlib + bot.notifier alias only).
Reach via `bot.detect_orphan_db_holders` etc. (proxy chain) or
`bot.orphan_db_watchdog.X` (direct). The 5-consumer `_telegram_state._TELEGRAM`
enumeration: bot/orphan_db_watchdog.py REPLACES bot/_impl.py as the 5th
consumer — net stays at 5. MainLoop.startup() late-binding retargeted from
`from bot._impl import detect_orphan_db_holders` to
`from bot.orphan_db_watchdog import detect_orphan_db_holders`.
**bot/__main__.py swap (master plan L2197)**: from `from bot._impl import
MainLoop` to direct `from bot.main_loop import MainLoop`, with
`import bot._thread_env` as the FIRST import (preserves the
OMP_NUM_THREADS=1-before-numpy guarantee since the chain
`bot/__main__.py → bot.main_loop → models → numpy` no longer routes
through bot._impl's first non-stdlib import). `.importlinter` `helpers-leaf`
`forbidden_modules` extended with `bot.orphan_db_watchdog`; net contracts
stays at 5. bot/_impl.py: 767 → ~580 LOC. The `_BotProxy` shim stays
operational for proxy-resolved reads/writes (used by tests/test_execution.py
`@patch("bot.X")` × 94 + tests/test_ladder_escalation.py × 16 + production
kill-switch self-mutation sites). Bit 9.3-iii.a (2026-05-11) relocated the
4 boot-time bindings (`_HPSB_VALIDATOR_UNAVAILABLE_REASON`, `_HPSB_MISSING_BLEEDERS`,
`_BLEED_BLOCK_MISSING_BLEEDERS`, `compute_for_15m_main_path`) + cal_mlp warmup
boot log to clean-leaf `bot/boot.py`. Bit 9.3-iii.b (full _BotProxy retirement)
and Bit 9.3-iii.c (DELETE bot/_impl.py) remain pending.

Caching the `bot._impl` module reference is safe: the module object itself
is stable; mutations land on its `__dict__` which `getattr`/`setattr`
re-resolve on every call.
"""
import sys
import types


class _BotProxy(types.ModuleType):
    # Cache attribute name `_impl_cache` (NOT `_impl`) per R5 #9: `bot._impl` is
    # ALSO the public submodule import target (`from bot._impl import MainLoop`).
    # If we cache there, any caller doing `bot._impl.X` BEFORE `_get_impl()` ran
    # OR `import bot._impl` was triggered elsewhere would hit class-attribute
    # lookup → return `None` (the class attr) → AttributeError on `.X` access —
    # bypassing __getattr__ (which Python only invokes when the lookup misses).
    # Using a separate name keeps the cache attribute private to the proxy and
    # leaves `bot._impl` resolution flowing through __getattr__ → _get_impl().
    _impl_cache = None

    def _get_impl(self):
        # Reentrancy-hardened (R6 #4): if a nested __getattr__ fires during
        # `bot/_impl.py`'s own module-level execution, sys.modules['bot._impl']
        # is the half-loaded module — return that rather than re-importing
        # (which Python would resolve to the same half-loaded reference but
        # masks the reentrancy from a debugging standpoint).
        if self._impl_cache is None:
            cached = sys.modules.get('bot._impl')
            if cached is not None:
                object.__setattr__(self, '_impl_cache', cached)
                return cached
            import bot._impl as _m
            object.__setattr__(self, '_impl_cache', _m)
        return self._impl_cache

    def __getattr__(self, name):
        # PEP 562: invoked only when `name` is not in the package __dict__.
        # Dunder misses propagate clean AttributeError so tooling can probe
        # `bot.__version__` etc. without spurious _impl attribute lookups.
        if name.startswith('__') and name.endswith('__'):
            raise AttributeError(f"module 'bot' has no attribute {name!r}")
        # Submodule short-circuit (R6 #2): `bot._impl` should resolve to the
        # SUBMODULE itself, not look for an attribute named '_impl' on the
        # body. After Python's import machinery has registered the submodule,
        # it's in sys.modules; return it directly.
        if name == '_impl':
            mod = sys.modules.get('bot._impl')
            if mod is None:
                # Trigger load via _get_impl, which sets sys.modules['bot._impl'].
                self._get_impl()
                mod = sys.modules['bot._impl']
            return mod
        return getattr(self._get_impl(), name)

    def __setattr__(self, name, value):
        # Dunders + the cache attribute stay on the package proxy itself.
        if (name.startswith('__') and name.endswith('__')) or name == '_impl_cache':
            object.__setattr__(self, name, value)
            return
        # Submodule binding (R6 #1 + R8 #1 generalized): when `import bot.X`
        # runs (for X in `_impl`, `scanner`, `clients`, `feeds`, ...), Python's
        # import machinery does `setattr(sys.modules['bot'], 'X', <submodule>)`.
        # That call lands here. Without this special case, the write would
        # route through to `bot._impl_module.X = <submodule>` — a stray
        # binding on the body that confuses introspection AND prevents
        # `bot.__dict__['X']` from being populated (so `bot.X` reads fall
        # through __getattr__ → _impl instead of finding the submodule).
        # Keep submodule bindings on the package itself so attribute access
        # short-circuits via __dict__ on subsequent reads. R8 #1 generalized
        # from the `name == '_impl'` special-case to any value that's a
        # submodule of `bot` (Bit 2.1b creates bot.clients, bot.feeds, etc.;
        # Sprint 3+ adds bot.constants, bot.helpers.*, etc.).
        if isinstance(value, types.ModuleType) and getattr(value, '__name__', '').startswith('bot.'):
            object.__setattr__(self, name, value)
            return
        # Everything else writes through to bot._impl. The kill-switch case
        # (item 4 routes those directly through `bot._impl` for defense-in-depth);
        # this branch is the safety net for any future kill-switch site that
        # forgets the direct form, AND for the ~94 + ~16 + ~10 mock.patch
        # ("bot.X") test-suite sites that target the package directly.
        setattr(self._get_impl(), name, value)

    def __delattr__(self, name):
        # `del bot.X` and mock.patch(..., create=True) exit semantics.
        # R4 #10: without this, delattr falls back to object.__delattr__
        # which targets the package's own __dict__, not bot._impl's, raising
        # AttributeError because the attribute lives on bot._impl.
        if (name.startswith('__') and name.endswith('__')) or name == '_impl_cache':
            # R6 #2: tolerate `del bot._impl_cache` when the cache hasn't been
            # populated on the instance yet (still living as the class attr).
            try:
                object.__delattr__(self, name)
            except AttributeError:
                pass
            return
        # Submodule bindings (R9 MAJOR fix — generalized to mirror
        # __setattr__'s submodule-binding branch). After R8's __setattr__
        # generalization, ANY `bot.<submodule>` auto-set by Python's import
        # machinery lands on the package proxy's __dict__ (not write-through
        # to body). del bot.<submodule> must therefore delete from package
        # __dict__, not delegate to bot._impl. Pre-R9 this branch was
        # `name == '_impl'` only, but Bit 2.1b adds `bot.scanner`,
        # `bot.feeds`, `bot.clients`, etc. plus Sprint 3+ adds more —
        # the hardcoded `_impl`-only check would cause AttributeError on
        # `del bot.scanner` etc.
        if name in vars(self):
            object.__delattr__(self, name)
            return
        delattr(self._get_impl(), name)

    def __dir__(self):
        # IDE/REPL/`inspect.getmembers` see the body's surface, not just the proxy.
        return sorted(set(super().__dir__()) | set(dir(self._get_impl())))


# Activate the proxy + initialize _impl_cache on the instance (R6 #2 — avoids
# AttributeError on `del bot._impl_cache` before any `_get_impl()` call):
sys.modules[__name__].__class__ = _BotProxy
object.__setattr__(sys.modules[__name__], '_impl_cache', None)
