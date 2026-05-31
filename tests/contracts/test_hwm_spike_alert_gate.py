"""HWM spike-alert mute gate (2026-05-31).

The HWM spike-rejection Telegram alert fires from
`bot/main_loop.py` when `_consecutive_spike_rejections == 3`. With the
bot not trading, the account balance bounces (56c <-> 30055c), which
re-trips the 3-consecutive-rejection counter and spams the Telegram
channel. This pins that the *Telegram send* is gated behind
`bot.constants.HWM_SPIKE_ALERT_ENABLED` (default OFF) while the
`logging.warning` continues to fire unconditionally for the journal.

Pins:
  1. `HWM_SPIKE_ALERT_ENABLED` exists and is env-driven, default OFF.
  2. The `_consecutive_spike_rejections == 3` Telegram send in
     `bot/main_loop.py` is guarded by `HWM_SPIKE_ALERT_ENABLED`.
  3. The `logging.warning(_msg)` for the spike is NOT gated (journal
     observability is preserved regardless of the mute).
"""
from __future__ import annotations

import ast
from pathlib import Path


def test_constant_exists_default_off(monkeypatch):
    monkeypatch.delenv("HWM_SPIKE_ALERT_ENABLED", raising=False)
    import importlib

    import bot.constants as constants

    importlib.reload(constants)
    assert hasattr(constants, "HWM_SPIKE_ALERT_ENABLED")
    assert constants.HWM_SPIKE_ALERT_ENABLED is False, (
        "Default must be OFF — the mute is the point. Flip via "
        "HWM_SPIKE_ALERT_ENABLED=1 to restore the alert."
    )


def test_constant_on_when_env_set(monkeypatch):
    monkeypatch.setenv("HWM_SPIKE_ALERT_ENABLED", "1")
    import importlib

    import bot.constants as constants

    importlib.reload(constants)
    try:
        assert constants.HWM_SPIKE_ALERT_ENABLED is True
    finally:
        monkeypatch.delenv("HWM_SPIKE_ALERT_ENABLED", raising=False)
        importlib.reload(constants)


def _main_loop_source() -> str:
    src = Path(__file__).resolve().parents[2] / "bot" / "main_loop.py"
    return src.read_text()


def test_telegram_send_gated_warning_not_gated():
    """The Telegram send is gated; logging.warning is not.

    AST-walk the HWM block: find the `_consecutive_spike_rejections == 3`
    branch and assert (a) the `_TELEGRAM.send(_msg)` call is nested under
    an `if` that tests `HWM_SPIKE_ALERT_ENABLED`, and (b) the
    `logging.warning(_msg)` is reachable without that guard.
    """
    tree = ast.parse(_main_loop_source())

    def _names(node):
        out = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        out |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        return out

    def _calls(node):
        out = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Attribute):
                    out.add(f.attr)
        return out

    # Find the `== 3` comparison branch that produces the spike alert.
    found_block = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (
            isinstance(test, ast.Compare)
            and "_consecutive_spike_rejections" in _names(test)
            and any(
                isinstance(c, ast.Constant) and c.value == 3
                for c in test.comparators
            )
        ):
            continue
        found_block = True
        # logging.warning must be reachable in this branch body directly.
        assert "warning" in _calls(node), (
            "logging.warning(_msg) must stay ungated for journal "
            "observability."
        )
        # The Telegram .send must be nested under an HWM_SPIKE_ALERT_ENABLED guard.
        send_is_gated = False
        for inner in ast.walk(node):
            if not isinstance(inner, ast.If):
                continue
            if "HWM_SPIKE_ALERT_ENABLED" in _names(inner.test):
                if "send" in _calls(inner):
                    send_is_gated = True
        assert send_is_gated, (
            "The HWM spike Telegram .send(_msg) must be nested under an "
            "`if HWM_SPIKE_ALERT_ENABLED ...` guard so the mute takes effect."
        )

    assert found_block, (
        "Could not locate the `_consecutive_spike_rejections == 3` spike "
        "alert branch in bot/main_loop.py."
    )
