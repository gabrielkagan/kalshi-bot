"""research/tests/ pytest config — sys.path for the cell-block parity test.

The parity test imports `scripts.alpha_audit` to lock research's
vendored constants against the live oracle. scripts/ is not a
Python package (no __init__.py); we mirror tests/test_alpha_audit.py's
sys.path insertion pattern so `import alpha_audit` works at test time.
Production research/ never imports scripts/ — only this test suite does.
"""
import os
import sys


_THIS = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_THIS, "..", "..", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
