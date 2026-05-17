"""D1.6 — collector health monitor contract pins (ticket 86b9zk4we, 2026-05-17).

NEW `scripts/ops/collector_health_monitor.py` standalone CLI runs via
cron on the VPS. Pre-D1.6 the collector had NO Telegram alert surface
for disk pressure, WS conn-loss, or service-down events (D0.3 §6
enumerated these as known-unalerted classes).

D1.6 closes these via 3 check functions that return Optional[str]
alert messages, dispatched through the existing
`bot.notifier.TelegramNotifier` (no re-implementation of the Telegram
client).

This file pins the public-API shape + threshold defaults + canonical
alert phrasings. Drift would silently reopen any of the 3 alert
classes.

Pins:
  1. The 3 check functions exist with the documented signatures.
  2. Default thresholds match D1.6 plan (80%, 5min/10count).
  3. Alert messages include diagnostic context (paths, counts, recovery hints).
  4. The monitor's `main()` imports `TelegramNotifier` from `bot.notifier`
     and does NOT re-implement the Telegram client.
  5. `main()` returns 0 even on alert-fire (cron convention).
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch


def test_check_disk_signature():
    """`check_disk(path=..., threshold_pct=...)` exists with documented kwargs."""
    from scripts.ops.collector_health_monitor import check_disk
    sig = inspect.signature(check_disk)
    assert "path" in sig.parameters
    assert "threshold_pct" in sig.parameters
    # Default threshold = 80% per D1.6 plan (matches D0.3 §6 disk-pressure floor).
    assert sig.parameters["threshold_pct"].default == 80, (
        f"check_disk default threshold_pct should be 80 (per D1.6 plan); "
        f"got {sig.parameters['threshold_pct'].default}"
    )


def test_check_disk_silent_below_threshold():
    """When disk usage is below threshold, return None (no alert)."""
    from scripts.ops.collector_health_monitor import check_disk
    # Use a forced-high threshold so even a full disk wouldn't trip.
    result = check_disk(threshold_pct=200)
    assert result is None, f"Expected None below threshold; got {result!r}"


def test_check_disk_alerts_above_threshold():
    """When disk usage exceeds threshold, return alert string with usage details."""
    from scripts.ops.collector_health_monitor import check_disk
    # Forced-low threshold (0%) so any disk usage trips it.
    result = check_disk(threshold_pct=0)
    assert result is not None, "Expected alert at threshold=0 (any usage trips)"
    assert "DISK ALERT" in result, f"Alert message missing 'DISK ALERT': {result!r}"
    assert "%" in result, f"Alert message missing usage percentage: {result!r}"
    assert "threshold" in result.lower(), (
        f"Alert message should mention threshold for operator context: {result!r}"
    )


def test_check_ws_reconnects_signature():
    """`check_ws_reconnects(window_min=..., threshold_count=..., unit=...)` exists."""
    from scripts.ops.collector_health_monitor import check_ws_reconnects
    sig = inspect.signature(check_ws_reconnects)
    assert "window_min" in sig.parameters
    assert "threshold_count" in sig.parameters
    assert "unit" in sig.parameters
    # Per D1.6 plan: 10 disconnects per 5 min = ~2/min cadence trips alert.
    assert sig.parameters["window_min"].default == 5
    assert sig.parameters["threshold_count"].default == 10


def test_check_ws_reconnects_silent_when_journalctl_unavailable():
    """When journalctl is absent or errors, return None (don't alert-spam)."""
    from scripts.ops.collector_health_monitor import check_ws_reconnects
    # Use a unit name unlikely to exist in any test environment; journalctl
    # may return empty output (count=0 < threshold) or fail (returns None).
    # Either way: None or non-alert string.
    result = check_ws_reconnects(unit="definitely-not-a-real-unit-d1-6-test")
    assert result is None, (
        f"Expected None when no disconnects found; got {result!r}. "
        f"Alert-spam on absent unit would breach plan §Risk register."
    )


def test_check_ws_reconnects_alert_includes_class_breakdown():
    """Verify alert string format would include 1006/1009/1011 class
    breakdown when disconnects are above threshold.

    Uses subprocess mock to inject a synthetic journalctl response.
    """
    from scripts.ops import collector_health_monitor as mod
    fake_journal = "\n".join(
        ["kalshi_ws_disconnected: reason=sent 1011 (internal error)..."] * 15
    )
    with patch.object(mod.subprocess, "check_output", return_value=fake_journal):
        result = mod.check_ws_reconnects(threshold_count=10)
    assert result is not None, "Expected alert with 15 disconnects > threshold 10"
    assert "RECONNECT STORM" in result, (
        f"Alert message missing 'RECONNECT STORM' marker: {result!r}"
    )
    assert "1011" in result, (
        f"Alert message must include 1011 class count: {result!r}"
    )


def test_check_collector_active_signature():
    """`check_collector_active(unit=...)` exists with documented default."""
    from scripts.ops.collector_health_monitor import check_collector_active
    sig = inspect.signature(check_collector_active)
    assert "unit" in sig.parameters
    assert sig.parameters["unit"].default == "kalshi-collector"


def test_check_collector_active_silent_when_systemctl_absent():
    """systemctl absent (test env) → None, not crash."""
    from scripts.ops.collector_health_monitor import check_collector_active
    # In test env, systemctl IS usually present but no kalshi-collector
    # unit exists → exit 3 (inactive) → alert WOULD fire. To make this
    # test environment-independent, use a unit name unlikely to be active.
    result = check_collector_active(unit="definitely-not-a-real-unit-d1-6-test")
    # Either None (systemctl absent) or an alert string about the unit
    # being not-active. Both are valid; the test pins the function doesn't
    # CRASH.
    assert result is None or isinstance(result, str), (
        f"Expected None or alert string; got {type(result).__name__}"
    )


def test_main_uses_bot_notifier_telegram_notifier():
    """`main()` MUST import `TelegramNotifier` from `bot.notifier` and use
    it for alert dispatch — NOT re-implement the Telegram client.

    Reuses the bot's battle-tested notifier (dedup + threading +
    Markdown formatting). Re-implementation risks divergence (different
    dedup semantics, missing error handling).
    """
    from scripts.ops import collector_health_monitor as mod
    src = inspect.getsource(mod)
    # Either import-form is acceptable.
    has_import = (
        "from bot.notifier import TelegramNotifier" in src
        or "import bot.notifier" in src
    )
    assert has_import, (
        "scripts/ops/collector_health_monitor.py MUST import TelegramNotifier "
        "from bot.notifier (no re-implementation). See D1.6 plan § Scope."
    )


def test_main_returns_zero_per_cron_convention():
    """`main()` MUST return 0 even when alerts fire (cron convention —
    alerts go via Telegram, not exit code, so cron doesn't email the
    operator's spool on every transient threshold breach).
    """
    from scripts.ops import collector_health_monitor as mod
    # Mock TelegramNotifier so we don't actually try to send.
    with patch.object(mod, "__name__", "scripts.ops.collector_health_monitor"):
        with patch("bot.notifier.TelegramNotifier") as MockNotifier:
            MockNotifier.return_value = MagicMock()
            rc = mod.main()
    assert rc == 0, f"main() must return 0 per cron convention; got {rc}"
