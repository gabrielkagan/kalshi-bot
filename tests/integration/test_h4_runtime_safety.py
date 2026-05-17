"""Tests for scripts/ops/_h4_runtime_safety.py — shared hard-timeout
guard for H-4 backfill scripts.

Layer 2 of orphan prevention (May 3 2026 incident postmortem in
`kb/failures/shape-d-contention-explosion-may03.md`). The wrapper
fix (`scripts/ops/h4_run_with_alert.py`) handles SIGTERM/SIGHUP-mediated
orphans. This layer prevents the script itself from running away
silently — if `cryptocompare_news_backfill.py` gets stuck in an API
or DNS loop with no signal arriving, the alarm fires and the script
exits, releasing state.db's writer lock.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts" / "ops"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def test_install_hard_timeout_registers_sigalrm_handler():
    """install_hard_timeout must register a non-default SIGALRM handler.
    Without this, signal.alarm() would terminate the process via the
    default SIG_DFL behavior, which doesn't release sqlite3 connections
    cleanly via Python finalizers."""
    if not hasattr(signal, "SIGALRM"):
        pytest.skip("SIGALRM not available on this platform")
    import _h4_runtime_safety
    # Save current handler so we can restore after.
    prev = signal.getsignal(signal.SIGALRM)
    try:
        _h4_runtime_safety.install_hard_timeout(seconds=3600, label="test")
        h = signal.getsignal(signal.SIGALRM)
        assert h is not signal.SIG_DFL, "must install a Python handler"
        assert h is not signal.SIG_IGN, "must not ignore SIGALRM"
    finally:
        signal.alarm(0)  # cancel pending alarm
        signal.signal(signal.SIGALRM, prev)


@pytest.mark.serial
def test_install_hard_timeout_exits_124_when_alarm_fires():
    """When SIGALRM fires, the handler must `sys.exit(124)`. Code 124
    matches GNU `timeout` convention; the wrapper script (h4_run_with_-
    alert.py) translates non-zero exits to Telegram alerts so the
    operator gets a clear 'hard timeout' indicator.

    Bit-5 (CI perf umbrella 86b9zjtzk): @serial because this test uses
    a 1-second SIGALRM (`seconds=1`) + `time.sleep(2.0)` to verify
    delivery — only 1s of headroom. Under pytest-xdist parallel workers,
    CPU contention on a 2-vCPU CI runner could delay Python signal
    delivery past the 1s window and produce a flaky failure."""
    if not hasattr(signal, "SIGALRM"):
        pytest.skip("SIGALRM not available on this platform")
    import _h4_runtime_safety
    prev = signal.getsignal(signal.SIGALRM)
    try:
        _h4_runtime_safety.install_hard_timeout(seconds=1, label="test")
        # Sleep past the alarm; SIGALRM fires; handler raises SystemExit.
        with pytest.raises(SystemExit) as excinfo:
            time.sleep(2.0)
        assert excinfo.value.code == 124, (
            f"expected exit code 124 (GNU timeout convention); "
            f"got {excinfo.value.code}"
        )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


def test_install_hard_timeout_idempotent():
    """Calling install_hard_timeout twice must replace the previous
    alarm — not stack two alarms. Otherwise repeated invocations from
    different code paths would either fire prematurely (stacked) or
    leak handlers."""
    if not hasattr(signal, "SIGALRM"):
        pytest.skip("SIGALRM not available on this platform")
    import _h4_runtime_safety
    prev = signal.getsignal(signal.SIGALRM)
    try:
        _h4_runtime_safety.install_hard_timeout(seconds=10, label="t1")
        _h4_runtime_safety.install_hard_timeout(seconds=20, label="t2")
        # The second call should have replaced the alarm. We can't
        # easily inspect the remaining time, but signal.alarm(N)
        # returning the previous remaining time is the documented
        # API; calling alarm(0) here returns the remaining time of
        # the most recent alarm (the second one we installed).
        remaining = signal.alarm(0)
        # Should be close to 20 (the second call's value), not 10 or
        # 30 (the sum). Wide tolerance (15-20) survives slow CI /
        # coverage instrumentation that can add seconds of latency
        # between the two install calls; the failure mode we care
        # about is "stacked to ~30" or "first install (~10) survived",
        # not exact second-precision.
        assert 15 <= remaining <= 20, (
            f"second install should replace (not stack), and not be "
            f"the first install's value; got remaining={remaining}, "
            f"expected ~20"
        )
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)


def test_install_hard_timeout_no_op_on_platforms_without_sigalrm(monkeypatch):
    """On Windows (where SIGALRM doesn't exist), install_hard_timeout
    must be a no-op rather than raise AttributeError. H-4 scripts run
    on Linux today, but unit tests on a Mac developer laptop may
    import the module — and SIGALRM IS available on Mac, so this
    test directly mocks `hasattr(signal, 'SIGALRM')` to verify the
    Windows fallback path."""
    import _h4_runtime_safety
    # Force the no-op path by stripping SIGALRM from signal during
    # the call. We can't actually remove it (built-in module), so
    # we patch hasattr lookup at the function's own scope.
    import types
    fake_signal = types.SimpleNamespace()
    monkeypatch.setattr(_h4_runtime_safety, "signal", fake_signal)
    # If the function doesn't crash, the no-op path worked.
    _h4_runtime_safety.install_hard_timeout(seconds=10, label="x")


def test_default_hard_timeout_is_25_minutes():
    """The default timeout (1500s = 25 min) is intentional: it sits
    inside the H-4 wrapper's command_timeout=30m and well under the
    systemd TimeoutStartSec=1h. Increasing this without coordinating
    those upstream timeouts re-creates the orphan window."""
    import _h4_runtime_safety
    assert _h4_runtime_safety.DEFAULT_HARD_TIMEOUT_S == 1500, (
        f"DEFAULT_HARD_TIMEOUT_S={_h4_runtime_safety.DEFAULT_HARD_TIMEOUT_S}; "
        f"expected 1500 (25 min). If you're raising this, also raise "
        f"the wrapper command_timeout in .github/workflows/h4_backfill.yml."
    )


# ── Integration: each backfill script imports + installs ──────────────

def test_gdelt_backfill_main_installs_hard_timeout():
    """Each backfill script's main() MUST call install_hard_timeout
    near the top, before any work begins. Verified via AST scan
    (avoids spinning up a real backfill in tests)."""
    _assert_script_installs_hard_timeout("gdelt_backfill.py")


def test_cryptocompare_backfill_main_installs_hard_timeout():
    _assert_script_installs_hard_timeout("cryptocompare_news_backfill.py")


def test_glassnode_backfill_main_installs_hard_timeout():
    _assert_script_installs_hard_timeout("glassnode_backfill.py")


def _assert_script_installs_hard_timeout(script_name: str) -> None:
    import ast
    # Bit 11.2 (2026-05-12): the H-4 backfill scripts live in scripts/backfill/,
    # not scripts/ops/. The runtime-safety helper itself is in scripts/ops/.
    backfill_dir = Path(__file__).resolve().parents[2] / "scripts" / "backfill"
    src_path = backfill_dir / script_name
    src = src_path.read_text()
    tree = ast.parse(src)
    # Find the main() function.
    main_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            main_fn = node
            break
    assert main_fn is not None, f"{script_name} has no main()"
    # Walk main()'s body for a call to install_hard_timeout.
    found = False
    for node in ast.walk(main_fn):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name)
                else None
            )
            if name == "install_hard_timeout":
                found = True
                break
    assert found, (
        f"{script_name} main() must call install_hard_timeout(); "
        f"this is the orphan-prevention guard from the May 3 incident"
    )
