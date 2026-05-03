"""Tests for scripts/h4_run_with_alert.py — Telegram-on-failure wrapper.

Round-1 adversarial review critique #1: H-4 backfill failures are
otherwise invisible until the v2 acceptance gate fires weeks later
(`null_pct < 0.10` per kb/decisions/phase-h-forward-going-capture-
required-may02.md). This wrapper ensures operator gets a Telegram
alert the same day a backfill exits non-zero.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))


def test_exit_code_zero_on_success(monkeypatch):
    """Wrapped command exits 0 → wrapper exits 0 → no Telegram alert."""
    import h4_run_with_alert
    sent = []
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert',
        lambda msg: sent.append(msg) or True,
    )
    rc = h4_run_with_alert.main([
        '--label', 'gdelt', '--', 'true',
    ])
    assert rc == 0
    assert sent == [], f"no alert on success; got: {sent}"


def test_exit_code_nonzero_triggers_alert(monkeypatch):
    """Wrapped command exits non-zero → wrapper exits same code → Telegram fired."""
    import h4_run_with_alert
    sent = []
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert',
        lambda msg: sent.append(msg) or True,
    )
    rc = h4_run_with_alert.main([
        '--label', 'glassnode', '--', 'false',
    ])
    assert rc != 0, "wrapper must propagate non-zero exit"
    assert len(sent) == 1, f"expected exactly 1 alert; got: {sent}"
    assert 'H-4 backfill FAILED' in sent[0]
    assert 'glassnode' in sent[0]


def test_format_failure_message_labels_124_as_hard_timeout(monkeypatch):
    """Exit code 124 is the GNU `timeout` convention used by
    `_h4_runtime_safety.install_hard_timeout` — the in-script
    orphan-prevention layer fires `sys.exit(124)`. The wrapper must
    label this distinctly from generic FAILURE so the operator
    doesn't mistake an expected first-run hard-timeout (multi-day
    historical backfill drain) for a fault that needs investigation."""
    import h4_run_with_alert
    msg = h4_run_with_alert.format_failure_message(
        label='gdelt',
        exit_code=124,
        cmd=['python3', 'scripts/gdelt_backfill.py', '--db', 'state.db'],
    )
    assert 'HARD TIMEOUT' in msg, (
        f"exit code 124 must be labeled 'HARD TIMEOUT'; got: {msg!r}"
    )
    assert 'will resume' in msg.lower(), (
        f"hard-timeout message must signal that the script will resume "
        f"on the next run (idempotent under retry); got: {msg!r}"
    )
    assert 'FAILED' not in msg, (
        f"must NOT label 124 as 'FAILED' — that's for non-orphan-"
        f"prevention faults; got: {msg!r}"
    )


def test_format_failure_message_labels_other_codes_as_failed(monkeypatch):
    """Non-124 exit codes still get the generic 'FAILED' label."""
    import h4_run_with_alert
    for code in (1, 2, 127, 143):
        msg = h4_run_with_alert.format_failure_message(
            label='gdelt', exit_code=code, cmd=['x'],
        )
        assert 'FAILED' in msg, f"code {code} must say 'FAILED'; got: {msg!r}"
        assert 'HARD TIMEOUT' not in msg, (
            f"code {code} should not say 'HARD TIMEOUT'; got: {msg!r}"
        )


def test_alert_message_includes_label_exit_code_and_journal_hint(monkeypatch):
    """Operator needs label + exit_code + how-to-find-logs in one shot."""
    import h4_run_with_alert
    msg = h4_run_with_alert.format_failure_message(
        label='cryptocompare',
        exit_code=42,
        cmd=['python3', 'scripts/cryptocompare_news_backfill.py', '--db', 'state.db'],
    )
    assert 'cryptocompare' in msg
    assert 'exit_code=42' in msg
    assert 'journalctl' in msg
    assert 'kalshi-h4-cryptocompare.service' in msg
    # Plain text (no markdown) — file paths shouldn't get mauled.
    assert '*' not in msg or 'asterisk' in msg.lower()


def test_send_telegram_no_creds_returns_false_does_not_raise(monkeypatch, capsys):
    """Missing env creds must NOT compound a backfill failure with an
    alert-side stack trace. Mirrors doc_drift_check.send_telegram contract.
    """
    import h4_run_with_alert
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)
    result = h4_run_with_alert.send_telegram_alert("test message")
    assert result is False
    captured = capsys.readouterr()
    assert 'TELEGRAM_BOT_TOKEN' in captured.err or 'CHAT_ID' in captured.err


def test_send_telegram_handles_network_error(monkeypatch):
    """Network/HTTP failure during alert POST must be caught — the
    wrapper's job is to AMPLIFY backfill failures, never to raise its
    own.
    """
    import h4_run_with_alert
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'dummy_token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '12345')

    def boom(*args, **kwargs):
        raise OSError("simulated network failure")

    monkeypatch.setattr(h4_run_with_alert.urllib.request, 'urlopen', boom)
    result = h4_run_with_alert.send_telegram_alert("test")
    assert result is False  # graceful degrade


def test_command_not_found_alerts_and_returns_127(monkeypatch):
    """If the wrapped script doesn't exist (typo, deploy regression),
    wrapper still alerts. Otherwise systemd marks the unit failed but
    operator never sees it.
    """
    import h4_run_with_alert
    sent = []
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert',
        lambda msg: sent.append(msg) or True,
    )
    rc = h4_run_with_alert.main([
        '--label', 'gdelt', '--',
        '/no/such/binary/this/should/not/exist',
    ])
    assert rc == 127
    assert len(sent) == 1
    assert 'gdelt' in sent[0]
    assert 'exit_code=127' in sent[0]


def test_label_required():
    """--label is the alert identity; refuse to run without it."""
    import h4_run_with_alert
    with pytest.raises(SystemExit):
        h4_run_with_alert.main(['--', 'true'])


def test_empty_command_after_separator_returns_2(monkeypatch, capsys):
    """If operator misconfigures ExecStart and forgets the command,
    refuse to proceed silently. Exit 2 (usage error)."""
    import h4_run_with_alert
    sent = []
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert',
        lambda msg: sent.append(msg) or True,
    )
    rc = h4_run_with_alert.main(['--label', 'gdelt'])
    assert rc == 2
    assert sent == [], "no alert for usage errors (would spam on misconfig)"


def test_telegram_message_truncated_at_4096(monkeypatch):
    """Telegram message API limit is 4096 chars. Wrapper must truncate
    to avoid POST rejection."""
    import h4_run_with_alert
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'tok')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '1')
    captured = {}

    def fake_urlopen(req, timeout=10):
        body = req.data.decode()
        import json as _json
        parsed = _json.loads(body)
        captured['text'] = parsed['text']
        class _R: pass
        return _R()

    monkeypatch.setattr(h4_run_with_alert.urllib.request, 'urlopen', fake_urlopen)
    huge = 'x' * 10_000
    h4_run_with_alert.send_telegram_alert(huge)
    assert len(captured['text']) <= 4096


def test_sigterm_handler_alerts_and_exits_143(monkeypatch):
    """Round-2 critique fix: when systemd's TimeoutStartSec hits, SIGTERM
    is delivered to the wrapper. Without a custom handler, Python's
    SIG_DFL terminates immediately and no alert fires. The handler MUST
    fire send_telegram_alert with the right context AND sys.exit(143).
    """
    import h4_run_with_alert
    sent = []
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert',
        lambda msg: sent.append(msg) or True,
    )
    # Populate module state as main() would. monkeypatch.setattr ensures
    # auto-restore after the test (round-3 critique #2 fix).
    monkeypatch.setattr(h4_run_with_alert, '_LABEL', 'gdelt')
    monkeypatch.setattr(
        h4_run_with_alert, '_CMD', ['python3', 'scripts/gdelt_backfill.py'],
    )
    monkeypatch.setattr(h4_run_with_alert, '_START_TIME', 0.0)
    monkeypatch.setattr(h4_run_with_alert, '_PROC', None)

    with pytest.raises(SystemExit) as exc:
        h4_run_with_alert._on_sigterm(15, None)
    assert exc.value.code == 143, "must exit 143 (128+SIGTERM) so systemd sees non-zero"
    assert len(sent) == 1, f"handler must send exactly one alert; got: {sent}"
    msg = sent[0]
    assert 'SIGTERM' in msg
    assert 'gdelt' in msg
    assert 'TimeoutStartSec' in msg or 'systemd' in msg, (
        "alert must hint at the likely cause"
    )


def test_sigterm_handler_signals_child_with_sigterm(monkeypatch):
    """If a child subprocess is still running when SIGTERM arrives, the
    handler must send SIGTERM to its PID to avoid leaking the process.
    Verifies the raw `os.kill(pid, SIGTERM)` call (post-adversarial-
    review #1: we use raw os.kill instead of `_PROC.terminate()` to
    avoid the misleading detour through `Popen.send_signal` that
    contends with `_waitpid_lock`)."""
    import signal as _signal
    import h4_run_with_alert
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert',
        lambda msg: True,
    )

    class FakeProc:
        pid = 12345

    # Track signals sent. Initial state: child cooperates (dies on
    # SIGTERM, _wait_for_exit returns True without escalation).
    signals_sent = []
    monkeypatch.setattr(
        h4_run_with_alert.os, 'kill',
        lambda pid, sig: signals_sent.append((pid, sig)),
    )
    monkeypatch.setattr(
        h4_run_with_alert, '_wait_for_exit', lambda pid, timeout: True,
    )

    fake = FakeProc()
    monkeypatch.setattr(h4_run_with_alert, '_LABEL', 'glassnode')
    monkeypatch.setattr(h4_run_with_alert, '_CMD', ['x'])
    monkeypatch.setattr(h4_run_with_alert, '_START_TIME', 0.0)
    monkeypatch.setattr(h4_run_with_alert, '_PROC', fake)

    with pytest.raises(SystemExit):
        h4_run_with_alert._on_sigterm(15, None)
    assert (12345, _signal.SIGTERM) in signals_sent, (
        f"handler must send SIGTERM to child pid; got: {signals_sent}"
    )


# ── Orphan-prevention regression (May 3 2026 incident) ────────────────

def test_sigterm_handler_kills_uncooperative_child(monkeypatch):
    """Regression for the 2026-05-03 orphan: a child process that
    IGNORES SIGTERM (e.g., a backfill script with no signal handler,
    or one stuck in an uninterruptible system call) must be escalated
    to SIGKILL after a short grace period — not left for the wrapper
    to abandon.

    Pre-fix: `_on_sigterm` called `_PROC.terminate()` then immediately
    `sys.exit(143)`, leaving the child orphaned. A
    `cryptocompare_news_backfill.py` instance survived 2h42m holding
    the state.db writer lock, eventually wedging the live bot's
    eval-write path post-restart. See
    `kb/decisions/h4-backfill-bugs-may04.md` (Bug 2 SEVERITY UPGRADE)
    and `kb/failures/shape-d-contention-explosion-may03.md`.

    Post-fix: handler must call `terminate()` → `wait(timeout=N)` →
    on `TimeoutExpired` escalate to `kill()` → `wait(timeout=M)` →
    exit. This test installs a fake child that survives terminate but
    dies on kill, and asserts BOTH escalation calls happened."""
    import subprocess
    import h4_run_with_alert
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert',
        lambda msg: True,
    )

    import signal as _signal

    class UncooperativeProc:
        pid = 99999

    fake = UncooperativeProc()
    monkeypatch.setattr(h4_run_with_alert, '_LABEL', 'cryptocompare')
    monkeypatch.setattr(h4_run_with_alert, '_CMD', ['python3', 'x.py'])
    monkeypatch.setattr(h4_run_with_alert, '_START_TIME', 0.0)
    monkeypatch.setattr(h4_run_with_alert, '_PROC', fake)

    # After SIGTERM: _wait_for_exit returns False (child ignored).
    # After SIGKILL: _wait_for_exit returns True (child died).
    signals_sent = []
    monkeypatch.setattr(
        h4_run_with_alert.os, 'kill',
        lambda pid, sig: signals_sent.append((pid, sig)),
    )
    wait_calls = []
    def fake_wait_for_exit(pid, timeout):
        wait_calls.append((pid, timeout))
        # First call (after SIGTERM): child still alive → False.
        # Second call (after SIGKILL): child dead → True.
        return len(wait_calls) >= 2
    monkeypatch.setattr(
        h4_run_with_alert, '_wait_for_exit', fake_wait_for_exit,
    )

    with pytest.raises(SystemExit):
        h4_run_with_alert._on_sigterm(1, None)  # SIGHUP

    assert (99999, _signal.SIGTERM) in signals_sent, (
        "must send SIGTERM first"
    )
    assert (99999, _signal.SIGKILL) in signals_sent, (
        "must escalate to SIGKILL when SIGTERM doesn't reap the child "
        "— this is the orphan-prevention regression"
    )
    assert len(wait_calls) == 2, (
        f"must call _wait_for_exit twice (after SIGTERM AND after "
        f"SIGKILL); got {len(wait_calls)} calls"
    )


def test_sigterm_handler_returns_quickly_when_child_cooperates(monkeypatch):
    """Companion to the orphan-prevention test: when the child DOES
    cooperate with SIGTERM, the handler must NOT escalate to kill().
    Otherwise we'd be sending unnecessary SIGKILLs to well-behaved
    children, masking real bugs in their shutdown paths."""
    import h4_run_with_alert
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert',
        lambda msg: True,
    )

    import signal as _signal

    class CooperativeProc:
        pid = 88888

    fake = CooperativeProc()
    monkeypatch.setattr(h4_run_with_alert, '_LABEL', 'glassnode')
    monkeypatch.setattr(h4_run_with_alert, '_CMD', ['x'])
    monkeypatch.setattr(h4_run_with_alert, '_START_TIME', 0.0)
    monkeypatch.setattr(h4_run_with_alert, '_PROC', fake)
    # Child cooperates: dies on SIGTERM (_wait_for_exit returns True),
    # so SIGKILL escalation is not reached.
    signals_sent = []
    monkeypatch.setattr(
        h4_run_with_alert.os, 'kill',
        lambda pid, sig: signals_sent.append((pid, sig)),
    )
    monkeypatch.setattr(
        h4_run_with_alert, '_wait_for_exit', lambda pid, timeout: True,
    )

    with pytest.raises(SystemExit):
        h4_run_with_alert._on_sigterm(15, None)

    sigs = [s for (_, s) in signals_sent]
    assert _signal.SIGTERM in sigs
    assert _signal.SIGKILL not in sigs, (
        "must NOT send SIGKILL when SIGTERM was honored"
    )


def test_sigterm_handler_installed_by_main(monkeypatch):
    """SIGTERM handler MUST be installed BEFORE the subprocess starts —
    if installed after, a SIGTERM during the early seconds slips through
    the default handler and kills us silently.
    """
    import h4_run_with_alert
    import signal as _signal
    handler_at_popen = []

    real_popen = h4_run_with_alert.subprocess.Popen

    def spy_popen(*args, **kwargs):
        # Capture the SIGTERM handler at the moment Popen is called.
        handler_at_popen.append(_signal.getsignal(_signal.SIGTERM))
        return real_popen(['true'], **{k: v for k, v in kwargs.items() if k != 'cmd'})

    monkeypatch.setattr(h4_run_with_alert.subprocess, 'Popen', spy_popen)
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert', lambda msg: True,
    )
    rc = h4_run_with_alert.main(['--label', 'gdelt', '--', 'true'])
    assert rc == 0
    # The handler observed at Popen time must be our custom handler,
    # NOT the default (SIG_DFL or signal.Handlers.SIG_DFL).
    assert handler_at_popen, "Popen was not called"
    h = handler_at_popen[0]
    assert h is h4_run_with_alert._on_sigterm, (
        f"SIGTERM handler not installed before subprocess start; "
        f"got: {h}"
    )


def test_sighup_handler_installed_by_main(monkeypatch):
    """H-4 GH-Actions pivot critique #5: appleboy/ssh-action command_timeout
    closes the SSH channel → child gets SIGHUP (not SIGTERM). Without a
    SIGHUP handler, the wrapper dies silently in the GH-Actions timeout
    case — exactly the failure mode the alerting was meant to surface.
    """
    import h4_run_with_alert
    import signal as _signal
    handler_at_popen = []

    real_popen = h4_run_with_alert.subprocess.Popen

    def spy_popen(*args, **kwargs):
        handler_at_popen.append(_signal.getsignal(_signal.SIGHUP))
        return real_popen(['true'], **{k: v for k, v in kwargs.items() if k != 'cmd'})

    monkeypatch.setattr(h4_run_with_alert.subprocess, 'Popen', spy_popen)
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert', lambda msg: True,
    )
    rc = h4_run_with_alert.main(['--label', 'gdelt', '--', 'true'])
    assert rc == 0
    assert handler_at_popen, "Popen was not called"
    h = handler_at_popen[0]
    assert h is h4_run_with_alert._on_sigterm, (
        f"SIGHUP handler not installed before subprocess start; "
        f"got: {h}"
    )


def test_end_to_end_sigterm_during_subprocess(monkeypatch, tmp_path):
    """E2E regression: spawn wrapper as a subprocess running `sleep 30`,
    SIGTERM the wrapper after 1s, assert wrapper exits 143 AND the
    alert was POSTed AND the inner `sleep 30` child is dead — the
    third assertion is the orphan-prevention regression that the
    May 3 incident bypassed (pre-fix, wrapper exited 143 but child
    survived 2h42m).
    """
    import os as _os
    import shutil
    import signal as _signal
    import subprocess as _subprocess
    import textwrap
    import time as _time

    sentinel = tmp_path / 'alert_fired'
    pid_file = tmp_path / 'inner_pid'
    # Write a shim script that monkeypatches send_telegram_alert to
    # write the sentinel AND records the inner subprocess PID so the
    # test can verify the inner child dies. Run it under the same Python.
    shim = tmp_path / 'shim.py'
    shim.write_text(textwrap.dedent(f"""
        import sys, subprocess as _sp
        sys.path.insert(0, {str(SCRIPT_DIR)!r})
        import h4_run_with_alert
        # Capture the child PID by patching subprocess.Popen.
        _orig_popen = _sp.Popen
        def _spying_popen(*a, **k):
            p = _orig_popen(*a, **k)
            with open({str(pid_file)!r}, 'w') as f:
                f.write(str(p.pid))
            return p
        h4_run_with_alert.subprocess.Popen = _spying_popen
        def _fake_alert(msg):
            with open({str(sentinel)!r}, 'w') as f:
                f.write(msg)
            return True
        h4_run_with_alert.send_telegram_alert = _fake_alert
        sys.exit(h4_run_with_alert.main([
            '--label', 'gdelt', '--', 'sleep', '30',
        ]))
    """))

    proc = _subprocess.Popen(
        [sys.executable, str(shim)],
        stdout=_subprocess.PIPE, stderr=_subprocess.PIPE,
    )
    # Give it a moment to install the handler + start the inner sleep.
    _time.sleep(1.0)
    proc.send_signal(15)  # SIGTERM
    try:
        rc = proc.wait(timeout=10)
    except _subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError("wrapper did not exit within 10s of SIGTERM")
    assert rc == 143, f"wrapper must exit 143 on SIGTERM; got {rc}"
    assert sentinel.exists(), "alert sentinel not written — handler did not fire"
    msg = sentinel.read_text()
    assert 'SIGTERM' in msg
    assert 'gdelt' in msg
    # Orphan-prevention regression check: inner `sleep 30` MUST be dead.
    # This is the precise May 3 incident behavior that previous tests
    # missed — wrapper exited 143 but child survived.
    assert pid_file.exists(), "inner PID was not captured — shim broken"
    inner_pid = int(pid_file.read_text())
    # Give the OS a brief moment to fully reap (the wrapper just exited;
    # init/launchd may need a tick to clean up if the child was orphaned).
    _time.sleep(0.5)
    try:
        _os.kill(inner_pid, 0)
        # Process exists → orphan! Clean up to avoid leaving zombies.
        try:
            _os.kill(inner_pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise AssertionError(
            f"inner sleep child (pid={inner_pid}) survived wrapper "
            f"exit — orphan-prevention regression"
        )
    except ProcessLookupError:
        pass  # child is dead, as required


# ── Direct tests of `_wait_for_exit` polling logic (Round 1 #11) ─────

def test_wait_for_exit_returns_true_when_child_exits_quickly(tmp_path):
    """`_wait_for_exit` must return True when the child exits within
    the timeout. Uses a real short-lived subprocess so the polling
    logic is exercised end-to-end (not mocked)."""
    import subprocess
    import time as _time
    import h4_run_with_alert

    proc = subprocess.Popen(['sleep', '0.1'])
    t0 = _time.monotonic()
    result = h4_run_with_alert._wait_for_exit(proc.pid, timeout=2.0)
    elapsed = _time.monotonic() - t0
    assert result is True, "should return True when child exits"
    assert elapsed < 1.0, f"should detect exit quickly; took {elapsed:.2f}s"
    # Note: the function reaped the child, so proc.wait() may raise.
    try:
        proc.wait(timeout=0.1)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def test_wait_for_exit_returns_false_when_child_outlives_timeout(tmp_path):
    """`_wait_for_exit` must return False when timeout elapses with the
    child still alive. Verifies the timeout path doesn't false-positive
    a long-running process as exited."""
    import os as _os
    import signal as _signal
    import subprocess
    import h4_run_with_alert

    proc = subprocess.Popen(['sleep', '5'])
    try:
        result = h4_run_with_alert._wait_for_exit(proc.pid, timeout=0.3)
        assert result is False, (
            "should return False when child outlives timeout"
        )
        assert proc.poll() is None, "child should still be alive"
    finally:
        # Clean up — kill the sleep, reap.
        try:
            _os.kill(proc.pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def test_wait_for_exit_handles_already_reaped_child(tmp_path):
    """If the child has already been reaped (e.g., outer wait() got
    there first), `_wait_for_exit` must NOT spin forever — it must
    treat ECHILD from os.waitpid as 'process is gone' and return
    True."""
    import subprocess
    import h4_run_with_alert

    proc = subprocess.Popen(['true'])
    proc.wait()  # outer wait reaps; PID is no longer waitable by us
    pid = proc.pid

    # _wait_for_exit will get ChildProcessError immediately.
    result = h4_run_with_alert._wait_for_exit(pid, timeout=2.0)
    assert result is True, (
        "must return True when child has already been reaped (ECHILD)"
    )
