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
`_telegram_state._TELEGRAM` alias inside bot/_impl.py + bot/scanner/__init__.py
+ bot/executor.py + bot/settlement.py — 4 consumers post-Bit-9.2).

Also note: `StateManager` lives in `bot.state` post-Bit-7.1 (2026-05-10),
NOT in `bot._impl`. Reach it via `bot.StateManager` (proxy chain:
`bot.X` → `bot._impl.X` → `bot.state.X` via the line-109 re-export
`from bot.state import StateManager`) or `bot.state.StateManager`
(direct). The `_get_compute_for_15m_main_path()` helper inside
`bot/state.py` late-binds `bot._impl.compute_for_15m_main_path`
(the closure bound at `compute_for_15m_main_path = make_compute_for_15m_main_path()` near the top of bot/_impl.py) to feed the
`scripts/cal_mlp/integration.py::sizing_parity_assert` call inside
StateManager.__init__ — single-name access discipline (NOT a
whole-namespace `bot._impl.__dict__` proxy) per the Bit 7.1 path-A++
refactor that dropped `bot_globals` from `parity_assert` and
`sizing_parity_assert` signatures. **Bit 7.1 fu (Smell 4, ticket 86b9vhccw,
2026-05-10)**: `make_compute_for_15m_main_path` itself was subsequently
refactored to drop `bot_globals: dict` — the closure now imports its 11
dependent names directly from `bot.constants` + `config` inside the
function body (mirrors path-A++).

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
to `bot.notifier` (alongside the `TelegramNotifier` class). All four of
`bot._impl` (MainLoop reads only post-Bit-9.2), `bot.scanner`,
`bot.executor` (Bit 9.1, 2026-05-10), and `bot.settlement` (Bit 9.2,
2026-05-10) reach it via `import bot.notifier as _telegram_state` plus
`_telegram_state._TELEGRAM` module-attribute access — preserves mutation
freshness across consumers (parallel to the Bit 6.3 path-B
`_cal_state._CALIBRATION_ENGINE` pattern). Tests using
`patch.object(bot, "_TELEGRAM", ...)` were retargeted to
`patch.object(bot.notifier, "_TELEGRAM", ...)` in the same atomic
commit.

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
also lives in `bot.settlement` (bundled with SettlementTracker per
master plan Phase Z+AA decision; settlement-adjacent in source layout,
sole caller is MainLoop._refresh_active_windows). The L81 alias-import
for `_append_raw_api_journal` in bot/_impl.py:285 RETIRED atomically —
both historical SettlementTracker callers moved to bot.settlement with
the public name `append_raw_api_journal`; bot/_impl.py has zero callers
post-Bit-9.2. The `_telegram_state._TELEGRAM` consumer enumeration
extends to 4 consumers (bot/_impl.py for MainLoop reads + bot/scanner +
bot/executor + bot/settlement). No new `.importlinter` carve-out
needed — clean leaf extraction; net contracts stays at 5. Sprint 9 ⅔
done after Bit 9.2; Bit 9.3 (MainLoop → bot/main_loop.py) is the last
documented Sprint 9 leaf. Per master plan, Bit 9.3-ii ultimately deletes
bot/_impl.py; the residual OrderFlowEngine (~122 LOC) + KalshiOrderFlowTracker
(~243 LOC) classes still live in bot/_impl.py post-Bit-9.2 and need to
be relocated either as part of Bit 9.3 or as a Sprint 9 / Sprint 10
follow-up — surface this scope question when planning Bit 9.3.

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
