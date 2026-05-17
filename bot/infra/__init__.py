"""bot/infra/ — infrastructure modules (Sprint 10.5 sibling-reorg, 2026-05-11).

Per master plan L2225-2238 Sprint 10 row: "infra/ — capital_allocator,
circuit_breaker, watchdog, models". Sprint 10.5 split into:

  - 10.5a (SHIPPED 2026-05-11): capital_allocator.py + circuit_breaker.py
    Both are pure-Python library modules — no `__file__`-derived load-bearing
    paths, no CLI invocation surface, low blast radius.

  - 10.5b (SHIPPED 2026-05-11): models.py → bot/models.py
    Relocated as a SIBLING under bot/ (NOT under bot/infra/) — pure-math
    module is conceptually peer to engines/shadows/feeds/fetchers, not
    infrastructure. 22 caller retargets + helpers-leaf `.importlinter`
    carve-out for bot.helpers.tm_sweep -> bot.models (function-scoped
    lazy import in `_get_calculate_taker_fee()` inside
    bot/helpers/tm_sweep.py mirrors the Sprint 10.5a
    bot.helpers.breakers -> bot.infra.circuit_breaker pattern).

  - 10.5c (CLOSED via Sprint 14-A Bit X.5, 2026-05-17): watchdog.py
    Relocated to `ops/watchdog.py`, NOT under bot/infra/ — root-cleanup
    track umbrella `86b9zfbt8` placed it alongside the systemd units
    + install.sh. STATE_FILE + DB_PATH bumped to
    `Path(__file__).parent.parent` so `.watchdog_state.json` + `state.db`
    still resolve at repo root. `ops/__init__.py` added so
    `import ops.watchdog` resolves from the test suite. VPS crontab
    line updated to `python3 ops/watchdog.py` (post-merge operator
    action; covered by the Bit X.5 ship summary).

No package-level re-exports — callers reach each module directly via
submodule path (mirrors bot/engines/ + bot/shadows/ minimal-__init__
precedent).
"""
