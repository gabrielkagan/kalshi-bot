"""Runtime entrypoint — `python -m bot` invokes this file.

Body lives in `bot/main_loop.py` (Bit 9.3-i extraction, 2026-05-10) post the
Bit 9.3-ii bot/__main__.py swap (2026-05-10). Pre-9.3-ii this file imported
`MainLoop` from `bot._impl` via the proxy chain; post-9.3-ii it imports
directly from the canonical bot.main_loop module per master plan L2197.

R-p7-deploy-r7 CRITICAL: `import bot._thread_env` must fire BEFORE any
transitive numpy/scipy/torch/sklearn/pandas load. The bot.main_loop import
chain transitively loads numpy via `from models import ...` (a sibling of
bot/main_loop.py that pulls EGARCHEstimator / MincerZarnowitzTracker /
PositionSizer math). Pre-9.3-ii the chain went `bot/__main__.py → bot._impl
→ bot._thread_env (line 11) → numpy (line 37 of bot/_impl.py)` so thread_env
fired by virtue of being bot._impl's first non-stdlib import. Post-9.3-ii
that chain is bypassed; bot._thread_env must therefore be the FIRST import
here for the same OMP_NUM_THREADS=1-before-numpy guarantee. See
kb/failures/cal-mlp-torch-thread-contention-apr29.md for the regression
this prevents.

`logging.basicConfig(force=True)` was moved here from `bot/_impl.py`
module-level (R5 #8): firing it at every `import bot._impl` would clobber
pytest's caplog fixture handlers — silent test-suite regression. By living
in __main__, it runs ONLY when production starts via `python -m bot`, never
on test-suite imports.
"""
import bot._thread_env  # noqa: F401, E402 — MUST be first; sets OMP_NUM_THREADS=1 before numpy loads transitively via bot.main_loop → models → numpy
import logging
import sys

from bot.main_loop import MainLoop  # noqa: F401

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )
    MainLoop().run()
