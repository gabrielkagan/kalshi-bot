"""Regression: bot/_impl.py module-level must NOT call logging.basicConfig.

Bit 2.1a R5 #8 — module-level basicConfig in bot/_impl.py would fire on
EVERY `import bot._impl` (including pytest collection), clobbering pytest's
caplog fixture handlers. The basicConfig now lives in bot/__main__.py
inside `if __name__ == "__main__":` so it only runs when production
starts via `python -m bot`.

This test runs in a SUBPROCESS (R7 #2 correction) — otherwise
`sys.modules['bot._impl']` is already populated from earlier tests in the
same pytest run; an in-process `import bot._impl` is a no-op and would
not detect a re-introduced `logging.basicConfig` call.

Detection scope (Bit 2.1a implementation-review fix): we trap
`logging.basicConfig` invocations and only fail when an EXPLICITLY
configured call (with kwargs like `format=`, `level=`, `handlers=`,
`force=True`) lands during module-level execution. The Python
stdlib's `logging.info()` / `logging.warning()` / etc. module-level
convenience functions auto-invoke `basicConfig()` with NO arguments
when the root logger has no handlers configured (see CPython
`Lib/logging/__init__.py`); that auto-call is benign for caplog because
pytest installs a handler before any test runs and the auto-trigger
short-circuits. The regression we guard against is an EXPLICIT
`logging.basicConfig(level=..., format=..., handlers=..., force=True)`
in bot/_impl.py module-level code, which clobbers caplog regardless of
prior handler state via `force=True`. (config.py:58 already triggers
the no-arg auto-call as a transitive side effect of `_load_dist_config`'s
`logging.info(...)`; that's a pre-existing pattern unrelated to the
basicConfig-clobber regression Bit 2.1a is preventing.)
"""
import subprocess
import sys


def test_import_bot_impl_does_not_call_explicit_basicconfig():
    """Subprocess-isolated check: `import bot._impl` in a fresh interpreter
    must NOT make an EXPLICIT `logging.basicConfig(...)` call (one with
    arguments — the kind that clobbers existing handlers / forces format
    overrides). The no-arg auto-call from stdlib convenience functions
    when the root logger is unconfigured is benign and ignored."""
    script = '''
import logging, sys
explicit_calls = []
orig = logging.basicConfig
def trap(*args, **kwargs):
    if args or kwargs:
        explicit_calls.append((args, sorted(kwargs.keys())))
    return orig(*args, **kwargs)
logging.basicConfig = trap
import bot._impl  # full module-level execution in a fresh interpreter
if explicit_calls:
    print(f"FAIL: explicit logging.basicConfig was called during import: {explicit_calls!r}")
    sys.exit(1)
sys.exit(0)
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (
        f"bot._impl import called explicit logging.basicConfig (would clobber "
        f"pytest caplog):\nstdout={result.stdout}\nstderr={result.stderr}"
    )
