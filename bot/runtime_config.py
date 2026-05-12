"""Read-only runtime-config view for bot/snapshots/dashboard_snapshot.py + bot/snapshots/supabase_sync.py.

Replaces the deleted `bot/_impl.py` residual shim's role as a single getattr
target for runtime-config introspection. Provides PEP 562 module-level
`__getattr__` that dual-probes `bot.constants` then `bot.config` on EACH access —
which preserves mutation freshness for runtime-mutable flags (the scanner
kill-switch writes target `bot.constants.X`; this module reads through to that
same module attribute on the dashboard's next snapshot tick).

## Caller contract

bot/snapshots/dashboard_snapshot.py and bot/snapshots/supabase_sync.py (Sprint 10.4, 2026-05-12) use the pattern

    import bot.runtime_config as _bot_mod
    val = getattr(_bot_mod, "WEATHER_NO_SIDE_LIVE", False)

The `getattr(_bot_mod, NAME, default)` form survives missing names: PEP 562
`__getattr__` raises `AttributeError`, Python's `getattr` catches it and
returns the caller-provided default. This matches the pre-Bit-9.3-iii.c
behavior of `getattr(bot._impl, NAME, default)` against the bot._impl
namespace populated by `from bot.constants import *` + `from bot.config import *`
(Bit 12.1 retargeted `config` → `bot.config`).

## Why a new module instead of importing bot.constants directly

bot.constants does not re-export bot.config constants (and shouldn't — they have
different home modules per the Bit 3.1 / Sprint 4 layering). The dashboard
needs both sets reachable from a single getattr target without per-name
retargets. PEP 562 dual-probe is the minimum-edit-distance shim that delivers
that without re-introducing the bot._impl star-import laundering.

## Forbidden: don't add `from bot.constants import *` here

That would re-create the captured-by-value binding pattern that defeated the
pre-Bit-9.3-iii.c kill-switch fix. Module-level `__getattr__` reads the
underlying module's attribute on each call — that's what gives mutation
freshness.

Bit 9.3-iii.c (2026-05-11); Bit 12.1 (2026-05-12) retargeted `config` → `bot.config`
after the config.py relocation.
"""
from __future__ import annotations

import bot.config as _cf
import bot.constants as _bc


def __getattr__(name: str):
    """PEP 562 dual-module probe: bot.constants → bot.config → AttributeError."""
    if hasattr(_bc, name):
        return getattr(_bc, name)
    if hasattr(_cf, name):
        return getattr(_cf, name)
    raise AttributeError(f"module 'bot.runtime_config' has no attribute {name!r}")
