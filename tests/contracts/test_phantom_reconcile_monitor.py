"""Phantom reconcile monitor contract pins (ticket TBD, 2026-05-19).

NEW `scripts/ops/phantom_reconcile_monitor.py` standalone CLI runs via
cron on the VPS. Wraps the existing `scripts/audit/phantom_pnl_audit.py`
in an hourly automation surface that writes `phantom_corrections` rows
AND fires Telegram alerts on drift detection with day-stable
cross-process dedup (sidecar JSON, R1-M1 fix).

Pre-this-Bit the audit was manual-only — operator ran
`--run-id may18` on 2026-05-18, nothing since. Per-PnL phantom drift
was therefore silently accumulating between manual runs.

This file pins the public-API shape + threshold defaults + canonical
alert phrasings + dedup semantics. Drift would silently reopen any of
the alert classes RCA'd at design time:
  - C1: high-unverified-rate masking drift behind Kalshi REST flakes.
  - C2: auditor-crash silent-fail (cron swallows traceback).
  - M1: alert storm (one Telegram per phantom) + cross-cron dedup.
  - M2: dedup collision (24 hourly alerts/day for one real phantom).
  - M3: audit_run_id namespace collision with operator manual runs.
  - M4: LEFT-JOIN row multiplication on persistent phantoms.

R1 adversarial review revealed:
  - hour-granular run_id accumulated 24 rows/day per persistent
    phantom → changed to day-granular (`auto-YYYY-MM-DD`).
  - in-memory ``TelegramNotifier._dedup`` resets per cron tick →
    added JSON-sidecar cross-process dedup at the wrapper layer.
  - ``BaseException`` swallowed ``KeyboardInterrupt`` /
    ``SystemExit`` → narrowed to ``Exception``.

Sibling pattern: `tests/contracts/test_collector_health_monitor.py`
(D1.6, 2026-05-17) — same `inspect.signature` + mock-Notifier shape.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------
# Module-level + helper-function pins
# ---------------------------------------------------------------------

def test_module_importable():
    """The wrapper script must be importable as a module."""
    import scripts.ops.phantom_reconcile_monitor  # noqa: F401


def test_compute_run_id_day_granular_format():
    """`compute_run_id(dt)` returns `auto-YYYY-MM-DD` (DAY granular).

    R1-M4 fix: day granular (not hour) means 24 hourly cron firings
    within one UTC day share one run_id, and `INSERT OR REPLACE` on
    `UNIQUE(audit_run_id, ticker, side)` keeps the table to AT MOST
    one row per (day, ticker, side). Hour granularity would have
    accumulated 24 rows per persistent phantom per day — breaking
    downstream LEFT JOIN consumers.
    """
    import datetime
    from scripts.ops.phantom_reconcile_monitor import compute_run_id
    dt = datetime.datetime(2026, 5, 19, 11, 7, 42,
                           tzinfo=datetime.timezone.utc)
    assert compute_run_id(dt) == "auto-2026-05-19"


def test_compute_run_id_stable_across_hours_same_day():
    """Two different hours of the same UTC day → identical run_id.

    Pins the day-granular semantic: if a regression flipped this to
    hour-granular (auto-YYYY-MM-DDTHH), the row-multiplication class
    reopens silently.
    """
    import datetime
    from scripts.ops.phantom_reconcile_monitor import compute_run_id
    a = datetime.datetime(2026, 5, 19, 0, 30, tzinfo=datetime.timezone.utc)
    b = datetime.datetime(2026, 5, 19, 23, 30, tzinfo=datetime.timezone.utc)
    assert compute_run_id(a) == compute_run_id(b)


def test_compute_run_id_uses_auto_prefix():
    """Run-id must use `auto-` prefix to namespace-isolate from operator runs."""
    import datetime
    from scripts.ops.phantom_reconcile_monitor import compute_run_id
    rid = compute_run_id(
        datetime.datetime(2026, 1, 1, 0, tzinfo=datetime.timezone.utc)
    )
    assert rid.startswith("auto-"), (
        f"run_id must start with 'auto-' to namespace-isolate from "
        f"operator manual runs (e.g., --run-id may18). Got: {rid!r}"
    )


def test_compute_run_id_rejects_naive_datetime():
    """Defensive guard: reject naive datetime (would silently treat as
    local time and drift across DST). R1-N6: keep the guard honest with
    a negative-pin test.
    """
    import datetime
    import pytest
    from scripts.ops.phantom_reconcile_monitor import compute_run_id
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_run_id(datetime.datetime(2026, 5, 19, 11, 0))


# ---------------------------------------------------------------------
# Threshold default pins
# ---------------------------------------------------------------------

def test_default_threshold_cents():
    """`DEFAULT_THRESHOLD_CENTS = 500` ($5)."""
    from scripts.ops.phantom_reconcile_monitor import DEFAULT_THRESHOLD_CENTS
    assert DEFAULT_THRESHOLD_CENTS == 500


def test_default_unverified_rate_threshold():
    """`DEFAULT_UNVERIFIED_RATE_THRESHOLD = 0.5` (C1 fix)."""
    from scripts.ops.phantom_reconcile_monitor import (
        DEFAULT_UNVERIFIED_RATE_THRESHOLD,
    )
    assert DEFAULT_UNVERIFIED_RATE_THRESHOLD == 0.5


def test_default_lookback_days():
    """`DEFAULT_LOOKBACK_DAYS = 1` — hourly cron over 24h window."""
    from scripts.ops.phantom_reconcile_monitor import DEFAULT_LOOKBACK_DAYS
    assert DEFAULT_LOOKBACK_DAYS == 1


# ---------------------------------------------------------------------
# build_summary_alert
# ---------------------------------------------------------------------

def test_build_summary_alert_signature():
    """`build_summary_alert(summary, threshold_cents, ...)` exists."""
    from scripts.ops.phantom_reconcile_monitor import build_summary_alert
    sig = inspect.signature(build_summary_alert)
    assert "summary" in sig.parameters
    assert "threshold_cents" in sig.parameters


def test_build_summary_alert_none_when_no_material_findings():
    """Below-threshold phantoms only → return None."""
    from scripts.ops.phantom_reconcile_monitor import build_summary_alert
    summary = {
        "n_audited": 10, "n_divergent": 2, "n_unverified": 0, "n_matched": 8,
        "sum_delta_count": 1, "sum_delta_pnl_cents": 100,
        "findings": [
            {"ticker": "KXTEST-A", "side": "yes", "delta_count": 1,
             "delta_pnl_cents": 50, "local_count": 10, "kalshi_count": 9,
             "local_pnl_cents": -100, "corrected_pnl_cents": -50,
             "kalshi_revenue_cents": 0, "avg_price_cents": 50,
             "market_result": "no"},
            {"ticker": "KXTEST-B", "side": "yes", "delta_count": 1,
             "delta_pnl_cents": 50, "local_count": 5, "kalshi_count": 4,
             "local_pnl_cents": -100, "corrected_pnl_cents": -50,
             "kalshi_revenue_cents": 0, "avg_price_cents": 50,
             "market_result": "no"},
        ],
    }
    assert build_summary_alert(summary, threshold_cents=500) is None


def test_build_summary_alert_aggregates_multiple_findings_into_one_message():
    """M1 fix: 50 phantoms → 1 alert message."""
    from scripts.ops.phantom_reconcile_monitor import build_summary_alert
    findings = [
        {"ticker": f"KXTEST-{i:02d}", "side": "yes", "delta_count": 5,
         "delta_pnl_cents": 1000, "local_count": 10, "kalshi_count": 5,
         "local_pnl_cents": -1000, "corrected_pnl_cents": 0,
         "kalshi_revenue_cents": 0, "avg_price_cents": 100,
         "market_result": "no"}
        for i in range(50)
    ]
    summary = {
        "n_audited": 50, "n_divergent": 50, "n_unverified": 0, "n_matched": 0,
        "sum_delta_count": 250, "sum_delta_pnl_cents": 50_000,
        "findings": findings,
    }
    alert = build_summary_alert(summary, threshold_cents=500)
    assert alert is not None
    assert isinstance(alert, str)
    assert "50" in alert
    assert "$500" in alert or "500.00" in alert


def test_build_summary_alert_top_tickers_ranked_by_abs_delta_pnl():
    """R1-N3: pin BOTH that the worst-by-|Δpnl| ticker appears AND that
    it appears BEFORE the smaller-delta ticker. A regression that
    hardcoded `top = [findings[0]]` would pass a naive ``in`` check but
    not this ordering check.
    """
    from scripts.ops.phantom_reconcile_monitor import build_summary_alert
    findings = [
        # SMALL first in list — but smaller |Δpnl|, so should appear AFTER HUGE.
        {"ticker": "KXSMALL", "side": "yes", "delta_count": 1,
         "delta_pnl_cents": 600, "local_count": 1, "kalshi_count": 0,
         "local_pnl_cents": -50, "corrected_pnl_cents": 0,
         "kalshi_revenue_cents": 0, "avg_price_cents": 50,
         "market_result": "no"},
        {"ticker": "KXHUGE", "side": "yes", "delta_count": 100,
         "delta_pnl_cents": 10_000, "local_count": 100, "kalshi_count": 0,
         "local_pnl_cents": -10000, "corrected_pnl_cents": 0,
         "kalshi_revenue_cents": 0, "avg_price_cents": 100,
         "market_result": "no"},
    ]
    summary = {
        "n_audited": 2, "n_divergent": 2, "n_unverified": 0, "n_matched": 0,
        "sum_delta_count": 101, "sum_delta_pnl_cents": 10_600,
        "findings": findings,
    }
    alert = build_summary_alert(summary, threshold_cents=500)
    assert alert is not None
    assert "KXHUGE" in alert and "KXSMALL" in alert
    # Pin top-by-|Δpnl| ordering — KXHUGE listed before KXSMALL.
    assert alert.index("KXHUGE") < alert.index("KXSMALL"), (
        "Top-N listing must rank by |Δpnl| desc; KXHUGE (|Δpnl|=$100) "
        "must come before KXSMALL (|Δpnl|=$6)"
    )


# ---------------------------------------------------------------------
# build_unverified_rate_alert — C1 visibility-degraded signal
# ---------------------------------------------------------------------

def test_build_unverified_rate_alert_silent_below_threshold():
    """Low unverified rate → None."""
    from scripts.ops.phantom_reconcile_monitor import build_unverified_rate_alert
    summary = {
        "n_audited": 100, "n_divergent": 5, "n_unverified": 2,
        "n_matched": 93, "findings": [],
    }
    assert build_unverified_rate_alert(summary, rate_threshold=0.5) is None


def test_build_unverified_rate_alert_fires_above_threshold():
    """C1 fix: high unverified rate fires a separate alert."""
    from scripts.ops.phantom_reconcile_monitor import build_unverified_rate_alert
    summary = {
        "n_audited": 100, "n_divergent": 0, "n_unverified": 80,
        "n_matched": 20, "findings": [],
    }
    alert = build_unverified_rate_alert(summary, rate_threshold=0.5)
    assert alert is not None
    assert "unverified" in alert.lower()
    assert "80" in alert and "100" in alert


def test_build_unverified_rate_alert_handles_zero_audited():
    """Edge case: nothing audited → no divide-by-zero, no alert."""
    from scripts.ops.phantom_reconcile_monitor import build_unverified_rate_alert
    summary = {"n_audited": 0, "n_divergent": 0, "n_unverified": 0,
               "n_matched": 0, "findings": []}
    assert build_unverified_rate_alert(summary, rate_threshold=0.5) is None


# ---------------------------------------------------------------------
# _format_dollars (R1-N2)
# ---------------------------------------------------------------------

def test_format_dollars_zero_is_unsigned():
    """R1-N2: `_format_dollars(0)` → `$0.00`, NOT `+$0.00`."""
    from scripts.ops.phantom_reconcile_monitor import _format_dollars
    assert _format_dollars(0) == "$0.00"


def test_format_dollars_nonzero_signed():
    """Nonzero values keep mandatory sign so direction is unambiguous."""
    from scripts.ops.phantom_reconcile_monitor import _format_dollars
    assert _format_dollars(1234) == "+$12.34"
    assert _format_dollars(-500) == "-$5.00"


# ---------------------------------------------------------------------
# Dedup sidecar (R1-M1 cross-process dedup)
# ---------------------------------------------------------------------

def test_should_send_today_fresh_sidecar(tmp_path):
    """No sidecar yet → should_send returns True."""
    from scripts.ops.phantom_reconcile_monitor import _should_send_today
    sidecar = tmp_path / "dedup.json"
    assert _should_send_today(sidecar, "phantom_reconcile_summary_2026-05-19",
                              "2026-05-19") is True


def test_should_send_today_blocked_after_record(tmp_path):
    """After _record_sent, _should_send_today returns False for same key+date."""
    from scripts.ops.phantom_reconcile_monitor import (
        _record_sent, _should_send_today,
    )
    sidecar = tmp_path / "dedup.json"
    _record_sent(sidecar, "phantom_reconcile_summary_2026-05-19", "2026-05-19")
    assert _should_send_today(sidecar, "phantom_reconcile_summary_2026-05-19",
                              "2026-05-19") is False


def test_should_send_today_fresh_on_new_day(tmp_path):
    """Yesterday's record does NOT suppress today's alert."""
    from scripts.ops.phantom_reconcile_monitor import (
        _record_sent, _should_send_today,
    )
    sidecar = tmp_path / "dedup.json"
    _record_sent(sidecar, "phantom_reconcile_summary_2026-05-18", "2026-05-18")
    assert _should_send_today(sidecar, "phantom_reconcile_summary_2026-05-19",
                              "2026-05-19") is True


def test_dedup_sidecar_corrupted_falls_back_clean(tmp_path):
    """Malformed JSON / dict shape → treated as empty (don't raise)."""
    from scripts.ops.phantom_reconcile_monitor import _load_dedup_state
    sidecar = tmp_path / "dedup.json"
    sidecar.write_text("not valid json {")
    assert _load_dedup_state(sidecar) == {}


def test_dedup_sidecar_atomic_replace(tmp_path):
    """`_save_dedup_state` writes via tmp+rename so a crash mid-write
    doesn't truncate the on-disk state.
    """
    from scripts.ops.phantom_reconcile_monitor import _save_dedup_state
    sidecar = tmp_path / "dedup.json"
    _save_dedup_state(sidecar, {"k": "2026-05-19"})
    assert json.loads(sidecar.read_text())["k"] == "2026-05-19"


# ---------------------------------------------------------------------
# main() — orchestration contract
# ---------------------------------------------------------------------

def test_main_returns_zero_on_clean_run(tmp_path):
    """No phantoms above threshold + low unverified rate → exit 0, no alert."""
    from scripts.ops import phantom_reconcile_monitor as prm

    mock_notifier = MagicMock()
    mock_summary = {
        "n_audited": 10, "n_divergent": 0, "n_unverified": 1,
        "n_matched": 9, "sum_delta_count": 0, "sum_delta_pnl_cents": 0,
        "findings": [],
    }

    sidecar = tmp_path / "dedup.json"
    with patch.object(prm, "_run_audit_safely", return_value=mock_summary), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        rc = prm.main(argv=["--dedup-sidecar", str(sidecar)])
    assert rc == 0
    mock_notifier.send.assert_not_called()


def test_main_returns_zero_on_auditor_crash(tmp_path):
    """C2: even on auditor crash, exit 0 (cron convention) + Telegram alert."""
    from scripts.ops import phantom_reconcile_monitor as prm

    mock_notifier = MagicMock()

    def _raise(*a, **kw):
        raise RuntimeError("simulated KalshiClient init crash")

    sidecar = tmp_path / "dedup.json"
    with patch.object(prm, "_run_audit_safely", side_effect=_raise), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        rc = prm.main(argv=["--dedup-sidecar", str(sidecar)])
    assert rc == 0
    assert mock_notifier.send.called, (
        "Auditor crash without Telegram alert is C2 — operator blind."
    )
    args, kwargs = mock_notifier.send.call_args
    msg = args[0] if args else kwargs.get("message", "")
    assert "auditor" in msg.lower() or "crashed" in msg.lower()


def test_main_crash_alert_uses_crash_dedup_prefix(tmp_path):
    """R1-M6: pin the crash alert's dedup_key prefix.

    A regression that changed the crash prefix to summary would
    silently collide with the M1 summary alert on the same day.
    """
    from scripts.ops import phantom_reconcile_monitor as prm

    mock_notifier = MagicMock()
    sidecar = tmp_path / "dedup.json"

    with patch.object(prm, "_run_audit_safely",
                      side_effect=RuntimeError("boom")), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        prm.main(argv=["--dedup-sidecar", str(sidecar)])

    _, kwargs = mock_notifier.send.call_args
    dedup_key = kwargs.get("dedup_key", "")
    assert dedup_key.startswith(prm.DEDUP_PREFIX_CRASH), (
        f"crash dedup_key must start with {prm.DEDUP_PREFIX_CRASH!r}; "
        f"got {dedup_key!r}"
    )


def test_main_does_not_swallow_systemexit(tmp_path):
    """R1-M2: SystemExit MUST propagate so phantom_pnl_audit's
    load_client() sys.exit(1) on missing env lands in journalctl
    rather than being silenced as 'auditor crashed'.
    """
    from scripts.ops import phantom_reconcile_monitor as prm
    import pytest

    mock_notifier = MagicMock()
    sidecar = tmp_path / "dedup.json"

    with patch.object(prm, "_run_audit_safely",
                      side_effect=SystemExit(1)), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        with pytest.raises(SystemExit):
            prm.main(argv=["--dedup-sidecar", str(sidecar)])


def test_main_does_not_swallow_keyboard_interrupt(tmp_path):
    """R1-M2: KeyboardInterrupt propagates so Ctrl-C aborts cleanly."""
    from scripts.ops import phantom_reconcile_monitor as prm
    import pytest

    mock_notifier = MagicMock()
    sidecar = tmp_path / "dedup.json"

    with patch.object(prm, "_run_audit_safely",
                      side_effect=KeyboardInterrupt), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        with pytest.raises(KeyboardInterrupt):
            prm.main(argv=["--dedup-sidecar", str(sidecar)])


def test_main_alerts_on_material_phantoms(tmp_path):
    """Above-threshold phantoms → single aggregated alert fires."""
    from scripts.ops import phantom_reconcile_monitor as prm

    mock_notifier = MagicMock()
    mock_summary = {
        "n_audited": 5, "n_divergent": 2, "n_unverified": 0, "n_matched": 3,
        "sum_delta_count": 10, "sum_delta_pnl_cents": 2000,
        "findings": [
            {"ticker": "KXHYPE15M-A", "side": "yes", "delta_count": 5,
             "delta_pnl_cents": 1000, "local_count": 10, "kalshi_count": 5,
             "local_pnl_cents": -500, "corrected_pnl_cents": 500,
             "kalshi_revenue_cents": 1000, "avg_price_cents": 100,
             "market_result": "no"},
            {"ticker": "KXHYPE15M-B", "side": "yes", "delta_count": 5,
             "delta_pnl_cents": 1000, "local_count": 10, "kalshi_count": 5,
             "local_pnl_cents": -500, "corrected_pnl_cents": 500,
             "kalshi_revenue_cents": 1000, "avg_price_cents": 100,
             "market_result": "no"},
        ],
    }

    sidecar = tmp_path / "dedup.json"
    with patch.object(prm, "_run_audit_safely", return_value=mock_summary), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        rc = prm.main(argv=["--dedup-sidecar", str(sidecar)])
    assert rc == 0
    assert mock_notifier.send.call_count == 1, (
        "Expected 1 aggregated summary alert (M1 alert-storm regression)"
    )
    # Pin summary dedup_key prefix (R1-M6).
    _, kwargs = mock_notifier.send.call_args
    assert kwargs.get("dedup_key", "").startswith(prm.DEDUP_PREFIX_SUMMARY)


def test_main_fires_unverified_rate_alert_when_visibility_degraded(tmp_path):
    """R1-M7: main() actually wires the C1 unverified alert. A
    regression to `if unverified_alert: pass` would silently un-fire.
    """
    from scripts.ops import phantom_reconcile_monitor as prm

    mock_notifier = MagicMock()
    mock_summary = {
        "n_audited": 100, "n_divergent": 0, "n_unverified": 80,
        "n_matched": 20, "sum_delta_count": 0, "sum_delta_pnl_cents": 0,
        "findings": [],
    }

    sidecar = tmp_path / "dedup.json"
    with patch.object(prm, "_run_audit_safely", return_value=mock_summary), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        prm.main(argv=["--dedup-sidecar", str(sidecar)])

    assert mock_notifier.send.call_count == 1, (
        "C1 visibility-degraded alert must fire when n_unverified/n_audited "
        "exceeds threshold even if n_divergent=0"
    )
    _, kwargs = mock_notifier.send.call_args
    assert kwargs.get("dedup_key", "").startswith(prm.DEDUP_PREFIX_UNVERIFIED)


def test_main_dedup_sidecar_suppresses_repeat_alerts_within_day(tmp_path):
    """R1-M1 cross-process dedup verification: two consecutive main()
    invocations with the SAME summary fire the alert ONCE total.

    The in-memory `TelegramNotifier._dedup` would reset between cron
    invocations (fresh process per tick). The on-disk sidecar persists
    the last-fired UTC date so the second invocation suppresses.
    """
    from scripts.ops import phantom_reconcile_monitor as prm

    mock_notifier = MagicMock()
    mock_summary = {
        "n_audited": 1, "n_divergent": 1, "n_unverified": 0, "n_matched": 0,
        "sum_delta_count": 5, "sum_delta_pnl_cents": 1000,
        "findings": [
            {"ticker": "KXHYPE15M-A", "side": "yes", "delta_count": 5,
             "delta_pnl_cents": 1000, "local_count": 10, "kalshi_count": 5,
             "local_pnl_cents": -500, "corrected_pnl_cents": 500,
             "kalshi_revenue_cents": 1000, "avg_price_cents": 100,
             "market_result": "no"},
        ],
    }

    sidecar = tmp_path / "dedup.json"

    # First invocation — sidecar empty → alert fires.
    with patch.object(prm, "_run_audit_safely", return_value=mock_summary), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        prm.main(argv=["--dedup-sidecar", str(sidecar)])
    first_count = mock_notifier.send.call_count
    assert first_count == 1

    # Second invocation — same UTC day, same dedup_key → suppressed.
    with patch.object(prm, "_run_audit_safely", return_value=mock_summary), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        prm.main(argv=["--dedup-sidecar", str(sidecar)])
    assert mock_notifier.send.call_count == first_count, (
        "Same-day repeat invocation must be suppressed by sidecar dedup; "
        f"got {mock_notifier.send.call_count} sends (expected {first_count})"
    )


def test_main_alerts_dedup_key_includes_ymd_date(tmp_path):
    """dedup_key contains a YYYY-MM-DD date suffix for day-stable dedup."""
    from scripts.ops import phantom_reconcile_monitor as prm

    mock_notifier = MagicMock()
    mock_summary = {
        "n_audited": 1, "n_divergent": 1, "n_unverified": 0, "n_matched": 0,
        "sum_delta_count": 5, "sum_delta_pnl_cents": 1000,
        "findings": [
            {"ticker": "KXHYPE15M-A", "side": "yes", "delta_count": 5,
             "delta_pnl_cents": 1000, "local_count": 10, "kalshi_count": 5,
             "local_pnl_cents": -500, "corrected_pnl_cents": 500,
             "kalshi_revenue_cents": 1000, "avg_price_cents": 100,
             "market_result": "no"},
        ],
    }

    sidecar = tmp_path / "dedup.json"
    with patch.object(prm, "_run_audit_safely", return_value=mock_summary), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        prm.main(argv=["--dedup-sidecar", str(sidecar)])

    _, kwargs = mock_notifier.send.call_args
    dedup_key = kwargs.get("dedup_key", "")
    assert re.search(r"\d{4}-\d{2}-\d{2}", dedup_key), (
        f"dedup_key must include YYYY-MM-DD. Got: {dedup_key!r}"
    )


def test_main_uses_lazy_telegram_import():
    """`TelegramNotifier` imported INSIDE main() (lazy)."""
    import scripts.ops.phantom_reconcile_monitor as prm
    src = inspect.getsource(prm.main)
    assert "from bot.notifier import TelegramNotifier" in src


# ---------------------------------------------------------------------
# R2-M1 fix: content-fingerprint dedup so new drift defeats same-day dedup
# ---------------------------------------------------------------------

def _make_finding(ticker, delta_pnl_cents, side="yes", delta_count=5):
    return {
        "ticker": ticker, "side": side, "delta_count": delta_count,
        "delta_pnl_cents": delta_pnl_cents, "local_count": 10,
        "kalshi_count": 5, "local_pnl_cents": -delta_pnl_cents,
        "corrected_pnl_cents": 0, "kalshi_revenue_cents": 0,
        "avg_price_cents": 100, "market_result": "no",
    }


def test_summary_fingerprint_stable_for_same_material():
    """Same material findings → identical fingerprint (drift state unchanged)."""
    from scripts.ops.phantom_reconcile_monitor import _summary_fingerprint
    m = [_make_finding("KX-A", 1000), _make_finding("KX-B", 800)]
    assert _summary_fingerprint(m) == _summary_fingerprint(m)


def test_summary_fingerprint_changes_when_count_grows():
    """A FRESH material phantom appearing (one phantom → two) bumps the
    fingerprint so the new alert defeats same-day dedup (R2-M1 scenario)."""
    from scripts.ops.phantom_reconcile_monitor import _summary_fingerprint
    m1 = [_make_finding("KX-A", 1000)]
    m2 = [_make_finding("KX-A", 1000), _make_finding("KX-B", 50_000)]
    assert _summary_fingerprint(m1) != _summary_fingerprint(m2)


def test_summary_fingerprint_changes_when_top_ticker_changes():
    """Top-by-|Δpnl| ticker swap → fresh fingerprint."""
    from scripts.ops.phantom_reconcile_monitor import _summary_fingerprint
    m1 = [_make_finding("KX-A", 1000)]
    m2 = [_make_finding("KX-B", 1000)]
    assert _summary_fingerprint(m1) != _summary_fingerprint(m2)


def test_summary_fingerprint_stable_within_bucket():
    """A small Δpnl drift that stays in the same $10 bucket dedups.

    Chronic-drift class: a phantom inching from $5.50 → $6.00 → $6.50
    shouldn't re-fire 24 times/day. The dollar-bucket rule absorbs it.
    """
    from scripts.ops.phantom_reconcile_monitor import _summary_fingerprint
    m1 = [_make_finding("KX-A", 550)]
    m2 = [_make_finding("KX-A", 650)]
    # Both bucket = 550//1000 = 0 and 650//1000 = 0.
    assert _summary_fingerprint(m1) == _summary_fingerprint(m2)


def test_summary_fingerprint_changes_across_bucket_boundary():
    """A real growth across the $10 boundary → fresh fingerprint."""
    from scripts.ops.phantom_reconcile_monitor import _summary_fingerprint
    m1 = [_make_finding("KX-A", 900)]   # bucket 0
    m2 = [_make_finding("KX-A", 1500)]  # bucket 1
    assert _summary_fingerprint(m1) != _summary_fingerprint(m2)


def test_unverified_fingerprint_decile_bucketing():
    """Decile bucket dedups intra-decile, breaks dedup across deciles."""
    from scripts.ops.phantom_reconcile_monitor import _unverified_fingerprint
    # 55% and 58% both in decile 5.
    s1 = {"n_audited": 100, "n_unverified": 55}
    s2 = {"n_audited": 100, "n_unverified": 58}
    assert _unverified_fingerprint(s1) == _unverified_fingerprint(s2)
    # 55% and 65% cross decile boundary.
    s3 = {"n_audited": 100, "n_unverified": 65}
    assert _unverified_fingerprint(s1) != _unverified_fingerprint(s3)


def test_crash_fingerprint_by_exception_class():
    """Different exception classes → different fingerprints."""
    from scripts.ops.phantom_reconcile_monitor import _crash_fingerprint
    assert _crash_fingerprint(RuntimeError("x")) != _crash_fingerprint(ValueError("x"))
    # Same class, different message → same fingerprint (dedup).
    assert _crash_fingerprint(RuntimeError("a")) == _crash_fingerprint(RuntimeError("b"))


def test_main_summary_dedup_breaks_when_new_material_phantom_appears(tmp_path):
    """R2-M1 scenario regression test:
    09:00 UTC: 1 small material phantom fires alert + sidecar records.
    15:00 UTC same day: NEW big phantom appears → must fire (NOT dedup).
    """
    from scripts.ops import phantom_reconcile_monitor as prm

    mock_notifier = MagicMock()
    sidecar = tmp_path / "dedup.json"

    summary_early = {
        "n_audited": 5, "n_divergent": 1, "n_unverified": 0, "n_matched": 4,
        "sum_delta_count": 1, "sum_delta_pnl_cents": 600,
        "findings": [_make_finding("KX-SMALL", 600)],
    }
    summary_later = {
        "n_audited": 5, "n_divergent": 2, "n_unverified": 0, "n_matched": 3,
        "sum_delta_count": 11, "sum_delta_pnl_cents": 50_600,
        "findings": [
            _make_finding("KX-SMALL", 600),
            _make_finding("KX-HUGE", 50_000),
        ],
    }

    with patch.object(prm, "_run_audit_safely", return_value=summary_early), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        prm.main(argv=["--dedup-sidecar", str(sidecar)])

    with patch.object(prm, "_run_audit_safely", return_value=summary_later), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        prm.main(argv=["--dedup-sidecar", str(sidecar)])

    # Must have fired TWICE — second time defeats dedup via fingerprint.
    assert mock_notifier.send.call_count == 2, (
        f"R2-M1 regression: fresh material phantom must defeat same-day "
        f"dedup. Got {mock_notifier.send.call_count} sends (expected 2)."
    )


def test_main_summary_dedup_holds_when_state_unchanged(tmp_path):
    """Complement to the prior test: same state across two runs MUST dedup."""
    from scripts.ops import phantom_reconcile_monitor as prm

    mock_notifier = MagicMock()
    sidecar = tmp_path / "dedup.json"
    summary = {
        "n_audited": 1, "n_divergent": 1, "n_unverified": 0, "n_matched": 0,
        "sum_delta_count": 5, "sum_delta_pnl_cents": 1000,
        "findings": [_make_finding("KX-A", 1000)],
    }

    for _ in range(2):
        with patch.object(prm, "_run_audit_safely", return_value=summary), \
             patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
            prm.main(argv=["--dedup-sidecar", str(sidecar)])

    assert mock_notifier.send.call_count == 1, (
        "Unchanged state across two runs must dedup; "
        f"got {mock_notifier.send.call_count} sends"
    )


def test_main_skips_send_and_record_when_notifier_disabled(tmp_path):
    """R2-N2 fix: when notifier.enabled is False (missing tokens),
    skip BOTH the send AND the sidecar record. Otherwise an env fix
    mid-day stays suppressed until tomorrow.
    """
    from scripts.ops import phantom_reconcile_monitor as prm
    import json as _json

    mock_notifier = MagicMock()
    mock_notifier.enabled = False  # simulates missing TELEGRAM_*
    sidecar = tmp_path / "dedup.json"
    summary = {
        "n_audited": 1, "n_divergent": 1, "n_unverified": 0, "n_matched": 0,
        "sum_delta_count": 5, "sum_delta_pnl_cents": 1000,
        "findings": [_make_finding("KX-A", 1000)],
    }

    with patch.object(prm, "_run_audit_safely", return_value=summary), \
         patch("bot.notifier.TelegramNotifier", return_value=mock_notifier):
        prm.main(argv=["--dedup-sidecar", str(sidecar)])

    mock_notifier.send.assert_not_called()
    # Sidecar must NOT have a stale "sent" record.
    if sidecar.exists():
        state = _json.loads(sidecar.read_text())
        assert state == {}, (
            f"sidecar must not record sends when notifier disabled; got {state}"
        )


# ---------------------------------------------------------------------
# AST pins (R1-M8: apply=True must NEVER drift to apply=False)
# ---------------------------------------------------------------------

def test_run_audit_safely_invokes_apply_true():
    """R1-M8: AST-pin the literal ``apply=True`` in `_run_audit_safely`.

    A regression to ``apply=False`` would silently produce
    alert-only-no-DB-write behavior — alerts would fire but
    `phantom_corrections` would stay empty and the downstream
    LEFT JOIN consumers never see corrected numbers.
    """
    src_path = Path(__file__).resolve().parent.parent.parent / (
        "scripts/ops/phantom_reconcile_monitor.py"
    )
    tree = ast.parse(src_path.read_text())

    fn_node = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "_run_audit_safely"),
        None,
    )
    assert fn_node is not None, "_run_audit_safely function missing"

    apply_call_found = False
    for call in ast.walk(fn_node):
        if not isinstance(call, ast.Call):
            continue
        if not (isinstance(call.func, ast.Name) and call.func.id == "run_audit"):
            continue
        apply_kwarg = next(
            (kw for kw in call.keywords if kw.arg == "apply"),
            None,
        )
        assert apply_kwarg is not None, (
            "_run_audit_safely must pass apply=... explicitly to run_audit"
        )
        # Must be the literal True, not a variable/expression.
        assert isinstance(apply_kwarg.value, ast.Constant), (
            "apply must be a literal True constant, not an expression"
        )
        assert apply_kwarg.value.value is True, (
            f"_run_audit_safely must pass apply=True (got "
            f"apply={apply_kwarg.value.value!r}) — apply=False would "
            f"silently produce alert-only-no-DB-write drift"
        )
        apply_call_found = True
    assert apply_call_found, (
        "_run_audit_safely must call run_audit(...)"
    )
