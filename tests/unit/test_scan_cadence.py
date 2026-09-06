"""15M stays on the fast tick; weather/hourly/SPX only when slow_due.

Regression for kb/failures/scan-body-5-8s-collecting-mode-sep06.md.
"""
from bot.helpers.scan_cadence import include_window_this_tick, slow_scan_due


def test_15m_always_included():
    assert include_window_this_tick("15m", slow_due=False) is True
    assert include_window_this_tick(None, slow_due=False) is True


def test_weather_skipped_when_not_due():
    assert include_window_this_tick("weather", slow_due=False) is False
    assert include_window_this_tick("hourly", slow_due=False) is False
    assert include_window_this_tick("spx_hourly", slow_due=False) is False


def test_weather_included_when_due():
    assert include_window_this_tick("weather", slow_due=True) is True
    assert include_window_this_tick("hourly", slow_due=True) is True


def test_slow_scan_due_interval():
    assert slow_scan_due(now=30.0, last_ts=0.0, interval=30.0) is True
    assert slow_scan_due(now=29.9, last_ts=0.0, interval=30.0) is False
    # First live tick: last_ts defaults to 0, now is wall clock >> interval.
    assert slow_scan_due(now=1_700_000_000.0, last_ts=0.0, interval=30.0) is True


def test_convergence_velocity_slow_cadence_not_identically_zero():
    """Samples 32s apart sit outside CONVERGENCE_WINDOW_SECONDS=30.

    After the 30s slow-product skip, append-to-append is ~32s when scan
    body is 5–8s. Naive in-window walk returned 0.0. Scale the previous
    sample onto a 30s unit instead.
    """
    from collections import deque
    from unittest.mock import patch

    from bot.constants import CONVERGENCE_WINDOW_SECONDS
    from bot.scanner import OpportunityScanner

    now = 1_700_000_032.0
    scanner = OpportunityScanner.__new__(OpportunityScanner)
    scanner._ticker_ask_history = {
        "T": deque([(now - 32.0, 88), (now, 94)], maxlen=300),
    }
    with patch("bot.scanner.time.time", return_value=now):
        vel = scanner._scanner_convergence_velocity("T")
    assert vel != 0.0
    assert abs(vel - (6.0 * CONVERGENCE_WINDOW_SECONDS / 32.0)) < 1e-9


def test_convergence_velocity_dense_15m_unchanged():
    """1 Hz history still uses the in-window oldest, unscaled."""
    from collections import deque
    from unittest.mock import patch

    from bot.scanner import OpportunityScanner

    now = 1_700_000_030.0
    scanner = OpportunityScanner.__new__(OpportunityScanner)
    scanner._ticker_ask_history = {
        "T": deque(
            [(now - 25.0, 90), (now - 10.0, 91), (now, 93)],
            maxlen=300,
        ),
    }
    with patch("bot.scanner.time.time", return_value=now):
        vel = scanner._scanner_convergence_velocity("T")
    assert vel == 3.0
