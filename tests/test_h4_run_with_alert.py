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


def test_sigterm_handler_terminates_child_process(monkeypatch):
    """If a child subprocess is still running when SIGTERM arrives, the
    handler must terminate it to avoid leaking the process. Otherwise
    systemd waits for SIGKILL after TimeoutStopSec and the operator
    sees zombies in journalctl.
    """
    import h4_run_with_alert
    monkeypatch.setattr(
        h4_run_with_alert, 'send_telegram_alert',
        lambda msg: True,
    )

    class FakeProc:
        def __init__(self):
            self.terminated = False
        def poll(self):
            return None  # still running
        def terminate(self):
            self.terminated = True

    fake = FakeProc()
    monkeypatch.setattr(h4_run_with_alert, '_LABEL', 'glassnode')
    monkeypatch.setattr(h4_run_with_alert, '_CMD', ['x'])
    monkeypatch.setattr(h4_run_with_alert, '_START_TIME', 0.0)
    monkeypatch.setattr(h4_run_with_alert, '_PROC', fake)

    with pytest.raises(SystemExit):
        h4_run_with_alert._on_sigterm(15, None)
    assert fake.terminated, "child process must be terminated by SIGTERM handler"


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


def test_end_to_end_sigterm_during_subprocess(monkeypatch, tmp_path):
    """E2E regression: spawn wrapper as a subprocess running `sleep 30`,
    SIGTERM the wrapper after 1s, assert wrapper exits 143 AND the
    alert was POSTed (we verify via a sentinel file the fake telegram
    function writes, since we can't intercept across processes).
    """
    import os as _os
    import shutil
    import subprocess as _subprocess
    import textwrap
    import time as _time

    sentinel = tmp_path / 'alert_fired'
    # Write a shim script that monkeypatches send_telegram_alert to touch
    # the sentinel, then invokes h4_run_with_alert.main(). Run it under
    # the same Python.
    shim = tmp_path / 'shim.py'
    shim.write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(SCRIPT_DIR)!r})
        import h4_run_with_alert
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
