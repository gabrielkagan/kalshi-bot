"""Shared runtime safety for H-4 backfill scripts.

May 3 2026 incident: a `cryptocompare_news_backfill.py` instance hung
for 2h42m holding state.db's writer lock, eventually wedging the live
bot's eval-write path post-restart. The H-4 wrapper script's signal
handler is one defense layer (SIGTERM/SIGHUP-mediated reaping); this
module is the second: a SIGALRM-based hard timeout INSIDE each
backfill script that fires regardless of upstream signals.

Postmortem: `kb/failures/shape-d-contention-explosion-may03.md` and
`kb/decisions/h4-backfill-bugs-may04.md` (Bug 2 SEVERITY UPGRADE).

Usage from each backfill script's main():

    from _h4_runtime_safety import install_hard_timeout
    install_hard_timeout(label="h4c_cc_news")

Defaults to 25 minutes — sized to fit inside the wrapper's 30-min SSH
command_timeout and the systemd TimeoutStartSec=1h, so the alarm
fires before either upstream timeout would.
"""

from __future__ import annotations

import logging
import signal
import sys

logger = logging.getLogger(__name__)

# 1500s = 25 min. Sized to fit:
#   - GitHub Actions ssh-action `command_timeout: 30m`
#   - systemd unit's `TimeoutStartSec=3600` (1h)
# Raising this without coordinating those upstream timeouts re-creates
# the orphan window (script outlives its supervisor).
#
# First-run note: on a fresh historical backfill (e.g., GDELT's ~6,720
# bucket × ~2s/bucket = ~3-5 hours), this ceiling will fire repeatedly
# until the NULL rows are drained across multiple daily runs. That is
# BY DESIGN — the wrapper labels exit code 124 as "HARD TIMEOUT (will
# resume next run)" so the operator doesn't mistake it for a fault.
# All 3 backfill scripts are idempotent under retry (WHERE col IS NULL
# filter advances the checkpoint after each committed batch).
DEFAULT_HARD_TIMEOUT_S: int = 1500


def install_hard_timeout(
    seconds: int = DEFAULT_HARD_TIMEOUT_S,
    label: str = "h4_script",
) -> None:
    """Install a SIGALRM-based hard timeout. After `seconds`, the
    handler logs the event and exits with code 124 (GNU `timeout`
    convention). Idempotent: calling twice replaces the previous
    alarm rather than stacking.

    Why exit code 124: matches `/usr/bin/timeout`. The H-4 wrapper
    (`h4_run_with_alert.py`) treats any non-zero exit as a backfill
    failure and sends a Telegram alert with `exit_code=124`, giving
    the operator a distinct signal that 'the script self-killed at
    the hard ceiling' as opposed to other failure modes.

    No-op on platforms without SIGALRM (Windows). H-4 scripts run
    only on the VPS (Linux), so this is acceptable; the no-op path
    exists so unit tests on dev machines don't crash."""
    if not hasattr(signal, "SIGALRM"):
        return

    def _on_alarm(_signum, _frame):
        logger.error(
            "%s: HARD TIMEOUT after %ds — forcing exit (124). "
            "Investigate stuck loop / API hang / DB lock in the "
            "calling script.",
            label, seconds,
        )
        sys.exit(124)

    signal.signal(signal.SIGALRM, _on_alarm)
    # signal.alarm(N) returns previous remaining time; calling
    # alarm(N) replaces any pending alarm.
    signal.alarm(seconds)
