"""Runtime entrypoint — `python -m bot` invokes this file.

Body lives in `bot/main_loop.py` (Bit 9.3-i extraction, 2026-05-10) post the
Bit 9.3-ii bot/__main__.py swap (2026-05-10). Pre-9.3-ii this file imported
`MainLoop` from `bot._impl` via the proxy chain; post-9.3-ii it imports
directly from the canonical bot.main_loop module per master plan L2197.

## Import ordering (THREE LOAD-BEARING INVARIANTS)

1. **`import bot._thread_env` MUST be the FIRST non-stdlib import.**
   Reason: OMP_NUM_THREADS=1 must be set in os.environ BEFORE numpy / scipy /
   torch / sklearn / pandas C-extensions load, because those libraries cache
   OpenBLAS thread count at LIBRARY LOAD TIME. The bot.main_loop import chain
   transitively loads numpy via `from bot.models import ...` (pure-math
   sibling). Postmortem: kb/failures/cal-mlp-torch-thread-contention-apr29.md.

2. **`logging.basicConfig(force=True)` must come BEFORE
   `from bot.main_loop import MainLoop`** (Bit 9.3-iii.b CALMLP-boot-log fu,
   ticket 86b9w1j4r). Reason: the `bot.main_loop` import triggers `bot.boot`
   module-load, which fires `[CALMLP] enabled=N at boot` via
   `logging.getLogger("bot.boot").info(...)`. If basicConfig hasn't run yet,
   the root logger has no handlers and the log line is silent-dropped at
   production runtime. Pre-9.3-iii.b basicConfig lived inside `if __name__ ==
   "__main__":` block (line 33 of the historical file) which ran AFTER all
   imports finished — observable journalctl regression post-Bit-9.3-iii.a.

3. **`import bot._thread_env` BEFORE `logging.basicConfig`** — bot._thread_env
   only mutates os.environ (no logging calls), so this ordering is for
   readability, not correctness. But maintaining it keeps the "first non-stdlib
   import" invariant unambiguous.

## Why basicConfig is safe at module top-level HERE specifically

`logging.basicConfig(force=True)` clobbers any pre-existing root-logger
handlers. The R5 #8 concern (Bit 9.3-iii.a historical) was that calling it at
`import bot._impl` module-load would clobber pytest's caplog fixture handlers
— a silent test-suite regression. That concern doesn't fire in `bot/__main__.py`
because **pytest never imports `bot.__main__`** (pytest collects individual
`bot.<module>` submodules; the `__main__` shim only runs under `python -m bot`).
So the hoist is safe in this file specifically. Don't pull this basicConfig
call up into `bot/__init__.py` — that WOULD run on `import bot` from pytest.
"""
import bot._thread_env  # noqa: F401, E402 — MUST be first non-stdlib import; sets OMP_NUM_THREADS=1 before numpy loads transitively via bot.main_loop → bot.models → numpy
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
    force=True,
)

from bot.main_loop import MainLoop  # noqa: E402, F401 — must come AFTER basicConfig so the [CALMLP] enabled=N at boot log emitted during bot.boot module-load reaches the configured stderr handler

if __name__ == "__main__":
    MainLoop().run()
