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
