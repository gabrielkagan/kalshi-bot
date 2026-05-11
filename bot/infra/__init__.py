"""bot/infra/ — infrastructure modules (Sprint 10.5 sibling-reorg, 2026-05-11).

Per master plan L2225-2238 Sprint 10 row: "infra/ — capital_allocator,
circuit_breaker, watchdog, models". Sprint 10.5 split into:

  - 10.5a (THIS Bit): capital_allocator.py + circuit_breaker.py
    Both are pure-Python library modules — no `__file__`-derived load-bearing
    paths, no CLI invocation surface, low blast radius.

  - 10.5b (DEFERRED): models.py
    Higher import surface (8 prod + 6 test sites) — deserves dedicated
    3-round adversarial Bit.

  - 10.5c (DEFERRED): watchdog.py
    Same risk class as Sprint 10.3 ai/ auditor.py + researcher.py — has
    `__file__`-derived load-bearing paths (STATE_FILE + DB_PATH at lines
    23-24) AND is a standalone CLI (likely invoked via VPS crontab).
    Requires coordinated VPS crontab update post-deploy.

No package-level re-exports — callers reach each module directly via
submodule path (mirrors bot/engines/ + bot/shadows/ minimal-__init__
precedent).
"""
