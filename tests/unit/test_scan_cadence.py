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
        vel = scanner._scanner_convergence_velocity("T", product_type="hourly")
    assert vel != 0.0
    assert abs(vel - (6.0 * CONVERGENCE_WINDOW_SECONDS / 32.0)) < 1e-9


def test_convergence_velocity_15m_gap_stays_zero():
    """15M sparse gap must stay 0.0 — scaled fallback is slow-product only.

    A 31s 15M hole (one-sided book / OB-fetch cap) previously returned 0.
    Ungated fallback would scale ~6¢ into TAKER_URGENT (velocity > 5).
    """
    from collections import deque
    from unittest.mock import patch

    from bot.scanner import OpportunityScanner

    now = 1_700_000_032.0
    scanner = OpportunityScanner.__new__(OpportunityScanner)
    scanner._ticker_ask_history = {
        "T": deque([(now - 32.0, 88), (now, 94)], maxlen=300),
    }
    with patch("bot.scanner.time.time", return_value=now):
        assert scanner._scanner_convergence_velocity("T") == 0.0
        assert scanner._scanner_convergence_velocity("T", product_type="15m") == 0.0


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
        vel = scanner._scanner_convergence_velocity("T", product_type="15m")
    assert vel == 3.0


def test_scan_wires_include_window_this_tick():
    """OpportunityScanner.scan must call the cadence helper. Helper unit
    tests stay green if the continue is deleted.
    """
    from pathlib import Path

    src = Path("bot/scanner/__init__.py").read_text(encoding="utf-8")
    assert "include_window_this_tick(_pt, _slow_due)" in src
    assert "from bot.helpers.scan_cadence import" in src
