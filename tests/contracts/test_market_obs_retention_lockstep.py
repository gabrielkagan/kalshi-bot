"""Ticket 86ba0jb39 — market_obs retention vs S3-archive lookback lockstep.

`bot/snapshots/market_observations_snapshotter.py::DEFAULT_RETENTION_DAYS`
controls when the snapshotter's hourly sweep deletes rows from
`market_observations_continuous`. `scripts/ops/export_market_obs_to_s3.py
::_DEFAULT_LOOKBACK_DAYS` controls which day the nightly S3 archive timer
reads. If the lookback >= retention, the archive timer reads rows that
have already been deleted — silent data loss into S3 nulls.

This test pins:
  1. The specific values shipped by Bit 86ba0jb39 (retention=5, lookback=4) —
     RED before the Bit, GREEN after; forces future maintainers updating
     either constant to think about the relationship.
  2. The structural invariant `lookback < retention` — catches drift if
     a future change updates one constant without the other.

Why values: 14d retention was conservative; the H-3 fill simulator only
needs minutes-to-hours of NBBO history. 5d retention shrinks the table
~3x, which directly reduces executemany lock-hold tail driving the
2026-05-18→19 MarketObsSnapshotter contention storm (see ticket body
for the 3.24s executemany_ms data point + 805/10min lock-error peak).
S3 archive at day-4 leaves day-4 + day-5 both safely readable when the
timer fires at 05:30 UTC.
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ops is not on sys.path by default — sister tests
# (test_export_market_obs_to_s3.py) use the same shim.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_DIR = _REPO_ROOT / "scripts" / "ops"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from bot.snapshots import market_observations_snapshotter as _snap  # noqa: E402
import export_market_obs_to_s3 as _export  # noqa: E402


def test_retention_days_pinned_to_five() -> None:
    """DEFAULT_RETENTION_DAYS shipped at 5 by Bit 86ba0jb39 (was 14).

    Updating this value also requires updating _DEFAULT_LOOKBACK_DAYS in
    the export script + the sizing-math comment block immediately above
    `DEFAULT_RETENTION_DAYS` in the snapshotter module + the corresponding
    pin in the export script docstring + the sister-doc surfaces in
    bot/CLAUDE.md / ops/CLAUDE.md / scripts/CLAUDE.md /
    setup_market_obs_archive_timer.sh / tests/contracts/test_backup_timer_cadence.py
    (R4 found this 5th sister surface — historical Bit 86b9zkp89 docstring
    referenced 14d retention as a present-tense property of market_obs;
    future re-tunes need to update its framing in lockstep).
    """
    assert _snap.DEFAULT_RETENTION_DAYS == 5, (
        f"DEFAULT_RETENTION_DAYS={_snap.DEFAULT_RETENTION_DAYS}; expected 5 "
        f"per Bit 86ba0jb39. If retention is being re-tuned, update this "
        f"value-pin AND scripts/ops/export_market_obs_to_s3.py's "
        f"_DEFAULT_LOOKBACK_DAYS in the same commit."
    )


def test_export_lookback_days_pinned_to_four() -> None:
    """_DEFAULT_LOOKBACK_DAYS shipped at 4 by Bit 86ba0jb39 (was 13).

    Must remain strictly less than DEFAULT_RETENTION_DAYS — see the
    lockstep test below.
    """
    assert _export._DEFAULT_LOOKBACK_DAYS == 4, (
        f"_DEFAULT_LOOKBACK_DAYS={_export._DEFAULT_LOOKBACK_DAYS}; expected 4 "
        f"per Bit 86ba0jb39. If lookback is being re-tuned, update this "
        f"value-pin AND bot/snapshots/market_observations_snapshotter.py's "
        f"DEFAULT_RETENTION_DAYS in the same commit."
    )


def test_lookback_strictly_less_than_retention() -> None:
    """Structural invariant: the S3 archive must read a date that has not
    yet been deleted by the snapshotter's retention sweep.

    Rationale:
      * Snapshotter deletes rows WHERE observation_time < (now - retention_days)
      * Export reads rows WHERE substr(observation_time,1,10) == (today - lookback_days)
      * For the export to find any rows, the lookback target must fall
        INSIDE the retention window: (today - lookback) > (now - retention)
        which simplifies to lookback < retention (when both are in days).

    Equality (lookback == retention) is unsafe: the sweep fires hourly,
    the export fires daily at 05:30 UTC, and the relative timing means
    the sweep CAN delete the day's rows before the export reads them.
    Strict inequality (one full day of margin) is the contract.
    """
    assert _export._DEFAULT_LOOKBACK_DAYS < _snap.DEFAULT_RETENTION_DAYS, (
        f"_DEFAULT_LOOKBACK_DAYS={_export._DEFAULT_LOOKBACK_DAYS} must be "
        f"STRICTLY less than DEFAULT_RETENTION_DAYS={_snap.DEFAULT_RETENTION_DAYS}. "
        f"Otherwise the nightly S3 archive timer reads rows that the "
        f"snapshotter's retention sweep has already deleted → silent data "
        f"loss into S3."
    )
