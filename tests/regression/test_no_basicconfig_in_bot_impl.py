"""Regression: `import bot._impl` MUST NOT call `logging.basicConfig`.

Bit 2.1a moved `logging.basicConfig` from `bot/_impl.py` module-level into
`bot/__main__.py`'s `if __name__ == "__main__":` guard. If a future bit
re-introduces module-level basicConfig to `bot/_impl.py`, it would clobber
pytest's caplog fixture handlers — silent test-suite regression.

Subprocess isolation is required (R7 #2): in-process `import bot._impl`
is a no-op once `sys.modules['bot._impl']` is populated by an earlier
test, and `importlib.reload` doesn't reset the root-logger state Python's
logging module accumulated across prior tests.
"""
import subprocess
import sys


def test_import_bot_impl_does_not_clobber_root_logger():
    """Subprocess-isolated check: `import bot._impl` in a fresh interpreter
    must NOT call `logging.basicConfig` (would clobber pytest caplog when
    someone re-introduces module-level basicConfig to bot/_impl.py)."""
    script = '''
import logging, sys
pre_handlers = len(logging.getLogger().handlers)
pre_level = logging.getLogger().level
import bot._impl  # full module-level execution in a fresh interpreter
post_handlers = len(logging.getLogger().handlers)
post_level = logging.getLogger().level
assert post_handlers == pre_handlers, f"handlers changed: {pre_handlers} -> {post_handlers}"
assert post_level == pre_level, f"root level changed: {pre_level} -> {post_level}"
sys.exit(0)
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"bot._impl import clobbers root logger:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
