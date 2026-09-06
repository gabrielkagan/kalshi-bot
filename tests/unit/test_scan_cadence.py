"""15M stays on the fast tick; weather/hourly/SPX only when slow_due.

Regression for kb/failures/scan-body-5-8s-collecting-mode-sep06.md.
"""
from bot.helpers.scan_cadence import (
    include_window_this_tick,
    rotate_slow_product_windows,
    slow_scan_due,
)


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


def test_rotate_slow_keeps_15m_prefix_and_rotates_slow():
    """12-fetch slow budget must not always hit the same weather/hourly
    prefix. 15M stays in original relative order at the front so REST
    for observation products cannot delay the live 15M path.
    """
    windows = [
        {"product_type": "15m", "id": "a"},
        {"product_type": "weather", "id": "w0"},
        {"product_type": "15m", "id": "b"},
        {"product_type": "hourly", "id": "h0"},
        {"product_type": "weather", "id": "w1"},
        {"product_type": "spx_hourly", "id": "s0"},
    ]
    out0 = rotate_slow_product_windows(windows, 0)
    assert [w["id"] for w in out0] == ["a", "b", "w0", "h0", "w1", "s0"]
    out1 = rotate_slow_product_windows(windows, 1)
    assert [w["id"] for w in out1] == ["a", "b", "h0", "w1", "s0", "w0"]
    out4 = rotate_slow_product_windows(windows, 4)
    assert [w["id"] for w in out4] == ["a", "b", "w0", "h0", "w1", "s0"]


def test_rotate_slow_empty_and_fast_only():
    assert rotate_slow_product_windows([], 3) == []
    fast = [{"product_type": "15m", "id": "a"}]
    assert rotate_slow_product_windows(fast, 9) == fast


def test_scan_wires_include_window_this_tick():
    """OpportunityScanner.scan must call the cadence helper. Helper unit
    tests stay green if the continue is deleted.
    """
    from pathlib import Path

    src = Path("bot/scanner/__init__.py").read_text(encoding="utf-8")
    assert "include_window_this_tick(_pt, _slow_due)" in src
    assert "from bot.helpers.scan_cadence import" in src
    # Slow-due ticks use a separate REST budget, not an uncapped tick.
    assert "MAX_OB_FETCHES_PER_SLOW_TICK" in src
    assert "slow_ob_fetches_this_tick" in src
    # 15M cap must still be consulted on slow-due ticks (not `if _slow_due`).
    assert "elif ob_fetches_this_tick >= MAX_OB_FETCHES_PER_TICK" in src
    assert "rotate_slow_product_windows(" in src
    # Pre-loop OFT must not take KalshiFeed._lock per hourly/weather ticker.
    assert "get_all_orderbooks_snapshot()" in src
    assert "get_subscribed_tickers()" in src
