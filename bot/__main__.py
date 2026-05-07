"""Runtime entrypoint — `python -m bot` invokes this file.

Body lives in `bot/_impl.py`; this is a thin shim by design so the
_impl module loads ONCE under `python -m bot` semantics (avoiding
runpy's double-execution that would otherwise occur if the body
were here directly — see Bit 2.0 R2 review).

`logging.basicConfig(force=True)` was moved here from `bot/_impl.py`
module-level (R5 #8): firing it at every `import bot._impl` would
clobber pytest's caplog fixture handlers — silent test-suite
regression. By living in __main__, it runs ONLY when production
starts via `python -m bot`, never on test-suite imports.
"""
import logging
import sys

from bot._impl import MainLoop  # noqa: F401

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
        force=True,
    )
    MainLoop().run()
