"""B3-fu3 contract pins (ticket 86b9zxb4c, 2026-05-18).

Adds a 5th check tier to `scripts/ops/collector_health_monitor.py`:
``check_insert_evaluated_opportunity_failures`` — alerts when the
`insert_evaluated_opportunity failed` WARNING fires in `kalshi-bot`
journalctl.

Why: the marker substring matches ~20 WARN sites across
`bot/scanner/__init__.py` + `bot/state.py`. B3-fu2 narrowed 2 of them
(LPNE + dc_shadow_no_side POR) to `sqlite3.OperationalError`; the
other ~18 still use bare `except Exception:` and will WARN for any
Python-level exception (B3-fu7 `86ba067mg` sweep scope). Either way
the alert is real-signal: a hit means either a genuine DB error at
the narrowed sites OR an exception (DB or otherwise) at the
bare-except sister sites. Both warrant operator awareness within
minutes, not 42 days (B3 itself was 42 days of silent LPNE row drops
behind the pre-narrow bare-except swallow at the LPNE site).

Pins:
  1. `check_insert_evaluated_opportunity_failures` exists with
     documented kwargs (window_min, threshold_count, unit, log_marker).
  2. Default unit = "kalshi-bot"; default log_marker matches the
     WARNING message produced by `bot/scanner/__init__.py` LPNE +
     dc_shadow_no_side narrow swallows (post-B3-fu2 form).
  3. Below-threshold count returns None (no alert).
  4. At-threshold count returns alert string with diagnostic context.
  5. journalctl-absent returns None (fail-quiet, cron convention).
  6. `main()` includes a `kalshi-bot` tier with `b3_fu3` dedup prefix,
     so the new alert doesn't collide with the existing
     `kalshi-collector` (d1_6) or `kalshi-coinbase-collector` (d2_5)
     tier dedup windows.
"""
from __future__ import annotations

import inspect
from unittest.mock import patch


def test_check_insert_eval_failures_signature():
    """Function exists with documented kwargs."""
    from scripts.ops.collector_health_monitor import (
        check_insert_evaluated_opportunity_failures,
    )
    sig = inspect.signature(check_insert_evaluated_opportunity_failures)
    assert "window_min" in sig.parameters
    assert "threshold_count" in sig.parameters
    assert "unit" in sig.parameters
    assert "log_marker" in sig.parameters
    # Defaults: alert on FIRST hit in last 5 minutes — these WARNs
    # should be 0/day under healthy operation; even 1 hit warrants
    # operator attention.
    assert sig.parameters["window_min"].default == 5
    assert sig.parameters["threshold_count"].default == 1
    assert sig.parameters["unit"].default == "kalshi-bot", (
        "Default unit must be `kalshi-bot` — the WARNING fires from the "
        "scanner, NOT the collector."
    )
    assert (
        "insert_evaluated_opportunity failed"
        in sig.parameters["log_marker"].default
    ), (
        "Default log_marker must contain `insert_evaluated_opportunity "
        "failed` — matches the canonical WARN message produced by the "
        "L106-narrowed except clauses in bot/scanner/__init__.py."
    )


def test_check_insert_eval_failures_silent_below_threshold():
    """When no matching log lines are found, return None (no alert)."""
    from scripts.ops.collector_health_monitor import (
        check_insert_evaluated_opportunity_failures,
    )
    from scripts.ops import collector_health_monitor as mod

    with patch.object(mod.subprocess, "check_output", return_value=""):
        result = check_insert_evaluated_opportunity_failures()
    assert result is None, (
        f"Expected None when no matching log lines; got {result!r}"
    )


def test_check_insert_eval_failures_alerts_at_threshold():
    """When matching log lines >= threshold, return alert string."""
    from scripts.ops import collector_health_monitor as mod

    fake_journal = "\n".join(
        [
            "May 18 14:23:01 ubuntu start.sh[1234]: WARNING: insert_evaluated_opportunity failed (lpne): database is locked",
        ]
        * 3
    )
    with patch.object(mod.subprocess, "check_output", return_value=fake_journal):
        result = mod.check_insert_evaluated_opportunity_failures(threshold_count=1)
    assert result is not None, "Expected alert with 3 hits > threshold 1"
    assert "INSERT_EVALUATED_OPPORTUNITY" in result.upper(), (
        f"Alert must include the canonical marker for operator grep: {result!r}"
    )
    assert "3" in result, f"Alert should include hit count: {result!r}"


def test_check_insert_eval_failures_silent_when_journalctl_absent():
    """When journalctl is absent or errors, return None (don't alert-spam)."""
    from scripts.ops.collector_health_monitor import (
        check_insert_evaluated_opportunity_failures,
    )
    import subprocess
    from scripts.ops import collector_health_monitor as mod

    with patch.object(
        mod.subprocess,
        "check_output",
        side_effect=FileNotFoundError("journalctl"),
    ):
        result = check_insert_evaluated_opportunity_failures()
    assert result is None, (
        f"Expected None when journalctl absent; got {result!r}. "
        f"Alert-spam on missing tooling violates cron-convention."
    )

    with patch.object(
        mod.subprocess,
        "check_output",
        side_effect=subprocess.CalledProcessError(1, "journalctl"),
    ):
        result = check_insert_evaluated_opportunity_failures()
    assert result is None, (
        f"Expected None on CalledProcessError; got {result!r}."
    )

    with patch.object(
        mod.subprocess,
        "check_output",
        side_effect=subprocess.TimeoutExpired("journalctl", 10),
    ):
        result = check_insert_evaluated_opportunity_failures()
    assert result is None, (
        f"Expected None on TimeoutExpired; got {result!r}."
    )


def test_main_includes_bot_tier_with_b3_fu3_dedup_prefix():
    """`main()` must include a `kalshi-bot` tier with `b3_fu3` dedup prefix.

    Source-scan rather than runtime invocation so we don't have to
    mock TelegramNotifier + every check function. The tier-list is the
    structural surface; if it doesn't include the bot tier, the alert
    never fires regardless of the check function's behavior.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "collector_health_monitor.py"
    text = src.read_text()
    # The tier entry uses these three tokens in close proximity.
    assert "kalshi-bot" in text, (
        "main() tier list must include kalshi-bot — the unit emitting "
        "the insert_evaluated_opportunity failed WARNING."
    )
    assert "b3_fu3" in text, (
        "main() tier list must use `b3_fu3` dedup prefix for the "
        "kalshi-bot tier so it doesn't collide with d1_6 (Kalshi "
        "collector) or d2_5 (Coinbase collector) dedup windows."
    )
    # Soft-check: confirm the new check function is wired into main().
    assert "check_insert_evaluated_opportunity_failures" in text, (
        "main() must invoke check_insert_evaluated_opportunity_failures "
        "in the bot-tier check list."
    )
